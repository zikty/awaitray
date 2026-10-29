#!/usr/bin/env bash
# End-to-end check of the devtray → awaitray upgrade on Linux: run the real
# update.sh against a home that holds a devtray install (config, autostart
# entry, icon cache) and check what's left. Needs a display for the app that
# update.sh starts, e.g.:
#
#   xvfb-run -a tests/e2e/linux_migration.sh
#
# systemctl, pgrep and pkill are faked so the check never touches the real
# user session (restarting or killing an awaitray you're running).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
cleanup() {
  if [[ -f "$WORK/pids" ]]; then
    while read -r pid; do kill "$pid" 2>/dev/null || true; done <"$WORK/pids"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

PYTHON="${PYTHON:-python3}"
REAL_PYTHON="$(command -v "$PYTHON")"
APP="$WORK/app" HOME_DIR="$WORK/home" BIN="$WORK/bin"
mkdir -p "$APP" "$BIN" "$HOME_DIR/.config/devtray" "$HOME_DIR/.config/autostart" \
  "$HOME_DIR/.cache/devtray/icons"

# A copy of the checkout that isn't a git repo, so update.sh skips the pull.
(cd "$ROOT" && git ls-files -z | xargs -0 tar -c) | tar -x -C "$APP"

cp "$ROOT/tests/e2e/fake-gh" "$BIN/gh"
cat >"$BIN/python3" <<EOF
#!/bin/sh
echo \$\$ >>"$WORK/pids"
exec "$REAL_PYTHON" "\$@"
EOF
printf '#!/bin/sh\nexit 1\n' >"$BIN/systemctl"  # no user systemd: .desktop path
printf '#!/bin/sh\nexit 1\n' >"$BIN/pgrep"
printf '#!/bin/sh\nexit 0\n' >"$BIN/pkill"
printf '#!/bin/sh\nexit 0\n' >"$BIN/notify-send"
chmod +x "$BIN"/*

# What a devtray install leaves behind.
echo '{"hosts": [{"hostname": "ghe.example.com"}], "intervals": {"pr_check_minutes": 7}}' \
  >"$HOME_DIR/.config/devtray/config.json"
printf '[Desktop Entry]\nExec=python3 -m devtray\n' >"$HOME_DIR/.config/autostart/devtray.desktop"
echo '<svg/>' >"$HOME_DIR/.cache/devtray/icons/devtray-idle.svg"

HOME="$HOME_DIR" PATH="$BIN:$PATH" XDG_CONFIG_HOME="" XDG_CACHE_HOME="" \
  bash "$APP/update.sh" >"$WORK/update.log" 2>&1 || {
  echo "FAIL: update.sh exited with $?"; cat "$WORK/update.log"; exit 1
}

# The app update.sh started deletes devtray's cache once it runs.
for _ in $(seq 1 20); do
  [[ -d "$HOME_DIR/.cache/devtray" ]] || break
  sleep 1
done

fail=0
check() {
  if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
cfg="$HOME_DIR/.config/awaitray/config.json"
check "settings moved to ~/.config/awaitray" "[[ -f '$cfg' && ! -e '$HOME_DIR/.config/devtray' ]]"
check "settings kept" "grep -q ghe.example.com '$cfg' && grep -q '\"pr_check_minutes\": 7' '$cfg'"
check "backed up before the update" \
  "ls '$HOME_DIR'/.config/awaitray/backups/config-*-pre-update.json >/dev/null 2>&1"
check "devtray autostart entry removed" "[[ ! -e '$HOME_DIR/.config/autostart/devtray.desktop' ]]"
check "awaitray autostart entry added" "grep -q 'Exec=python3 -m awaitray' '$HOME_DIR/.config/autostart/awaitray.desktop'"
check "devtray icon cache removed" "[[ ! -e '$HOME_DIR/.cache/devtray' ]]"

if [[ $fail -ne 0 ]]; then
  echo "--- update.log"; cat "$WORK/update.log"
fi
exit $fail
