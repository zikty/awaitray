"""Render a `menu_model.Menu` into an NSMenu.

Same structure as Linux (docs/ui-spec.md), richer rendering: PR rows are two
lines (title, then a secondary-color meta line) with a color dot, and holding
⌥ turns a row into its actions submenu (an alternate item). Clicks are
reported as `menu_model.Action`s through `on_action`.
"""

from __future__ import annotations

from collections.abc import Callable

import AppKit
import objc
from Foundation import NSMakeRect, NSObject

from awaitray import menu_model as mm

_OPTION = 1 << 19  # NSEventModifierFlagOption
_ON = 1  # NSControlStateValueOn


class _Trampoline(NSObject):
    """Holds a Python callable so an NSMenuItem can target it."""

    def initWithCallback_(self, callback):
        self = objc.super(_Trampoline, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def invoke_(self, sender):
        self._callback()


def hex_color(value: str | None):
    """'#RRGGBB' → NSColor, or None for empty/invalid."""
    if not value:
        return None
    s = value.lstrip("#")
    if len(s) != 6:
        return None
    try:
        r, g, b = (int(s[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


_dot_cache: dict = {}


def dot_image(value: str | None):
    """A small filled circle for a hex color (survives menu highlight and
    dark mode, unlike text color)."""
    color = hex_color(value)
    if color is None:
        return None
    cached = _dot_cache.get(value)
    if cached is not None:
        return cached
    size = 10.0
    img = AppKit.NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    color.set()
    AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(1, 1, size - 2, size - 2)).fill()
    img.unlockFocus()
    img.setTemplate_(False)
    _dot_cache[value] = img
    return img


class Renderer:
    """Builds NSMenus from model items; keeps the action targets alive for as
    long as the menu it built is in use (call `render` again to replace)."""

    def __init__(self, on_action: Callable[[mm.Action], None]) -> None:
        self._on_action = on_action
        self._trampolines: list = []

    def render(self, items: tuple[mm.Item, ...]):
        self._trampolines = []
        return self._menu(items)

    # ----- internals -----

    def _menu(self, items):
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for it in items:
            for ns_item in self._items(it):
                menu.addItem_(ns_item)
        return menu

    def _items(self, it: mm.Item) -> list:
        if it.kind == mm.SEPARATOR:
            return [AppKit.NSMenuItem.separatorItem()]
        item = self._base(it.label)
        item.setEnabled_(it.enabled)

        if it.kind == mm.HEADER:
            self._set_title(
                item,
                it.label,
                font=AppKit.NSFont.boldSystemFontOfSize_(11),
                color=hex_color(it.color) or AppKit.NSColor.secondaryLabelColor(),
            )
        elif it.kind in (mm.EMPTY, mm.STATUS):
            self._set_title(item, it.label, color=AppKit.NSColor.secondaryLabelColor())

        if it.kind == mm.SUBMENU:
            item.setSubmenu_(self._menu(it.children))
            dot = dot_image(it.color)
            if dot is not None:
                item.setImage_(dot)
            return [item]

        if it.action is not None and it.enabled:
            self._target(item, it.action)
        if it.kind == mm.CHECK:
            item.setState_(_ON if it.checked else 0)
        if it.kind != mm.PR:
            return [item]

        # PR row: two lines + color dot; ⌥ swaps in the actions submenu.
        self._set_two_lines(item, it.title or it.label, it.meta, hex_color(it.color))
        dot = dot_image(it.color)
        if dot is not None:
            item.setImage_(dot)
        item.setKeyEquivalentModifierMask_(0)
        if not it.alternate:
            return [item]
        alt = self._base(f"Actions: {it.title or it.label}")
        alt.setSubmenu_(self._menu(it.alternate))
        alt.setAlternate_(True)
        alt.setKeyEquivalentModifierMask_(_OPTION)
        if dot is not None:
            alt.setImage_(dot)
        return [item, alt]

    def _base(self, title):
        return AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")

    def _target(self, item, action: mm.Action):
        tramp = _Trampoline.alloc().initWithCallback_(lambda a=action: self._on_action(a))
        self._trampolines.append(tramp)
        item.setTarget_(tramp)
        item.setAction_("invoke:")

    def _set_title(self, item, text, *, font=None, color=None):
        attrs = {}
        if font is not None:
            attrs[AppKit.NSFontAttributeName] = font
        if color is not None:
            attrs[AppKit.NSForegroundColorAttributeName] = color
        item.setAttributedTitle_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        )

    def _set_two_lines(self, item, title, meta, color):
        text = AppKit.NSMutableAttributedString.alloc().init()
        title_attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.menuFontOfSize_(13)}
        if color is not None:
            title_attrs[AppKit.NSForegroundColorAttributeName] = color
        text.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(title, title_attrs)
        )
        if meta:
            text.appendAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    "\n" + meta,
                    {
                        AppKit.NSFontAttributeName: AppKit.NSFont.menuFontOfSize_(11),
                        AppKit.NSForegroundColorAttributeName: AppKit.NSColor.secondaryLabelColor(),
                    },
                )
            )
        item.setAttributedTitle_(text)
