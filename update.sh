#!/usr/bin/env bash
# Refresh awaitray: pull latest commits, refresh autostart/unit (in case the
# project moved), and restart the running process. Only invokes sudo if a
# system dependency is actually missing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/xdg.sh
source "$HERE/lib/xdg.sh"
# shellcheck source=lib/linux-autostart.sh
source "$HERE/lib/linux-autostart.sh"
check_install_dir "$HERE"

# shellcheck source=lib/linux-deps.sh
source "$HERE/lib/linux-deps.sh"
ensure_linux_deps

# Keep a copy of the current settings before anything changes (a no-op on a
# fresh install). `python3 -m awaitray config snapshots` lists them.
echo "==> Snapshotting settings"
( cd "$HERE" && python3 -m awaitray config snapshot pre-update ) \
  || echo "!! Could not snapshot settings; continuing" >&2

if [[ -d "$HERE/.git" ]]; then
  echo "==> Pulling latest commits"
  git -C "$HERE" pull --ff-only
else
  echo "==> Skipping git pull ($HERE is not a git repository)"
fi

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 \
    && systemctl --user show-environment >/dev/null 2>&1
}

AUTOSTART_DIR="$(config_home)/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/awaitray.desktop"
UNIT_DIR="$(config_home)/systemd/user"
UNIT_FILE="$UNIT_DIR/awaitray.service"

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

if systemd_user_available; then
  mkdir -p "$UNIT_DIR"
  write_unit "$HERE" "$UNIT_FILE"

  if [[ -f "$DESKTOP_FILE" ]]; then
    rm -f "$DESKTOP_FILE"
    echo "==> Removed legacy autostart entry $DESKTOP_FILE"
  fi

  systemctl --user daemon-reload
  systemctl --user enable awaitray.service >/dev/null 2>&1 || true

  # Stop any non-systemd instance to avoid duplicates.
  if app_running awaitray; then
    stop_app awaitray
    sleep 1
  fi

  echo "==> Restarting awaitray via systemd"
  systemctl --user restart awaitray.service
else
  mkdir -p "$AUTOSTART_DIR"
  write_desktop "$HERE" "$DESKTOP_FILE"
  echo "==> Refreshed $DESKTOP_FILE"

  echo "==> Restarting awaitray (if running)"
  if app_running awaitray; then
    stop_app awaitray
    sleep 1
  fi
  ( cd "$HERE" && nohup python3 -m awaitray >/dev/null 2>&1 & )
fi
echo "==> Done."
