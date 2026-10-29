#!/usr/bin/env bash
# Uninstall awaitray on macOS: stop the LaunchAgent, remove the plist, and
# optionally delete the user config. The venv and project folder are left in
# place (delete the folder manually if you want).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/xdg.sh
source "$HERE/lib/xdg.sh"

PLIST="$HOME/Library/LaunchAgents/com.awaitray.agent.plist"
CONFIG_DIR="$(config_home)/awaitray"

PURGE_CONFIG=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE_CONFIG=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--purge] [-y|--yes]

  --purge      also delete $CONFIG_DIR
  -y, --yes    don't prompt, assume yes
USAGE
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -rp "This will stop awaitray and remove its LaunchAgent. Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted."; exit 1; }
fi

# Pre-rename LaunchAgent (the project was called "devtray"): unload and remove
# it so the old and new names never run side by side.
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.devtray.agent.plist"
if [[ -f "$LEGACY_PLIST" ]]; then
  launchctl unload "$LEGACY_PLIST" 2>/dev/null || true
  rm -f "$LEGACY_PLIST"
  echo "==> Removed legacy $LEGACY_PLIST"
fi

if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "==> Removed $PLIST"
fi

if [[ "$PURGE_CONFIG" -eq 1 ]]; then
  [[ -d "$CONFIG_DIR" ]] && rm -rf "$CONFIG_DIR" && echo "==> Removed $CONFIG_DIR"
else
  [[ -d "$CONFIG_DIR" ]] && echo "==> Keeping $CONFIG_DIR (re-run with --purge to delete)"
fi

echo "==> Done."
