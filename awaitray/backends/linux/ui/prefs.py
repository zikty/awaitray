"""GTK 3 Preferences window rendered from `prefs_model` (docs/ui-spec.md).

A sidebar with the six sections; each section is a list of groups, each
group a framed list of rows (label and help on the left, control on the
right). Simple fields are built generically from the model; hosts, repos,
tray-icon tiers, backup and logs have their own editors below.

Changes apply as you go (no Save button): every edit goes to the
`EditSession` working copy, which commits after a short debounce. Nothing
here runs `gh` or `git` on the main thread.
"""

from __future__ import annotations

import copy
import os
import subprocess
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from awaitray import backup as backup_mod
from awaitray import browsers, pr_sync, sounds
from awaitray import config as cfg_mod
from awaitray import github as gh
from awaitray import log as log_mod
from awaitray import prefs_model as pm
from awaitray.backends.linux import notifier

# SF Symbol names in the model → freedesktop icon names.
_ICONS = {
    "person.crop.circle": "avatar-default-symbolic",
    "arrow.triangle.pull": "emblem-shared-symbolic",
    "bell": "preferences-system-notifications-symbolic",
    "folder": "folder-symbolic",
    "paintpalette": "applications-graphics-symbolic",
    "gearshape": "emblem-system-symbolic",
}


def _rgba(hex_color: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if not rgba.parse(hex_color or "#5B9BD5"):
        rgba.parse("#5B9BD5")
    return rgba


def _hex(rgba: Gdk.RGBA) -> str:
    r, g, b = (max(0, min(255, int(c * 255))) for c in (rgba.red, rgba.green, rgba.blue))
    return f"#{r:02X}{g:02X}{b:02X}"


def browser_combo(
    choices: list[browsers.Choice], value: str, on_change: Callable[[Gtk.Entry, str], None]
) -> Gtk.ComboBoxText:
    """The installed browsers and their profiles, by name, with the typed
    command as a fallback. `on_change(entry, command)` gets what to
    store: a picked label becomes its command."""
    combo = Gtk.ComboBoxText.new_with_entry()
    for choice in choices:
        combo.append_text(choice.label)
    entry = combo.get_child()
    entry.set_text(browsers.label_for(value, choices))
    entry.set_width_chars(30)
    combo.connect(
        "changed",
        lambda w: on_change(entry, browsers.command_for(w.get_active_text() or "", choices)),
    )
    return combo


def _mark(entry: Gtk.Entry, error: str | None) -> None:
    """A warning icon (with the error as its tooltip) at the entry's end."""
    entry.set_icon_from_icon_name(
        Gtk.EntryIconPosition.SECONDARY, "dialog-warning-symbolic" if error else None
    )
    entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, error)


def _dim(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True)
    label.set_max_width_chars(60)
    label.get_style_context().add_class("dim-label")
    return label


def _run_in_thread(work: Callable, done: Callable) -> None:
    """Run `work()` off the main thread, then `done(result)` on it."""

    def target():
        result = work()
        GLib.idle_add(lambda: done(result) and False)

    threading.Thread(target=target, daemon=True).start()


