#!/usr/bin/env python3
"""claude-tg — Telegram remote control for Claude Code sessions.

Drives Claude Code sessions over the cross-session UDS messaging protocol
(undocumented, reverse-engineered from claude-code 2.1.309 — re-verify after
CLI updates):

    discovery:  claude agents --json + ~/.claude/sessions registry merge
    injection:  NDJSON to /tmp/cc-socks/<pid>.sock
                  line 1: {"type":"auth","token":"<peerToken>"}
                  line 2: {"type":"user","message":{"role":"user","content":...}}
                  token from ~/.claude/sessions/<pid>.<sha256(sock)>.key
    reply:      tails the target session's transcript JSONL and forwards new
                assistant text to Telegram (the Stop hook is not involved and
                stays inert — no pending markers are written)
    live view:  claude attach <id|name> (bg) / tmux attach (interactive)

Bot commands:
  /sessions          list running sessions, tap to target
  /new <dir> [tmux|uds|bg]
                     spawn a session in dir (default: tmux window)
  /oneshot [<dir>] <question>
                     one-shot Q&A in a neutral scratch dir (opt: <dir> for
                     project context); answer delivered, session closed
  /status            show target session state + pane status line
  /stop              interrupt the target's current turn
  /close             close the targeted session
  /loop <prompt>     native Ralph loop: re-prompt up to 5x until "DONE"
  /chatid            show this chat's id (for per-session topics)
  /reload            re-read ~/.claude/telegram.env (live config)
  /help              show this list
  anything else      forwarded to the target session
"""
import os
import re
import signal
import sys
import json
import hashlib
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

try:  # package import (pip/pipx install, python -m claude_tg)
    from .telegram_lib import build_opener, send_markdown
except ImportError:  # run as a plain script from a checkout — use the sibling lib
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from telegram_lib import build_opener, send_markdown  # noqa: E402

import telegram_lib  # noqa: E402  (reload_proxy is called on live config updates)

CONF_FILE = os.path.expanduser("~/.claude/telegram.env")
_CONF_MTIME = 0.0
# seeded from process env; ~/.claude/telegram.env (KEY=VALUE) overrides and is
# re-read on SIGHUP, /reload, and each poll cycle (mtime-checked)
_RUNTIME = {
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "ALLOWED_TELEGRAM_USER_IDS": os.environ.get("ALLOWED_TELEGRAM_USER_IDS", ""),
    "TELEGRAM_HTTP_PROXY": os.environ.get("TELEGRAM_HTTP_PROXY", ""),
    "TELEGRAM_FORUM_CHAT_ID": os.environ.get("TELEGRAM_FORUM_CHAT_ID", ""),
    "TMUX_SESSION": os.environ.get("TMUX_SESSION", "claude"),
    # neutral /oneshot workspace: no project context (subdirs of a trusted
    # workspace inherit trust — ~/.claude itself is not trusted, so first use
    # needs a one-time trust acceptance in the dir)
    "ONESHOT_DIR": os.environ.get("ONESHOT_DIR", os.path.expanduser("~/.claude/oneshot")),
}
CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
TARGET_FILE = os.path.expanduser("~/.claude/telegram_target")
TOPICS_FILE = os.path.expanduser("~/.claude/telegram_topics")
# Per-session channel overrides, remembered when /new spawns a session
CHANNELS_FILE = os.path.expanduser("~/.claude/telegram_channels")
SOCKS_DIR = "/tmp/cc-socks"
KEYS_DIR = Path.home() / ".claude" / "sessions"
PICKUP_TIMEOUT = 120     # s — injected but session never woke up
TURN_TIMEOUT = 1800      # s — hard give-up
OPENER = build_opener()

BOT_COMMANDS = [
    {"command": "sessions", "description": "List running Claude sessions, pick target"},
    {"command": "new", "description": "Spawn a session: /new <dir> [tmux]"},
    {"command": "chatid", "description": "Show this chat's id (for TELEGRAM_FORUM_CHAT_ID)"},
    {"command": "close", "description": "Close the targeted session"},
    {"command": "status", "description": "Show target session state"},
    {"command": "stop", "description": "Interrupt the current turn"},
    {"command": "loop", "description": "Ralph Loop: /loop <prompt>"},
    {"command": "oneshot", "description": "One-shot Q&A: /oneshot [<dir>] <question>"},
    {"command": "help", "description": "Show bot usage"},
]
BLOCKED_COMMANDS = [
    "/mcp", "/settings", "/config", "/model", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen"
]

# turn tracking: sessionId -> {chat_id, tpath, offset, started, state, loop}
TURNS = {}
QUEUES = {}   # sessionId -> [(text, loop), ...]
LOCK = threading.RLock()


def munge(path):
    """CLI transcript-bucket name for a cwd: 2.1.309 maps every character
    outside [A-Za-z0-9_] to '-' and KEEPS the leading dash ("/x/y.z" →
    "-x-y-z"). Verified against ~/.claude/projects listings; the old
    replace("/","-")+lstrip spelling matched no bucket at all, which silently
    killed the whole transcript reply tail."""
    return re.sub(r"[^A-Za-z0-9_]", "-", path)


# --- session discovery --------------------------------------------------------

_AGENTS = {"ts": 0.0, "data": None}


