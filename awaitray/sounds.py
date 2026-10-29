"""Sound packs: one sound per kind of notification.

A notification carries a *slot* (`Notice.sound`): a new review request, good
news, bad news, or plain information. A pack has a sound for each slot.

- Built-in packs are synthesized here (square waves, sweeps, chimes) and
  written once as WAV files into the cache: no audio files in the repo and
  no third-party licenses. Their sound is original, not copied from any game
  or film.
- Your own packs are folders in ~/.config/awaitray/sounds/<name>/ holding
  `review`, `good`, `bad` and `info` files (.wav, .ogg, .flac or .aiff; any
  missing slot is silent). A folder named like a built-in pack replaces it.

Pure logic and file I/O; the backends play the path (`UIPort.play_sound`).
"""

from __future__ import annotations

import io
import math
import os
import struct
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg_mod

REVIEW = "review"  # a review request arrived (or waits too long)
GOOD = "good"  # approved, ready to merge, checks passed
BAD = "bad"  # changes requested, checks failed, conflict, paused on errors
INFO = "info"  # new comments, your new PR, a repo behind, the daily summary
SLOTS = (REVIEW, GOOD, BAD, INFO)
SILENT = "none"  # an event's choice: no sound at all

# Your own PRs' events, each of which can have its own sound
# (`SoundsConfig.events`): one of the pack's slots, SILENT, or a file. By
# default a pack's own file for the event (`approved.wav`) or else its slot.
EVENTS: dict[str, str] = {
    "approved": "Approved",
    "ready_to_merge": "Ready to merge",
    "changes_requested": "Changes requested",
    "ci_failed": "Checks failed",
    "ci_passed": "Checks back to green",
    "conflict": "Merge conflict",
    "new_comments": "New comments",
    "checks_finished": "Watched checks finished",
}
# The slot each event falls back to (checks_finished is good or bad by result).
EVENT_SLOT: dict[str, str] = {
    "approved": GOOD,
    "ready_to_merge": GOOD,
    "ci_passed": GOOD,
    "checks_finished": GOOD,
    "changes_requested": BAD,
    "ci_failed": BAD,
    "conflict": BAD,
    "new_comments": INFO,
}

# Formats both players read: paplay (libsndfile) on Linux, afplay on macOS.
EXTENSIONS = (".wav", ".ogg", ".oga", ".flac", ".aiff", ".aif")

_RATE = 22050
_VERSION = 1  # bump when the synthesis changes, so cached files are rebuilt


@dataclass(frozen=True)
class Pack:
    id: str
    label: str
    builtin: bool


BUILTIN: dict[str, str] = {
    "arcade": "Arcade (win / game over)",
    "retro": "Retro (8-bit)",
    "scifi": "Sci-fi",
    "chimes": "Chimes",
}


# Effects you can give any of your PRs' events (`fx:<id>`), synthesized like
# the packs. Original sounds in the style of games, not copies of any.
FX: dict[str, str] = {
    "victory": "Victory fanfare (WIN)",
    "game_over": "Game over",
    "level_up": "Level up",
    "power_up": "Power-up",
    "coin": "Coin",
    "tada": "Ta-da",
    "sad_trombone": "Sad trombone",
    "buzzer": "Buzzer",
    "alarm": "Alarm",
    "laser": "Laser",
    "pop": "Pop",
    "bell": "Bell",
}
FX_PREFIX = "fx:"

# Built-in packs whose sounds are effects: per slot, and per event of your
# PRs (so "approved" can be a victory and "checks failed" a game over).
_PACK_FX: dict[str, dict[str, str]] = {
    "arcade": {
        REVIEW: "power_up",
        GOOD: "level_up",
        BAD: "game_over",
        INFO: "coin",
        "approved": "victory",
        "ready_to_merge": "tada",
        "ci_passed": "level_up",
        "checks_finished": "level_up",
        "changes_requested": "sad_trombone",
        "ci_failed": "game_over",
        "conflict": "buzzer",
        "new_comments": "coin",
    },
}


def fx_path(fx: str, cache: Path | None = None) -> str | None:
    """An effect's file (written to the cache on first use), or None."""
    if fx not in FX:
        return None
    return _cached(cache or cache_dir(), "fx", fx, lambda: synthesize("fx", fx))


