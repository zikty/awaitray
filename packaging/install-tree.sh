#!/usr/bin/env bash
# Install awaitray into a staging root, the same way for every Linux package
# (.deb, AUR): the package under /usr/lib/awaitray, a launcher in /usr/bin,
# a desktop entry, the icon, a systemd user unit and the license.
#
#   packaging/install-tree.sh <destdir>
set -euo pipefail

DEST="${1:?usage: install-tree.sh <destdir>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -d "$DEST/usr/lib/awaitray"
(cd "$ROOT" && find awaitray -name '*.py' -not -path '*/__pycache__/*' -print0) |
  while IFS= read -r -d '' f; do
    install -Dm644 "$ROOT/$f" "$DEST/usr/lib/awaitray/$f"
  done

install -Dm755 /dev/stdin "$DEST/usr/bin/awaitray" <<'LAUNCHER'
#!/bin/sh
# awaitray launcher: no arguments starts the tray app, anything else is the CLI.
# Isolated (-I): `python3 -m` would put the current directory first on the
# path, so a folder holding an `awaitray` or `gi` package could run its code.
exec /usr/bin/python3 -I -c '
import runpy, sys
sys.path.insert(0, "/usr/lib/awaitray")
runpy.run_module("awaitray", run_name="__main__", alter_sys=True)
' "$@"
LAUNCHER

install -Dm644 /dev/stdin "$DEST/usr/share/applications/awaitray.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=awaitray
Comment=PR review notifications and periodic git fetch
Exec=awaitray
Icon=awaitray
Terminal=false
StartupNotify=false
Categories=Development;Utility;
DESKTOP

install -Dm644 "$ROOT/icons/awaitray.svg" \
  "$DEST/usr/share/icons/hicolor/scalable/apps/awaitray.svg"

install -Dm644 /dev/stdin "$DEST/usr/lib/systemd/user/awaitray.service" <<'UNIT'
[Unit]
Description=awaitray — PR review tray + git fetch
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/awaitray
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
UNIT

install -Dm644 "$ROOT/LICENSE" "$DEST/usr/share/licenses/awaitray/LICENSE"
