"""Linux GTK backend: indicator + menu, wired to the shared Controller.

This module owns only the GTK rendering and the GLib scheduling; all the
PR/fetch/snooze/error orchestration lives in `awaitray.controller`. The
class implements the controller's `UIPort` (refresh_icon, rebuild_menu,
notify, present_prefs) and a `HostPort` (GLib timers + idle_add) so the
controller can drive the app without knowing about GTK.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator  # type: ignore[no-redef]

from gi.repository import Gdk, GLib, Gtk

from awaitray import config as cfg_mod
from awaitray import icon as icon_mod
from awaitray import menu_model as mm
from awaitray.controller import TRAY_IDLE, Controller, TrayState

from . import (
    icon_renderer,
    notifier,
)
from . import menu as menu_render
from .ui.prefs import PreferencesWindow


class _GLibHost:
    """HostPort implementation backed by the GLib main loop."""

    def timeout_add_seconds(self, seconds: int, callback) -> object:
        return GLib.timeout_add_seconds(seconds, callback)

    def source_remove(self, handle: object) -> None:
        GLib.source_remove(handle)

    def run_on_main(self, callback, *args) -> None:
        GLib.idle_add(callback, *args)

    def default_opener(self) -> str:
        return "xdg-open"


class AwaitrayApp:
    def __init__(self) -> None:
        self._ctl = Controller(ui=self, host=_GLibHost())
        self._prefs_window: PreferencesWindow | None = None

        theme_path, name = icon_renderer.render(TrayState(TRAY_IDLE))
        self._indicator = AppIndicator.Indicator.new(
            "awaitray",
            name,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_icon_theme_path(theme_path)
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._indicator.set_title("awaitray")

        self._menu = Gtk.Menu()
        self._menu_signature: int | None = None  # hash of everything but the status line
        self._status_item: Gtk.MenuItem | None = None
        self._indicator.set_menu(self._menu)
        self._watch_menu_opens()
        self.rebuild_menu()
        self.refresh_icon()

        self._ctl.start()

    def _watch_menu_opens(self) -> None:
        """Tell the controller when the menu opens (that clears the unseen dot).

        With AppIndicator the Gtk.Menu is never shown: the desktop reads it
        over DBusMenu and draws its own. What reaches us is the DBusMenu
        server's root item: `about-to-show` and an `opened` event. Both are
        handled (shells differ in which they send); menu_opened is idempotent.
        """
        try:
            server = self._indicator.get_property("dbus-menu-server")
            root = server.get_property("root-node") if server is not None else None
        except (TypeError, AttributeError):  # an AppIndicator fork without it
            root = None
        if root is None:
            return
        root.connect("about-to-show", self._on_menu_about_to_show)
        root.connect("event", self._on_menu_event)

    def _on_menu_about_to_show(self, *_args) -> bool:
        self._ctl.menu_opened()
        return False

    def _on_menu_event(self, _item, name: str, *_args) -> None:
        if name == "opened":
            self._ctl.menu_opened()

    # ----- Public entry point -----

    def run(self) -> None:
        Gtk.main()

    # ----- UIPort implementation (called by the controller) -----

    def refresh_icon(self) -> None:
        state = self._ctl.tray_state()
        theme_path, name = icon_renderer.render(state)
        self._indicator.set_icon_theme_path(theme_path)
        self._indicator.set_icon_full(name, icon_mod.describe(state))

    def notify(self, title: str, body: str, urgency: str, icon: str) -> None:
        notifier.notify(title, body, urgency=urgency, icon=icon)

    def play_sound(self, sound: str) -> None:
        notifier.play_sound(sound)

    def present_prefs(self) -> None:
        self._on_open_prefs(None)

    # ----- Menu -----

    def rebuild_menu(self) -> None:
        """Render the controller's menu model. Skipped when nothing changed,
        so a poll that finds the same PRs never collapses an open submenu."""
        model = self._ctl.menu_model()
        status, body = model.items[0], hash(model.items[1:])
        if body == self._menu_signature:
            # Only the status line changed (e.g. "updated 14:37"): update it
            # in place, which DBusMenu applies live without closing anything.
            if self._status_item is not None and self._status_item.get_label() != status.label:
                self._status_item.set_label(status.label)
            return
        self._menu_signature = body
        by_action = menu_render.render(self._menu, model.items, self._on_action)
        self._status_item = self._menu.get_children()[0]
        refresh = by_action.get("refresh")
        if refresh is not None:
            # Middle click on the tray icon = Refresh now (GNOME and KDE).
            self._indicator.set_secondary_activate_target(refresh)

    def _on_action(self, action: mm.Action) -> None:
        if action.kind == mm.PREFS:
            self._on_open_prefs(None)
        elif action.kind == mm.QUIT:
            Gtk.main_quit()
        elif action.kind == mm.COPY:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(action.args[0], -1)
            clipboard.store()
        else:
            self._ctl.perform(action)

    # ----- Menu handlers -----

    def _on_open_prefs(self, _item) -> None:
        if self._prefs_window is not None:
            self._prefs_window.present()
            return
        win = PreferencesWindow(
            self._ctl.cfg, on_saved=self._on_prefs_saved, live=lambda: self._ctl.cfg
        )
        self._prefs_window = win

        def _on_destroy(_w):
            self._prefs_window = None

        win.connect("destroy", _on_destroy)
        win.show_all()

    def _on_prefs_saved(self, new_cfg: cfg_mod.Config) -> bool:
        self._ctl.on_prefs_saved(new_cfg)
        return False


def run() -> None:
    AwaitrayApp().run()
