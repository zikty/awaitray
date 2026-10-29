"""Generate the tray icon SVGs on demand, one per `TrayState`.

Variants are written to `~/.cache/awaitray/icons/` and exposed to the tray via
`set_icon_theme_path()` + `set_icon_full(name, desc)` (absolute paths are
unreliable across AppIndicator forks).

Monochrome glyphs are drawn with `currentColor`:
  * KDE Plasma recolors them to the panel's text color through the
    `ColorScheme-Text` class;
  * everywhere else they fall back to a mid gray that reads on both light and
    dark panels (the old near-white disappeared on light panels).
We don't use GTK's `-symbolic` naming: GTK 3 recolors symbolic icons by
*filling* shapes, which would mangle these stroked glyphs.

The only colored variant is the alert count (a high-priority repo has a
waiting PR): a red pill with white text.
"""

from __future__ import annotations

from awaitray import config as cfg_mod
from awaitray.controller import (
    TRAY_COUNT,
    TRAY_ERROR,
    TRAY_OFFLINE,
    TRAY_PAUSED,
    TrayState,
)

CACHE_DIR = cfg_mod.xdg_dir("XDG_CACHE_HOME", ".cache") / "awaitray" / "icons"

FALLBACK_GRAY = "#888888"  # ≥3:1 contrast on both light and dark panels
ALERT_RED = "#e53935"
UNSEEN_BLUE = "#1e88e5"  # the "new since you last looked" dot


def _svg(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22" '
        f'color="{FALLBACK_GRAY}">\n'
        '  <style id="current-color-scheme" type="text/css">'
        f".ColorScheme-Text {{ color:{FALLBACK_GRAY}; }}</style>\n"
        f"{body}"
        "</svg>\n"
    )


_STROKE = (
    'class="ColorScheme-Text" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round"'
)


def svg_idle() -> str:
    """Pull-request glyph: source branch on the left, target with an arrow."""
    return _svg(
        f"  <g {_STROKE}>\n"
        '    <circle cx="6" cy="5" r="2"/>\n'
        '    <circle cx="6" cy="17" r="2"/>\n'
        '    <path d="M6 7 V15"/>\n'
        '    <circle cx="16" cy="17" r="2"/>\n'
        '    <path d="M16 15 V9 a3 3 0 0 0 -3 -3 H10"/>\n'
        '    <path d="M12 3.8 L9.8 6 L12 8.2"/>\n'
        "  </g>\n"
    )


def svg_count(count: int, alert: bool = False, unseen: bool = False) -> str:
    text = "99+" if count > 99 else str(count)
    size = {1: 12, 2: 11}.get(len(text), 7.5)
    if alert:
        pill = f'  <rect x="1" y="4" width="20" height="14" rx="7" fill="{ALERT_RED}"/>\n'
        fill = 'fill="#ffffff"'
    else:
        pill = f'  <rect x="1.9" y="4.9" width="18.2" height="12.2" rx="6.1" {_STROKE}/>\n'
        fill = 'class="ColorScheme-Text" fill="currentColor"'
    dot = f'  <circle cx="18.6" cy="3.4" r="3.2" fill="{UNSEEN_BLUE}"/>\n' if unseen else ""
    return _svg(
        pill + f'  <text x="11" y="{11 + size * 0.36:.1f}" text-anchor="middle" '
        'font-family="DejaVu Sans, Liberation Sans, sans-serif" '
        f'font-size="{size}" font-weight="700" {fill}>{text}</text>\n' + dot
    )


def svg_paused() -> str:
    return _svg(
        '  <g class="ColorScheme-Text" fill="currentColor">\n'
        '    <rect x="6" y="5" width="3.5" height="12" rx="0.8"/>\n'
        '    <rect x="12.5" y="5" width="3.5" height="12" rx="0.8"/>\n'
        "  </g>\n"
    )


def svg_error() -> str:
    """Warning triangle with an exclamation mark."""
    return _svg(
        f'  <path d="M11 3 L20 18.5 H2 Z" {_STROKE}/>\n'
        '  <g class="ColorScheme-Text" fill="currentColor">\n'
        '    <rect x="10.1" y="8.5" width="1.8" height="5.2" rx="0.9"/>\n'
        '    <circle cx="11" cy="15.8" r="1.05"/>\n'
        "  </g>\n"
    )


def svg_offline() -> str:
    """A link that is cut: no connection (distinct from an error)."""
    return _svg(
        f"  <g {_STROKE}>\n"
        '    <circle cx="11" cy="11" r="7.5"/>\n'
        '    <path d="M5.8 5.8 L16.2 16.2"/>\n'
        "  </g>\n"
    )


def icon_for(state: TrayState) -> tuple[str, str]:
    """(icon name, SVG content) for a tray state."""
    if state.kind == TRAY_PAUSED:
        return "awaitray-paused", svg_paused()
    if state.kind == TRAY_OFFLINE:
        return "awaitray-offline", svg_offline()
    if state.kind == TRAY_ERROR:
        return "awaitray-error", svg_error()
    if state.kind == TRAY_COUNT and state.count > 0:
        bucket = "99plus" if state.count > 99 else str(state.count)
        suffix = ("-alert" if state.alert else "") + ("-new" if state.unseen else "")
        return f"awaitray-{bucket}{suffix}", svg_count(state.count, state.alert, state.unseen)
    return "awaitray-idle", svg_idle()


def render(state: TrayState) -> tuple[str, str]:
    """Write (or reuse) the SVG for `state`; return (theme_path, icon_name)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    name, content = icon_for(state)
    out = CACHE_DIR / f"{name}.svg"
    if not out.exists() or out.read_text() != content:
        out.write_text(content)
    return str(CACHE_DIR), name
