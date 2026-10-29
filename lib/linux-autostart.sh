# shellcheck shell=bash
# Autostart entries and the running process, shared by install.sh, update.sh
# and uninstall.sh. Sourced, not executed.

# This user's `python3 -m <name>` processes, and nothing else: an unanchored
# pattern would also match an editor or a shell that has the words in its
# command line.
_app_pattern() { printf '^([^ ]*/)?python3? -m %s$' "$1"; }
app_running() { pgrep -u "$(id -u)" -f "$(_app_pattern "$1")" >/dev/null; }
stop_app() { pkill -u "$(id -u)" -f "$(_app_pattern "$1")" >/dev/null 2>&1 || true; }

# The folder ends up inside unit and desktop files: a newline can't be
# written there at all.
check_install_dir() {
  if [[ "$1" == *$'\n'* ]]; then
    echo "!! The project folder's path has a line break; move it and re-run." >&2
    exit 1
  fi
}

# awaitray.service.in with the project folder filled in. `%` starts a
# systemd specifier, so it's doubled; the replacement is quoted so a `&` in
# the path stays a `&` (bash 5.2 patsub_replacement).
write_unit() {
  local here="$1" out="$2" unit dir
  unit="$(<"$here/awaitray.service.in")"
  dir="${here//%/%%}"
  printf '%s\n' "${unit//@HERE@/"$dir"}" >"$out"
}

# The .desktop autostart entry. Backslashes are escapes in desktop files.
write_desktop() {
  local here="$1" out="$2" dir
  dir="${here//\\/\\\\}"
  cat >"$out" <<DESKTOP
[Desktop Entry]
Type=Application
Name=awaitray
Comment=PR review notifications and periodic git fetch
Exec=python3 -m awaitray
Path=$dir
Icon=$dir/icons/awaitray.svg
Terminal=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
Categories=Utility;
DESKTOP
}
