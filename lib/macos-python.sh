# shellcheck shell=bash
# Sourced by the macOS scripts: sets PYTHON to an interpreter new enough for
# awaitray (pyproject: >= 3.10). macOS's own `python3` is 3.9, and a venv built
# on it starts fine and then crashes (e.g. opening Preferences), so it is only
# the last resort. AWAITRAY_PYTHON overrides the search.

PYTHON=""
for candidate in ${AWAITRAY_PYTHON:-} python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "!! awaitray needs Python >= 3.10 (found: $(python3 -V 2>&1 || echo none))." >&2
  echo "   Install one (brew install python@3.13) or set AWAITRAY_PYTHON." >&2
  exit 1
fi
