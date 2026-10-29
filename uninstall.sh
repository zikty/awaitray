#!/usr/bin/env bash
# Uninstall awaitray. Stops the process, removes the autostart entry, and
# optionally deletes the user config. System packages (gh, git, libnotify-bin,
# etc.) are left alone since they're shared with other tools.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/xdg.sh
source "$HERE/lib/xdg.sh"
# shellcheck source=lib/linux-autostart.sh
source "$HERE/lib/linux-autostart.sh"

AUTOSTART_DIR="$(config_home)/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/awaitray.desktop"
UNIT_DIR="$(config_home)/systemd/user"
UNIT_FILE="$UNIT_DIR/awaitray.service"
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
  read -rp "This will remove the autostart entry and stop awaitray. Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "aborted."; exit 1; }
fi

# Pre-rename leftovers (the project was called "devtray"): stop and remove
# them so the old and new names never run side by side.
LEGACY_UNIT_FILE="$UNIT_DIR/devtray.service"
LEGACY_DESKTOP_FILE="$AUTOSTART_DIR/devtray.desktop"
if [[ -f "$LEGACY_UNIT_FILE" ]]; then
  systemctl --user disable --now devtray.service >/dev/null 2>&1 || true
  rm -f "$LEGACY_UNIT_FILE"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  echo "==> Removed legacy $LEGACY_UNIT_FILE"
fi
if [[ -f "$LEGACY_DESKTOP_FILE" ]]; then
  rm -f "$LEGACY_DESKTOP_FILE"
  echo "==> Removed legacy $LEGACY_DESKTOP_FILE"
fi
stop_app devtray

echo "==> Stopping running awaitray (if any)"
if [[ -f "$UNIT_FILE" ]] && command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now awaitray.service >/dev/null 2>&1 || true
  rm -f "$UNIT_FILE"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  echo "==> Removed $UNIT_FILE"
fi
stop_app awaitray

if [[ -f "$DESKTOP_FILE" ]]; then
  rm -f "$DESKTOP_FILE"
  echo "==> Removed $DESKTOP_FILE"
fi

if [[ "$PURGE_CONFIG" -eq 1 ]]; then
  if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    echo "==> Removed $CONFIG_DIR"
  fi
else
  if [[ -d "$CONFIG_DIR" ]]; then
    echo "==> Keeping $CONFIG_DIR (re-run with --purge to delete)"
  fi
fi

echo "==> Done. The project folder itself was not touched; delete it manually if you want."