def _cached(cache: Path, group: str, name: str, make: Callable[[], bytes]) -> str | None:
    target = cache / group / f"{name}-v{_VERSION}.wav"
    if not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(make())
            tmp.replace(target)
        except OSError:
            return None
    return str(target)


def user_dir() -> Path:
    return cfg_mod.CONFIG_DIR / "sounds"


def cache_dir() -> Path:
    return cfg_mod.xdg_dir("XDG_CACHE_HOME", ".cache") / "awaitray" / "sounds"


def packs(user: Path | None = None) -> list[Pack]:
    """Built-in packs, then yours (a folder with at least one slot file)."""
    user = user or user_dir()
    found = {pid: Pack(pid, label, True) for pid, label in BUILTIN.items()}
    try:
        folders = sorted(p for p in user.iterdir() if p.is_dir())
    except OSError:
        folders = []
    for folder in folders:
        if _is_pack(folder):
            label = BUILTIN.get(folder.name, folder.name)
            found[folder.name] = Pack(folder.name, f"{label} (yours)", False)
    return list(found.values())


def path_for(
    pack: str, slot: str, user: Path | None = None, cache: Path | None = None
) -> str | None:
    """The file to play for `slot` in `pack`, or None (no pack, unknown slot,
    or a slot your pack leaves silent). Built-in sounds are written on first
    use."""
    if not pack or slot not in SLOTS:
        return None
    folder = (user or user_dir()) / pack
    if folder.is_dir() and _is_pack(folder):
        found = _user_file(folder, slot)
        return str(found) if found else None
    if pack not in BUILTIN:
        return None
    if pack in _PACK_FX:
        return fx_path(_PACK_FX[pack][slot], cache)
    return _cached(cache or cache_dir(), pack, slot, lambda: synthesize(pack, slot))


def resolve(
    cfg: cfg_mod.SoundsConfig,
    slot: str,
    event: str = "",
    user: Path | None = None,
    cache: Path | None = None,
) -> str | None:
    """The file to play for a notice of `slot` (and, for your PRs, `event`):
    the event's own choice if you made one, else the pack's file for that
    event, else the pack's sound for the slot."""
    events = cfg.events if isinstance(cfg.events, dict) else {}
    choice = events.get(event, "") if event else ""
    choice = choice if isinstance(choice, str) else ""
    if choice == SILENT:
        return None
    if os.path.isabs(choice):
        if os.path.isfile(choice):
            return choice
        choice = ""  # the file is gone: back to the pack
    if choice in SLOTS:
        return path_for(cfg.pack, choice, user, cache)
    if choice.startswith(FX_PREFIX):
        return fx_path(choice[len(FX_PREFIX) :], cache)
    if event:
        folder = (user or user_dir()) / cfg.pack
        if cfg.pack and folder.is_dir() and _is_pack(folder):
            own = _user_file(folder, event)
            if own is not None:
                return str(own)
        elif event in _PACK_FX.get(cfg.pack, {}):
            return fx_path(_PACK_FX[cfg.pack][event], cache)
    return path_for(cfg.pack, slot, user, cache)


def _is_pack(folder: Path) -> bool:
    return any(_user_file(folder, name) for name in (*SLOTS, *EVENTS))


def _user_file(folder: Path, slot: str) -> Path | None:
    for ext in EXTENSIONS:
        candidate = folder / f"{slot}{ext}"
        if candidate.is_file():
            return candidate
    return None


# ----- synthesis -----

Wave = Callable[[float], float]  # phase (cycles) -> sample in [-1, 1]


def _square(phase: float) -> float:
    return 1.0 if phase % 1.0 < 0.5 else -1.0


def _pulse25(phase: float) -> float:  # the thinner NES-style pulse
    return 1.0 if phase % 1.0 < 0.25 else -1.0


def _sine(phase: float) -> float:
    return math.sin(2 * math.pi * phase)


def _note(n: str) -> float:
    """ "A4" -> 440.0 Hz; sharps as "C#5"."""
    names = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}
    semis = names[n[0]] + (1 if n[1] == "#" else 0)
    octave = int(n[2:] if n[1] == "#" else n[1:])
    return 440.0 * 2 ** ((semis + 12 * (octave - 4)) / 12)