def agents(max_age=2.0):
    """All running sessions (interactive + background), cached briefly.

    agents --json omits some registry fields (notably `tmux`) and hides
    sessions that parked a background job, so entries are enriched from — and
    hidden sessions recovered from — ~/.claude/sessions/<pid>.json. Also the
    fallback source when agents --json itself fails.
    """
    now = time.time()
    if now - _AGENTS["ts"] < max_age:
        return _AGENTS["data"]
    try:
        r = subprocess.run(["claude", "agents", "--json"],
                           capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout) if r.returncode == 0 else None
        if data is None:
            print(f"agents --json failed: {(r.stderr or '').strip()[:200]}")
    except Exception as e:
        data = None
        print(f"agents --json error: {e}")
    reg = Path.home() / ".claude" / "sessions"
    if data:
        for e in data:
            try:
                extra = json.loads((reg / f"{e['pid']}.json").read_text())
            except Exception:
                continue
            for k in ("tmux", "messagingSocketPath", "entrypoint"):
                if k not in e and k in extra:
                    e[k] = extra[k]
    else:
        data = []  # agents --json failed — the registry merge below still runs
    # agents --json hides sessions that parked a background job (parkedJobId
    # set), so recover them from the registry to keep them targetable. The
    # registry is shared across pid domains (containers write their own
    # entries), hence the darwin + liveness filters.
    seen = {e.get("pid") for e in data}
    for f in sorted(reg.glob("*.json")):
        try:
            e = json.loads(f.read_text())
        except Exception:
            continue
        pid = e.get("pid")
        if not isinstance(pid, int) or pid in seen or e.get("pidDomain") != "darwin":
            continue
        try:
            os.kill(pid, 0)  # registry keeps dead leftovers
        except OSError:
            continue
        if e.get("spare"):
            continue  # daemon pre-warm spare (name = jobId[:8]) — not a session
        if e.get("kind") == "bg":  # registry says bg, agents --json says background
            e["kind"] = "background"
        data.append(e)
    _AGENTS["ts"], _AGENTS["data"] = now, data
    return data


def find_session(session_id):
    for e in (agents() or []):
        if e.get("sessionId") == session_id:
            return e
    return None


def stop_id(entry):
    """Id `claude stop` matches on: the bg job id (agents --json `id` /
    registry `jobId`) — not the conversation sessionId. The two differ for
    forked resumes (`--session-id <job> --fork-session --resume <conv>`),
    where `claude stop <sessionId[:8]>` fails with "No job matching"."""
    return (entry.get("id") or entry.get("jobId")
            or entry.get("sessionId", ""))[:8]


def transcript_path(entry):
    sid = entry["sessionId"]
    p = (Path.home() / ".claude" / "projects" /
         munge(entry.get("cwd", "")) / f"{sid}.jsonl")
    if p.exists():
        return p
    # munge drift across CLI builds — the sessionId UUID is unique project-wide,
    # the bucket spelling is not: find the transcript wherever it lives
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else p


# --- UDS injection ------------------------------------------------------------

def tmux_pane_of(entry):
    """'claude:@2.%3' → '%3' — the pane Claude runs in."""
    ref = entry.get("tmux") or ""
    return ref.rsplit(":", 1)[-1].rsplit(".", 1)[-1] if ref else None


def pane_status_line(pane):
    """The pane's own status line (model · dir · git…) — bottom-most content
    line, skipping blanks, rules and input-box leftovers. None if not found."""
    try:
        cap = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane],
                             capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    if cap.returncode != 0:
        return None
    for line in reversed(cap.stdout.splitlines()):
        s = line.strip()
        if not s or s == "-- INSERT --":
            continue
        if set(s) <= set("─━-—=·|❯ "):
            continue
        return s[:160]
    return None


def inject_via_tmux(entry, text):
    """Type the prompt into the session's tmux pane. No cross-session wrapper
    is added on this channel — clean injection for panes you watch. Returns
    error string or None."""
    pane = tmux_pane_of(entry)
    if not pane:
        return "session has no tmux pane"
    text = " ".join(text.split())  # send-keys submits on Enter: single line only
    if not text:
        return "empty after sanitize"
    try:
        for args in (["tmux", "send-keys", "-t", pane, "-l", text],
                     ["tmux", "send-keys", "-t", pane, "Enter"]):
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return f"tmux send-keys failed: {(r.stderr or '').strip()}"
    except subprocess.TimeoutExpired:
        return "tmux send-keys timed out"
    return None


def uds_inject(pid, text):
    """Inject a user message into a live session. Returns (ok, detail)."""
    sock_path = f"{SOCKS_DIR}/{pid}.sock"
    key_file = KEYS_DIR / f"{pid}.{hashlib.sha256(sock_path.encode()).hexdigest()}.key"
    try:
        token = json.loads(key_file.read_text())["peerToken"]
    except Exception as e:
        return False, f"no auth key for pid {pid} ({e})"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(sock_path)
        s.sendall(json.dumps({"type": "auth", "token": token}).encode() + b"\n")
        s.sendall(json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}}).encode() + b"\n")
        return True, "injected"
    except OSError as e:
        return False, str(e)
    finally:
        s.close()


