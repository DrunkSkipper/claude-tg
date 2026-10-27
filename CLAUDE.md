# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**claude-tg** (initial release) — a Telegram remote control for Claude Code sessions; started as a fork of `hanxiao/claudecode-telegram`. Drives any running Claude Code session over the **cross-session UDS messaging socket** (reverse-engineered from claude-code 2.1.309 — undocumented, re-verify after CLI updates). No tmux required for injection; no Stop-hook involvement for replies.

There is no test tooling — `python3 -m py_compile` + `bash -n` are the checks. Packaging: `pyproject.toml` (`pip install .` / `pipx install .` → `claude-tg` console script).

## Components

- `claude_tg/` — the package: `__init__.py` is the bridge (long-polls Telegram, discovers sessions via `claude agents --json`, injects prompts over the UDS socket, tails the target session's transcript for replies; installed as the `claude-tg` console script via `claude_tg:main`); `telegram_lib.py` is the shared sender (proxy precedence, markdown→Telegram-HTML converter, 4096-splitting); deployed copy at `~/.claude/telegram_lib.py` used by hook and MCP server.
- `install.py` — installer: deploys lib + hook + MCP server (`--hook/--mcp/--lib`); launchd service options `--service` / `--stop-service` / `--reload-service` / `--uninstall-service`.
- `hooks/send-to-telegram.sh` — Stop hook. **Inert under claude-tg** (no pending markers are written); kept for hand-rolled flows that write markers directly.
- `mcp/telegram_server.py` — stdio MCP server, `send_telegram(text)` tool for mid-turn pushes (auto-labeled with the session's name, resolved from the owning claude process's pid via `agents --json`); registered user-scope via `claude mcp add --scope user telegram -- python3 ~/.claude/telegram-mcp/server.py`.
- `Dockerfile` + `docker-entrypoint.sh` + `compose.yaml` + `env.example` — container mode (in-container sessions only on macOS; see README Docker).

## Running

```bash
python3 install.py     # deploys lib + hook + MCP server (--hook/--mcp/--lib for pieces)
claude mcp add --scope user telegram -- python3 ~/.claude/telegram-mcp/server.py
claude-tg             # console script; needs TELEGRAM_BOT_TOKEN, claude on PATH (or: python3 -m claude_tg)
claude --bg            # optional: persistent idle background session to drive
docker compose up -d --build   # containerized instead (config in .env, see README Docker)
```

Env (`~/.zshenv`, so Claude's env has it): `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`, `TELEGRAM_HTTP_PROXY` (`none` forces direct; else auto-detects `HTTP_PROXY`/`HTTPS_PROXY`), `TELEGRAM_FORUM_CHAT_ID` (forum group → one topic per session; bot needs can_manage_topics; topics cached in `~/.claude/telegram_topics`; without it, deliveries to the private chat get a bold session-name header), `TMUX_SESSION` (default `claude` — the session `/new <dir> tmux` adds windows to). All of these are **live-overridable** via `~/.claude/telegram.env` (KEY=VALUE) — re-read each poll cycle, on SIGHUP, and on `/reload`; `telegram_lib.reload_proxy()` rebuilds the opener.

## Architecture

Bridge and sessions never share a process; coordination is via session registries and transcripts:

- **Discovery**: `claude agents --json` (2s cache in bridge) → `[{sessionId, pid, name, status: idle|busy, cwd, kind: interactive|background, tmux?, ...}]`, merged with `~/.claude/sessions/<pid>.json` entries it hides (see Gotchas; registry is also the fallback source when `agents --json` fails).
- **Injection (hybrid, per target)**: channel precedence — per-session override (`telegram_channels`, set by `/new`) > tmux pane (`tmux send-keys -l` + Enter, no wrapper; multi-line collapsed) > UDS. UDS: connect `/tmp/cc-socks/<pid>.sock`, line 1 `{"type":"auth","token":"<peerToken>"}`, line 2 `{"type":"user","message":{"role":"user","content":…}}` — NDJSON, first-line deadline, no ack. Token: `~/.claude/sessions/<pid>.<sha256(socket_path)>.key` → `{"peerToken":"<32hex>"}`. UDS deliveries appear in the session wrapped in Claude Code's hardcoded cross-session safeguard (`This came from another Claude session…`) — verified not omissible (no setting/env; receiver-side; only binary patching would remove it). The ♻ resume channel (`claude stop` + `claude --bg --resume <id> "<prompt>"`, background sessions only) delivers a real user turn, no wrapper, at the cost of a process restart per message.
- **Reply path**: at inject, record transcript size; watcher thread (1s) reads new complete JSONL lines from `~/.claude/projects/<munged-cwd>/<sessionId>.jsonl`, collects `type:"assistant"` text blocks, delivers when the session's `agents --json` status leaves `busy`. State machine per turn: `wait` (require pickup: status busy or transcript growth, else 120s warn) → `work` → deliver. 30-min hard timeout.
- **Busy-target queueing (♻ resume only)**: tmux and UDS channels inject immediately, even mid-turn — the CLI holds the input natively (TUI buffer / cross-session inbox) and the watcher delivers only once the injected marker appears in the transcript, so replies stay straight. Only ♻ resume queues bridge-side (a mid-turn `claude stop` would kill the running turn); the watcher injects the next one when the current turn finishes.
- **Targeting**: `~/.claude/telegram_target` stores `{"session_id": …}`; `/sessions` (callback `sel:<uuid>`) sets it. Target is re-resolved against `agents --json` every use — dead sessions error cleanly.
- **Spawning**: `/new <dir> [tmux|uds|bg]` — tmux (default) adds a window to the existing tmux session (`TMUX_SESSION` or the sole one); uds/bg spawn `claude --bg -n <basename>` in the dir (trust hint for untrusted workspaces). The chosen channel persists in `~/.claude/telegram_channels` and governs routing for that session.
- **`/status`**: one line for the targeted session — name · project · state (`in-flight` while the watcher tracks a turn, else `idle`/`busy`), queue depth, and the pane's own status line for ⌨ targets.
- **`/stop`**: interactive sessions — Escape via the `tmux` field in the registry (`"claude:@2.%3"` → pane `%3`); background — `claude stop <shortId>` (conversation kept, `claude attach` reopens).
- **`/close`**: shut a session down entirely — `tmux kill-pane` for panes, `claude stop` for background; conversation stays resumable (`claude --resume <shortId>`).
- **`/loop`** is native now: re-injects the prompt up to 5× until the reply's tail matches `\bDONE\b`.
- **`/oneshot [<dir>] <question>`**: spawn a throwaway ⚡ bg session (`spawn_bg`, shared with `/new`), inject the question with `oneshot=True` on the turn; `turn_cleanup` closes the session when the answer is delivered (also on pickup/turn timeout, and on inject failure right after spawn). `job_sid` is closed instead of the parent for parked turns. Cwd: dir arg > the neutral scratch dir `ONESHOT_DIR` (default `~/.claude/oneshot`, created on first use — no project CLAUDE.md, only the global `~/CLAUDE.md`). Workspace trust: a fresh dir fails `--bg` spawn with "Workspace not trusted"; subdirs of a trusted workspace inherit trust (verified), so `ONESHOT_DIR` under one needs no setup; `~/.claude/oneshot` itself needs a one-time manual trust acceptance. Trust flag lives at `projects[<realpath>].hasTrustDialogAccepted` in `~/.claude.json` — the bridge does not write it.
- **`/chatid`** prints the chat's id — setup helper for `TELEGRAM_FORUM_CHAT_ID`. **`/help`** mirrors the README usage card.

## Gotchas

- The UDS protocol is **undocumented** — the binary's embedded JS is the spec (`LC_ALL=C /usr/bin/grep -a -b -o -F 'pattern' <claude.exe>` then `tail -c +OFF | head -c N` for context; see memory `claude-code-bg-and-channels-findings`). Expect breakage on major CLI updates; the bridge surfaces inject failures in Telegram.
- Only *deployed* copies run: `~/.claude/telegram_lib.py`, `~/.claude/hooks/send-to-telegram.sh`, `~/.claude/telegram-mcp/server.py`. `install.py` redeploys all three — run it after edits.
- Only one bridge may poll a bot token (Telegram 409s on concurrent `getUpdates`). Kill the old bridge before starting a new one.
- Key-file names embed `sha256(socket_path)` — socket path changes (pid reuse, `/tmp` layout) change the filename, never hand-craft it.
- The hook's debug log is `~/.claude/hooks/telegram-hook.log`.
- The project expects Claude Code backed by Z.ai GLM (`ANTHROPIC_BASE_URL` in `~/.claude/settings.json`); native `channels/telegram` does NOT work over that router (Anthropic-gated).
- **Docker on macOS cannot drive host sessions** (verified): the registry is pidDomain-isolated (`agents --json` → `[]` for darwin entries inside a container) and host UDS/tmux don't cross the Docker VM. Container mode = bridge + its own in-container sessions; do NOT mount host `/tmp/cc-socks` into it (it shadows the container's own socket dir). `~/.claude.json` should be mounted alongside `~/.claude` to keep your real CLI config in the container (docker-entrypoint.sh seeds a minimal one when absent, so the container runs without the mount too).
- `claude agents --json` **omits sessions that parked a background job** (`parkedJobId` in their `~/.claude/sessions/<pid>.json` — i.e. sessions that forked a `--bg` job off their own conversation). The registry files still have them: the bridge merges registry entries back (filter `pidDomain == "darwin"` + `os.kill(pid, 0)` liveness — the registry is shared across pid domains and keeps dead leftovers; registry `kind: "bg"` must normalize to `"background"`), and the MCP server reads the registry directly for auto-labels (verified 2026-10-25: `pt-ngfw-eb` was invisible → its `send_telegram` pushes went out unlabeled).
- **Transcript bucket munging** (2.1.309, verified 2026-10-27): `~/.claude/projects/<bucket>/` maps every char outside `[A-Za-z0-9_]` to `-` and KEEPS the leading dash (`/x/y.z` → `-x-y-z`). The original bridge `munge()` (`replace("/","-")+lstrip`) matched **no bucket at all** — the transcript reply tail was dead from release until fixed 2026-10-27. `transcript_path()` now falls back to a sessionId glob (`projects/*/<sid>.jsonl`, UUIDs are unique) whenever the munged bucket is missing — re-verify munging after CLI updates.
- `setMessageReaction` accepts only Telegram's bot reaction whitelist (70 emojis — ✅ is NOT one; the code uses 🫡 to acknowledge receipt). Exact strings matter (`❤` not `❤️`, `✍` not `✍️`); full list at core.telegram.org/bots/api → ReactionTypeEmoji. Cosmetic reactions pass `quiet=True` so failures never spam the bridge log.
