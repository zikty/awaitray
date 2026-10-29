"""macOS backend: NSStatusItem menu-bar app in pure PyObjC (no rumps).

Owns only the AppKit rendering and the scheduling primitives; all the
PR/fetch/snooze/error orchestration lives in `awaitray.controller`. The
delegate implements the controller's `UIPort` and a `HostPort` backed by
NSTimer (for the poll/fetch/snooze timers) and the main NSOperationQueue
(for posting worker results back to the main thread, the macOS analogue
of GLib's `idle_add`).

The menu is the controller's menu model (docs/ui-spec.md) rendered by
`menu.py`, like the Linux backend renders the same model with GTK. All
configuration lives in the single PyObjC window in `ui/prefs_window.py`.

Methods that are NOT Cocoa actions are marked `@objc.python_method` so
PyObjC doesn't try to register them as selectors.
"""

from __future__ import annotations

import AppKit
import objc
from Foundation import (
    NSBundle,
    NSObject,
    NSOperationQueue,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
)

from awaitray import icon as icon_mod
from awaitray import menu_model as mm
from awaitray.controller import Controller

from . import menu as menu_render
from . import notifier, status_icon
from .ui import prefs_window

_VARIABLE_LEN = -1.0

_ACCESSORY = 1  # NSApplicationActivationPolicyAccessory (menu-bar only)
APP_NAME = "awaitray"


class _MacHost:
    """HostPort implementation backed by NSTimer + the main NSOperationQueue.

    `timeout_add_seconds` always schedules a *repeating* NSTimer and lets
    the callback's return value decide whether to keep firing — matching
    GLib's `timeout_add_seconds` semantics exactly (truthy keeps it, falsy
    stops it), so the same controller callbacks work unchanged.
    """

    def __init__(self):
        self._timers = set()

    def timeout_add_seconds(self, seconds, callback):
        def block(timer):
            try:
                keep = callback()
            except Exception:  # never let a timer callback kill the runloop
                keep = False
            if not keep:
                timer.invalidate()
                self._timers.discard(timer)

        # Common modes, not the default mode `scheduledTimer…` uses: while a
        # menu is open the run loop is in event-tracking mode and default-mode
        # timers stop firing, which would pause polling, retries and sweeps.
        timer = NSTimer.timerWithTimeInterval_repeats_block_(float(seconds), True, block)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._timers.add(timer)
        return timer

    def source_remove(self, handle):
        handle.invalidate()
        self._timers.discard(handle)

    def run_on_main(self, callback, *args):
        def block():
            callback(*args)  # discard return; the ObjC block expects void

        NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    def default_opener(self):
        return "open"


class _App(NSObject):
    def init(self):
        self = objc.super(_App, self).init()
        if self is None:
            return None
        self._ctl = Controller(ui=self, host=_MacHost())
        self._renderer = menu_render.Renderer(self._on_action)
        self._menu_signature = None
        bar = AppKit.NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(_VARIABLE_LEN)
        self.refresh_icon()
        self.rebuild_menu()
        self._ctl.start()
        return self

    # ----- UIPort implementation (called by the controller) -----

    @objc.python_method
    def refresh_icon(self):
        state = self._ctl.tray_state()
        pres = icon_mod.menubar_presentation(state, self._ctl.cfg.tray_icon)
        status_icon.apply(self._item.button(), pres, icon_mod.describe(state))

    @objc.python_method
    def notify(self, title, body, urgency, icon):
        notifier.notify(title, body, urgency=urgency, icon=icon)

    @objc.python_method
    def play_sound(self, sound):
        notifier.play_sound(sound)

    @objc.python_method
    def present_prefs(self):
        prefs_window.open_window(self._ctl, self._on_prefs_saved)

    # ----- menu -----

    @objc.python_method
    def rebuild_menu(self):
        """Render the controller's menu model, only when it changed."""
        model = self._ctl.menu_model()
        if model.signature == self._menu_signature:
            return
        self._menu_signature = model.signature
        menu = self._renderer.render(model.items)
        menu.setDelegate_(self)  # menuWillOpen_: clears the unseen dot
        self._item.setMenu_(menu)

    def menuWillOpen_(self, _menu):
        self._ctl.menu_opened()

    @objc.python_method
    def _on_action(self, action):
        if action.kind == mm.PREFS:
            self.present_prefs()
        elif action.kind == mm.QUIT:
            self._quit()
        elif action.kind == mm.COPY:
            board = AppKit.NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(action.args[0], AppKit.NSPasteboardTypeString)
        else:
            self._ctl.perform(action)

    @objc.python_method
    def _quit(self):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _on_prefs_saved(self, new_cfg):
        self._ctl.on_prefs_saved(new_cfg)


_app_delegate = None


def name_the_app(bundle=None) -> bool:
    """Call ourselves awaitray, not Python. Run as `python -m awaitray`, the
    process lives in the interpreter's Python.app, whose CFBundleName names
    the app menu (and, depending on the macOS version, the Dock and ⌘-Tab)
    while Settings is open. It has to be set before NSApplication starts.
    Returns whether the name could be set."""
    bundle = bundle or NSBundle.mainBundle()
    try:
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        info["CFBundleName"] = APP_NAME
    except (TypeError, AttributeError, KeyError):  # immutable or no bundle
        return False
    return True


def run() -> None:
    global _app_delegate
    name_the_app()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(_ACCESSORY)
    _app_delegate = _App.alloc().init()
    app.run()
