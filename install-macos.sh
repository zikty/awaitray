#!/usr/bin/env bash
# Set up the macOS venv and register a LaunchAgent so awaitray starts at
# login. Mirrors install.sh (Linux) but uses a venv + LaunchAgent. Auth is
# still delegated to the `gh` CLI; no tokens are stored.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if ! command -v gh >/dev/null 2>&1; then
  echo "!! gh CLI not found. Install it first:  brew install gh" >&2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "!! git not found. Install it first:  brew install git" >&2
fi

# shellcheck source=lib/macos-python.sh
source "$HERE/lib/macos-python.sh"
echo "==> Using $PYTHON ($("$PYTHON" -V 2>&1))"

# Keep a copy of the current settings before anything changes (a no-op on a
# fresh install). `python3 -m awaitray config snapshots` lists them.
echo "==> Snapshotting settings"
( cd "$HERE" && "$PYTHON" -m awaitray config snapshot pre-update ) \
  || echo "!! Could not snapshot settings; continuing" >&2

echo "==> Creating venv and installing dependencies"
"$PYTHON" -m venv --clear "$VENV"  # --clear: never keep an old 3.9 venv around
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$HERE/requirements-macos.txt"

AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/com.awaitray.agent.plist"
mkdir -p "$AGENT_DIR"

# Pre-rename LaunchAgent (the project was called "devtray"): unload and remove
# it so the old and new names never run side by side.
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.devtray.agent.plist"
if [[ -f "$LEGACY_PLIST" ]]; then
  launchctl unload "$LEGACY_PLIST" 2>/dev/null || true
  rm -f "$LEGACY_PLIST"
  echo "==> Removed legacy $LEGACY_PLIST"
fi

# plistlib writes the XML, so a folder named with `&` or `<` still makes a
# valid agent; -I so nothing in the current folder can stand in for it.
"$VENV/bin/python" -I - "$PLIST" "$VENV/bin/python" "$HERE" <<'PY'
import plistlib
import sys

out, python, here = sys.argv[1:]
agent = {
    "Label": "com.awaitray.agent",
    "ProgramArguments": [python, "-m", "awaitray"],
    "WorkingDirectory": here,
    "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    "RunAtLoad": True,
    "StandardOutPath": f"{here}/awaitray.log",
    "StandardErrorPath": f"{here}/awaitray.log",
}
with open(out, "wb") as f:
    plistlib.dump(agent, f)
PY

echo "==> Wrote $PLIST"

# Reload the agent so it starts now and at each login.
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo
echo "Done. awaitray is running in the menu bar."
echo "Open the menu → Preferences → Hosts to log in with the gh CLI, e.g.:"
echo "    gh auth login --hostname github.com"
