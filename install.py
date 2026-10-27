#!/usr/bin/env python3
"""claude-tg installer — deploys the shared lib, the Stop hook, and the MCP
server, and registers them with Claude Code.

  python3 install.py            # everything: lib + hook + mcp
  python3 install.py --hook     # Stop hook only (deploys the lib it needs)
  python3 install.py --mcp      # MCP server only (deploys the lib it needs)
  python3 install.py --lib      # shared lib only
  python3 install.py --service [--dir PATH]
                                # launchd user service running the bridge;
                                # --dir selects the checkout (default: the
                                # directory containing install.py)

Nothing here reads or writes credentials: TELEGRAM_BOT_TOKEN stays in your
environment, and the MCP registration is delegated to the `claude` CLI.
"""
import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent
DEST_LIB = HOME / ".claude" / "telegram_lib.py"
DEST_HOOK = HOME / ".claude" / "hooks" / "send-to-telegram.sh"
DEST_MCP = HOME / ".claude" / "telegram-mcp" / "server.py"
SETTINGS = HOME / ".claude" / "settings.json"
HOOK_CMD = "~/.claude/hooks/send-to-telegram.sh"
SERVICE_LABEL = "com.claude-tg.bridge"
LAUNCH_AGENTS = HOME / "Library" / "LaunchAgents"
SERVICE_PLIST = LAUNCH_AGENTS / f"{SERVICE_LABEL}.plist"
SERVICE_LOG = HOME / "Library" / "Logs" / "claude-tg.log"
CONF_FILE = HOME / ".claude" / "telegram.env"


def ok(msg):
    print(f"  ✅ {msg}")


def warn(msg):
    print(f"  ⚠️  {msg}")


def die(msg):
    print(f"  ❌ {msg}")
    sys.exit(1)


def deploy_lib():
    src = REPO / "claude_tg" / "telegram_lib.py"
    if not src.exists():
        die(f"{src} not found — run the installer from the repo directory")
    DEST_LIB.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, DEST_LIB)
    try:
        py_compile.compile(str(DEST_LIB), doraise=True)
    except py_compile.PyCompileError as e:
        die(f"deployed telegram_lib.py does not compile: {e}")
    ok(f"telegram_lib deployed → {DEST_LIB}")


def register_hook_settings():
    """Append our entry to hooks.Stop in ~/.claude/settings.json (idempotent,
    preserving all other content and the file mode)."""
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text())
        except (OSError, json.JSONDecodeError) as e:
            die(f"{SETTINGS} is not valid JSON — fix or back it up first: {e}")
        mode = SETTINGS.stat().st_mode & 0o777
    else:
        data, mode = {}, 0o644
    stop = data.get("hooks", {}).get("Stop", [])
    if any(h.get("command", "").endswith("send-to-telegram.sh")
           for grp in stop for h in grp.get("hooks", [])):
        ok("hook already registered in settings.json")
        return
    stop.append({"matcher": "",
                 "hooks": [{"type": "command", "command": HOOK_CMD}]})
    data.setdefault("hooks", {})["Stop"] = stop
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode or 0o644)
    os.replace(tmp, SETTINGS)
    ok("hook registered in settings.json (settings.json rewritten by json — "
       "check it if you hand-edited formatting)")


def deploy_hook():
    src = REPO / "hooks" / "send-to-telegram.sh"
    if not src.exists():
        die(f"{src} not found — run the installer from the repo directory")
    if not shutil.which("jq"):
        warn("jq is not installed — the hook needs it at runtime "
             "(brew install jq / apt install jq)")
    DEST_HOOK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, DEST_HOOK)
    os.chmod(DEST_HOOK, 0o755)
    r = subprocess.run(["bash", "-n", str(DEST_HOOK)], capture_output=True)
    if r.returncode != 0:
        die(f"hook has syntax errors: {r.stderr.decode().strip()}")
    ok(f"hook deployed → {DEST_HOOK}")
    register_hook_settings()


def seed_conf_file():
    """Capture the current TELEGRAM_* environment into the live config file
    (~/.claude/telegram.env) so the launchd service picks it up. Existing
    files are never overwritten — edit the file and use --reload-service."""
    if CONF_FILE.exists():
        warn(f"live config already exists: {CONF_FILE}")
        return
    keys = ["TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_IDS",
            "TELEGRAM_HTTP_PROXY", "TELEGRAM_FORUM_CHAT_ID"]
    lines = ["# claude-tg live config — KEY=VALUE, overrides process env.",
             "# Edit, then: python3 install.py --reload-service  (or /reload in Telegram)"]
    for k in keys:
        lines.append(f"{k}={os.environ.get(k, '')}")
    CONF_FILE.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(CONF_FILE, 0o600)
    except OSError:
        pass
    ok(f"live config seeded → {CONF_FILE}")


