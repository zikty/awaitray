"""macOS Settings window rendered from `prefs_model` (docs/ui-spec.md).

A toolbar-style tab view (NSTabViewController, as Apple's HIG recommends for
Settings) with the six sections; each section is a scrollable stack of
groups, each group a box of rows (label + help on the left, control on the
right). Simple fields come from the model; hosts, repos, tray-icon tiers,
backup and logs have their own editors below.

Changes apply as you go through the shared `EditSession` (debounced). No
`gh` or `git` call runs on the main thread.
"""

from __future__ import annotations

import copy
import os
import subprocess
import threading
from collections.abc import Callable

import AppKit
import objc
from Foundation import (
    NSMakeRect,
    NSObject,
    NSOperationQueue,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
)

from awaitray import backup as backup_mod
from awaitray import browsers, pr_sync, sounds
from awaitray import config as cfg_mod
from awaitray import github as gh
from awaitray import log as log_mod
from awaitray import prefs_model as pm

from .. import notifier

_WIDTH = 640
_VERTICAL = AppKit.NSUserInterfaceLayoutOrientationVertical
_HORIZONTAL = AppKit.NSUserInterfaceLayoutOrientationHorizontal
_TOOLBAR_TABS = 2  # NSTabViewControllerTabStyleToolbar
_LAST_PANE_KEY = "awaitray.prefs.lastPane"
_SOUND_NONE = "None"
_SOUND_CUSTOM = "Custom file…"


# ----- small helpers -----


class _Target(NSObject):
    """Bridges Cocoa actions and delegates to a Python callback."""

    def initWithCallback_(self, callback):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def invoke_(self, sender):
        self._callback(sender)

    def controlTextDidChange_(self, notification):
        self._callback(notification.object())

    def textDidChange_(self, notification):
        self._callback(notification.object())

    def comboBoxSelectionDidChange_(self, notification):
        # Sent before the combo box shows the picked item: show it first.
        box = notification.object()
        index = box.indexOfSelectedItem()
        if index >= 0:
            box.setStringValue_(box.itemObjectValueAtIndex_(index))
        self._callback(box)


def _on_main(fn, *args) -> None:
    NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: fn(*args))


def _run_in_thread(work: Callable, done: Callable) -> None:
    threading.Thread(target=lambda: _on_main(done, work()), daemon=True).start()


def _label(text, *, secondary=False, bold=False, wrap=False, width=_WIDTH - 100):
    field = AppKit.NSTextField.labelWithString_(text)
    if secondary:
        field.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
    if bold:
        field.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
    if wrap:
        field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        field.cell().setWraps_(True)
        field.setPreferredMaxLayoutWidth_(width)
    return field


def _shrinkable(field, mode=AppKit.NSLineBreakByTruncatingTail):
    """A label that truncates rather than widening its row (paths, statuses):
    a row wider than its group breaks the page's layout."""
    field.setLineBreakMode_(mode)
    field.setContentCompressionResistancePriority_forOrientation_(250, _HORIZONTAL)
    return field


def _browser_combo(target_for, choices, value, width, on_change):
    """The installed browsers and their profiles, by name, with the typed
    command as a fallback. `on_change(box, command)` gets what to
    store: a picked label becomes its command."""
    box = AppKit.NSComboBox.alloc().initWithFrame_(NSMakeRect(0, 0, width, 26))
    box.addItemsWithObjectValues_([c.label for c in choices])
    box.setNumberOfVisibleItems_(min(12, len(choices)))
    box.setCompletes_(False)
    box.setStringValue_(browsers.label_for(value, choices))
    box.setTranslatesAutoresizingMaskIntoConstraints_(False)
    box.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    box.setDelegate_(
        target_for(lambda b: on_change(b, browsers.command_for(b.stringValue(), choices)))
    )
    return box


def _pick_sound_file():
    panel = AppKit.NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setAllowedFileTypes_([ext.lstrip(".") for ext in sounds.EXTENSIONS])
    panel.setMessage_("Choose a sound (.wav, .ogg, .aiff)")
    if panel.runModal() != AppKit.NSModalResponseOK:
        return None
    return panel.URLs()[0].path()


def _stack(orientation=_VERTICAL, spacing=8):
    stack = AppKit.NSStackView.alloc().init()
    stack.setOrientation_(orientation)
    stack.setSpacing_(spacing)
    stack.setAlignment_(
        AppKit.NSLayoutAttributeLeading
        if orientation == _VERTICAL
        else AppKit.NSLayoutAttributeCenterY
    )
    return stack


def _button(title, target):
    button = AppKit.NSButton.buttonWithTitle_target_action_(title, target, "invoke:")
    button.setBezelStyle_(AppKit.NSBezelStyleRounded)
    return button


def _error_color(error):
    return AppKit.NSColor.systemRedColor() if error else AppKit.NSColor.labelColor()


