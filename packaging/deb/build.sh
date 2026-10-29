#!/usr/bin/env bash
# Build dist/awaitray_<version>_all.deb from this checkout. No network: the
# package is pure Python and its dependencies are Debian/Ubuntu packages.
#
#   packaging/deb/build.sh            # version from pyproject.toml
#   VERSION=1.2.3 packaging/deb/build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)}"
OUT="${OUT:-$ROOT/dist}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

"$ROOT/packaging/install-tree.sh" "$STAGE"

# Debian keeps the license under /usr/share/doc/<pkg>/copyright.
install -Dm644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/awaitray/copyright"
rm -rf "$STAGE/usr/share/licenses"

install -d "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: awaitray
Version: $VERSION
Architecture: all
Maintainer: zikty <zikty@users.noreply.github.com>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, libnotify-bin, git
Recommends: gh
Section: devel
Priority: optional
Homepage: https://github.com/zikty/awaitray
Description: tray icon for pull requests awaiting your review
 Shows GitHub pull requests waiting for your review (github.com and GitHub
 Enterprise, several accounts) in the system tray, notifies when they
 arrive or change, and runs git fetch on your local clones. Auth is
 delegated to the GitHub CLI (gh).
 .
 Start it with "awaitray", or at login with:
 systemctl --user enable --now awaitray.service
CONTROL

mkdir -p "$OUT"
DEB="$OUT/awaitray_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
