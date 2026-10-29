"""Pure helpers for the count-based tray glyph / sound.

Buckets are `Bucket(up_to, emoji, sound)` objects (see `config.Bucket`). The
first bucket whose `up_to` the count fits in wins; the catch-all (`up_to` None)
has no upper bound and is tried last. Kept toolkit-agnostic so any backend can
reuse it. Config and controller types are only imported for type checking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import Bucket, TrayIcon
    from .controller import TrayState

from dataclasses import dataclass


def _ordered(buckets: list[Bucket]) -> tuple[list[Bucket], Bucket | None]:
    """Return (finite buckets sorted ascending by up_to, catch-all or None)."""
    finite = sorted((b for b in buckets if b.up_to is not None), key=lambda b: b.up_to or 0)
    catch_all = next((b for b in buckets if b.up_to is None), None)
    return finite, catch_all


def pick_bucket(count: int, buckets: list[Bucket]) -> Bucket | None:
    """Return the first bucket the count fits in (finite ascending, then catch-all)."""
    finite, catch_all = _ordered(buckets)
    for b in finite:
        if b.up_to is not None and count <= b.up_to:
            return b
    return catch_all


def pick_glyph(count: int, buckets: list[Bucket]) -> str | None:
    """Return the glyph for `count`, or None if the matching bucket has no emoji."""
    b = pick_bucket(count, buckets)
    return (b.emoji if b is not None else "") or None


def pick_sound(count: int, buckets: list[Bucket]) -> str | None:
    """Return the sound for `count`'s bucket, or None if silent / no match."""
    b = pick_bucket(count, buckets)
    return (b.sound if b is not None else "") or None


# ----- macOS menu-bar presentation (pure, so it's testable anywhere) -----

# SF Symbols per tray state (template images: the system tints them for
# light/dark menu bars and the highlighted state).
MENUBAR_SYMBOLS = {
    "idle": "arrow.triangle.pull",
    "count": "arrow.triangle.pull",
    "paused": "pause.circle",
    "error": "exclamationmark.triangle",
    "offline": "wifi.slash",
}


@dataclass(frozen=True)
class MenubarPresentation:
    symbol: str  # SF Symbol name
    title: str  # text next to the symbol ("" for none)
    red: bool = False  # tint symbol + title red (priority PR or error)


def _number(count: int) -> str:
    return "99+" if count > 99 else str(count)


def menubar_presentation(state: TrayState, icon_cfg: TrayIcon | None = None) -> MenubarPresentation:
    """What the macOS status item shows for a `controller.TrayState`.

    The symbol carries the state, so the title is only the count, optionally
    prefixed by the tray-icon bucket emoji (Preferences → Appearance)."""
    symbol = MENUBAR_SYMBOLS.get(state.kind, MENUBAR_SYMBOLS["idle"])
    if state.kind in ("paused", "error", "offline"):
        return MenubarPresentation(symbol, "", red=state.kind == "error")
    count = state.count if state.kind == "count" else 0
    title = _number(count) if count > 0 else ""
    if icon_cfg is not None and icon_cfg.enabled:
        glyph = pick_glyph(count, icon_cfg.buckets)
        if glyph:
            title = f"{glyph} {title}" if (icon_cfg.show_count and title) else glyph
    if state.unseen:
        title = f"{title}•"  # new since the menu was last opened
    return MenubarPresentation(symbol, title, red=bool(state.alert))


def describe(state: TrayState) -> str:
    """Accessible description / tooltip for a `controller.TrayState`."""
    if state.kind == "paused":
        return "awaitray: paused"
    if state.kind == "offline":
        return "awaitray: offline"
    if state.kind == "error":
        return "awaitray: needs attention"
    if state.kind != "count" or state.count <= 0:
        return "awaitray: nothing to review"
    extra = (", high priority" if state.alert else "") + (
        ", new since you last looked" if state.unseen else ""
    )
    return f"awaitray: {state.count} PR{'s' if state.count != 1 else ''} to review{extra}"
