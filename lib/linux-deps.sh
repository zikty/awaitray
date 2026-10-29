# shellcheck shell=bash
# System dependencies for the Linux backend, shared by install.sh and
# update.sh. Sourced, not executed.
#
# We check what awaitray actually needs at runtime (Python can load GTK 3 and
# AyatanaAppIndicator3, plus git/gh/notify-send on PATH) instead of package
# names, so the check works on any distro. Packages are only installed when
# something is missing, with the distro's package manager.

# Python snippet that fails unless the Linux backend's imports work.
AWAITRAY_PY_CHECK='
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, AyatanaAppIndicator3  # noqa: F401
'

detect_package_manager() {
  if command -v apt-get >/dev/null 2>&1; then echo apt
  elif command -v dnf >/dev/null 2>&1; then echo dnf
  elif command -v pacman >/dev/null 2>&1; then echo pacman
  else echo unknown
  fi
}

# Package names per package manager (Debian/Ubuntu, Fedora, Arch).
packages_for() {
  case "$1" in
    apt)    echo "python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin git gh" ;;
    dnf)    echo "python3-gobject gtk3 libayatana-appindicator-gtk3 libnotify git gh" ;;
    pacman) echo "python-gobject gtk3 libayatana-appindicator libnotify git github-cli" ;;
    *)      echo "" ;;
  esac
}

# Prints what's missing, one item per line; prints nothing when all is fine.
missing_runtime_deps() {
  if ! python3 -c "$AWAITRAY_PY_CHECK" >/dev/null 2>&1; then
    echo "python3 GTK 3 + AyatanaAppIndicator3 bindings"
  fi
  local cmd
  for cmd in git gh notify-send; do
    command -v "$cmd" >/dev/null 2>&1 || echo "$cmd"
  done
}

install_packages() {
  local pm="$1"; shift
  case "$pm" in
    apt)
      sudo apt-get update
      sudo apt-get install -y --no-install-recommends "$@" ;;
    dnf)
      sudo dnf install -y "$@" ;;
    pacman)
      sudo pacman -S --needed --noconfirm "$@" ;;
  esac
}

ensure_linux_deps() {
  local missing pm pkgs line
  missing="$(missing_runtime_deps)"
  if [[ -z "$missing" ]]; then
    echo "==> All runtime dependencies present, nothing to install."
    return 0
  fi
  echo "==> Missing:"
  while IFS= read -r line; do echo "    $line"; done <<<"$missing"

  pm="$(detect_package_manager)"
  pkgs="$(packages_for "$pm")"
  if [[ -z "$pkgs" ]]; then
    echo "!! Unknown package manager. Install the equivalent of these, then re-run:" >&2
    echo "   PyGObject (python3), GTK 3, libayatana-appindicator (GTK 3 + GObject" >&2
    echo "   introspection typelib), libnotify (notify-send), git, gh (GitHub CLI)." >&2
    return 1
  fi
  echo "==> Installing with $pm (sudo required): $pkgs"
  # shellcheck disable=SC2086  # word splitting of the package list is intended
  install_packages "$pm" $pkgs

  missing="$(missing_runtime_deps)"
  if [[ -n "$missing" ]]; then
    echo "!! Still missing after install:" >&2
    while IFS= read -r line; do echo "   $line" >&2; done <<<"$missing"
    echo "   (gh may need its own repository on older distros: https://cli.github.com)" >&2
    return 1
  fi
}
