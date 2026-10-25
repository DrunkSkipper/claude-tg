#!/bin/sh
# Ensure the container's CLI config exists and trusts /app (the image's
# workdir) so `claude --bg` works headlessly. Merges into whatever config is
# present (a restored backup, a mounted file, or nothing). The container owns
# this file — the host's ~/.claude.json is not mounted.
python3 - <<'EOF'
import json

p = "/root/.claude.json"
try:
    d = json.load(open(p))
except Exception:
    d = {"hasCompletedOnboarding": True}
projects = d.setdefault("projects", {})
projects.setdefault("/app", {})["hasTrustDialogAccepted"] = True
d["hasCompletedOnboarding"] = True
with open(p, "w") as f:
    json.dump(d, f)
EOF
exec claude-tg