def inject_via_resume(session_id, text):
    """Deliver a prompt by stopping the background session and resuming it
    with the prompt as a real user turn (no cross-session wrapper). Returns
    error string or None."""
    # best-effort: the session may already be stopped, which is exactly the
    # state resume wants; the resume itself is the gate that must succeed
    entry = find_session(session_id)
    try:
        subprocess.run(["claude", "stop", stop_id(entry) if entry else session_id[:8]],
                       capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass  # best-effort: already-stopped is exactly the state resume wants
    try:
        r = subprocess.run(["claude", "--bg", "--resume", session_id, text],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "resume-spawn timed out"
    out = (r.stdout or "") + (r.stderr or "")
    if "started a copy" in out:
        return "resume raced a running session and started a copy — try again"
    if r.returncode != 0 or "backgrounded" not in out:
        return f"resume-spawn failed: {out.strip()[:300]}"
    return None


# --- transcript reply tail ------------------------------------------------------

def transcript_size(tpath):
    try:
        return os.path.getsize(tpath)
    except OSError:
        return 0


def _real_user(obj):
    """A real user prompt (not a tool_result entry, not a sidechain)."""
    if not isinstance(obj, dict) or obj.get("type") != "user" or obj.get("isSideChain"):
        return False
    c = obj.get("message", {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") == "text"
                   and (b.get("text") or "").strip() for b in c)
    return False


def _obj_text(obj):
    c = obj.get("message", {}).get("content")
    chunks = []
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                chunks.append(b["text"])
    elif isinstance(c, str) and c.strip():
        chunks.append(c)
    return "\n".join(chunks).strip()


def collect_turn_text(tpath, offset, marker):
    """Assistant text belonging to THIS turn only: from the injected user line
    (matched by content) up to the next real user line. Turns that run
    back-to-back therefore cannot bleed into each other's delivery."""
    try:
        with open(tpath, "rb") as f:
            f.seek(offset)
            objs = []
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break  # partial write; not ours to read
                try:
                    objs.append(json.loads(line))
                except Exception:
                    objs.append(None)
    except OSError:
        return None
    in_turn, texts = False, []
    for obj in objs:
        if obj is None:
            continue
        if not in_turn:
            if _real_user(obj) and marker in (_obj_text(obj) or ""):
                in_turn = True
            continue
        if _real_user(obj):
            break  # next turn started — its output is not ours to deliver
        if obj.get("type") == "assistant" and not obj.get("isSideChain"):
            t = _obj_text(obj)
            if t:
                texts.append(t)
    return "\n\n".join(texts) if texts else None


# --- turn lifecycle -------------------------------------------------------------

def inject_text(session_id, text, chat_id, loop=None, oneshot=False):
    """Queue or inject. Channel is chosen per target: tmux panes get clean
    send-keys (no cross-session wrapper), background sessions may use the
    ♻ stop+resume pair, everything else gets UDS. No bridge-side queueing
    except ♻ resume — a mid-turn `claude stop` would kill the running turn.
    The CLI holds mid-turn input natively (TUI buffer / cross-session
    inbox), and the watcher delivers only once our marker shows up in the
    transcript, so the reply path stays straight. A oneshot turn auto-closes
    its session once the answer is delivered. Returns error string or
    None on success."""
    with LOCK:  # check + slot reservation must be atomic vs the poll thread and queue drain
        entry = find_session(session_id)
        if not entry:
            return "target session ended — /sessions to pick another"
        # channel precedence: per-session override (set by /new) > tmux pane > UDS
        override = _load_channels().get(session_id)
        if entry.get("tmux") and override not in ("uds", "resume"):
            channel = "tmux"
        elif override in ("uds", "resume"):
            channel = override
        else:
            channel = "uds"
        if channel == "resume" and (entry.get("status") == "busy" or session_id in TURNS):
            q = QUEUES.setdefault(session_id, [])
            q.append((text, loop))
            return f"session is busy — queued (position {len(q)})"
        tpath = str(transcript_path(entry))
        offset = transcript_size(tpath)
        if channel == "tmux":
            wire = " ".join(text.split())  # single line on the wire; marker must match
        else:
            wire = text
        turn = {
            "chat_id": chat_id, "tpath": tpath, "offset": offset,
            "marker": wire, "started": time.time(), "state": "injecting",
            "loop": loop, "oneshot": oneshot, "channel": channel,
            "label": entry.get("name", session_id[:8]),
        }
        TURNS[session_id] = turn
    # inject OUTSIDE the bridge-wide lock — a slow channel (the resume pair can
    # take minutes of subprocesses) must not freeze every other session
    if channel == "tmux":
        err = inject_via_tmux(entry, wire)
    elif channel == "resume":
        err = (inject_via_resume(session_id, wire)
               if entry.get("kind") == "background"
               else "resume channel needs a background session")
    else:
        ok, detail = uds_inject(entry["pid"], wire)
        err = None if ok else f"inject failed: {detail}"
    with LOCK:
        t = TURNS.get(session_id)
        if err:
            if t and t.get("state") == "injecting":
                TURNS.pop(session_id, None)
            return err
        if t and t.get("state") == "injecting":
            t["state"] = "wait"
    threading.Thread(target=typing_loop, args=(session_id, turn), daemon=True).start()
    return None


def typing_loop(session_id, turn):
    # bound to the exact turn dict: a re-injected /loop turn creates a new
    # dict, so stale threads exit instead of stacking across chains
    while TURNS.get(session_id) is turn:
        t = TURNS.get(session_id) or {}
        chat_id, thread = deliver_target(session_id, t)
        if chat_id:
            action = {"chat_id": chat_id, "action": "typing"}
            if thread:
                action["message_thread_id"] = thread
            telegram_api("sendChatAction", action)
        time.sleep(4)


def drop_turn(sid):
    with LOCK:
        TURNS.pop(sid, None)
        QUEUES.pop(sid, None)


def turn_cleanup(sid, t):
    """Retire a finished turn: a plain turn just drops its tracking state; a
    /oneshot session is closed too — the spawned entry lives for one question.
    Returns a delivery note for oneshot closures, else None."""
    if not t.get("oneshot"):
        drop_turn(sid)
        return None
    # a parked turn is answered by its forked job — close that, not the parent
    ok, note = close_session(t.get("job_sid") or sid)  # drops the turn itself
    return (f"☑ {t.get('label', sid[:8])} closed — resumable: "
            f"claude --resume {sid[:8]}"
            if ok else f"⚠️ auto-close failed: {note}")


def deliver(sid, t, text):
    chat_id, thread = deliver_target(sid, t)
    if not chat_id:
        return
    # No forum topic → mark the answer with the session name; with a topic,
    # the topic itself is the marking.
    header = None if thread is not None else f"**{t.get('label', sid[:8])}**"
    sent, total, errors = send_markdown(text, chat_id, _RUNTIME["TELEGRAM_BOT_TOKEN"], header=header, thread=thread)
    if errors:
        print(f"deliver failed [{sid[:8]}]: {'; '.join(errors)}")


def finish_turn(sid, t, note=None):
    """Send the collected text / loop bookkeeping, then clear or chain."""
    loop = t.get("loop")
    text = collect_turn_text(t["tpath"], t["offset"], t.get("marker", ""))
    if text:
        deliver(sid, t, text)
    elif note:
        deliver(sid, t, note)

    if loop:
        if text and re.search(r"\bDONE\b", text[-500:]):
            deliver(sid, t, f"🏁 loop complete after {loop['iter']} iteration(s)")
            drop_turn(sid)
            return
        if loop["iter"] >= 5:
            deliver(sid, t, "🏁 loop gave up after 5 iterations without DONE")
            drop_turn(sid)
            return
        deliver(sid, t, f"↻ iteration {loop['iter']} done, continuing…")
        drop_turn(sid)
        err = inject_text(sid, loop["prompt"], t["chat_id"],
                          loop={"prompt": loop["prompt"], "iter": loop["iter"] + 1})
        if err:
            deliver(sid, t, f"loop stopped: {err}")
        return

    with LOCK:  # drain before cleanup — drop_turn inside it clears QUEUES too
        queued = QUEUES.pop(sid, [])
    cnote = turn_cleanup(sid, t)
    if cnote:
        deliver(sid, t, cnote)
        return
    if queued:
        nxt, lp = queued[0]
        err = inject_text(sid, nxt, t["chat_id"], loop=lp)
        if err:
            deliver(sid, t, f"queued message failed: {err}")


def watcher():
    """Single daemon loop: drives every tracked turn to completion."""
    while True:
        time.sleep(1)
        with LOCK:
            if not TURNS:
                continue
            snapshot = dict(TURNS)
        ags = agents()
        now = time.time()
        for sid, t in snapshot.items():
            try:
                step_turn(sid, t, ags, now)
            except Exception as e:
                print(f"watcher error [{sid[:8]}]: {e}")


def step_turn(sid, t, ags, now):
    with LOCK:
        if sid not in TURNS:
            return  # /stop, /close or a queue drain retired it — stale snapshot
    if ags is None:
        return  # discovery unavailable — hold state; TURN_TIMEOUT is the backstop
    entry = next((e for e in (ags or []) if e.get("sessionId") == sid), None)
    if entry is not None and entry.get("parkedJobId"):
        # 2.1.3xx daemon: the session PARKED our turn — a claimed bg-spare
        # (forked job) runs it, the parent vanishes from agents --json with a
        # frozen busy status, and the reply lands in the JOB's transcript.
        # Follow the job: its status drives busy/idle, its transcript the tail.
        pjob = entry["parkedJobId"]
        job = next((e for e in (ags or [])
                    if pjob in (e.get("jobId"), e.get("id"))), None)
        if job is not None:
            if t.get("job_sid") != job.get("sessionId"):
                t["job_sid"] = job.get("sessionId")
                t["tpath"] = str(transcript_path(job))
                t["offset"] = 0  # fork holds full history; marker re-anchors the tail
            entry = job
        elif t.get("job_sid"):
            entry = next((e for e in (ags or [])
                          if e.get("sessionId") == t["job_sid"]), None)
        else:
            return  # fork not visible yet — hold; TURN_TIMEOUT is the backstop
    if now - t["started"] > TURN_TIMEOUT:
        deliver(sid, t, "⏱ no completion within 30 min — giving up on this turn")
        cnote = turn_cleanup(sid, t)
        if cnote:
            deliver(sid, t, cnote)
        return
    if t["state"] == "wait":
        if entry is None:
            t["state"] = "work"      # session ended; finish_turn reports what exists
        elif entry.get("status") == "busy" or transcript_size(t["tpath"]) > t["offset"]:
            t["state"] = "work"
        elif now - t["started"] > PICKUP_TIMEOUT:
            deliver(sid, t, "⚠️ session stayed idle — message may not have been "
                            "picked up. Try /sessions and resend.")
            cnote = turn_cleanup(sid, t)
            if cnote:
                deliver(sid, t, cnote)
            return
    if t["state"] == "work":
        if now - t["started"] < 3:
            return
        if entry is not None and entry.get("status") == "busy":
            return                   # still working
        text = collect_turn_text(t["tpath"], t["offset"], t.get("marker", ""))
        if text is None and entry is not None:
            return                   # idle blip before any output — keep waiting
        finish_turn(sid, t, note="⚠️ session ended without a text reply" if entry is None else None)


# --- telegram api ----------------------------------------------------------------

def reload_runtime(force=False):
    """Re-read CONF_FILE (KEY=VALUE) over the seeded process env and push the
    proxy into telegram_lib. Mtime-guarded; force=True re-reads unconditionally."""
    global _CONF_MTIME
    try:
        mtime = os.path.getmtime(CONF_FILE)
    except OSError:
        return False
    if not force and mtime == _CONF_MTIME:
        return False
    _CONF_MTIME = mtime
    try:
        with open(CONF_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k in _RUNTIME:
                    _RUNTIME[k] = v.strip().strip('"').strip("'")
    except OSError:
        return False
    os.environ["TELEGRAM_HTTP_PROXY"] = _RUNTIME["TELEGRAM_HTTP_PROXY"]
    telegram_lib.reload_proxy()
    return True


def telegram_api(method, data, quiet=False):
    token = _RUNTIME["TELEGRAM_BOT_TOKEN"]
    if not token:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=70) as r:
            return json.loads(r.read())
    except Exception as e:
        if not quiet:
            print(f"Telegram API error ({method}): {e}")
        return None


def reply(chat_id, text):
    telegram_api("sendMessage", {"chat_id": chat_id, "text": text})


def is_allowed(update):
    allowed = _RUNTIME["ALLOWED_TELEGRAM_USER_IDS"]
    if not allowed:
        return True
    cb = update.get("callback_query")
    if cb:
        # callback_query.message.from is the BOT (it sent the message the
        # button lives on) — the human is callback_query.from
        uid = (cb.get("from") or {}).get("id")
        if uid is None:
            uid = (cb.get("message", {}).get("chat", {}) or {}).get("id")
    else:
        msg = update.get("message", {})
        uid = (msg.get("from") or msg.get("chat", {}).get("from", {})).get("id")
    return uid in {int(x) for x in allowed.split(",") if x.strip().isdigit()}


# --- per-session reply routing (forum topics / name marking) --------------------

_TOPICS_LOCK = threading.Lock()
_TOPICS_INFLIGHT = set()


def _load_topics():
    try:
        with open(TOPICS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def topic_for(entry):
    """Forum topic thread id for a session — created once, cached on disk.
    Returns None when no forum is configured or creation recently failed
    (negative-cached 10 min so a broken setup can't spam createForumTopic).
    Creation runs outside the lock; concurrent creators lose and fall back."""
    forum_chat = _RUNTIME["TELEGRAM_FORUM_CHAT_ID"]
    if not forum_chat or not entry:
        return None
    sid = entry["sessionId"]
    with _TOPICS_LOCK:
        topics = _load_topics()
        t = topics.get(sid)
        if t:
            if t.get("thread_id"):
                return t["thread_id"]
            if time.time() - t.get("failed_at", 0) < 600:
                return None
        if sid in _TOPICS_INFLIGHT:
            return None
        _TOPICS_INFLIGHT.add(sid)
    tid = None
    name = f"{entry.get('name', sid[:8])} · {os.path.basename(entry.get('cwd', '?'))}"[:128]
    try:
        r = telegram_api("createForumTopic", {"chat_id": forum_chat, "name": name})
        if r and r.get("ok"):
            tid = r.get("result", {}).get("message_thread_id")
    finally:
        with _TOPICS_LOCK:
            _TOPICS_INFLIGHT.discard(sid)
            topics = _load_topics()
            topics[sid] = {"thread_id": tid, "name": name,
                           **({} if tid else {"failed_at": time.time()})}
            with open(TOPICS_FILE, "w") as f:
                json.dump(topics, f, indent=1)
    if tid is None:
        print(f"createForumTopic failed for {sid[:8]} — falling back to private chat")
    return tid


def deliver_target(sid, t):
    """(chat_id, thread) for a turn's output."""
    thread = topic_for(find_session(sid)) if _RUNTIME["TELEGRAM_FORUM_CHAT_ID"] else None
    if thread is not None:
        return _RUNTIME["TELEGRAM_FORUM_CHAT_ID"], thread
    return t.get("chat_id"), None


# --- target persistence ------------------------------------------------------------

def load_target():
    try:
        with open(TARGET_FILE) as f:
            t = json.load(f)
        return t if t.get("session_id") else None
    except Exception:
        return None


def save_target(session_id):
    with open(TARGET_FILE, "w") as f:
        json.dump({"session_id": session_id}, f)


def _load_channels():
    try:
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_channel(sid, channel):
    """Remember the injection channel a session was spawned with."""
    with LOCK:
        ch = _load_channels()
        ch[sid] = channel
        with open(CHANNELS_FILE, "w") as f:
            json.dump(ch, f, indent=1)


_PENDING_NAMES = set()  # spawned but not yet in agents --json — closes the rapid-/new race


def uniq_name(base):
    """First free display name: base, base-2, base-3… The CLI allows
    duplicate display names silently (verified — two `--bg -n X` coexist
    unrenamed), which would make Telegram markings ambiguous; suffix only
    on a real collision so names stay clean otherwise."""
    taken = ({e.get("name") for e in (agents(0) or []) if e.get("name")}
             | _PENDING_NAMES)
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def spawn_bg(d, name):
    """Spawn `claude --bg -n <name>` in dir d. Returns (short_id, error)."""
    _PENDING_NAMES.add(name)
    try:
        r = subprocess.run(["claude", "--bg", "-n", name], cwd=d,
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        _PENDING_NAMES.discard(name)
        return None, "spawn timed out"
    out = (r.stdout or "") + (r.stderr or "")
    if "not trusted" in out.lower():
        _PENDING_NAMES.discard(name)
        return None, (f"{d} is not a trusted workspace — run `claude` once in it "
                      "locally and accept the trust prompt, then retry.")
    m = re.search(r"backgrounded · ([0-9a-f]+)", out)
    if not m:
        _PENDING_NAMES.discard(name)
        return None, f"spawn failed: {out.strip()[:300]}"
    return m.group(1), None


# --- message handling ----------------------------------------------------------------

def close_session(sid):
    """Close a session: kill its tmux pane (interactive) or `claude stop` (bg).
    Returns (ok, note). Conversations persist — resumable via --resume/attach."""
    entry = find_session(sid)
    if not entry:
        drop_turn(sid)
        return True, "session already gone"
    if entry.get("kind") == "interactive":
        pane = tmux_pane_of(entry)
        if not pane:
            drop_turn(sid)
            return False, "no tmux pane registered — close it in its terminal"
        r = subprocess.run(["tmux", "kill-pane", "-t", pane],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False, f"kill-pane failed: {(r.stderr or '').strip()}"
        note = "tmux pane closed"
    else:
        r = subprocess.run(["claude", "stop", stop_id(entry)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, f"claude stop failed: {(r.stderr or '').strip()[:200]}"
        note = (r.stdout or "stopped").strip()
    drop_turn(sid)
    if (load_target() or {}).get("session_id") == sid:
        try:
            os.remove(TARGET_FILE)
        except OSError:
            pass
    short = sid[:8]
    return True, f"{note} — resumable: claude --resume {short}"


def handle_callback(cb):
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    data = cb.get("data", "")
    telegram_api("answerCallbackQuery", {"callback_query_id": cb.get("id")})
    if data.startswith("sel:"):
        sid = data.split(":", 1)[1]
        entry = find_session(sid)
        if not entry:
            reply(chat_id, "That session is gone — try /sessions again")
            return
        save_target(sid)
        reply(chat_id, f"Now talking to {entry.get('name', sid[:8])} "
                       f"({os.path.basename(entry.get('cwd', '?'))}) — send messages freely.")
        return


def handle_message(msg):
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    if not text or not chat_id:
        return

    with open(CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))

    if text.startswith("/"):
        # strip @botname suffix Telegram appends when a command is picked from the menu
        cmd = text.split()[0].lower().split("@", 1)[0]

        if cmd == "/help":
            reply(chat_id,
                  "🤖 Claude bridge\n\n"
                  "/sessions — list running sessions, tap to target, ✖ to close\n"
                  "/new <dir> [tmux|uds|bg] — spawn a session (default: tmux window)\n"
                  "/close — close the targeted session (resumable)\n"
                  "/chatid — show this chat's id (for per-session topics)\n"
                  "/reload — re-read ~/.claude/telegram.env (live config)\n"
                  "/status — show target session state\n"
                  "/stop — interrupt the current turn\n"
                  "/loop <prompt> — re-prompt up to 5× until DONE\n"
                  "/oneshot [<dir>] <question> — one-shot Q&A, no project "
                  "context (opt: <dir>), auto-closed\n"
                  "anything else — sent to the targeted session immediately "
                  "(the CLI queues mid-turn input; only ♻ resume waits for idle)\n\n"
                  "Replies come from the session's transcript. Watch live: "
                  "claude attach <id> (⚡) · tmux attach (⌨)")
            return

        if cmd == "/sessions":
            entries = agents()
            if not entries:
                reply(chat_id, "No running Claude sessions.\n"
                               "Spawn one: /new <dir>  (background, ⚡ UDS)\n"
                               "               /new <dir> tmux  (interactive pane, ⌨ send-keys)")
                return
            cur = (load_target() or {}).get("session_id")
            kb = []
            # One row per conversation: a parked session and its running fork
            # (jobId == the parent's parkedJobId) are the same conversation —
            # skip the fork's row and fold its live status into the parent's.
            parked_ids = {e["parkedJobId"] for e in entries if e.get("parkedJobId")}
            for e in entries:
                mark = "✓ " if e["sessionId"] == cur else ""
                ov = _load_channels().get(e["sessionId"])
                if ov == "resume":
                    ch = "♻"
                elif ov == "uds":
                    ch = "⚡"
                elif e.get("tmux"):
                    ch = "⌨"
                else:
                    ch = "⚡"
                jid = e.get("jobId") or e.get("id")
                if jid and jid in parked_ids and not e.get("parkedJobId"):
                    continue  # running fork of a parked session — parent row carries it
                status = e.get("status", "?")
                pj = e.get("parkedJobId")
                if pj:
                    job = next((j for j in entries
                                if (j.get("jobId") or j.get("id")) == pj), None)
                    status = job.get("status", "parked") if job else "parked"
                name = e.get('name', e['sessionId'][:8])
                if len(name) > 32:
                    name = name[:31] + "…"
                # channel glyph leads the label: Telegram ellipsizes long
                # button text, so a trailing glyph gets cut off
                label = (f"{ch} {mark}{name} · "
                         f"{os.path.basename(e.get('cwd', '?'))} · "
                         f"{status}")
                kb.append([{"text": label, "callback_data": f"sel:{e['sessionId']}"}])
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": "Running sessions — tap to target (⌨ send-keys · ⚡ UDS · "
                        "♻ bg+resume); /close shuts the target down:",
                "reply_markup": {"inline_keyboard": kb}})
            return

        if cmd == "/new":
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                reply(chat_id, "Usage: /new <dir> [tmux|uds|bg]\n"
                               "  tmux (default)  → window in your tmux session (⌨ clean send-keys)\n"
                               "  uds             → background session (⚡ socket injection)\n"
                               "  bg              → background session (♻ resume channel)")
                return
            d = os.path.abspath(os.path.expanduser(parts[1].strip()))
            mode = (parts[2].lower() if len(parts) > 2 else "tmux")
            if not os.path.isdir(d):
                reply(chat_id, f"No such directory: {d}")
                return
            if mode in ("uds", "bg", "resume", "b", "r"):
                name = uniq_name(re.sub(r"[^A-Za-z0-9._-]", "_",
                                        os.path.basename(d) or "claude"))
                short, err = spawn_bg(d, name)
                if err:
                    reply(chat_id, f"❌ {err}")
                    return
                time.sleep(1)
                _AGENTS["ts"] = 0
                entry = next((e for e in (agents(0) or [])
                              if e["sessionId"].startswith(short)), None)
                channel = "resume" if mode in ("bg", "resume", "r") else "uds"
                glyph = "♻" if channel == "resume" else "⚡"
                if entry:
                    _PENDING_NAMES.discard(name)  # registered — agents has the name now
                    save_channel(entry["sessionId"], channel)
                    save_target(entry["sessionId"])
                    reply(chat_id, f"Spawned {glyph} {name} ({short}) in {d} — targeted.\n"
                                   f"Channel: {channel}. Watch live: claude attach {short}")
                else:
                    reply(chat_id, f"Spawned {short} but it is not registered yet — "
                                   "/sessions in a moment.")
                return
            if mode in ("tmux", "i", "interactive"):
                wname = uniq_name(re.sub(r"[^A-Za-z0-9_-]", "-",
                                         os.path.basename(d) or "claude") or "claude")
                _PENDING_NAMES.add(wname)
                # Locate an existing tmux session to add a window to:
                # TMUX_SESSION (default "claude"), else the only session; else new.
                sname = _RUNTIME["TMUX_SESSION"]
                if subprocess.run(["tmux", "has-session", "-t", sname],
                                  capture_output=True).returncode != 0:
                    lst = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                                         capture_output=True, text=True)
                    names = [n.strip() for n in lst.stdout.splitlines() if n.strip()]
                    sname = names[0] if len(names) == 1 else None
                if sname:
                    # trailing colon: -t <session> alone resolves as a *window*
                    # target and reuses its index ("index N in use"); with ':'
                    # tmux picks the next free index.
                    r = subprocess.run(["tmux", "new-window", "-t", sname + ":", "-n", wname,
                                        "-c", d, "-P", "-F", "#{pane_id}", "claude", "-n", wname],
                                       capture_output=True, text=True, timeout=15)
                    where = f"window in tmux '{sname}'"
                else:
                    sname = wname
                    i = 2
                    while subprocess.run(["tmux", "has-session", "-t", sname],
                                         capture_output=True).returncode == 0:
                        sname, i = f"{wname}-{i}", i + 1
                    r = subprocess.run(["tmux", "new-session", "-d", "-s", sname,
                                        "-n", wname, "-c", d,
                                        "-P", "-F", "#{pane_id}", "claude", "-n", wname],
                                       capture_output=True, text=True, timeout=15)
                    where = f"tmux session '{sname}'"
                if r.returncode != 0:
                    _PENDING_NAMES.discard(wname)
                    reply(chat_id, f"tmux spawn failed: {(r.stderr or '').strip()[:300]}")
                    return
                pane = r.stdout.strip()
                subprocess.run(["tmux", "select-window", "-t", pane], capture_output=True)
                entry = None
                for _ in range(10):
                    time.sleep(1)
                    _AGENTS["ts"] = 0
                    entry = next((e for e in (agents(0) or [])
                                  if e.get("kind") == "interactive"
                                  and (e.get("tmux") or "").endswith(pane)), None)
                    if entry:
                        break
                if entry:
                    _PENDING_NAMES.discard(wname)  # registered — agents has the name now
                    save_channel(entry["sessionId"], "tmux")
                    save_target(entry["sessionId"])
                    reply(chat_id, f"Spawned ⌨ {where} in {d} — targeted.\n"
                                   "Injection is clean send-keys; attach: "
                                   f"tmux attach -t {sname}")
                else:
                    reply(chat_id, f"⌨ {where} created but not registered yet — "
                                   "/sessions in a moment. If it never appears, "
                                   f"attach once to accept the trust prompt: tmux attach -t {sname}")
                return
            reply(chat_id, f"Unknown mode '{parts[2]}' — use 'tmux' (default), 'uds', or 'bg'")
            return

        if cmd == "/close":
            target = load_target()
            if not target:
                reply(chat_id, "No target — /sessions to pick one, then /close")
                return
            sid = target["session_id"]
            entry = find_session(sid)
            nm = entry.get("name", sid[:8]) if entry else sid[:8]
            ok, note = close_session(sid)
            reply(chat_id, (f"Closed {nm}: {note}" if ok else f"❌ {nm}: {note}"))
            return

        if cmd == "/chatid":
            msg_obj = msg
            extra = (f"\nforum topic thread: {msg_obj['message_thread_id']}"
                     if msg_obj.get("is_topic_message") and msg_obj.get("message_thread_id") else "")
            reply(chat_id, f"This chat's id: {chat_id}\n"
                           f"Set TELEGRAM_FORUM_CHAT_ID to it to get per-session topics.{extra}")
            return

        if cmd == "/reload":
            global _CONF_MTIME
            _CONF_MTIME = 0.0
            reload_runtime()
            reply(chat_id, "reloaded config from ~/.claude/telegram.env")
            return

        if cmd == "/status":
            target = load_target()
            if not target:
                reply(chat_id, "No target — /sessions to pick one")
                return
            sid = target["session_id"]
            entry = find_session(sid)
            if not entry:
                reply(chat_id, "Target session has ended — /sessions to pick another")
                return
            state = "in-flight" if sid in TURNS else entry.get("status", "?")
            q = len(QUEUES.get(sid, []))
            sl = pane_status_line(tmux_pane_of(entry)) if entry.get("tmux") else None
            reply(chat_id, f"{entry.get('name', sid[:8])} · "
                           f"{os.path.basename(entry.get('cwd', '?'))} · {state}"
                           + (f" · {q} queued" if q else "")
                           + (f"\n{sl}" if sl else ""))
            return

        if cmd == "/stop":
            target = load_target()
            if not target:
                reply(chat_id, "No target — /sessions to pick one")
                return
            sid = target["session_id"]
            entry = find_session(sid)
            if entry is None:
                drop_turn(sid)
                reply(chat_id, "Session already gone")
                return
            if entry.get("kind") == "interactive":
                pane = tmux_pane_of(entry)
                if pane:
                    subprocess.run(["tmux", "send-keys", "-t", pane, "Escape"],
                                   capture_output=True, timeout=15)
                    note = "Escape sent to tmux pane"
                else:
                    note = "no tmux pane registered — interrupt it in its terminal"
            elif entry.get("kind") == "background":
                r = subprocess.run(["claude", "stop", stop_id(entry)],
                                   capture_output=True, text=True, timeout=30)
                note = ((r.stdout or r.stderr).strip() or "stopped")
                note += " — claude attach reopens it"
            else:
                note = "unknown session kind; not interrupted"
            drop_turn(sid)
            reply(chat_id, f"Interrupted — {note}")
            return

        if cmd == "/loop":
            target = load_target()
            if not target:
                reply(chat_id, "No target — /sessions to pick one")
                return
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                reply(chat_id, "Usage: /loop <prompt>")
                return
            prompt = parts[1].strip() + "\n\nOutput DONE when complete."
            err = inject_text(target["session_id"], prompt, chat_id,
                              loop={"prompt": prompt, "iter": 1})
            reply(chat_id, err or "Ralph Loop started (max 5 iterations)")
            return

        if cmd == "/oneshot":
            parts = text.split(maxsplit=1)
            rest = parts[1].strip() if len(parts) > 1 else ""
            if not rest:
                reply(chat_id, "Usage: /oneshot [<dir>] <question>\n"
                               "Asks a fresh throwaway session in a neutral scratch "
                               "dir (no project context — pass <dir> to ground it "
                               "in a project), delivers the answer, closes it.")
                return
            cwd, question = None, rest
            first = rest.split(None, 1)
            if len(first) == 2:
                d = os.path.abspath(os.path.expanduser(first[0]))
                if os.path.isdir(d):
                    cwd, question = d, first[1].strip()
            scratch = False
            if cwd is None:
                # dir-agnostic default: the neutral scratch workspace — no
                # project CLAUDE.md applies, only the global ~/CLAUDE.md
                cwd = _RUNTIME["ONESHOT_DIR"]
                os.makedirs(cwd, exist_ok=True)
                scratch = True
            name = uniq_name("oneshot" if scratch else
                             "oneshot-" + re.sub(r"[^A-Za-z0-9._-]", "_",
                                                 os.path.basename(cwd) or "claude"))
            short, err = spawn_bg(cwd, name)
            if err:
                if scratch and "trust" in err.lower():
                    err = (f"{cwd} is not trusted yet — one-time setup: run "
                           "`claude` in it once and accept the trust prompt, "
                           "then retry (never needed again).")
                reply(chat_id, f"❌ {err}")
                return
            # wait for registration — the sessionId is needed to inject
            entry = None
            for _ in range(15):
                time.sleep(1)
                _AGENTS["ts"] = 0
                entry = next((e for e in (agents(0) or [])
                              if e["sessionId"].startswith(short)), None)
                if entry:
                    break
            if not entry:
                _PENDING_NAMES.discard(name)
                try:
                    subprocess.run(["claude", "stop", short],
                                   capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                reply(chat_id, "Oneshot spawned but never registered — stopped it. Try again.")
                return
            _PENDING_NAMES.discard(name)
            sid = entry["sessionId"]
            err = None
            for _ in range(3):  # fresh session: socket + key may lag registration
                err = inject_text(sid, question, chat_id, oneshot=True)
                if not err:
                    break
                time.sleep(2)
            if err:
                close_session(sid)
                reply(chat_id, f"❌ {err} — spawned session closed")
                return
            reply(chat_id, f"⚡ {name} ({short}) in {cwd} — answering…")
            return

        if cmd in BLOCKED_COMMANDS:
            reply(chat_id, f"'{cmd}' not supported (interactive)")
            return

    # Regular message → inject into target
    target = load_target()
    if not target:
        reply(chat_id, "No target session — /sessions to pick one")
        return
    err = inject_text(target["session_id"], text, chat_id)
    if err:
        reply(chat_id, err)
        return
    if msg_id:
        # 🫡 acknowledges receipt; ✅ is not in Telegram's bot reaction whitelist
        telegram_api("setMessageReaction", {
            "chat_id": chat_id, "message_id": msg_id,
            "reaction": [{"type": "emoji", "emoji": "🫡"}]}, quiet=True)


# --- poll loop -----------------------------------------------------------------------

def poll_loop():
    offset = 0
    print("Polling Telegram for updates...")
    while True:
        reload_runtime()  # mtime-guarded — picks up telegram.env edits live
        try:
            r = telegram_api("getUpdates", {
                "offset": offset,
                "timeout": 60,
                "allowed_updates": ["message", "callback_query"],
            })
        except Exception as e:
            print(f"getUpdates crashed: {e}; retrying in 5s")
            time.sleep(5)
            continue
        if not r or not r.get("ok"):
            time.sleep(2)
            continue
        for update in r.get("result", []):
            offset = update["update_id"] + 1
            if not is_allowed(update):
                print(f"Ignored update from unauthorized user: {update}")
                continue
            try:
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])
            except Exception as e:
                # one bad update must never take the whole bridge down
                print(f"update handling failed: {e}")


def delete_webhook():
    """Force polling: a stale webhook makes getUpdates 409 forever."""
    r = telegram_api("deleteWebhook", {"drop_pending_updates": False})
    print(f"Webhook cleared: {r.get('ok') if r else 'failed'}")


def setup_bot_commands():
    """Register the menu everywhere it may have been set before — including a
    chat-specific scope, which overrides the default list in that chat (that's
    how leftovers from other Telegram integrations survive a plain reset)."""
    scopes = [{"type": "default"}, {"type": "all_private_chats"}]
    try:
        with open(CHAT_ID_FILE) as f:
            cid = f.read().strip()
        if cid:
            scopes.append({"type": "chat", "chat_id": int(cid)})
    except (OSError, ValueError):
        pass
    ok = True
    for sc in scopes:
        r = telegram_api("setMyCommands", {"commands": BOT_COMMANDS, "scope": sc})
        ok = ok and bool(r and r.get("ok"))
    print("Bot commands registered" if ok else "Bot command registration incomplete")


def main():
    reload_runtime()
    signal.signal(signal.SIGHUP, lambda *_: reload_runtime(force=True))
    if not _RUNTIME["TELEGRAM_BOT_TOKEN"]:
        # non-zero exit so orchestrators/restart policies treat it as a failure
        sys.exit("Error: TELEGRAM_BOT_TOKEN not set — set it in the environment "
                 f"or in {CONF_FILE}")
    setup_bot_commands()
    delete_webhook()   # force polling mode; a stale webhook = endless 409s
    target = load_target()
    print("claude-tg | target: " +
          (target["session_id"][:8] + "…" if target else "none — /sessions to pick"))
    threading.Thread(target=watcher, daemon=True).start()
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
