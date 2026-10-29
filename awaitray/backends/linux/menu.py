"""Render a `menu_model.Menu` into a Gtk.Menu (exported over DBusMenu).

Only what survives DBusMenu is used: labels, a color swatch image, check
state, sensitivity, separators and submenus (docs/ui-spec.md). Clicks are
reported as `menu_model.Action`s through `on_action`.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from awaitray import menu_model as mm

_SWATCH_SIZE = 12
_swatch_cache: dict[str, GdkPixbuf.Pixbuf] = {}


def swatch_pixbuf(hex_color: str) -> GdkPixbuf.Pixbuf | None:
    cached = _swatch_cache.get(hex_color)
    if cached is not None:
        return cached
    rgba = Gdk.RGBA()
    if not rgba.parse(hex_color):
        return None
    r, g, b = (max(0, min(255, int(c * 255))) for c in (rgba.red, rgba.green, rgba.blue))
    pixels = bytes([r, g, b, 255]) * (_SWATCH_SIZE * _SWATCH_SIZE)
    pix = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(pixels),
        GdkPixbuf.Colorspace.RGB,
        True,
        8,
        _SWATCH_SIZE,
        _SWATCH_SIZE,
        _SWATCH_SIZE * 4,
    )
    _swatch_cache[hex_color] = pix
    return pix


def _labelled(label: str, color: str | None) -> Gtk.MenuItem:
    pix = swatch_pixbuf(color) if color else None
    if pix is None:
        return Gtk.MenuItem(label=label)
    # DBusMenu strips markup, but relays the item's image: a color swatch.
    item = Gtk.ImageMenuItem.new_with_label(label)
    item.set_image(Gtk.Image.new_from_pixbuf(pix))
    item.set_always_show_image(True)
    return item


def render(
    menu: Gtk.Menu,
    items: tuple[mm.Item, ...],
    on_action: Callable[[mm.Action], None],
) -> dict[str, Gtk.MenuItem]:
    """Replace `menu`'s children with `items`. Returns created items keyed by
    action kind for the top level (e.g. "refresh" for the middle-click target)."""
    for child in menu.get_children():
        menu.remove(child)
    by_action: dict[str, Gtk.MenuItem] = {}
    for it in items:
        widget = _widget(it, on_action)
        menu.append(widget)
        if it.action is not None:
            by_action.setdefault(it.action.kind, widget)
    menu.show_all()
    return by_action


def _widget(it: mm.Item, on_action: Callable[[mm.Action], None]) -> Gtk.MenuItem:
    if it.kind == mm.SEPARATOR:
        return Gtk.SeparatorMenuItem()

    if it.kind == mm.CHECK:
        widget = Gtk.CheckMenuItem(label=it.label)
        widget.set_active(it.checked)  # before connecting: no spurious toggle
        widget.connect("toggled", lambda _w, a=it.action: on_action(a))
        return widget

    widget = _labelled(it.label, it.color)
    if it.kind == mm.SUBMENU:
        sub = Gtk.Menu()
        render(sub, it.children, on_action)
        widget.set_submenu(sub)
    elif it.action is not None and it.enabled:
        widget.connect("activate", lambda _w, a=it.action: on_action(a))
    widget.set_sensitive(it.enabled)
    return widget