def service_stop():
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok("service stopped (plist kept — --service starts it again)")
    else:
        ok("service was not running")


def service_reload():
    uid = os.getuid()
    r = subprocess.run(["launchctl", "kill", "HUP", f"gui/{uid}/{SERVICE_LABEL}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok("SIGHUP sent — the bridge re-reads ~/.claude/telegram.env")
    else:
        warn(f"could not signal the service: {(r.stderr or r.stdout).strip()}")


def service_install():
    """macOS launchd user service: keep the bridge running (repo checkout,
    script launched by absolute path — no working-directory dependency),
    restart on crash, log to a dedicated file."""
    if not shutil.which("launchctl"):
        die("launchctl not found — the launchd service is macOS-only")
    script = REPO / "claude_tg" / "__init__.py"
    if not script.exists():
        die(f"{REPO} does not look like the claude-tg repo checkout")
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [sys.executable, "-u", str(script)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(SERVICE_LOG),
        "StandardErrorPath": str(SERVICE_LOG),
    }
    SERVICE_PLIST.write_text(json.dumps(plist, indent=2) + "\n")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{SERVICE_LABEL}"],
                   capture_output=True, text=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(SERVICE_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"launchctl bootstrap failed: {(r.stderr or r.stdout).strip()}")
    ok(f"service installed and started → {SERVICE_PLIST}")
    ok(f"bridge log → {SERVICE_LOG}")


def service_uninstall():
    service_stop()
    if SERVICE_PLIST.exists():
        SERVICE_PLIST.unlink()
        ok(f"plist removed → {SERVICE_PLIST}")
    else:
        ok("no plist to remove")


def deploy_mcp():
    src = REPO / "mcp" / "telegram_server.py"
    if not src.exists():
        die(f"{src} not found — run the installer from the repo directory")
    DEST_MCP.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, DEST_MCP)
    try:
        py_compile.compile(str(DEST_MCP), doraise=True)
    except py_compile.PyCompileError as e:
        die(f"deployed MCP server does not compile: {e}")
    ok(f"MCP server deployed → {DEST_MCP}")
    try:
        probe = subprocess.run(["claude", "mcp", "get", "telegram"],
                               capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        warn("`claude` CLI not on PATH — register manually:")
        print(f"     claude mcp add --scope user telegram -- python3 {DEST_MCP}")
        return
    if probe.returncode == 0 and "telegram" in probe.stdout:
        ok("MCP server already registered (user scope)")
        return
    r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "telegram",
                        "--", "python3", str(DEST_MCP)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        ok("MCP server registered (user scope) — restart Claude sessions to load it")
    else:
        warn("registration via CLI failed — run manually:")
        print(f"     claude mcp add --scope user telegram -- python3 {DEST_MCP}")


def main():
    global REPO
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", metavar="PATH", type=Path,
                   help="claude-tg checkout to deploy from and run the bridge "
                        f"from (default: {REPO})")
    p.add_argument("--hook", action="store_true", help="install the Stop hook")
    p.add_argument("--mcp", action="store_true", help="install the MCP server")
    p.add_argument("--lib", action="store_true",
                   help="install only the shared telegram_lib")
    p.add_argument("--service", action="store_true",
                   help="macOS: install+start a launchd user service running the bridge")
    p.add_argument("--stop-service", action="store_true",
                   help="stop the launchd service (plist kept)")
    p.add_argument("--reload-service", action="store_true",
                   help="SIGHUP the launchd service (re-reads ~/.claude/telegram.env)")
    p.add_argument("--uninstall-service", action="store_true",
                   help="stop the launchd service and remove its plist")
    args = p.parse_args()
    if args.dir:
        REPO = args.dir.expanduser().resolve()
    if args.uninstall_service:
        service_uninstall()
        return
    if args.stop_service:
        service_stop()
        return
    if args.reload_service:
        service_reload()
        return
    if args.service:
        deploy_lib()
        seed_conf_file()
        service_install()
        print("done.")
        return
    do_all = not (args.hook or args.mcp or args.lib)
    do_hook, do_mcp = (args.hook or do_all), (args.mcp or do_all)

    print(f"claude-tg installer → {HOME / '.claude'}")
    deploy_lib()  # always: hook and MCP server both depend on it
    if do_hook:
        deploy_hook()
    if do_mcp:
        deploy_mcp()
    print("done.")


if __name__ == "__main__":
    main()