class PreferencesWindow(Gtk.Window):
    def __init__(
        self,
        cfg: cfg_mod.Config,
        on_saved: Callable[[cfg_mod.Config], object],
        section: str | None = None,
        live: Callable[[], cfg_mod.Config] | None = None,
    ):
        super().__init__(title="awaitray Preferences")
        self.set_default_size(780, 580)
        self._on_saved = on_saved
        # Found once per window: a few plist/.desktop reads, no subprocess.
        self.browser_choices = browsers.installed()
        self.session = pm.EditSession(
            cfg,
            self._committed,
            schedule=lambda ms, fn: GLib.timeout_add(ms, fn),
            cancel=GLib.source_remove,
            live=live,
        )
        self._rows: list[tuple[pm.Field, Gtk.Widget]] = []  # for depends_on sensitivity

        header = Gtk.HeaderBar(title="Preferences")
        header.set_show_close_button(True)
        self.set_titlebar(header)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self.stack)
        sidebar.set_size_request(180, -1)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.pack_start(sidebar, False, False, 0)
        body.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        body.pack_start(self.stack, True, True, 0)
        self.add(body)

        self._build_pages()
        if section:
            self.stack.set_visible_child_name(section)
        self.connect("delete-event", self._on_close)

    # ----- pages -----

    def _build_pages(self) -> None:
        current = self.stack.get_visible_child_name()
        for child in self.stack.get_children():
            self.stack.remove(child)
        self._rows = []
        for s in pm.SECTIONS:
            self.stack.add_titled(self._page(s), s.id, s.title)
            self.stack.child_set_property(
                self.stack.get_child_by_name(s.id),
                "icon-name",
                _ICONS.get(s.icon, "emblem-system-symbolic"),
            )
        self.stack.show_all()
        if current:
            self.stack.set_visible_child_name(current)
        self._refresh_sensitivity()

    def _page(self, section: pm.Section) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_border_width(24)
        for group in section.groups:
            box.pack_start(self._group(group), False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(box)
        return scroller

    def _group(self, group: pm.Group) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(group.title)}</b>")
        box.pack_start(title, False, False, 0)
        if group.help:
            box.pack_start(_dim(group.help), False, False, 0)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        for f in group.fields:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.add(self._field(f))
            listbox.add(row)
            if f.depends_on:
                self._rows.append((f, row))
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.add(listbox)
        box.pack_start(frame, False, False, 0)
        return box

    # ----- generic fields -----

    def _field(self, f: pm.Field) -> Gtk.Widget:
        if f.kind == pm.CUSTOM:
            editor = {
                pm.HOSTS: HostsEditor,
                pm.REPOS: ReposEditor,
                pm.BUCKETS: BucketsEditor,
                pm.SECTIONS_EDITOR: SectionsEditor,
                pm.BACKUP: BackupPanel,
                pm.LOGS: LogsPanel,
                pm.SOUNDS_FOLDER: SoundsFolderPanel,
            }[f.custom](self)
            editor.set_border_width(8)
            return editor

        value = pm.get_value(self.session.working, f.key)
        error = Gtk.Label(xalign=0)
        error.set_no_show_all(True)

        def edit(new_value):
            message = self.session.edit(f, new_value)
            if message:
                error.set_markup(
                    f'<span foreground="#c01c28">{GLib.markup_escape_text(message)}</span>'
                )
                error.show()
            else:
                error.hide()
            self._refresh_sensitivity()

        if f.kind == pm.LINES:
            view = Gtk.TextView()
            view.set_monospace(True)
            view.get_buffer().set_text("\n".join(value))
            view.get_buffer().connect(
                "changed",
                lambda buf: edit(
                    pm.parse_lines(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
                ),
            )
            scroller = Gtk.ScrolledWindow()
            scroller.set_min_content_height(72)
            scroller.set_shadow_type(Gtk.ShadowType.IN)
            scroller.add(view)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_border_width(10)
            box.pack_start(Gtk.Label(label=f.label, xalign=0), False, False, 0)
            if f.help:
                box.pack_start(_dim(f.help), False, False, 0)
            box.pack_start(scroller, False, False, 0)
            box.pack_start(error, False, False, 0)
            return box

        control = self._control(f, value, edit)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.pack_start(Gtk.Label(label=f.label, xalign=0), False, False, 0)
        if f.help:
            left.pack_start(_dim(f.help), False, False, 0)
        left.pack_start(error, False, False, 0)
        left.set_valign(Gtk.Align.CENTER)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_border_width(10)
        row.pack_start(left, True, True, 0)
        control.set_valign(Gtk.Align.CENTER)
        row.pack_end(control, False, False, 0)
        return row

    def _control(self, f: pm.Field, value, edit) -> Gtk.Widget:
        if f.kind == pm.BOOL:
            switch = Gtk.Switch(active=bool(value))
            switch.connect("notify::active", lambda w, _p: edit(w.get_active()))
            return switch
        if f.kind == pm.INT:
            spin = Gtk.SpinButton.new_with_range(f.minimum or 0, f.maximum or 100000, f.step)
            spin.set_value(value)
            spin.connect("value-changed", lambda w: edit(w.get_value_as_int()))
            return spin
        if f.kind in (pm.TEXT, pm.TIME):
            entry = Gtk.Entry(text=value or "")
            entry.set_placeholder_text(f.placeholder)
            entry.set_width_chars(8 if f.kind == pm.TIME else 22)
            entry.connect("changed", lambda w: edit(w.get_text()))
            return entry
        if f.kind == pm.BROWSER:

            def picked(entry: Gtk.Entry, command: str) -> None:
                _mark(entry, pm.browser_error(command))
                edit(command)

            return browser_combo(self.browser_choices, value or "", picked)
        if f.kind == pm.SOUND:
            return self._sound_combo(f, value or "", edit)
        if f.kind == pm.CHOICE:
            combo = Gtk.ComboBoxText()
            for v, label in pm.choices_for(f):
                combo.append(v, label)
            combo.set_active_id(value)

            def chosen(w: Gtk.ComboBoxText) -> None:
                edit(w.get_active_id())
                notifier.play_sound(
                    pm.preview_sound(f, w.get_active_id(), self.session.working) or ""
                )

            combo.connect("changed", chosen)
            return combo
        if f.kind == pm.COLOR:
            box = Gtk.Box(spacing=8)
            switch = Gtk.Switch(active=bool(value))
            picker = Gtk.ColorButton.new_with_rgba(_rgba(value))
            picker.set_sensitive(bool(value))

            def on_switch(w, _p):
                picker.set_sensitive(w.get_active())
                edit(_hex(picker.get_rgba()) if w.get_active() else "")

            switch.connect("notify::active", on_switch)
            picker.connect("color-set", lambda w: edit(_hex(w.get_rgba())))
            box.pack_start(picker, False, False, 0)
            box.pack_start(switch, False, False, 0)
            return box
        raise ValueError(f"unknown field kind {f.kind}")

    def _sound_combo(self, f: pm.Field, value: str, edit) -> Gtk.ComboBoxText:
        """An event's sound: the pack's, an effect, silent, or a file you
        choose ("Choose a file…" opens a picker). Picking one plays it."""
        combo = Gtk.ComboBoxText()
        ids = {}  # combo id -> stored value ("" can't be a GTK id)
        for i, (v, label) in enumerate(pm.sound_choices()):
            ids[f"c{i}"] = v
            combo.append(f"c{i}", label)
        if os.path.isabs(value):
            ids["file"] = value
            combo.append("file", os.path.basename(value))
        combo.append("choose", "Choose a file…")
        combo.set_active_id(next((k for k, v in ids.items() if v == value), "c0"))
        state = {"current": combo.get_active_id()}

        def changed(w: Gtk.ComboBoxText) -> None:
            picked = w.get_active_id()
            if picked == state["current"]:
                return
            if picked == "choose":
                path = self.pick_path("Choose a sound", Gtk.FileChooserAction.OPEN)
                if not path:
                    w.set_active_id(state["current"])  # cancelled: back to before
                    return
                if "file" in ids:
                    w.remove(list(ids).index("file"))
                ids["file"] = path
                w.insert(len(ids) - 1, "file", os.path.basename(path))
                picked = "file"
                state["current"] = picked  # first: set_active_id re-enters
                w.set_active_id("file")
            state["current"] = picked
            edit(ids[picked])
            notifier.play_sound(pm.preview_sound(f, ids[picked], self.session.working) or "")

        combo.connect("changed", changed)
        return combo

    def _refresh_sensitivity(self) -> None:
        for f, row in self._rows:
            row.set_sensitive(pm.is_active(self.session.working, f))

    # ----- session plumbing -----

    def _committed(self, new_cfg: cfg_mod.Config) -> None:
        self._on_saved(new_cfg)

    def reload(self, cfg: cfg_mod.Config) -> None:
        """Start over from `cfg` (after an import) and redraw every page."""
        self.session.reset(cfg)
        self._build_pages()

    def _on_close(self, *_args) -> bool:
        if self.session.pending:
            self.session.commit()
        return False

    def show_message(self, title: str, body: str, kind=Gtk.MessageType.INFO) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=kind,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(body)
        dlg.run()
        dlg.destroy()

    def pick_path(self, title: str, action: Gtk.FileChooserAction, name: str = "") -> str | None:
        dialog = Gtk.FileChooserDialog(title=title, parent=self, action=action)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        if name:
            dialog.set_current_name(name)
            dialog.set_do_overwrite_confirmation(True)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        return path


# ----- custom editors -----


class _ListEditor(Gtk.Box):
    """Rows rebuilt from the working copy, plus a row of action buttons."""

    def __init__(self, win: PreferencesWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.win = win
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.pack_start(self.rows_box, False, False, 0)
        self.buttons = Gtk.Box(spacing=6)
        self.pack_start(self.buttons, False, False, 0)

    @property
    def cfg(self) -> cfg_mod.Config:
        return self.win.session.working

    def changed(self) -> None:
        self.win.session.changed()

    def rebuild(self) -> None:
        for child in self.rows_box.get_children():
            self.rows_box.remove(child)
        self.fill()
        self.rows_box.show_all()

    def fill(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def add_button(self, label: str, callback) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda *_: callback())
        self.buttons.pack_start(button, False, False, 0)
        return button

    @staticmethod
    def remove_button(callback) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
        button.set_tooltip_text("Remove")
        button.connect("clicked", lambda *_: callback())
        return button


class HostsEditor(_ListEditor):
    def __init__(self, win):
        super().__init__(win)
        self._status: list[Gtk.Label] = []  # one per row: the same host may repeat
        self.add_button("Add host", self._add)
        self.add_button("Check sign-in", self._check_all)
        self.rebuild()
        self._check_all()

    def fill(self) -> None:
        self._status = []
        for i, host in enumerate(self.cfg.hosts):
            row = Gtk.Box(spacing=8)
            entry = Gtk.Entry(text=host.hostname)
            entry.set_width_chars(20)
            entry.connect("changed", lambda w, i=i: self._rename(i, w.get_text()))
            user = Gtk.Entry(text=host.user, placeholder_text="active account")
            user.set_width_chars(14)
            user.set_tooltip_text(
                "A gh account on this host. Empty: whichever is active in gh. "
                "Add the host again for another account."
            )
            user.connect("changed", lambda w, i=i: self._set_user(i, w.get_text()))
            switch = Gtk.Switch(active=host.enabled)
            switch.set_tooltip_text("Enabled")
            switch.connect("notify::active", lambda w, _p, i=i: self._toggle(i, w.get_active()))
            status = _dim("…")
            self._status.append(status)
            login = Gtk.Button(label="Log in…")
            login.connect("clicked", lambda *_, i=i: self._login(i))
            row.pack_start(switch, False, False, 0)
            row.pack_start(entry, False, False, 0)
            row.pack_start(user, False, False, 0)
            row.pack_start(status, True, True, 0)
            row.pack_end(self.remove_button(lambda i=i: self._remove(i)), False, False, 0)
            row.pack_end(login, False, False, 0)
            if i:
                self.rows_box.pack_start(Gtk.Separator(), False, False, 2)
            self.rows_box.pack_start(row, False, False, 0)
            self.rows_box.pack_start(self._browser_row(i, host.browser), False, False, 0)

    def _browser_row(self, i: int, value: str) -> Gtk.Box:
        """This account's browser, under its row."""
        row = Gtk.Box(spacing=8)
        choices = [browsers.Choice("Default browser", ""), *self.win.browser_choices[1:]]
        combo = browser_combo(
            choices, value, lambda entry, command, i=i: self._set_browser(i, entry, command)
        )
        combo.set_tooltip_text(
            "Opens this account's PRs: pick a browser or one of its profiles, or type a "
            "command. Default browser: Preferences → Accounts → Browser."
        )
        row.pack_start(_dim("Browser"), False, False, 0)
        row.pack_start(combo, False, False, 0)
        return row

    def _set_browser(self, i: int, entry: Gtk.Entry, command: str) -> None:
        error = pm.browser_error(command)
        _mark(entry, error)
        if error is None:
            self.cfg.hosts[i].browser = command
            self.changed()

    def _rename(self, i: int, text: str) -> None:
        if text.strip():
            self.cfg.hosts[i].hostname = text.strip()
            self.changed()

    def _toggle(self, i: int, on: bool) -> None:
        self.cfg.hosts[i].enabled = on
        self.changed()

    def _set_user(self, i: int, text: str) -> None:
        self.cfg.hosts[i].user = text.strip().lstrip("@")
        self.changed()

    def _add(self) -> None:
        self.cfg.hosts.append(cfg_mod.HostConfig("github.example.com"))
        self.changed()
        self.rebuild()

    def _remove(self, i: int) -> None:
        del self.cfg.hosts[i]
        self.changed()
        self.rebuild()

    def _login(self, i: int) -> None:
        problem = gh.launch_login(self.cfg.hosts[i].hostname)
        if problem:
            self.win.show_message("Log in with gh", problem, Gtk.MessageType.WARNING)

    def _check_all(self) -> None:
        accounts = [(h.hostname, h.user) for h in self.cfg.hosts]
        labels = list(self._status)

        def work():
            if not gh.gh_available():
                return ["gh CLI not installed"] * len(accounts)
            return [gh.sign_in_status(host, user) for host, user in accounts]

        def done(result):
            for label, text in zip(labels, result, strict=False):
                label.set_text(text)

        _run_in_thread(work, done)


class SectionsEditor(_ListEditor):
    """Custom menu sections from saved searches."""

    def __init__(self, win):
        super().__init__(win)
        self.add_button("Add section", self._add)
        self.rebuild()

    def fill(self) -> None:
        if not self.cfg.sections:
            self.rows_box.pack_start(_dim("No custom sections."), False, False, 0)
        for i, sec in enumerate(self.cfg.sections):
            if i:
                self.rows_box.pack_start(Gtk.Separator(), False, False, 4)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            top = Gtk.Box(spacing=8)
            enabled = Gtk.Switch(active=sec.enabled)
            enabled.set_tooltip_text("Show this section")
            enabled.connect(
                "notify::active", lambda w, _p, i=i: self._set(i, "enabled", w.get_active())
            )
            title = Gtk.Entry(text=sec.title, placeholder_text="Title")
            title.connect("changed", lambda w, i=i: self._set_text(i, "title", w.get_text()))
            host = Gtk.Entry(text=sec.host, placeholder_text="All hosts")
            host.set_width_chars(16)
            host.connect("changed", lambda w, i=i: self._set(i, "host", w.get_text().strip()))
            top.pack_start(enabled, False, False, 0)
            top.pack_start(title, True, True, 0)
            top.pack_start(host, False, False, 0)
            top.pack_end(self.remove_button(lambda i=i: self._remove(i)), False, False, 0)

            query = Gtk.Entry(text=sec.query, placeholder_text="mentions:@me")
            query.connect("changed", lambda w, i=i: self._set_text(i, "query", w.get_text()))

            bottom = Gtk.Box(spacing=8)
            notify = Gtk.CheckButton(label="Notify new PRs", active=sec.notify)
            notify.connect("toggled", lambda w, i=i: self._set(i, "notify", w.get_active()))
            count = Gtk.CheckButton(label="Count in the menu bar", active=sec.count)
            count.connect("toggled", lambda w, i=i: self._set(i, "count", w.get_active()))
            status = _dim("")
            test = Gtk.Button(label="Test query")
            test.connect("clicked", lambda *_, i=i, status=status: self._test(i, status))
            bottom.pack_start(notify, False, False, 0)
            bottom.pack_start(count, False, False, 0)
            bottom.pack_start(test, False, False, 0)
            bottom.pack_start(status, True, True, 0)

            for widget in (top, query, bottom):
                box.pack_start(widget, False, False, 0)
            self.rows_box.pack_start(box, False, False, 0)

    def _set(self, i: int, attr: str, value) -> None:
        setattr(self.cfg.sections[i], attr, value)
        self.changed()

    def _set_text(self, i: int, attr: str, text: str) -> None:
        if text.strip():  # an empty title or query would be dropped on load
            self._set(i, attr, text.strip())

    def _add(self) -> None:
        self.cfg.sections.append(cfg_mod.CustomSection("Mentions", "mentions:@me"))
        self.changed()
        self.rebuild()

    def _remove(self, i: int) -> None:
        del self.cfg.sections[i]
        self.changed()
        self.rebuild()

    def _test(self, i: int, status: Gtk.Label) -> None:
        cfg, section = self.cfg, copy.deepcopy(self.cfg.sections[i])
        status.set_text("Searching…")
        _run_in_thread(lambda: pr_sync.test_section(cfg, section), status.set_text)


class ReposEditor(_ListEditor):
    def __init__(self, win):
        super().__init__(win)
        self.add_button("Add repository…", lambda: self._add(False))
        self.add_button("Add folder of repos…", lambda: self._add(True))
        self.rebuild()

    def fill(self) -> None:
        if not self.cfg.repos:
            self.rows_box.pack_start(_dim("No repositories yet."), False, False, 0)
        for i, repo in enumerate(self.cfg.repos):
            row = Gtk.Box(spacing=8)
            enabled = Gtk.Switch(active=repo.enabled)
            enabled.set_tooltip_text("Fetch this")
            enabled.connect(
                "notify::active", lambda w, _p, i=i: self._set(i, "enabled", w.get_active())
            )
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            text.pack_start(Gtk.Label(label=repo.path, xalign=0, ellipsize=3), False, False, 0)
            kind = "folder of repos" if repo.auto_discover else "repository"
            extra = f" · {len(repo.excluded)} excluded" if repo.excluded else ""
            text.pack_start(_dim(f"{kind} · remote {repo.remote}{extra}"), False, False, 0)
            update = Gtk.CheckButton(label="Auto-update")
            update.set_tooltip_text("Fast-forward after fetch when the working tree is clean")
            update.set_active(repo.auto_update)
            update.connect("toggled", lambda w, i=i: self._set(i, "auto_update", w.get_active()))
            row.pack_start(enabled, False, False, 0)
            row.pack_start(text, True, True, 0)
            row.pack_end(self.remove_button(lambda i=i: self._remove(i)), False, False, 0)
            row.pack_end(update, False, False, 0)
            if repo.auto_discover:
                choose = Gtk.Button(label="Choose…")
                choose.set_tooltip_text("Pick which repos in this folder to fetch")
                choose.connect("clicked", lambda *_, i=i: self._choose(i))
                row.pack_end(choose, False, False, 0)
            self.rows_box.pack_start(row, False, False, 0)

    def _set(self, i: int, attr: str, value) -> None:
        setattr(self.cfg.repos[i], attr, value)
        self.changed()

    def _choose(self, i: int) -> None:
        from awaitray import repos as repo_mod

        repo = self.cfg.repos[i]
        found = repo_mod.discover_repos(repo.path)
        dlg = Gtk.Dialog(title="Repos to fetch", transient_for=self.win, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(6)
        area.set_border_width(12)
        area.pack_start(_dim(f"Subfolders of {repo.path} that are git repos."), False, False, 0)
        checks = []
        for child in found:
            check = Gtk.CheckButton(label=child.rsplit("/", 1)[-1])
            check.set_active(child not in repo.excluded)
            checks.append((child, check))
            area.pack_start(check, False, False, 0)
        if not found:
            area.pack_start(_dim("(none found)"), False, False, 0)
        dlg.show_all()
        ok = dlg.run() == Gtk.ResponseType.OK
        excluded_now = [child for child, check in checks if not check.get_active()]
        dlg.destroy()
        if ok:
            self.cfg.repos[i].excluded = pm.exclusions_after_choice(
                repo.excluded, found, excluded_now
            )
            self.changed()
            self.rebuild()

    def _add(self, folder: bool) -> None:
        title = (
            "Select a folder containing git repositories" if folder else "Select a git repository"
        )
        path = self.win.pick_path(title, Gtk.FileChooserAction.SELECT_FOLDER)
        if not path:
            return
        if folder:
            from awaitray import repos as repo_mod

            if not repo_mod.discover_repos(path):
                self.win.show_message(
                    "No git repositories found",
                    f"No subfolder of {path} is a git repository.",
                    Gtk.MessageType.WARNING,
                )
                return
        self.cfg.repos.append(cfg_mod.RepoConfig(path=path, auto_discover=folder))
        self.changed()
        self.rebuild()

    def _remove(self, i: int) -> None:
        del self.cfg.repos[i]
        self.changed()
        self.rebuild()


class BucketsEditor(_ListEditor):
    def __init__(self, win):
        super().__init__(win)
        self.add_button("Add tier", self._add)
        self.rows_box.pack_start(
            _dim("Tiers are checked in order; the last one with no limit catches the rest."),
            False,
            False,
            0,
        )
        self.rebuild()

    def fill(self) -> None:
        for i, b in enumerate(self.cfg.tray_icon.buckets):
            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label="Up to"), False, False, 0)
            spin = Gtk.SpinButton.new_with_range(0, 999, 1)
            spin.set_value(b.up_to if b.up_to is not None else 0)
            spin.set_sensitive(b.up_to is not None)
            spin.connect(
                "value-changed", lambda w, i=i: self._set(i, "up_to", w.get_value_as_int())
            )
            more = Gtk.CheckButton(label="no limit")
            more.set_active(b.up_to is None)
            more.connect("toggled", lambda w, i=i, s=spin: self._no_limit(i, w.get_active(), s))
            emoji = Gtk.Entry(text=b.emoji)
            emoji.set_width_chars(4)
            emoji.set_tooltip_text("Emoji shown for this tier")
            emoji.connect("changed", lambda w, i=i: self._set(i, "emoji", w.get_text().strip()))
            sound = Gtk.Entry(text=b.sound)
            sound.set_placeholder_text("sound file (optional)")
            sound.connect("changed", lambda w, i=i: self._set(i, "sound", w.get_text().strip()))
            for widget in (spin, more, emoji):
                row.pack_start(widget, False, False, 0)
            row.pack_start(sound, True, True, 0)
            row.pack_end(self.remove_button(lambda i=i: self._remove(i)), False, False, 0)
            self.rows_box.pack_start(row, False, False, 0)

    def _set(self, i: int, attr: str, value) -> None:
        setattr(self.cfg.tray_icon.buckets[i], attr, value)
        self.changed()

    def _no_limit(self, i: int, on: bool, spin: Gtk.SpinButton) -> None:
        spin.set_sensitive(not on)
        self._set(i, "up_to", None if on else spin.get_value_as_int())

    def _add(self) -> None:
        self.cfg.tray_icon.buckets.append(cfg_mod.Bucket(up_to=10, emoji="🔔"))
        self.changed()
        self.rebuild()

    def _remove(self, i: int) -> None:
        del self.cfg.tray_icon.buckets[i]
        self.changed()
        self.rebuild()


class BackupPanel(Gtk.Box):
    def __init__(self, win: PreferencesWindow):
        super().__init__(spacing=6)
        self.win = win
        for label, callback in (
            ("Export settings…", self._export),
            ("Import settings…", self._import),
            ("Open snapshots folder", self._open_folder),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda *_, c=callback: c())
            self.pack_start(button, False, False, 0)

    def _export(self) -> None:
        path = self.win.pick_path(
            "Export settings", Gtk.FileChooserAction.SAVE, name=backup_mod.default_export_name()
        )
        if not path:
            return
        if self.win.session.pending:
            self.win.session.commit()  # export what's on screen

        def work():
            try:
                backup_mod.export_backup(path)
                return None
            except OSError as e:
                return str(e)

        def done(error):
            if error:
                self.win.show_message("Export failed", error, Gtk.MessageType.ERROR)
            else:
                self.win.show_message(
                    "Settings exported",
                    f"{path}\n\nNo tokens are included, but hosts and paths are: keep it private.",
                )

        _run_in_thread(work, done)

    def _import(self) -> None:
        path = self.win.pick_path("Import settings", Gtk.FileChooserAction.OPEN)
        if not path:
            return
        try:
            backup = backup_mod.read_backup(path)
        except backup_mod.BackupError as e:
            self.win.show_message("Can't import this file", str(e), Gtk.MessageType.ERROR)
            return
        _run_in_thread(
            lambda: backup_mod.plan_import(backup), lambda plan: self._confirm(backup, plan)
        )

    def _confirm(self, backup, plan) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=self.win,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Replace your settings with this backup?",
        )
        dlg.format_secondary_text(
            "\n".join(plan.summary_lines()) + "\n\nYour current settings are snapshotted first."
        )
        # Both off by default: a backup may come from someone else, and both
        # would run things here (a clone's files, a command on click).
        clone = Gtk.CheckButton(label=f"Clone the {len(plan.missing_clones)} missing repositories")
        commands = Gtk.CheckButton(
            label=f"Also import the {len(plan.new_commands)} commands marked with ! "
            "(only for a backup you trust)"
        )
        for box, wanted in ((clone, plan.missing_clones), (commands, plan.new_commands)):
            box.set_active(False)
            if wanted:
                dlg.get_message_area().pack_start(box, False, False, 0)
                box.show()
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if not ok:
            return
        do_clone, keep = clone.get_active(), commands.get_active()
        _run_in_thread(
            lambda: backup_mod.apply_import(
                backup, plan, clone_missing=do_clone, keep_commands=keep
            ),
            self._imported,
        )

    def _imported(self, lines) -> None:
        cfg = cfg_mod.load()
        self.win.reload(cfg)
        self.win._on_saved(copy.deepcopy(cfg))
        self.win.show_message("Settings imported", "\n".join(lines))

    def _open_folder(self) -> None:
        folder = backup_mod.backups_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(folder)])
        except OSError as e:
            self.win.show_message(
                "Can't open the folder", f"{folder}\n{e}", Gtk.MessageType.WARNING
            )