def _hex_to_color(value):
    s = (value or "#5B9BD5").lstrip("#")
    try:
        r, g, b = (int(s[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 0.5
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


def _color_to_hex(color):
    c = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace()) or color
    r, g, b = (
        max(0, min(255, round(x * 255)))
        for x in (c.redComponent(), c.greenComponent(), c.blueComponent())
    )
    return f"#{r:02X}{g:02X}{b:02X}"


class _Flipped(AppKit.NSStackView):
    """Flipped document view: short pages stay pinned to the top."""

    def isFlipped(self):
        return True


# ----- window -----


class PrefsWindow:
    def __init__(
        self,
        cfg: cfg_mod.Config,
        on_saved: Callable[[cfg_mod.Config], object],
        live: Callable[[], cfg_mod.Config] | None = None,
    ) -> None:
        self._on_saved = on_saved
        self._targets: list = []  # keep Cocoa targets alive
        self.controls: dict[str, object] = {}  # field key -> control (tests, focus)
        self._dependents: list = []  # (field, row view)
        self.session = pm.EditSession(cfg, on_saved, self._schedule, self._cancel, live=live)
        # Found once per window: a few plist reads, no subprocess.
        self.browser_choices = browsers.installed()

        self.tabs = AppKit.NSTabViewController.alloc().init()
        self.tabs.setTabStyle_(_TOOLBAR_TABS)
        self._build_tabs()
        self.window = AppKit.NSWindow.windowWithContentViewController_(self.tabs)
        self.window.setTitle_("Settings")
        self.window.setReleasedWhenClosed_(False)
        mask = self.window.styleMask() & ~AppKit.NSWindowStyleMaskMiniaturizable
        self.window.setStyleMask_(mask)
        self._close_observer = self._target(lambda _n: self._on_close())
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._close_observer, "invoke:", AppKit.NSWindowWillCloseNotification, self.window
        )
        last = AppKit.NSUserDefaults.standardUserDefaults().integerForKey_(_LAST_PANE_KEY)
        if 0 <= last < len(pm.SECTIONS):
            self.tabs.setSelectedTabViewItemIndex_(last)

    # ----- timers for the session -----

    def _schedule(self, ms, fn):
        timer = NSTimer.timerWithTimeInterval_repeats_block_(ms / 1000.0, False, lambda _t: fn())
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        return timer

    @staticmethod
    def _cancel(timer):
        timer.invalidate()

    def _target(self, callback):
        target = _Target.alloc().initWithCallback_(callback)
        self._targets.append(target)
        return target

    # ----- building -----

    def _build_tabs(self) -> None:
        for item in list(self.tabs.tabViewItems()):
            self.tabs.removeTabViewItem_(item)
        self.controls = {}
        self._dependents = []
        for section in pm.SECTIONS:
            vc = AppKit.NSViewController.alloc().init()
            vc.setView_(self._page(section))
            vc.setTitle_(section.title)
            item = AppKit.NSTabViewItem.tabViewItemWithViewController_(vc)
            item.setLabel_(section.title)
            maker = getattr(
                AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None
            )
            if maker is not None:
                item.setImage_(maker(section.icon, section.title))
            self.tabs.addTabViewItem_(item)
        self._refresh_dependents()

    def _page(self, section: pm.Section):
        page = _Flipped.alloc().init()
        page.setOrientation_(_VERTICAL)
        page.setAlignment_(AppKit.NSLayoutAttributeLeading)
        page.setSpacing_(18)
        page.setEdgeInsets_((20, 24, 20, 24))
        for group in section.groups:
            box, frame = self._group(group)
            page.addArrangedSubview_(box)
            # Follow the page, not the window: a legacy (always shown)
            # scroller makes the page narrower than _WIDTH.
            frame.widthAnchor().constraintEqualToAnchor_constant_(
                page.widthAnchor(), -48
            ).setActive_(True)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIDTH, 480))
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setDocumentView_(page)
        page.setTranslatesAutoresizingMaskIntoConstraints_(False)
        page.widthAnchor().constraintEqualToAnchor_(scroll.contentView().widthAnchor()).setActive_(
            True
        )
        return scroll

    def _group(self, group: pm.Group):
        box = _stack(spacing=6)
        box.addArrangedSubview_(_label(group.title, bold=True))
        if group.help:
            box.addArrangedSubview_(_label(group.help, secondary=True, wrap=True))
        rows = _stack(spacing=10)
        rows.setEdgeInsets_((10, 12, 10, 12))
        for f in group.fields:
            row = self._field(f)
            rows.addArrangedSubview_(row)
            if f.kind not in (pm.CUSTOM, pm.LINES):
                # label on the left, control on the right edge of the group
                row.widthAnchor().constraintEqualToAnchor_constant_(
                    rows.widthAnchor(), -24
                ).setActive_(True)
            if f.depends_on:
                self._dependents.append((f, row))
        frame = AppKit.NSBox.alloc().init()
        frame.setTitlePosition_(AppKit.NSNoTitle)
        frame.setContentViewMargins_((0, 0))  # the rows' insets are the margin
        frame.setContentView_(rows)
        frame.setTranslatesAutoresizingMaskIntoConstraints_(False)
        box.addArrangedSubview_(frame)
        return box, frame

    # ----- generic fields -----

    def _field(self, f: pm.Field):
        if f.kind == pm.CUSTOM:
            editor = {
                pm.HOSTS: _HostsEditor,
                pm.REPOS: _ReposEditor,
                pm.BUCKETS: _BucketsEditor,
                pm.SECTIONS_EDITOR: _SectionsEditor,
                pm.BACKUP: _BackupPanel,
                pm.LOGS: _LogsPanel,
                pm.SOUNDS_FOLDER: _SoundsFolderPanel,
            }[f.custom](self)
            self.controls[f.key] = editor
            return editor.view

        value = pm.get_value(self.session.working, f.key)
        error = _label("", secondary=True)
        error.setTextColor_(AppKit.NSColor.systemRedColor())
        error.setHidden_(True)

        def edit(new_value):
            message = self.session.edit(f, new_value)
            error.setStringValue_(message or "")
            error.setHidden_(not message)
            self._refresh_dependents()

        left = _stack(spacing=2)
        left.addArrangedSubview_(_label(f.label))
        if f.help:
            # Leaves room for the widest control (a 240pt popup) beside it.
            left.addArrangedSubview_(_label(f.help, secondary=True, wrap=True, width=280))
        left.addArrangedSubview_(error)

        if f.kind == pm.LINES:
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIDTH - 90, 72))
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(AppKit.NSBezelBorder)
            view = AppKit.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIDTH - 90, 72))
            view.setFont_(AppKit.NSFont.userFixedPitchFontOfSize_(12))
            view.setString_("\n".join(value))
            view.setDelegate_(self._target(lambda v: edit(pm.parse_lines(v.string()))))
            scroll.setDocumentView_(view)
            scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
            scroll.heightAnchor().constraintEqualToConstant_(72).setActive_(True)
            scroll.widthAnchor().constraintEqualToConstant_(_WIDTH - 90).setActive_(True)
            left.addArrangedSubview_(scroll)
            self.controls[f.key] = view
            return left

        control = self._control(f, value, edit)
        self.controls[f.key] = control
        row = _stack(_HORIZONTAL, spacing=12)
        row.addArrangedSubview_(left)
        spacer = AppKit.NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, 0)
        row.addArrangedSubview_(spacer)
        row.addArrangedSubview_(control)
        row.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return row

    def _control(self, f: pm.Field, value, edit):
        if f.kind == pm.BOOL:
            switch = AppKit.NSSwitch.alloc().init()
            switch.setState_(1 if value else 0)
            switch.setTarget_(self._target(lambda s: edit(bool(s.state()))))
            switch.setAction_("invoke:")
            return switch
        if f.kind == pm.INT:
            box = _stack(_HORIZONTAL, spacing=4)
            field = AppKit.NSTextField.textFieldWithString_(str(value))
            field.setAlignment_(AppKit.NSTextAlignmentRight)
            field.setTranslatesAutoresizingMaskIntoConstraints_(False)
            field.widthAnchor().constraintEqualToConstant_(56).setActive_(True)
            stepper = AppKit.NSStepper.alloc().init()
            stepper.setMinValue_(f.minimum or 0)
            stepper.setMaxValue_(f.maximum or 100000)
            stepper.setIncrement_(f.step)
            stepper.setIntegerValue_(value)

            def typed(field_):
                try:
                    number = int(field_.stringValue().strip())
                except ValueError:
                    edit(field_.stringValue())  # shows "Enter a whole number"
                    return
                stepper.setIntegerValue_(number)
                edit(number)

            def stepped(stepper_):
                field.setStringValue_(str(stepper_.integerValue()))
                edit(int(stepper_.integerValue()))

            field.setDelegate_(self._target(typed))
            stepper.setTarget_(self._target(stepped))
            stepper.setAction_("invoke:")
            box.addArrangedSubview_(field)
            box.addArrangedSubview_(stepper)
            return box
        if f.kind in (pm.TEXT, pm.TIME):
            field = AppKit.NSTextField.textFieldWithString_(value or "")
            field.setPlaceholderString_(f.placeholder)
            field.setTranslatesAutoresizingMaskIntoConstraints_(False)
            field.widthAnchor().constraintEqualToConstant_(
                70 if f.kind == pm.TIME else 200
            ).setActive_(True)
            field.setDelegate_(self._target(lambda t: edit(t.stringValue())))
            return field
        if f.kind == pm.BROWSER:

            def picked(box, command):
                box.setTextColor_(_error_color(pm.browser_error(command)))
                edit(command)

            return _browser_combo(self._target, self.browser_choices, value or "", 240, picked)
        if f.kind == pm.SOUND:
            return self._sound_popup(f, value or "", edit)
        if f.kind == pm.CHOICE:
            popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 0, 240, 26), False
            )
            choices = pm.choices_for(f)
            for _, label in choices:
                popup.addItemWithTitle_(label)
            values = [v for v, _ in choices]
            if value in values:
                popup.selectItemAtIndex_(values.index(value))

            def chosen(p):
                picked = values[p.indexOfSelectedItem()]
                edit(picked)
                notifier.play_sound(pm.preview_sound(f, picked, self.session.working) or "")

            popup.setTarget_(self._target(chosen))
            popup.setAction_("invoke:")
            return popup
        if f.kind == pm.COLOR:
            box = _stack(_HORIZONTAL, spacing=8)
            well = AppKit.NSColorWell.alloc().initWithFrame_(NSMakeRect(0, 0, 44, 24))
            well.setColor_(_hex_to_color(value))
            well.setEnabled_(bool(value))
            switch = AppKit.NSSwitch.alloc().init()
            switch.setState_(1 if value else 0)

            def toggled(s):
                well.setEnabled_(bool(s.state()))
                edit(_color_to_hex(well.color()) if s.state() else "")

            switch.setTarget_(self._target(toggled))
            switch.setAction_("invoke:")
            well.setTarget_(self._target(lambda w: edit(_color_to_hex(w.color()))))
            well.setAction_("invoke:")
            box.addArrangedSubview_(well)
            box.addArrangedSubview_(switch)
            return box
        raise ValueError(f"unknown field kind {f.kind}")

    def _sound_popup(self, f, value, edit):
        """An event's sound: the pack's, an effect, silent, or a file you
        choose ("Choose a file…" opens a panel). Picking one plays it."""
        popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 0, 240, 26), False
        )
        values = []
        for v, label in pm.sound_choices():
            popup.addItemWithTitle_(label)
            values.append(v)
        if os.path.isabs(value):
            popup.addItemWithTitle_(os.path.basename(value))
            values.append(value)
        popup.addItemWithTitle_("Choose a file…")
        popup.selectItemAtIndex_(values.index(value) if value in values else 0)
        state = {"index": popup.indexOfSelectedItem()}

        def chosen(p):
            index = p.indexOfSelectedItem()
            if index == len(values):  # "Choose a file…"
                path = _pick_sound_file()
                if not path:
                    p.selectItemAtIndex_(state["index"])
                    return
                if os.path.isabs(values[-1]):  # replace the file chosen before
                    p.removeItemAtIndex_(len(values) - 1)
                    values.pop()
                p.insertItemWithTitle_atIndex_(os.path.basename(path), len(values))
                values.append(path)
                index = len(values) - 1
                p.selectItemAtIndex_(index)
            state["index"] = index
            edit(values[index])
            notifier.play_sound(pm.preview_sound(f, values[index], self.session.working) or "")

        popup.setTarget_(self._target(chosen))
        popup.setAction_("invoke:")
        return popup

    def _refresh_dependents(self) -> None:
        for f, row in self._dependents:
            active = pm.is_active(self.session.working, f)
            row.setAlphaValue_(1.0 if active else 0.45)

    # ----- lifecycle -----

    def reload(self, cfg: cfg_mod.Config) -> None:
        """Start over from `cfg` (after an import) and redraw every pane."""
        index = self.tabs.selectedTabViewItemIndex()
        self.session.reset(cfg)
        self._build_tabs()
        self.tabs.setSelectedTabViewItemIndex_(max(0, index))

    def _on_close(self) -> None:
        if self.session.pending:
            self.session.commit()
        AppKit.NSUserDefaults.standardUserDefaults().setInteger_forKey_(
            self.tabs.selectedTabViewItemIndex(), _LAST_PANE_KEY
        )
        global _current
        _current = None
        _show_in_dock(False)

    def alert(self, title: str, body: str, accessory=None) -> bool:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(body)
        alert.addButtonWithTitle_("OK")
        if accessory is not None:
            alert.addButtonWithTitle_("Cancel")
            alert.setAccessoryView_(accessory)
        return alert.runModal() == AppKit.NSAlertFirstButtonReturn

    def present(self) -> None:
        _show_in_dock(True)
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


