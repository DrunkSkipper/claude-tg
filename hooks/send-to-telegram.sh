#!/bin/bash
# Claude Code Stop hook - sends response back to Telegram
# Install: copy to ~/.claude/hooks/send-to-telegram.sh and chmod +x
# Then register in ~/.claude/settings.json under hooks.Stop
#
# From hanxiao/claudecode-telegram (MIT), patched to support proxy.
# The sender (markdown->HTML conversion, splitting, proxy) lives in the shared
# ~/.claude/telegram_lib.py, also used by the bridge and the telegram MCP tool.
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-YOUR_BOT_TOKEN_HERE}"
# Proxy support — set TELEGRAM_HTTP_PROXY or HTTP_PROXY/HTTPS_PROXY in the
# environment Claude Code is launched in. The hook inherits that env.
#   export TELEGRAM_HTTP_PROXY="http://127.0.0.1:7890"
#   export TELEGRAM_HTTP_PROXY="socks5h://127.0.0.1:1080"  (needs PySocks)
#   export TELEGRAM_HTTP_PROXY="none"  # force-disable proxy
export TELEGRAM_HTTP_PROXY="${TELEGRAM_HTTP_PROXY:-}"
INPUT=$(cat)
echo "$(date '+%F %T') stop-hook run" >> "$HOME/.claude/hooks/telegram-hook.log"
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')
CHAT_ID_FILE=~/.claude/telegram_chat_id
PENDING_DIR=~/.claude/telegram_pending
# Resolve this instance's pending marker. The bridge writes one marker per
# project dir, named by the cwd munged with '/'->'-' and leading '-' stripped —
# the same encoding is computed here from the hook input's cwd (or, as a
# fallback, from the transcript's project directory name).
MUNGED=$(echo "$INPUT" | jq -r '.cwd // empty')
if [ -z "$MUNGED" ]; then
  MUNGED=$(basename "$(dirname "$TRANSCRIPT_PATH")" 2>/dev/null)
fi
MUNGED=$(printf '%s' "$MUNGED" | sed 's|/|-|g; s|^-*||')
[ -n "$MUNGED" ] || exit 0
PENDING_FILE="$PENDING_DIR/$MUNGED"
# Transitional: a pre-multi-instance bridge wrote one global marker file
if [ ! -f "$PENDING_FILE" ] && [ -f "$PENDING_DIR" ]; then PENDING_FILE="$PENDING_DIR"; fi
# Only respond to Telegram-initiated messages
[ ! -f "$PENDING_FILE" ] && exit 0
PENDING_TIME=$(cat "$PENDING_FILE" 2>/dev/null)
NOW=$(date +%s)
# 3600s: a long in-flight turn can legitimately outlive the pending file
[ -z "$PENDING_TIME" ] || [ $((NOW - PENDING_TIME)) -gt 3600 ] && rm -f "$PENDING_FILE" && exit 0
[ ! -f "$CHAT_ID_FILE" ] && rm -f "$PENDING_FILE" && exit 0
CHAT_ID=$(cat "$CHAT_ID_FILE")
# Prefer last_assistant_message from the hook stdin JSON: the transcript's
# final assistant entry is flushed only AFTER Stop hooks run, so parsing the
# file races the flush (and api-request-blob entries also match the grep).
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')
TMPFILE=$(mktemp)
if [ -n "$LAST_MSG" ]; then
  printf '%s' "$LAST_MSG" > "$TMPFILE"
else
  # Transcript only needed for the fallback parse
  [ ! -f "$TRANSCRIPT_PATH" ] && exit 0
  LAST_USER_LINE=$(grep -n '"type":"user"' "$TRANSCRIPT_PATH" | tail -1 | cut -d: -f1)
  [ -z "$LAST_USER_LINE" ] && exit 0
  tail -n "+$LAST_USER_LINE" "$TRANSCRIPT_PATH" | \
  grep '"type":"assistant"' | \
  jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")' > "$TMPFILE" 2>/dev/null
fi
# Empty extraction: the Telegram prompt may still be queued in the previous
# turn (race with the bridge), so no assistant text follows it yet.
# Keep PENDING so the Stop hook of the turn that answers can send it.
# (jq emits a bare newline on empty input, so test stripped content, not -s.)
if [ -z "$(tr -d '[:space:]' < "$TMPFILE")" ] || [ "$(cat "$TMPFILE")" = "null" ]; then
  rm -f "$TMPFILE"
  exit 0
fi
python3 - "$TMPFILE" "$CHAT_ID" "$TELEGRAM_BOT_TOKEN" << 'PYEOF'
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude"))
try:
    from telegram_lib import send_markdown
except ImportError as e:
    sys.stderr.write(f"telegram_lib import failed: {e}\n")
    sys.exit(0)  # a hook must not block Claude Code
tmpfile, chat_id, token = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(tmpfile).read().strip()
if text and text != "null":
    sent, total, errors = send_markdown(text, chat_id, token)
    if errors:
        sys.stderr.write("; ".join(errors) + "\n")
PYEOF
rm -f "$TMPFILE" "$PENDING_FILE"
exit 0
