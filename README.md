# claude-tg

A Telegram remote control for Claude Code sessions.

Started as a fork of [`hanxiao/claudecode-telegram`](https://github.com/hanxiao/claudecode-telegram) (MIT); the current implementation is independent.

```
Telegram ──getUpdates poll──► claude-tg ──inject──► any running Claude session
   ▲                            │  ▲                    (tmux pane, ⌨ · background, ⚡/♻)
   └────sendMessage◄────────────┘  └── transcript JSONL tail ◄── assistant text
```

- **Discovery** — `claude agents --json`: every running session (tmux panes and
  background) with name, project, and idle/busy status.
- **Injection** — three channels, picked per target (see
  [Injection channels](#injection-channels)): clean `send-keys` into tmux
  panes, the cross-session UDS socket for live sessions, or stop+resume for
  background sessions.
- **Replies** — the bridge tails the target session's transcript
  (`~/.claude/projects/<munged-cwd>/<sessionId>.jsonl`) and forwards exactly
  the injected turn's assistant text. The Stop hook stays registered but is
  inert: the bridge writes no pending markers.
- **Live view** — `claude attach <id|name>` for background sessions, `tmux
  attach` for interactive panes. Watching while Telegram drives works.

## Files

| File | Purpose |
|------|---------|
| `claude_tg/` | the package: bridge (`__init__.py`) + shared sender (`telegram_lib.py`); installed as the `claude-tg` command |
| `hooks/send-to-telegram.sh` | Claude Code `Stop` hook — inert under claude-tg; kept for hand-rolled marker flows |
| `mcp/telegram_server.py` | stdio MCP server exposing `send_telegram(text)` so Claude can push messages **mid-turn** (auto-labeled with the session name) |
| `install.py` | Installer: deploys lib + hook + MCP server (`--hook/--mcp/--lib`); launchd service options `--service` / `--stop-service` / `--reload-service` / `--uninstall-service` |
| `Dockerfile` + `docker-entrypoint.sh` | Container image (entrypoint seeds workspace trust for `/app`) |
| `compose.yaml` + `env.example` | Compose service + config template (`cp env.example .env`) |
| `pyproject.toml` | Packaging: `pip install .` / `pipx install .` → `claude-tg` command |

## Requirements

- **Python ≥ 3.9** — all Python dependencies are stdlib (`urllib.request`,
  `json`, `socket`, `subprocess`, `threading`, …); nothing to pip-install for
  the bridge itself. (Note: `urllib` is intentionally *not* a pip dependency —
  it ships with Python, and the PyPI name has been squatted by malware.)
- Optional: `pip install "claude-tg[socks]"` for `socks5(h)://` proxies
  (PySocks).
- System tools: `claude` CLI on PATH; `jq` for the Stop hook; `tmux` for the ⌨
  channel. The [Docker image](#docker) bundles all three.

## Setup

1. `npm install -g @anthropic-ai/claude-code` and create a bot via `@BotFather`.
2. Environment (`~/.zshenv` — the hook and MCP server inherit Claude's env):
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:AAH..."
   export ALLOWED_TELEGRAM_USER_IDS="123456789"   # recommended
   ```
   All options — proxy, forum topics, injection-channel switch, tmux session —
   are listed in [Environment variables](#environment-variables) below.

3. Install the Claude-side integration — **this does not install the bridge**
   (that's the next step); it deploys the shared lib, the Stop hook, and the
   MCP server, registering both with Claude Code:
   ```bash
   python3 install.py          # everything
   python3 install.py --mcp    # or just one piece: --hook / --mcp / --lib
   ```
4. Start the bridge — the installer doesn't cover this. Either install the
   console script (`pip install .`, or `pipx install .` for an isolated venv),
   or run it straight from the checkout:
   ```bash
   claude --bg                 # a persistent, idle background session (optional)
   claude-tg                   # console script (after pip/pipx install)…
   python3 -m claude_tg        # …or straight from the checkout
   ```
5. Message the bot on Telegram → `/sessions` → tap a session → just type.

### Autostart at login (macOS)

```bash
python3 install.py --service
```

Installs a launchd user agent that runs the bridge at login and restarts it on
crash. It seeds `~/.claude/telegram.env` from your current environment on
first install — after that, everyday changes go through `/reload` (no plist
edits, no restarts). `--stop-service` stops it, `--uninstall-service` removes
it.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from `@BotFather` |
| `ALLOWED_TELEGRAM_USER_IDS` | *(unset — accept all)* | Comma-separated Telegram user ids allowed to drive the bot |
| `TELEGRAM_HTTP_PROXY` | *(unset)* | Proxy for Telegram API calls: `http(s)://…`, `socks5(h)://…` (needs PySocks), or `none` to force direct |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | *(unset)* | Standard proxy vars, auto-detected when `TELEGRAM_HTTP_PROXY` is unset |
| `TELEGRAM_FORUM_CHAT_ID` | *(unset)* | Forum-enabled group id → one topic per session (bot needs *Manage Topics*); unset = bold-name marking in the DM |
| `TMUX_SESSION` | `claude` | Existing tmux session `/new <dir> tmux` adds windows to |

### Live updates

`~/.claude/telegram.env` (KEY=VALUE, same names as the table) **overrides these
at runtime** — edit it, then send `/reload` to the bot, run
`python3 install.py --reload-service`, or send the bridge SIGHUP. It is also
re-read automatically every poll cycle. The same file updates the native
bridge and the container (it lives in the mounted `~/.claude`).

## Bot commands

| Command | Action |
|---------|--------|
| `/sessions` | List running sessions (name · project · status · channel) — tap to target; `/close` shuts the target down |
| `/new <dir> [tmux\|uds\|bg]` | Spawn a session in `<dir>`: **tmux** (default — window in your tmux session, ⌨ clean send-keys), `uds` (⚡ background + socket), or `bg` (♻ background + resume channel). Sessions are named after the directory, suffixed `-2`, `-3`… only on a collision with a running session. The channel is remembered per session |
| `/close` | Close the targeted session (resumable) |
| `/status` | Show target session state, queue, and pane status line |
| `/stop` | Interrupt the target's turn (Escape in tmux panes; `claude stop` for bg) |
| `/loop <prompt>` | Native Ralph loop: re-prompts up to 5× until the reply contains `DONE` |
| `/chatid` | Show this chat's id (for `TELEGRAM_FORUM_CHAT_ID`) |
| `/help` | Show usage |
| `/reload` | Re-read `~/.claude/telegram.env` (live config) |
| any text | Injected into the target session immediately; the CLI holds mid-turn input itself (only ♻ resume waits for idle). The bot reacts 🫡 to acknowledge |

## Injection channels

Both session kinds work side by side — `/sessions` marks the channel of each
entry:

| Channel | Applies to | How | What the session sees |
|---------|-----------|-----|------------------------|
| ⌨ `send-keys` | sessions with a tmux pane | `tmux send-keys` types the prompt | **nothing extra** — clean text |
| ⚡ `uds` (default) | background and non-tmux sessions | cross-session UDS socket | Claude Code's peer-message safeguard header (~150 tokens of context) |
| ♻ `bg-resume` (opt-in) | background sessions | `claude stop` + `claude --bg --resume <id> "<prompt>"` per message | **nothing extra** — a real user turn |

Notes:

- ⌨ is the cleanest for panes you watch; multi-line prompts are collapsed to
  one line (Enter submits).
- ⚡ is instant and needs nothing but the socket. The UDS protocol is
  undocumented — verified on claude-code 2.1.309; the bridge logs loudly if a
  CLI update breaks it.
- ♻ is the no-UDS fallback for background sessions: spawn one with
  `/new <dir> bg`. Each message restarts the session process (the daemon's
  spare hosts keep this fast); there is no live socket dependency at all.
- UDS limitations to be aware of: the safeguard wrapper is added by Claude
  Code and cannot be omitted. Mid-turn messages are fine — the CLI holds
  them (TUI buffer / cross-session inbox) until the turn ends; the bridge
  itself never buffers except on ♻ resume, where sending early would kill
  the running turn.
- `/new` remembers the channel it spawned a session with
  (`~/.claude/telegram_channels`) and it governs that session for good.

Replies work identically on all channels: the bridge tails the transcript and
forwards only the injected turn's assistant text.

## Per-session topics

With several sessions answering at once, answers need attribution. Create a
private group, enable **Topics**, add the bot as an admin with *Manage Topics*,
type `/chatid` in it — the bot prints the id — and set `TELEGRAM_FORUM_CHAT_ID`
to that. The bridge then creates one topic per session — named
`<session> · <project>` — and routes each session's replies and typing
indicator there. Topics are created lazily on first reply and remembered in
`~/.claude/telegram_topics`.

Without a forum, answers to the private chat are prefixed with the session
name in bold, so you always know who spoke.

Spawned `tmux` panes run `claude` in a window of your existing tmux session
(`TMUX_SESSION`, default `claude`) or a new session named after the directory;
the window and the session share that name, suffixed `-2`, `-3`… only when a
running session already took it (the CLI would otherwise allow duplicates
silently). An untrusted workspace shows its trust dialog in the pane —
`tmux attach` once to accept it.

## Docker

Container mode = **bridge + its own Claude sessions, all inside the
container**, driven entirely from Telegram (sessions are created with
`docker exec claude-tg claude --bg -n <name>` and live in the container's
`~/.claude` volume).

```bash
cp env.example .env    # fill in TELEGRAM_BOT_TOKEN
docker compose up -d --build
docker exec claude-tg claude --bg -n myproj   # spawn a session to drive
docker compose logs -f
```

`restart: unless-stopped` keeps it alive across Docker Desktop restarts;
transcripts persist in the `~/.claude` volume, so sessions survive container
recreation and are resumable.

**macOS / Docker Desktop limitation (verified):** the container cannot see or
drive host-side Claude sessions. The session registry is pidDomain-isolated
(`agents --json` returns `[]` for darwin entries) and host Unix sockets /
tmux servers do not cross the Docker VM boundary. To drive your local tmux
panes and background sessions, run the bridge natively instead
(`python3 install.py && claude-tg`). On a **Linux** host the mounts
`~/.claude` + `/tmp/cc-socks` genuinely work and host-session driving from a
container is possible.

Equivalent raw `docker run` (no compose):

```bash
docker run -d --name claude-tg \
  --env-file .env \
  -v claude-tg-home:/root/.claude \
  claude-tg:latest
```

## The `send_telegram` MCP tool

Registered user-scope by `install.py`, so every Claude session can push
progress updates and questions to your Telegram mid-turn via the same shared
lib. It sends to the chat that last messaged the bot
(`~/.claude/telegram_chat_id`), auto-labeled with the session's name — the
server resolves it from the claude process that spawned it — unless an
explicit `header` is passed. It should not be used for a bridge-initiated
task's *final* answer — the bridge delivers that itself and a tool send would
duplicate it.

## Proxy

One implementation, in `telegram_lib.py` (hook, MCP tool, and bridge all
import it): `TELEGRAM_HTTP_PROXY` (or `none`) overrides auto-detected
`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`. SOCKS schemes need `pip install PySocks`.
Set proxy vars in `~/.zshenv` so Claude's environment has them.

## License

MIT — see [`LICENSE`](LICENSE). Original author of the fork ancestor: Han Xiao (@hanxiao).