def _tone(
    freq: float | Callable[[float], float],
    seconds: float,
    wave_fn: Wave = _square,
    decay: float = 0.0,
    volume: float = 0.35,
) -> list[float]:
    """One tone: a fixed or swept frequency (a function of 0..1 progress),
    with a short attack/release so it never clicks, and optional decay."""
    count = int(_RATE * seconds)
    out: list[float] = []
    phase = 0.0
    for i in range(count):
        t = i / count
        f = freq(t) if callable(freq) else freq
        phase += f / _RATE
        env = min(1.0, i / (_RATE * 0.004), (count - i) / (_RATE * 0.012))
        if decay:
            env *= math.exp(-decay * t)
        out.append(volume * env * wave_fn(phase))
    return out


def _mix(*layers: list[float]) -> list[float]:
    size = max(len(layer) for layer in layers)
    return [sum(layer[i] for layer in layers if i < len(layer)) for i in range(size)]


def _seq(*parts: list[float]) -> list[float]:
    return [s for part in parts for s in part]


def _rest(seconds: float) -> list[float]:
    return [0.0] * int(_RATE * seconds)


def _sweep(start: float, end: float) -> Callable[[float], float]:
    return lambda t: start * (end / start) ** t  # exponential: sounds even


def _chime(note: str, seconds: float, volume: float = 0.3) -> list[float]:
    f = _note(note)
    return _mix(
        _tone(f, seconds, _sine, decay=5, volume=volume),
        _tone(f * 2, seconds, _sine, decay=8, volume=volume * 0.35),
        _tone(f * 3.01, seconds, _sine, decay=11, volume=volume * 0.15),
    )


def _triangle(phase: float) -> float:
    p = phase % 1.0
    return 4 * p - 1 if p < 0.5 else 3 - 4 * p


def _vibrato(note: str, depth: float = 0.012, rate: float = 6.0) -> Callable[[float], float]:
    """A held note that wobbles (a sad trombone's "waah", a fanfare's last note)."""
    f = _note(note)
    return lambda t: f * (1 + depth * math.sin(2 * math.pi * rate * t))


def _steps(start: float, end: float, steps: int) -> Callable[[float], float]:
    """A pitch that climbs in steps (the 8-bit power-up)."""
    return lambda t: start * (end / start) ** (min(steps - 1, int(t * steps)) / (steps - 1))


def _fx_designs() -> dict[str, Callable[[], list[float]]]:
    n = _note
    return {
        # Short notes up, then a held one: "ta-ta-ta-taaa".
        "victory": lambda: _seq(
            *(_tone(n(x), 0.07, _square, volume=0.28) for x in ("G4", "C5", "E5", "G5")),
            _rest(0.05),
            _tone(n("E5"), 0.09, _square, volume=0.28),
            _mix(
                _tone(_vibrato("G5", 0.006), 0.45, _square, decay=1.5, volume=0.22),
                _tone(n("C5"), 0.45, _triangle, decay=1.5, volume=0.25),
            ),
        ),
        # Slowing down on the way down, ending low.
        "game_over": lambda: _seq(
            _tone(n("G4"), 0.16, _square, volume=0.26),
            _tone(n("F#4"), 0.2, _square, volume=0.26),
            _tone(n("F4"), 0.26, _square, volume=0.26),
            _tone(_vibrato("E4", 0.01, 5), 0.6, _triangle, decay=2.5, volume=0.4),
        ),
        "level_up": lambda: _seq(
            *(
                _tone(n(x), 0.045, _pulse25, volume=0.26)
                for x in ("C5", "E5", "G5", "C6", "E6", "G6")
            ),
            _tone(n("C7"), 0.16, _pulse25, decay=3, volume=0.26),
        ),
        "power_up": lambda: _tone(_steps(220, 1320, 10), 0.5, _square, decay=0.8, volume=0.22),
        "coin": lambda: _seq(
            _tone(n("G6"), 0.05, _pulse25, volume=0.25),
            _tone(n("B6"), 0.3, _pulse25, decay=4, volume=0.25),
        ),
        "tada": lambda: _seq(
            _mix(*(_tone(n(x), 0.1, _triangle, volume=0.22) for x in ("C5", "E5", "G5"))),
            _rest(0.04),
            _mix(
                *(_tone(n(x), 0.55, _triangle, decay=2, volume=0.2) for x in ("C5", "E5", "G5")),
                _tone(n("C6"), 0.55, _sine, decay=2, volume=0.2),
            ),
        ),
        # Four steps down, the last one long and wobbling.
        "sad_trombone": lambda: _seq(
            _tone(n("A#3"), 0.3, _triangle, decay=0.6, volume=0.45),
            _tone(n("A3"), 0.3, _triangle, decay=0.6, volume=0.45),
            _tone(n("G#3"), 0.3, _triangle, decay=0.6, volume=0.45),
            _tone(_vibrato("G3", 0.02, 5), 0.8, _triangle, decay=1.5, volume=0.45),
        ),
        "buzzer": lambda: _mix(
            _tone(110, 0.45, _square, volume=0.18),
            _tone(117, 0.45, _square, volume=0.18),
        ),
        "alarm": lambda: _seq(
            *(
                _tone(f, 0.11, _square, volume=0.2)
                for f in (n("A5"), n("E5"), n("A5"), n("E5"), n("A5"), n("E5"))
            )
        ),
        "laser": lambda: _tone(_sweep(2200, 180), 0.16, _square, volume=0.2),
        "pop": lambda: _tone(_sweep(900, 260), 0.06, _sine, volume=0.4),
        "bell": lambda: _chime("E6", 0.9),
    }


