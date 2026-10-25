# claude-tg — Telegram remote control for Claude Code sessions
#
# The image contains python3 + the claude CLI + tmux/jq, so it can run the
# bridge against sessions living on the host (mount the sockets/registry) or
# spawn its own sessions inside the container.
FROM node:22-bookworm-slim

# the image's only Python app is claude-tg itself — let pip manage it
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip tmux jq \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force

WORKDIR /app
COPY pyproject.toml README.md LICENSE install.py ./
COPY claude_tg/ claude_tg/
COPY hooks/ hooks/
COPY mcp/ mcp/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN pip install --no-deps . \
 && python3 -m py_compile claude_tg/__init__.py claude_tg/telegram_lib.py mcp/telegram_server.py \
 && bash -n hooks/send-to-telegram.sh

# Host mounts expected at runtime (see README "Docker"):
#   ~/.claude        -> /root/.claude   (sessions, keys, transcripts, target/topics state)
#   /tmp/cc-socks    -> /tmp/cc-socks   (UDS injection into host sessions)
# Credentials come from the environment only: TELEGRAM_BOT_TOKEN, etc.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
