#!/usr/bin/env bash
# Run awaitray on macOS from a local venv (created on first run).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
# (Re)create the venv when missing or built on a Python older than 3.10: an
# old venv from the system's 3.9 starts, then crashes opening Preferences.
if [[ ! -x "$VENV/bin/python" ]] \
  || ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  # shellcheck source=lib/macos-python.sh
  source "$HERE/lib/macos-python.sh"
  echo "==> Creating venv at $VENV with $PYTHON"
  "$PYTHON" -m venv --clear "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r "$HERE/requirements-macos.txt"
fi

exec "$VENV/bin/python" -m awaitray