# ----- Dock and main menu while Settings is open -----

_REGULAR = 0  # NSApplicationActivationPolicyRegular: Dock icon, ⌘-Tab, menu bar
_ACCESSORY = 1  # NSApplicationActivationPolicyAccessory: menu-bar item only


def _show_in_dock(on: bool) -> None:
    """A menu-bar app has no Dock icon, but its Settings window needs one:
    without it the window can't be found with ⌘-Tab or the Dock once another
    app covers it. Regular while Settings is open, accessory again after."""
    app = AppKit.NSApplication.sharedApplication()
    if not on:
        app.setActivationPolicy_(_ACCESSORY)
        return
    app.setActivationPolicy_(_REGULAR)
    if app.mainMenu() is None:
        app.setMainMenu_(_main_menu())
    icon = _app_icon()
    if icon is not None:
        app.setApplicationIconImage_(icon)


def _main_menu():
    """App and Edit menus. Text fields take ⌘C, ⌘V, ⌘X, ⌘A and ⌘Z from the
    Edit menu's key equivalents: without it, pasting a browser command or a
    query into Settings does nothing."""
    bar = AppKit.NSMenu.alloc().init()

    def submenu(title, items):
        menu = AppKit.NSMenu.alloc().initWithTitle_(title)
        for label, action, key in items:
            if label is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, key)
            if label == "Redo":
                item.setKeyEquivalentModifierMask_(
                    AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift
                )
            menu.addItem_(item)
        holder = AppKit.NSMenuItem.alloc().init()
        holder.setSubmenu_(menu)
        bar.addItem_(holder)

    submenu(
        "awaitray",
        [
            ("Close Window", "performClose:", "w"),
            (None, None, None),
            ("Quit awaitray", "terminate:", "q"),
        ],
    )
    submenu(
        "Edit",
        [
            ("Undo", "undo:", "z"),
            ("Redo", "redo:", "z"),
            (None, None, None),
            ("Cut", "cut:", "x"),
            ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"),
            ("Select All", "selectAll:", "a"),
        ],
    )
    return bar


