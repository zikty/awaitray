#!/usr/bin/env bash
# End-to-end check of the Linux backend as the desktop sees it: start the
# real app with a fake `gh`, read the menu it exports over DBusMenu, and
# check its structure. Needs xvfb-run, dbus-run-session and gdbus, e.g.:
#
#   xvfb-run -a dbus-run-session -- tests/e2e/linux_dbusmenu.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'kill "${APP:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/home"
cp "$ROOT/tests/e2e/fake-gh" "$WORK/bin/gh"
chmod +x "$WORK/bin/gh"

PYTHON="${PYTHON:-python3}"
(cd "$ROOT" && HOME="$WORK/home" PATH="$WORK/bin:$PATH" "$PYTHON" -m awaitray >"$WORK/app.log" 2>&1) &
APP=$!

# Wait for the menu to be exported and populated by the first poll.
layout=""
for _ in $(seq 1 30); do
  sleep 1
  for name in $(gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
                  --method org.freedesktop.DBus.ListNames | grep -o "':[0-9.]*'" | tr -d "'"); do
    out="$(gdbus call --session --dest "$name" --object-path /org/ayatana/NotificationItem/awaitray/Menu \
             --method com.canonical.dbusmenu.GetLayout 0 -- -1 "@as []" 2>/dev/null || true)"
    if [[ "$out" == *"Refresh now"* && "$out" == *"#412"* ]]; then layout="$out"; break 2; fi
  done
done

if [[ -z "$layout" ]]; then
  echo "FAIL: no populated DBusMenu found"; cat "$WORK/app.log"; exit 1
fi

fail=0
expect() {
  if [[ "$layout" == *"$1"* ]]; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
expect "NEEDS YOUR REVIEW"
expect "#412"
expect "Fix login"
expect "PR actions"
expect "Snooze: Until new activity"
expect "Refresh now"
expect "Quit"
# libdbusmenu-gtk escapes "_" itself (web_app → web__app); we must not add more.
expect "web__app#412"
if [[ "$layout" == *"web____app"* ]]; then echo "FAIL underscores escaped twice"; fail=1; fi

# Open the menu the way a desktop shell does (the app clears its unseen dot
# on this) and make sure it survives and keeps answering.
MENU=/org/ayatana/NotificationItem/awaitray/Menu
gdbus call --session --dest "$name" --object-path "$MENU" \
  --method com.canonical.dbusmenu.AboutToShow 0 >/dev/null
gdbus call --session --dest "$name" --object-path "$MENU" \
  --method com.canonical.dbusmenu.Event 0 opened "<int32 0>" 0 >/dev/null
sleep 1
if gdbus call --session --dest "$name" --object-path "$MENU" \
     --method com.canonical.dbusmenu.GetLayout 0 -- 0 "@as []" >/dev/null 2>&1 \
   && ! grep -q Traceback "$WORK/app.log"; then
  echo "ok   menu opened over DBusMenu"
else
  echo "FAIL menu opened over DBusMenu"; cat "$WORK/app.log"; fail=1
fi
exit "$fail"
