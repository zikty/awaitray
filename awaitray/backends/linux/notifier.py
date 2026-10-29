"""Desktop notifications via libnotify (`notify-send`).

Falls back to a no-op if `notify-send` is not on PATH so the app keeps
working on systems without libnotify-bin installed.
"""

from __future__ import annotations

import contextlib
import html
import os
import shutil
import subprocess

_HAS_NOTIFY_SEND = shutil.which("notify-send") is not None


def notify(
    title: str, body: str = "", urgency: str = "normal", icon: str = "dialog-information"
) -> None:
    if not _HAS_NOTIFY_SEND:
        return
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        subprocess.run(command(title, body, urgency, icon), timeout=5, check=False)


def command(title: str, body: str, urgency: str, icon: str) -> list[str]:
    """notify-send's argv. Titles and bodies carry PR titles, which anyone
    who requests your review writes: `--` keeps one that looks like an
    option (`--wait`, `--icon=…`) from being read as one, and the body is
    escaped because most notification servers render it as markup (an
    `<a href>` or `<img>` there would bypass the http(s)-only link check)."""
    return ["notify-send", "-a", "awaitray", "-u", urgency, "-i", icon, "--", title, _plain(body)]


def _plain(text: str) -> str:
    return html.escape(text, quote=False)


def play_sound(sound: str) -> None:
    """Best-effort sound playback.

    An absolute path is played with `paplay`; otherwise `sound` is treated as a
    freedesktop sound-theme name played via `canberra-gtk-play -i <name>`. Both
    are fire-and-forget and degrade to a no-op if the tool isn't installed."""
    if not sound:
        return
    cmd = ["paplay", sound] if os.path.isabs(sound) else ["canberra-gtk-play", "-i", sound]
    if shutil.which(cmd[0]) is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def system_sound_names() -> list[str]:
    """Common freedesktop sound-theme names for the prefs dropdown (best-effort)."""
    return [
        "message",
        "message-new-instant",
        "bell",
        "complete",
        "dialog-information",
        "dialog-warning",
    ]
