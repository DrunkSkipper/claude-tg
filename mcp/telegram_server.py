#!/usr/bin/env python3
"""Minimal stdio MCP server exposing one tool: send_telegram.

Lets any Claude Code session push markdown-formatted messages to the user's
Telegram chat. Uses the shared ~/.claude/telegram_lib.py (proxy-aware, same
converter as the Stop hook and the bridge). The reply destination is whatever
chat last messaged the bot (~/.claude/telegram_chat_id). Pushes are
auto-labeled with the session's name (resolved from the owning claude
process) unless an explicit header is passed.

Intended for mid-turn progress updates, questions, and reports. The final
answer of a bridge-driven turn is delivered by the bridge itself — do not
re-send it here (duplicates).

Register user-scope:
  claude mcp add --scope user telegram -- python3 ~/.claude/telegram-mcp/server.py
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.expanduser("~/.claude"))

from telegram_lib import read_chat_id, send_markdown  # noqa: E402

_label = None


def session_label():
    """Bold session name for auto-marking pushes, matching the bridge's
    delivery header. The claude process owns us (possibly via a shell
    wrapper), so walk ancestor pids and look each up in the session
    registry (~/.claude/sessions/<pid>.json) — the authoritative name
    source. `claude agents --json` is NOT one: it omits sessions that
    parked a background job (parkedJobId set), leaving their pushes
    unlabeled. Memoized; None when undiscoverable — pushes then go out
    unlabeled."""
    global _label
    if _label is None:
        reg = os.path.expanduser("~/.claude/sessions")
        pid = os.getppid()
        for _ in range(5):
            try:
                e = json.load(open(os.path.join(reg, f"{pid}.json")))
                os.kill(pid, 0)  # registry keeps dead leftovers — require liveness
                _label = f"**{e.get('name') or str(e.get('sessionId'))[:8]}**"
                break
            except (OSError, ValueError):
                pass
            try:
                out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=5).stdout
                pid = int(out.strip())
            except (ValueError, subprocess.SubprocessError):
                break
            if pid <= 1:
                break
    return _label

TOOLS = [
    {
        "name": "send_telegram",
        "description": (
            "Send a markdown-formatted message to the user's Telegram chat. "
            "Use for mid-turn progress updates, results, and questions while "
            "working. Do NOT send the final answer of a Telegram-initiated "
            "task — the bridge delivers it automatically at turn end (a "
            "tool send would duplicate it). Long texts are split into "
            "numbered parts automatically. Each push is auto-labeled with "
            "this session's name unless you pass an explicit header. "
            "Pre-format for Telegram: bold "
            "headings, code refs in backticks, no wide tables (use labeled "
            "lists instead)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Markdown-formatted message body.",
                },
                "header": {
                    "type": "string",
                    "description": "Optional first-line header, stamped on every delivered part.",
                },
            },
            "required": ["text"],
        },
    }
]


def tool_call(params: dict) -> dict:
    text = (params.get("text") or "").strip()
    if not text:
        return {"content": [{"type": "text", "text": "error: empty text"}], "isError": True}

    chat_id = read_chat_id()
    if not chat_id:
        return {"content": [{"type": "text", "text":
                "error: no Telegram chat known yet — message the bot once from "
                "Telegram so it can learn the chat id (~/.claude/telegram_chat_id)"}],
                "isError": True}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"content": [{"type": "text", "text":
                "error: TELEGRAM_BOT_TOKEN not set in this session's environment"}],
                "isError": True}

    sent, total, errors = send_markdown(text, chat_id, token,
                                        params.get("header") or session_label())
    summary = f"sent {sent}/{total} part(s)" + ("; " + "; ".join(errors) if errors else "")
    return {"content": [{"type": "text", "text": summary}], "isError": bool(errors)}


def reply(msg_id, result=None, error=None):
    resp = {"jsonrpc": "2.0", "id": msg_id}
    resp["result" if error is None else "error"] = error or result
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "telegram", "version": "2.0.0"},
            })
        elif method == "notifications/initialized" or method is None:
            continue
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            if msg.get("params", {}).get("name") != "send_telegram":
                reply(msg_id, error={"code": -32602, "message": "unknown tool"})
                continue
            try:
                reply(msg_id, tool_call(msg["params"].get("arguments", {})))
            except Exception as exc:  # noqa: BLE001 - report, do not crash the server
                reply(msg_id, {"content": [{"type": "text", "text": f"error: {exc}"}],
                               "isError": True})
        else:
            if msg_id is not None:
                reply(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})


if __name__ == "__main__":
    main()