def _app_icon():
    """The app icon drawn in code (there's no bundle to carry one): the
    menu-bar symbol in white on awaitray's dark rounded square."""
    make = getattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None)
    config_cls = getattr(AppKit, "NSImageSymbolConfiguration", None)
    if make is None or config_cls is None:
        return None
    symbol = make("arrow.triangle.pull", "awaitray")
    if symbol is None:
        return None
    config = config_cls.configurationWithPointSize_weight_(260, AppKit.NSFontWeightSemibold)
    white = getattr(config_cls, "configurationWithHierarchicalColor_", None)
    if white is not None:
        config = config.configurationByApplyingConfiguration_(white(AppKit.NSColor.whiteColor()))
    symbol = symbol.imageWithSymbolConfiguration_(config) or symbol
    size = 512
    icon = AppKit.NSImage.alloc().initWithSize_((size, size))
    icon.lockFocus()
    try:
        _hex_to_color("#24292F").setFill()
        # Apple's icon grid: an ~824/1024 rounded square, centered.
        inset = size * 0.1
        rect = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, size * 0.18, size * 0.18
        ).fill()
        w, h = symbol.size()
        symbol.drawInRect_(NSMakeRect((size - w) / 2, (size - h) / 2, w, h))
    finally:
        icon.unlockFocus()
    return icon


