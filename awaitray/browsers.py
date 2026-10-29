"""The browsers (and their profiles) installed here, to pick from.

Preferences offer these instead of asking for a command: each `Choice` is a
label for the list ("Google Chrome — Work") and the command browser.py runs
(`Google Chrome --profile-directory="Profile 1"`). Pure file reads, no
toolkit: plists with plistlib on macOS, .desktop files on Linux, and each
browser's own profile list (Chromium's "Local State", Firefox's
profiles.ini). Anything unreadable is skipped: this only makes suggestions.
"""

from __future__ import annotations

import configparser
import json
import os
import plistlib
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Choice:
    label: str
    command: str  # "" = the system browser


SYSTEM = Choice("System default", "")

# User-data folders of Chromium-based browsers, relative to the home folder.
_CHROMIUM_MAC = {
    "Google Chrome": "Library/Application Support/Google/Chrome",
    "Google Chrome Beta": "Library/Application Support/Google/Chrome Beta",
    "Google Chrome Canary": "Library/Application Support/Google/Chrome Canary",
    "Chromium": "Library/Application Support/Chromium",
    "Brave Browser": "Library/Application Support/BraveSoftware/Brave-Browser",
    "Microsoft Edge": "Library/Application Support/Microsoft Edge",
    "Vivaldi": "Library/Application Support/Vivaldi",
}
_CHROMIUM_LINUX = {
    "google-chrome": ".config/google-chrome",
    "google-chrome-stable": ".config/google-chrome",
    "google-chrome-beta": ".config/google-chrome-beta",
    "chromium": ".config/chromium",
    "chromium-browser": ".config/chromium",
    "brave-browser": ".config/BraveSoftware/Brave-Browser",
    "brave-browser-stable": ".config/BraveSoftware/Brave-Browser",
    "microsoft-edge": ".config/microsoft-edge",
    "microsoft-edge-stable": ".config/microsoft-edge",
    "vivaldi": ".config/vivaldi",
    "vivaldi-stable": ".config/vivaldi",
}
_FIREFOX_MAC = {"Firefox": "Library/Application Support/Firefox"}
_FIREFOX_LINUX = {
    "firefox": ".mozilla/firefox",
    "firefox-esr": ".mozilla/firefox",
}

_MAC_APP_DIRS = (
    "/Applications",
    "/System/Applications",
    "/System/Cryptexes/App/System/Applications",  # Safari on macOS 13+
    "~/Applications",
)
_LINUX_DESKTOP_DIRS = (  # later entries override earlier ones (XDG order, reversed)
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/snapd/desktop/applications",
    "/var/lib/flatpak/exports/share/applications",
    "~/.local/share/flatpak/exports/share/applications",
    "~/.local/share/applications",
)


def installed(platform: str | None = None, home: Path | None = None) -> list[Choice]:
    """System default first, then each browser and, when it has more than
    one, each of its profiles."""
    home = home or Path.home()
    if (platform or sys.platform) == "darwin":
        apps = _mac_browsers(home)
        chromium, firefox = _CHROMIUM_MAC, _FIREFOX_MAC
    else:
        apps = _linux_browsers(home)
        chromium, firefox = _CHROMIUM_LINUX, _FIREFOX_LINUX
    out = [SYSTEM]
    for label, command, key in apps:
        out.append(Choice(label, command))
        if key in chromium:
            for name, folder in _chromium_profiles(home / chromium[key]):
                # Quote the value only: browser.argv splits a macOS app from
                # its arguments at the first " -".
                arg = f"--profile-directory={shlex.quote(folder)}"
                out.append(Choice(f"{label} — {name}", f"{command} {arg}"))
        elif key in firefox:
            for name in _firefox_profiles(home / firefox[key]):
                out.append(Choice(f"{label} — {name}", f"{command} -P {shlex.quote(name)}"))
    return out


def label_for(command: str, choices: Sequence[Choice]) -> str:
    """What the list shows for a stored command: its label, or the command."""
    return next((c.label for c in choices if c.command == command), command)


def command_for(text: str, choices: Sequence[Choice]) -> str:
    """What to store for text from the list or typed by hand."""
    return next((c.command for c in choices if c.label == text), text.strip())


# ----- macOS -----


def _mac_browsers(home: Path) -> list[tuple[str, str, str]]:
    """(label, command, key) for every app that opens https links."""
    found: dict[str, tuple[str, str, str]] = {}
    for base in _MAC_APP_DIRS:
        root = Path(base.replace("~", str(home), 1))
        try:
            apps = sorted(root.glob("*.app"))
        except OSError:
            continue
        for app in apps:
            info = _plist(app / "Contents" / "Info.plist")
            if info is None or not _opens_https(info):
                continue
            name = app.stem
            found.setdefault(name, (str(info.get("CFBundleDisplayName") or name), name, name))
    return sorted(found.values(), key=lambda t: t[0].lower())


def _plist(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _opens_https(info: dict[str, object]) -> bool:
    types = info.get("CFBundleURLTypes")
    if not isinstance(types, list):
        return False
    for t in types:
        schemes = t.get("CFBundleURLSchemes") if isinstance(t, dict) else None
        if isinstance(schemes, list) and "https" in (str(s).lower() for s in schemes):
            return True
    return False


# ----- Linux -----


def _linux_browsers(home: Path) -> list[tuple[str, str, str]]:
    entries: dict[str, Path] = {}  # desktop file id -> path; user dirs win
    for base in _LINUX_DESKTOP_DIRS:
        root = Path(base.replace("~", str(home), 1))
        try:
            files = sorted(root.glob("*.desktop"))
        except OSError:
            continue
        for path in files:
            entries[path.name] = path
    found: dict[str, tuple[str, str, str]] = {}
    for path in entries.values():
        entry = _desktop_entry(path)
        if entry is None:
            continue
        name, command = entry
        key = os.path.basename(shlex.split(command)[0])
        found.setdefault(command, (name, command, key))
    return sorted(found.values(), key=lambda t: t[0].lower())


def _desktop_entry(path: Path) -> tuple[str, str] | None:
    """(Name, command without field codes) of a visible https handler."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except (configparser.Error, OSError, UnicodeDecodeError, KeyError):
        return None
    mime = entry.get("MimeType", "")
    if "x-scheme-handler/https" not in mime.split(";"):
        return None
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return None
    try:
        words = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return None
    # Drop the URL placeholders (%u, %U…) and Flatpak's file forwarding.
    words = [w for w in words if not w.startswith(("%", "@@")) and w != "--file-forwarding"]
    name = entry.get("Name", "").strip()
    if not words or not name:
        return None
    return name, shlex.join(words)


# ----- profiles -----


def _chromium_profiles(user_data: Path) -> list[tuple[str, str]]:
    """(name, folder) of each profile, only when there are several."""
    try:
        state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
        cache = state["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(cache, dict) or len(cache) < 2:
        return []
    profiles = [
        (str(info.get("name") or folder), str(folder))
        for folder, info in cache.items()
        if isinstance(info, dict)
    ]
    return sorted(profiles, key=lambda p: p[0].lower())


def _firefox_profiles(root: Path) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(root / "profiles.ini", encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return []
    sections = [parser[s] for s in parser.sections() if s.startswith("Profile")]
    names = [s["Name"] for s in sections if "Name" in s]
    return sorted(names, key=str.lower) if len(names) > 1 else []
