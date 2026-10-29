"""macOS menu-bar status item: an SF Symbol template image plus the count.

What to show comes from `icon.menubar_presentation(controller.tray_state())`
(pure, shared logic); this module only applies it to the status item's
button. Template images adapt to light/dark menu bars and the highlighted
state automatically. Red (priority PR or error) goes into the title's
attributes and the symbol's own color (a non-template, hierarchical red
symbol), never into `contentTintColor`: on macOS 26 setting that, even to
nil, makes the button lose the menu bar's automatic color and draw black.
"""

from __future__ import annotations

import AppKit

from awaitray.icon import MenubarPresentation


def _symbol(name: str, description: str):
    maker = getattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None)
    if maker is None:  # macOS < 11: no SF Symbols
        return None
    image = maker(name, description)
    if image is not None:
        image.setTemplate_(True)
    return image


# Shown when SF Symbols are unavailable, so the state is never invisible.
_FALLBACK_TITLES = {
    "pause.circle": "‖",
    "exclamationmark.triangle": "!",
    "wifi.slash": "⌁",
    "arrow.triangle.pull": "⎇",
}


def _red_symbol(image):
    """The symbol drawn red. Not a template, so the system leaves its color
    alone; falls back to the template image where palettes don't exist."""
    config_cls = getattr(AppKit, "NSImageSymbolConfiguration", None)
    make = getattr(config_cls, "configurationWithHierarchicalColor_", None)
    if make is None:  # macOS < 12
        return image
    red = image.imageWithSymbolConfiguration_(make(AppKit.NSColor.systemRedColor()))
    if red is None:
        return image
    red.setTemplate_(False)
    return red


def apply(button, pres: MenubarPresentation, description: str) -> None:
    # Never set contentTintColor (not even to nil): on macOS 26 a status
    # button with it set loses the menu bar's automatic label color and draws
    # the template symbol and the title black on the dark bar. Red goes into
    # the title's attributes and the symbol's own color instead, as devtray
    # did with its title.
    image = _symbol(pres.symbol, description)
    if image is not None and pres.red:
        image = _red_symbol(image)
    button.setImage_(image)
    button.setImagePosition_(AppKit.NSImageLeft if pres.title else AppKit.NSImageOnly)
    title = pres.title
    if image is None and not title:
        title = _FALLBACK_TITLES.get(pres.symbol, "⎇")
    if pres.red:
        attrs = {
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.systemRedColor(),
            # An attributed string without a font draws in Helvetica 12, not
            # the menu bar's font.
            AppKit.NSFontAttributeName: AppKit.NSFont.menuBarFontOfSize_(0),
        }
        button.setAttributedTitle_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(title, attrs)
        )
    else:
        # Clear a red attributed title left from a previous state, then set
        # the plain one so the bar colors it.
        button.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_(""))
        button.setTitle_(title)
    button.setToolTip_(description)