def _designs() -> dict[tuple[str, str], Callable[[], list[float]]]:
    n = _note
    return {
        **{("fx", name): make for name, make in _fx_designs().items()},
        # Retro: square/pulse waves, short notes, a chiptune arpeggio feel.
        ("retro", REVIEW): lambda: _seq(
            _tone(n("C6"), 0.06, _pulse25),
            _tone(n("E6"), 0.06, _pulse25),
            _tone(n("G6"), 0.14, _pulse25, decay=3),
        ),
        ("retro", GOOD): lambda: _seq(
            _tone(n("C5"), 0.07),
            _tone(n("E5"), 0.07),
            _tone(n("G5"), 0.07),
            _tone(n("C6"), 0.22, decay=2.5),
        ),
        ("retro", BAD): lambda: _seq(
            _tone(n("G4"), 0.1),
            _tone(n("D#4"), 0.1),
            _tone(n("A#3"), 0.28, decay=2),
        ),
        ("retro", INFO): lambda: _tone(n("A5"), 0.07, _pulse25, decay=4),
        # Sci-fi: sweeps and bleeps.
        ("scifi", REVIEW): lambda: _tone(_sweep(500, 1900), 0.2, _sine, volume=0.3),
        ("scifi", GOOD): lambda: _seq(
            _tone(_sweep(700, 1400), 0.12, _sine, volume=0.3),
            _rest(0.03),
            _tone(_sweep(1000, 2200), 0.2, _sine, decay=3, volume=0.3),
        ),
        ("scifi", BAD): lambda: _mix(
            _tone(_sweep(900, 120), 0.45, _square, decay=2, volume=0.2),
            _tone(_sweep(905, 121), 0.45, _sine, decay=2, volume=0.2),
        ),
        ("scifi", INFO): lambda: _seq(
            _tone(1320, 0.04, _sine, volume=0.25),
            _rest(0.04),
            _tone(1320, 0.04, _sine, volume=0.25),
        ),
        # Chimes: soft bells.
        ("chimes", REVIEW): lambda: _chime("E6", 0.6),
        ("chimes", GOOD): lambda: _seq(_chime("C6", 0.18), _chime("G6", 0.6)),
        ("chimes", BAD): lambda: _seq(_chime("C5", 0.2), _chime("A4", 0.7)),
        ("chimes", INFO): lambda: _chime("G5", 0.45, volume=0.22),
    }


def synthesize(pack: str, slot: str) -> bytes:
    """A built-in sound as a 16-bit mono WAV file: a pack's slot, or
    ("fx", id) for an effect."""
    if pack in _PACK_FX:
        pack, slot = "fx", _PACK_FX[pack][slot]
    samples = _designs()[(pack, slot)]()
    frames = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(frames)
    return buf.getvalue()