class SoundsFolderPanel(Gtk.Box):
    """Where your own sound packs go, and a button that opens it."""

    def __init__(self, win: PreferencesWindow):
        super().__init__(spacing=8)
        folder = sounds.user_dir()
        label = _dim(str(folder).replace(os.path.expanduser("~"), "~", 1))
        label.set_selectable(True)
        button = Gtk.Button(label="Open folder")
        button.connect("clicked", lambda *_: self._open(folder))
        self.pack_start(label, True, True, 0)
        self.pack_end(button, False, False, 0)

    @staticmethod
    def _open(folder) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)])


class LogsPanel(Gtk.Box):
    def __init__(self, win: PreferencesWindow):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        view = Gtk.TextView(editable=False, cursor_visible=False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view = view
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(200)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(view)
        self.pack_start(scroller, True, True, 0)
        buttons = Gtk.Box(spacing=6)
        for label, callback in (("Refresh", self.refresh), ("Clear", self._clear)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda *_, c=callback: c())
            buttons.pack_start(button, False, False, 0)
        self.pack_start(buttons, False, False, 0)
        self.refresh()

    def refresh(self) -> None:
        text = "\n".join(e.render() for e in log_mod.snapshot()) or "(no events yet)"
        self._view.get_buffer().set_text(text)

    def _clear(self) -> None:
        log_mod.clear()
        self.refresh()
