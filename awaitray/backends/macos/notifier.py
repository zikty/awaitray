"""Desktop notifications on macOS via `osascript`.

We shell out to `osascript -e 'display notification …'` so it works when
running as `python -m awaitray` (no signed .app bundle needed). `urgency`
and `icon` are accepted for API parity with the Linux notifier but
ignored — the `display notification` interface exposes neither.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

_SYSTEM_SOUNDS_DIR = "/System/Library/Sounds"


def play_sound(sound: str) -> None:
    """Play a notification sound via `afplay`.

    `sound` is either an absolute path to an audio file or a macOS system sound
    name (e.g. "Glass" → /System/Library/Sounds/Glass.aiff). Best-effort and
    fire-and-forget; failures are swallowed."""
    if not sound:
        return
    path = sound
    if not os.path.isabs(sound):
        path = os.path.join(_SYSTEM_SOUNDS_DIR, f"{sound}.aiff")
    if not os.path.exists(path):
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def system_sound_names() -> list[str]:
    """Names of the built-in macOS system sounds (for the prefs dropdown)."""
    try:
        names = [
            os.path.splitext(f)[0] for f in os.listdir(_SYSTEM_SOUNDS_DIR) if f.endswith(".aiff")
        ]
    except OSError:
        return []
    return sorted(names)


def _applescript_str(value: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify(
    title: str, body: str = "", urgency: str = "normal", icon: str = "dialog-information"
) -> None:
    del urgency, icon  # not supported by `display notification`
    script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        subprocess.run(["osascript", "-e", script], timeout=5, check=False)