# ----- custom editors -----


class _ListEditor:
    def __init__(self, win: PrefsWindow):
        self.win = win
        self.view = _stack(spacing=6)
        self.rows = _stack(spacing=6)
        self.buttons = _stack(_HORIZONTAL, spacing=8)
        self.view.addArrangedSubview_(self.rows)
        self.view.addArrangedSubview_(self.buttons)

    @property
    def cfg(self) -> cfg_mod.Config:
        return self.win.session.working

    def changed(self) -> None:
        self.win.session.changed()

    def rebuild(self) -> None:
        for view in list(self.rows.arrangedSubviews()):
            self.rows.removeArrangedSubview_(view)
            view.removeFromSuperview()
        self.fill()

    def fill(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def add_button(self, title, callback):
        button = _button(title, self.win._target(lambda _b: callback()))
        self.buttons.addArrangedSubview_(button)
        return button

    def remove_button(self, callback):
        button = _button("–", self.win._target(lambda _b: callback()))
        button.setToolTip_("Remove")
        return button

    def switch(self, on: bool, callback):
        switch = AppKit.NSSwitch.alloc().init()
        switch.setState_(1 if on else 0)
        switch.setTarget_(self.win._target(lambda s: callback(bool(s.state()))))
        switch.setAction_("invoke:")
        return switch


class _HostsEditor(_ListEditor):
    def __init__(self, win):
        super().__init__(win)
        self._status: list = []  # one per row: the same host may repeat
        self.add_button("Add host", self._add)
        self.add_button("Check sign-in", self._check_all)
        self.rebuild()
        self._check_all()

    def fill(self) -> None:
        self._status = []
        for i, host in enumerate(self.cfg.hosts):
            row = _stack(_HORIZONTAL, spacing=8)
            row.addArrangedSubview_(
                self.switch(host.enabled, lambda on, i=i: self._set(i, "enabled", on))
            )
            field = AppKit.NSTextField.textFieldWithString_(host.hostname)
            field.setTranslatesAutoresizingMaskIntoConstraints_(False)
            field.widthAnchor().constraintEqualToConstant_(180).setActive_(True)
            field.setDelegate_(self.win._target(lambda t, i=i: self._rename(i, t.stringValue())))
            row.addArrangedSubview_(field)
            user = AppKit.NSTextField.textFieldWithString_(host.user)
            user.setPlaceholderString_("active account")
            user.setToolTip_(
                "A gh account on this host. Empty: whichever is active in gh. "
                "Add the host again for another account."
            )
            user.setTranslatesAutoresizingMaskIntoConstraints_(False)
            user.widthAnchor().constraintEqualToConstant_(120).setActive_(True)
            user.setDelegate_(
                self.win._target(
                    lambda t, i=i: self._set(i, "user", t.stringValue().strip().lstrip("@"))
                )
            )
            row.addArrangedSubview_(user)
            row.addArrangedSubview_(
                _button("Log in…", self.win._target(lambda _b, i=i: self._login(i)))
            )
            row.addArrangedSubview_(self.remove_button(lambda i=i: self._remove(i)))
            if i:
                line = AppKit.NSBox.alloc().init()
                line.setBoxType_(AppKit.NSBoxSeparator)
                self.rows.addArrangedSubview_(line)
            self.rows.addArrangedSubview_(row)
            self.rows.addArrangedSubview_(self._second_line(i, host.browser))

    def _second_line(self, i, browser):
        """The account's browser and its sign-in status. The status
        truncates instead of widening the pane ("Signed in as … (active)")."""
        row = _stack(_HORIZONTAL, spacing=8)
        row.addArrangedSubview_(_label("Browser", secondary=True))
        choices = [browsers.Choice("Default browser", ""), *self.win.browser_choices[1:]]
        box = _browser_combo(
            self.win._target,
            choices,
            browser,
            230,
            lambda b, command, i=i: self._set_browser(i, b, command),
        )
        box.setToolTip_(
            "Opens this account's PRs: pick a browser or one of its profiles, or type "
            "an app name with arguments. Default browser: Settings → Accounts → Browser."
        )
        row.addArrangedSubview_(box)
        status = _shrinkable(_label("…", secondary=True))
        self._status.append(status)
        row.addArrangedSubview_(status)
        return row

    def _set_browser(self, i, box, command):
        error = pm.browser_error(command)
        box.setTextColor_(_error_color(error))
        if error is None:
            self._set(i, "browser", command)

    def _set(self, i, attr, value):
        setattr(self.cfg.hosts[i], attr, value)
        self.changed()

    def _rename(self, i, text):
        if text.strip():
            self._set(i, "hostname", text.strip())

    def _add(self):
        self.cfg.hosts.append(cfg_mod.HostConfig("github.example.com"))
        self.changed()
        self.rebuild()

    def _remove(self, i):
        del self.cfg.hosts[i]
        self.changed()
        self.rebuild()

    def _login(self, i):
        problem = gh.launch_login(self.cfg.hosts[i].hostname)
        if problem:
            self.win.alert("Log in with gh", problem)

    def _check_all(self):
        accounts = [(h.hostname, h.user) for h in self.cfg.hosts]
        labels = list(self._status)

        def work():
            if not gh.gh_available():
                return ["gh CLI not installed"] * len(accounts)
            return [gh.sign_in_status(host, user) for host, user in accounts]

        def done(result):
            for label, text in zip(labels, result, strict=False):
                label.setStringValue_(text)

        _run_in_thread(work, done)


class _SectionsEditor(_ListEditor):
    """Custom menu sections from saved searches."""

    def __init__(self, win):
        super().__init__(win)
        self.add_button("Add section", self._add)
        self.rebuild()

    def _text(self, value, placeholder, width, callback):
        field = AppKit.NSTextField.textFieldWithString_(value)
        field.setPlaceholderString_(placeholder)
        field.setTranslatesAutoresizingMaskIntoConstraints_(False)
        field.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        field.setDelegate_(self.win._target(lambda t: callback(t.stringValue())))
        return field

    def _check(self, title, on, callback):
        box = AppKit.NSButton.checkboxWithTitle_target_action_(
            title, self.win._target(lambda b: callback(bool(b.state()))), "invoke:"
        )
        box.setState_(1 if on else 0)
        return box

    def fill(self) -> None:
        if not self.cfg.sections:
            self.rows.addArrangedSubview_(_label("No custom sections.", secondary=True))
        for i, sec in enumerate(self.cfg.sections):
            if i:
                line = AppKit.NSBox.alloc().init()
                line.setBoxType_(AppKit.NSBoxSeparator)
                self.rows.addArrangedSubview_(line)
            box = _stack(spacing=4)
            top = _stack(_HORIZONTAL, spacing=8)
            top.addArrangedSubview_(
                self.switch(sec.enabled, lambda on, i=i: self._set(i, "enabled", on))
            )
            top.addArrangedSubview_(
                self._text(sec.title, "Title", 220, lambda t, i=i: self._set_text(i, "title", t))
            )
            top.addArrangedSubview_(
                self._text(
                    sec.host, "All hosts", 150, lambda t, i=i: self._set(i, "host", t.strip())
                )
            )
            top.addArrangedSubview_(self.remove_button(lambda i=i: self._remove(i)))
            box.addArrangedSubview_(top)
            box.addArrangedSubview_(
                self._text(
                    sec.query, "mentions:@me", 420, lambda t, i=i: self._set_text(i, "query", t)
                )
            )
            bottom = _stack(_HORIZONTAL, spacing=8)
            bottom.addArrangedSubview_(
                self._check(
                    "Notify new PRs", sec.notify, lambda on, i=i: self._set(i, "notify", on)
                )
            )
            bottom.addArrangedSubview_(
                self._check(
                    "Count in the menu bar", sec.count, lambda on, i=i: self._set(i, "count", on)
                )
            )
            status = _shrinkable(_label("", secondary=True))
            bottom.addArrangedSubview_(
                _button("Test query", self.win._target(lambda _b, i=i, s=status: self._test(i, s)))
            )
            bottom.addArrangedSubview_(status)
            box.addArrangedSubview_(bottom)
            self.rows.addArrangedSubview_(box)

    def _set(self, i, attr, value):
        setattr(self.cfg.sections[i], attr, value)
        self.changed()

    def _set_text(self, i, attr, text):
        if text.strip():  # an empty title or query would be dropped on load
            self._set(i, attr, text.strip())

    def _add(self):
        self.cfg.sections.append(cfg_mod.CustomSection("Mentions", "mentions:@me"))
        self.changed()
        self.rebuild()

    def _remove(self, i):
        del self.cfg.sections[i]
        self.changed()
        self.rebuild()

    def _test(self, i, status):
        cfg, section = self.cfg, copy.deepcopy(self.cfg.sections[i])
        status.setStringValue_("Searching…")
        _run_in_thread(lambda: pr_sync.test_section(cfg, section), status.setStringValue_)


class _ReposEditor(_ListEditor):
    def __init__(self, win):
        super().__init__(win)
        self.add_button("Add repository…", lambda: self._add(False))
        self.add_button("Add folder of repos…", lambda: self._add(True))
        self.rebuild()

    def fill(self) -> None:
        if not self.cfg.repos:
            self.rows.addArrangedSubview_(_label("No repositories yet.", secondary=True))
        for i, repo in enumerate(self.cfg.repos):
            row = _stack(_HORIZONTAL, spacing=8)
            row.addArrangedSubview_(
                self.switch(repo.enabled, lambda on, i=i: self._set(i, "enabled", on))
            )
            text = _stack(spacing=1)
            text.addArrangedSubview_(
                _shrinkable(_label(repo.path), AppKit.NSLineBreakByTruncatingMiddle)
            )
            kind = "folder of repos" if repo.auto_discover else "repository"
            extra = f" · {len(repo.excluded)} excluded" if repo.excluded else ""
            text.addArrangedSubview_(
                _shrinkable(_label(f"{kind} · remote {repo.remote}{extra}", secondary=True))
            )
            row.addArrangedSubview_(text)
            update = AppKit.NSButton.checkboxWithTitle_target_action_(
                "Auto-update",
                self.win._target(lambda b, i=i: self._set(i, "auto_update", bool(b.state()))),
                "invoke:",
            )
            update.setState_(1 if repo.auto_update else 0)
            update.setToolTip_("Fast-forward after fetch when the working tree is clean")
            row.addArrangedSubview_(update)
            if repo.auto_discover:
                row.addArrangedSubview_(
                    _button("Choose…", self.win._target(lambda _b, i=i: self._choose(i)))
                )
            row.addArrangedSubview_(self.remove_button(lambda i=i: self._remove(i)))
            self.rows.addArrangedSubview_(row)

    def _set(self, i, attr, value):
        setattr(self.cfg.repos[i], attr, value)
        self.changed()

    def _pick_folder(self, message):
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setMessage_(message)
        if panel.runModal() != AppKit.NSModalResponseOK:
            return None
        return panel.URLs()[0].path()

    def _add(self, folder):
        path = self._pick_folder(
            "Select a folder containing git repositories" if folder else "Select a git repository"
        )
        if not path:
            return
        if folder:
            from awaitray import repos as repo_mod

            if not repo_mod.discover_repos(path):
                self.win.alert(
                    "No git repositories found", f"No subfolder of {path} is a git repository."
                )
                return
        self.cfg.repos.append(cfg_mod.RepoConfig(path=path, auto_discover=folder))
        self.changed()
        self.rebuild()

    def _choose(self, i):
        from awaitray import repos as repo_mod

        repo = self.cfg.repos[i]
        found = repo_mod.discover_repos(repo.path)
        box = _stack(spacing=4)
        checks = []
        for child in found:
            check = AppKit.NSButton.checkboxWithTitle_target_action_(
                os.path.basename(child), None, None
            )
            check.setState_(0 if child in repo.excluded else 1)
            checks.append((child, check))
            box.addArrangedSubview_(check)
        box.setFrame_(NSMakeRect(0, 0, 320, max(24, 24 * len(found))))
        if self.win.alert("Repos to fetch", f"Subfolders of {repo.path} that are git repos.", box):
            excluded_now = [child for child, check in checks if not check.state()]
            repo.excluded = pm.exclusions_after_choice(repo.excluded, found, excluded_now)
            self.changed()
            self.rebuild()

    def _remove(self, i):
        del self.cfg.repos[i]
        self.changed()
        self.rebuild()


def _system_sounds() -> list[str]:
    try:
        return sorted(os.path.splitext(n)[0] for n in os.listdir("/System/Library/Sounds"))
    except OSError:
        return []


class _BucketsEditor(_ListEditor):
    def __init__(self, win):
        super().__init__(win)
        self._sounds = _system_sounds()
        self.rows.addArrangedSubview_(
            _label(
                "Checked in order; leave the limit empty on the last tier to catch the rest.",
                secondary=True,
            )
        )
        self.add_button("Add tier", self._add)
        self.rebuild()

    def rebuild(self) -> None:
        for view in list(self.rows.arrangedSubviews())[1:]:
            self.rows.removeArrangedSubview_(view)
            view.removeFromSuperview()
        self.fill()

    def fill(self) -> None:
        for i, b in enumerate(self.cfg.tray_icon.buckets):
            row = _stack(_HORIZONTAL, spacing=6)
            row.addArrangedSubview_(_label("Up to"))
            limit = AppKit.NSTextField.textFieldWithString_("" if b.up_to is None else str(b.up_to))
            limit.setPlaceholderString_("∞")
            limit.setTranslatesAutoresizingMaskIntoConstraints_(False)
            limit.widthAnchor().constraintEqualToConstant_(44).setActive_(True)
            limit.setDelegate_(self.win._target(lambda t, i=i: self._limit(i, t.stringValue())))
            emoji = AppKit.NSTextField.textFieldWithString_(b.emoji)
            emoji.setTranslatesAutoresizingMaskIntoConstraints_(False)
            emoji.widthAnchor().constraintEqualToConstant_(40).setActive_(True)
            emoji.setDelegate_(
                self.win._target(lambda t, i=i: self._set(i, "emoji", t.stringValue().strip()))
            )
            picker = _button("😀", self.win._target(lambda _b, e=emoji: self._emoji_palette(e)))
            sound = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 0, 140, 26), False
            )
            sound.addItemsWithTitles_([_SOUND_NONE, *self._sounds, _SOUND_CUSTOM])
            if not b.sound:
                sound.selectItemWithTitle_(_SOUND_NONE)
            elif b.sound in self._sounds:
                sound.selectItemWithTitle_(b.sound)
            else:
                sound.selectItemWithTitle_(_SOUND_CUSTOM)
            sound.setTarget_(
                self.win._target(lambda p, i=i: self._sound(i, p.titleOfSelectedItem()))
            )
            sound.setAction_("invoke:")
            preview = _button(
                "▶",
                self.win._target(
                    lambda _b, i=i: notifier.play_sound(self.cfg.tray_icon.buckets[i].sound)
                ),
            )
            for view in (
                limit,
                emoji,
                picker,
                sound,
                preview,
                self.remove_button(lambda i=i: self._remove(i)),
            ):
                row.addArrangedSubview_(view)
            self.rows.addArrangedSubview_(row)

    def _set(self, i, attr, value):
        setattr(self.cfg.tray_icon.buckets[i], attr, value)
        self.changed()

    def _limit(self, i, text):
        text = text.strip()
        if not text:
            self._set(i, "up_to", None)
        elif text.isdigit():
            self._set(i, "up_to", int(text))

    def _sound(self, i, title):
        if title == _SOUND_NONE:
            self._set(i, "sound", "")
        elif title == _SOUND_CUSTOM:
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(False)
            if panel.runModal() == AppKit.NSModalResponseOK:
                self._set(i, "sound", panel.URLs()[0].path())
        else:
            self._set(i, "sound", title)

    def _emoji_palette(self, field):
        self.win.window.makeFirstResponder_(field)
        AppKit.NSApplication.sharedApplication().orderFrontCharacterPalette_(None)

    def _add(self):
        self.cfg.tray_icon.buckets.append(cfg_mod.Bucket(up_to=10, emoji="🔔"))
        self.changed()
        self.rebuild()

    def _remove(self, i):
        del self.cfg.tray_icon.buckets[i]
        self.changed()
        self.rebuild()


