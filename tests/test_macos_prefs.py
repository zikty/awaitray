"""macOS Settings window built with real AppKit objects (macOS CI job)."""

import sys
import unittest
from unittest import mock

from awaitray import browsers
from awaitray import config as cfg_mod
from awaitray import prefs_model as pm

_BROWSERS = (
    browsers.SYSTEM,
    browsers.Choice("Google Chrome", "Google Chrome"),
    browsers.Choice("Google Chrome — Trabajo", "Google Chrome --profile-directory='Profile 2'"),
)


@unittest.skipUnless(sys.platform == "darwin", "macOS backend")
class MacPrefsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from awaitray.backends.macos.ui import prefs_window
        except Exception as e:  # pragma: no cover
            raise unittest.SkipTest(f"PyObjC unavailable: {e}") from e
        cls.pw = prefs_window

    def setUp(self):
        patches = (
            (cfg_mod, "save", None),
            (self.pw.gh, "gh_available", False),
            (self.pw.browsers, "installed", list(_BROWSERS)),
        )
        for target, attr, value in patches:
            p = mock.patch.object(target, attr, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.saved = []
        self.cfg = cfg_mod.Config(
            hosts=[cfg_mod.HostConfig("github.com")],
            repos=[cfg_mod.RepoConfig("/code", auto_discover=True, excluded=["/code/old"])],
        )
        self.win = self.pw.PrefsWindow(self.cfg, self.saved.append)
        self.addCleanup(self.win.window.close)

    def _click(self, control):
        control.target().invoke_(control)

    def test_six_toolbar_panes(self):
        items = self.win.tabs.tabViewItems()
        self.assertEqual([i.label() for i in items], [s.title for s in pm.SECTIONS])
        self.assertEqual(self.win.tabs.tabStyle(), 2)  # toolbar

    def test_switch_edits_the_working_copy_and_commits(self):
        switch = self.win.controls["my_prs.enabled"]
        switch.setState_(1)
        self._click(switch)
        self.assertTrue(self.win.session.working.my_prs.enabled)
        self.assertFalse(self.cfg.my_prs.enabled)
        self.assertTrue(self.win.session.pending)
        self.win.session.commit()
        self.assertTrue(self.saved[-1].my_prs.enabled)

    def test_choice_and_int(self):
        popup = self.win.controls["error_policy.on_error"]
        popup.selectItemAtIndex_(1)
        self._click(popup)
        self.assertEqual(self.win.session.working.error_policy.on_error, "retry")
        box = self.win.controls["intervals.fetch_minutes"]
        stepper = box.arrangedSubviews()[1]
        stepper.setIntegerValue_(42)
        self._click(stepper)
        self.assertEqual(self.win.session.working.intervals.fetch_minutes, 42)

    def test_repo_edits_keep_exclusions(self):
        editor = self.win.controls["repos"]
        row = editor.rows.arrangedSubviews()[0]
        checkbox = next(
            v for v in row.arrangedSubviews() if hasattr(v, "title") and v.title() == "Auto-update"
        )
        checkbox.setState_(1)
        self._click(checkbox)
        self.win.session.commit()
        self.assertTrue(self.saved[-1].repos[0].auto_update)
        self.assertEqual(self.saved[-1].repos[0].excluded, ["/code/old"])

    def test_custom_sections_editor(self):
        editor = self.win.controls["sections"]
        editor._add()
        box = editor.rows.arrangedSubviews()[0]
        _top, query, bottom = box.arrangedSubviews()
        query.setStringValue_("repo:acme/core label:urgent")
        query.delegate()._callback(query)  # what controlTextDidChange: forwards
        count = next(v for v in bottom.arrangedSubviews() if v.title() == "Count in the menu bar")
        count.setState_(1)
        self._click(count)
        self.win.session.commit()
        (section,) = self.saved[-1].sections
        self.assertEqual((section.query, section.count), ("repo:acme/core label:urgent", True))

    def test_dependent_rows_dim(self):
        row = next(r for f, r in self.win._dependents if f.key == "quiet_hours.start")
        self.assertLess(row.alphaValue(), 1.0)

    def test_reload_after_import(self):
        self.win.reload(cfg_mod.Config(hosts=[cfg_mod.HostConfig("ghe.corp")]))
        self.assertEqual(self.win.session.working.hosts[0].hostname, "ghe.corp")
        self.assertEqual(len(self.win.tabs.tabViewItems()), len(pm.SECTIONS))

    def test_account_browser_typed(self):
        box = self._combos()[0]
        box.setStringValue_('Google Chrome --profile-directory="Work')
        box.delegate().controlTextDidChange_(_Notification(box))
        self.assertFalse(self.win.session.pending)  # not written while invalid
        box.setStringValue_("Firefox -P work")
        box.delegate().controlTextDidChange_(_Notification(box))
        self.win.session.commit()
        self.assertEqual(self.saved[-1].hosts[0].browser, "Firefox -P work")

    def test_browsers_are_picked_by_name(self):
        account, default = self._combos()
        self.assertEqual(account.stringValue(), "Default browser")
        self.assertEqual(default.stringValue(), "System default")
        account.selectItemAtIndex_(2)  # Default browser, Google Chrome, … — Trabajo
        account.delegate().comboBoxSelectionDidChange_(_Notification(account))
        self.assertEqual(account.stringValue(), "Google Chrome — Trabajo")
        default.selectItemAtIndex_(1)
        default.delegate().comboBoxSelectionDidChange_(_Notification(default))
        self.win.session.commit()
        self.assertEqual(self.saved[-1].hosts[0].browser, _BROWSERS[2].command)
        self.assertEqual(self.saved[-1].browser.command, "Google Chrome")

    def test_an_event_gets_its_own_sound(self):
        popup = self.win.controls["sounds.events.approved"]
        titles = [str(t) for t in popup.itemTitles()]
        self.assertEqual(titles[-1], "Choose a file…")
        popup.selectItemAtIndex_(titles.index("Victory fanfare (WIN)"))
        with mock.patch.object(self.pw.notifier, "play_sound") as play:
            self._click(popup)
        self.assertTrue(play.call_args.args[0].endswith("victory-v1.wav"))
        self.win.session.commit()
        self.assertEqual(self.saved[-1].sounds.events, {"approved": "fx:victory"})

    def test_settings_shows_in_the_dock_while_open(self):
        app = self.pw.AppKit.NSApplication.sharedApplication()
        self.win.present()
        self.assertEqual(app.activationPolicy(), 0)  # regular: Dock and ⌘-Tab
        edit = app.mainMenu().itemArray()[1].submenu()
        self.assertIn("paste:", [str(i.action()) for i in edit.itemArray()])
        self.win.window.close()
        self.assertEqual(app.activationPolicy(), 1)  # accessory: menu bar only

    def test_app_icon_is_drawn(self):
        icon = self.pw._app_icon()
        self.assertIsNotNone(icon)
        self.assertEqual(tuple(icon.size()), (512.0, 512.0))

    def test_every_pane_fits_the_window(self):
        """Nothing is cut off at either side and group titles keep their
        margin. A row wider than its group used to break the page's insets:
        titles flush left in every pane, Accounts clipped on both sides."""
        AppKit = self.pw.AppKit
        for index, item in enumerate(self.win.tabs.tabViewItems()):
            self.win.tabs.setSelectedTabViewItemIndex_(index)
            self.win.window.layoutIfNeeded()
            scroll = item.viewController().view()
            width = scroll.contentView().bounds().size.width
            page = scroll.documentView()
            for view in _descendants(page):
                if view.isHidden() or isinstance(view, AppKit.NSScroller):
                    continue
                frame = view.convertRect_toView_(view.bounds(), scroll.contentView())
                where = f"{item.label()}: {type(view).__name__} {_text(view)!r}"
                self.assertGreaterEqual(frame.origin.x, -0.5, where)
                self.assertLessEqual(frame.origin.x + frame.size.width, width + 0.5, where)
            for group in page.arrangedSubviews():
                title = group.arrangedSubviews()[0]
                x = title.convertRect_toView_(title.bounds(), scroll.contentView()).origin.x
                self.assertGreaterEqual(x, 16, f"{item.label()}: {title.stringValue()!r}")

    def _combos(self):
        """The Accounts pane's browser boxes: the account's, then the default."""
        found = []

        def walk(view):
            if isinstance(view, self.pw.AppKit.NSComboBox):
                found.append(view)
            for sub in view.subviews():
                walk(sub)

        walk(self.win.tabs.tabViewItems()[0].viewController().view())
        return found


def _descendants(view):
    """Every view under `view`, but not inside nested scroll views (their
    content is meant to be wider or taller than what they show)."""
    import objc

    for sub in view.subviews():
        yield sub
        if not sub.isKindOfClass_(objc.lookUpClass("NSScrollView")):
            yield from _descendants(sub)


def _text(view):
    return view.stringValue() if hasattr(view, "stringValue") else ""


class _Notification:
    def __init__(self, obj):
        self._obj = obj

    def object(self):
        return self._obj


if __name__ == "__main__":
    unittest.main()