class _BackupPanel:
    def __init__(self, win: PrefsWindow):
        self.win = win
        self.view = _stack(_HORIZONTAL, spacing=8)
        for title, callback in (
            ("Export settings…", self._export),
            ("Import settings…", self._import),
            ("Open snapshots folder", self._open_folder),
        ):
            self.view.addArrangedSubview_(_button(title, win._target(lambda _b, c=callback: c())))

    def _export(self):
        panel = AppKit.NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(backup_mod.default_export_name())
        if panel.runModal() != AppKit.NSModalResponseOK:
            return
        path = panel.URL().path()
        if self.win.session.pending:
            self.win.session.commit()

        def work():
            try:
                backup_mod.export_backup(path)
                return None
            except OSError as e:
                return str(e)

        _run_in_thread(
            work,
            lambda error: self.win.alert(
                "Export failed" if error else "Settings exported",
                error
                or f"{path}\n\nNo tokens are included, but hosts and paths are: keep it private.",
            ),
        )

    def _import(self):
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        if panel.runModal() != AppKit.NSModalResponseOK:
            return
        try:
            backup = backup_mod.read_backup(panel.URLs()[0].path())
        except backup_mod.BackupError as e:
            self.win.alert("Can't import this file", str(e))
            return
        _run_in_thread(
            lambda: backup_mod.plan_import(backup), lambda plan: self._confirm(backup, plan)
        )

    def _confirm(self, backup, plan):
        # Both off by default: a backup may come from someone else, and both
        # would run things here (a clone's files, a command on click).
        clone = commands = None
        boxes = []
        if plan.missing_clones:
            clone = AppKit.NSButton.checkboxWithTitle_target_action_(
                f"Clone the {len(plan.missing_clones)} missing repositories", None, None
            )
            boxes.append(clone)
        if plan.new_commands:
            commands = AppKit.NSButton.checkboxWithTitle_target_action_(
                f"Also import the {len(plan.new_commands)} commands marked with ! "
                "(only for a backup you trust)",
                None,
                None,
            )
            boxes.append(commands)
        for box in boxes:
            box.setState_(0)
        if boxes:
            accessory = AppKit.NSStackView.stackViewWithViews_(boxes)
            accessory.setOrientation_(1)  # vertical
            accessory.setAlignment_(AppKit.NSLayoutAttributeLeading)
            accessory.setFrameSize_(accessory.fittingSize())
        else:
            accessory = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        body = "\n".join(plan.summary_lines()) + "\n\nYour current settings are snapshotted first."
        if not self.win.alert("Replace your settings with this backup?", body, accessory):
            return
        do_clone = bool(clone and clone.state())
        keep = bool(commands and commands.state())
        _run_in_thread(
            lambda: backup_mod.apply_import(
                backup, plan, clone_missing=do_clone, keep_commands=keep
            ),
            self._imported,
        )

    def _imported(self, lines):
        cfg = cfg_mod.load()
        self.win.reload(cfg)
        self.win._on_saved(copy.deepcopy(cfg))
        self.win.alert("Settings imported", "\n".join(lines))

    def _open_folder(self):
        folder = backup_mod.backups_dir()
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(folder)])


class _SoundsFolderPanel:
    """Where your own sound packs go, and a button that opens it."""

    def __init__(self, win: PrefsWindow):
        self.win = win
        folder = sounds.user_dir()
        self.view = _stack(_HORIZONTAL, spacing=8)
        path = _shrinkable(
            _label(str(folder).replace(os.path.expanduser("~"), "~", 1), secondary=True)
        )
        path.setSelectable_(True)
        self.view.addArrangedSubview_(path)
        self.view.addArrangedSubview_(
            _button("Open folder", win._target(lambda _b: self._open(folder)))
        )

    @staticmethod
    def _open(folder):
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(folder)])


class _LogsPanel:
    def __init__(self, win: PrefsWindow):
        self.win = win
        self.view = _stack(spacing=6)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIDTH - 90, 200))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        self._text = AppKit.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIDTH - 90, 200))
        self._text.setEditable_(False)
        self._text.setFont_(AppKit.NSFont.userFixedPitchFontOfSize_(11))
        scroll.setDocumentView_(self._text)
        scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
        scroll.heightAnchor().constraintEqualToConstant_(200).setActive_(True)
        scroll.widthAnchor().constraintEqualToConstant_(_WIDTH - 90).setActive_(True)
        self.view.addArrangedSubview_(scroll)
        buttons = _stack(_HORIZONTAL, spacing=8)
        buttons.addArrangedSubview_(_button("Refresh", win._target(lambda _b: self.refresh())))
        buttons.addArrangedSubview_(_button("Clear", win._target(lambda _b: self._clear())))
        self.view.addArrangedSubview_(buttons)
        self.refresh()

    def refresh(self):
        self._text.setString_(
            "\n".join(e.render() for e in log_mod.snapshot()) or "(no events yet)"
        )

    def _clear(self):
        log_mod.clear()
        self.refresh()


# ----- entry point -----

_current: PrefsWindow | None = None


def open_window(ctl, on_saved):
    """Open (or focus) the single Settings window."""
    global _current
    if _current is None:
        _current = PrefsWindow(ctl.cfg, on_saved, live=lambda: ctl.cfg)
    _current.present()
