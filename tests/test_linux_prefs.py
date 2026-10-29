"""Linux Preferences window with real GTK widgets (run under Xvfb in CI)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    _GTK = Gtk.init_check()[0]
except Exception:  # pragma: no cover
    _GTK = False

from awaitray import browsers, sounds
from awaitray import config as cfg_mod
from awaitray import prefs_model as pm


def _walk(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _walk(child)


_BROWSERS = (
    browsers.SYSTEM,
    browsers.Choice("Google Chrome", "google-chrome"),
    browsers.Choice("Google Chrome — Trabajo", "google-chrome --profile-directory='Profile 2'"),
)


@unittest.skipUnless(_GTK, "GTK 3 with a display required")
class LinuxPrefsTests(unittest.TestCase):
    def setUp(self):
        from awaitray.backends.linux.ui import prefs

        self.prefs = prefs
        patcher = mock.patch.object(cfg_mod, "save")
        self.save = patcher.start()
        self.addCleanup(patcher.stop)
        gh_patch = mock.patch.object(prefs.gh, "gh_available", return_value=False)
        gh_patch.start()
        self.addCleanup(gh_patch.stop)
        found = mock.patch.object(prefs.browsers, "installed", return_value=list(_BROWSERS))
        found.start()
        self.addCleanup(found.stop)
        self.saved = []
        self.cfg = cfg_mod.Config(
            hosts=[cfg_mod.HostConfig("github.com")],
            repos=[cfg_mod.RepoConfig("/code", auto_discover=True, excluded=["/code/old"])],
        )
        self.win = prefs.PreferencesWindow(self.cfg, on_saved=self.saved.append)
        self.addCleanup(self.win.destroy)

    def _page(self, section_id):
        return self.win.stack.get_child_by_name(section_id)

    def _find(self, section_id, cls, predicate=lambda w: True):
        return [w for w in _walk(self._page(section_id)) if isinstance(w, cls) and predicate(w)]

    def test_custom_sections_editor(self):
        add = self._find("pull_requests", Gtk.Button, lambda b: b.get_label() == "Add section")[0]
        add.clicked()
        entries = self._find("pull_requests", Gtk.Entry, lambda e: e.get_text() == "mentions:@me")
        entries[0].set_text("repo:acme/core label:urgent")
        count = self._find(
            "pull_requests", Gtk.CheckButton, lambda c: c.get_label() == "Count in the menu bar"
        )[0]
        count.set_active(True)
        self.win.session.commit()
        (section,) = self.saved[-1].sections
        self.assertEqual((section.query, section.count), ("repo:acme/core label:urgent", True))
        with mock.patch.object(
            self.prefs.pr_sync, "test_section", return_value="3 open PRs now"
        ) as test:
            self._find("pull_requests", Gtk.Button, lambda b: b.get_label() == "Test query")[
                0
            ].clicked()
            for _ in range(50):  # let the worker thread post back
                Gtk.main_iteration_do(False)
                if test.called:
                    break
        test.assert_called_once()

    def test_six_pages(self):
        self.assertEqual(
            [self.win.stack.child_get_property(c, "name") for c in self.win.stack.get_children()],
            [s.id for s in pm.SECTIONS],
        )

    def test_switch_edits_the_working_copy_and_commits_on_close(self):
        switches = self._find("pull_requests", Gtk.Switch)
        switches[0].set_active(True)  # "Show your open PRs"
        self.assertTrue(self.win.session.working.my_prs.enabled)
        self.assertFalse(self.cfg.my_prs.enabled)  # the original is untouched
        self.assertTrue(self.win.session.pending)
        self.win._on_close()
        self.assertEqual(len(self.saved), 1)
        self.assertTrue(self.saved[0].my_prs.enabled)

    def test_dependent_rows_follow_their_switch(self):
        rows = [row for f, row in self.win._rows if f.key == "my_prs.include_drafts"]
        self.assertFalse(rows[0].get_sensitive())
        self._find("pull_requests", Gtk.Switch)[0].set_active(True)
        self.assertTrue(rows[0].get_sensitive())

    def test_invalid_time_shows_an_error_and_is_not_written(self):
        entries = self._find("notifications", Gtk.Entry, lambda e: e.get_text() == "20:00")
        entries[0].set_text("25:99")
        self.assertEqual(self.win.session.working.quiet_hours.start, "20:00")
        errors = self._find("notifications", Gtk.Label, lambda w: "HH:MM" in (w.get_label() or ""))
        self.assertTrue(errors)

    def test_repo_edits_keep_exclusions(self):
        check = self._find(
            "repositories", Gtk.CheckButton, lambda c: c.get_label() == "Auto-update"
        )[0]
        check.set_active(True)
        self.win.session.commit()
        repo = self.saved[-1].repos[0]
        self.assertTrue(repo.auto_update)
        self.assertEqual(repo.excluded, ["/code/old"])  # the old window dropped these

    def test_hosts_add_and_remove(self):
        add = self._find("accounts", Gtk.Button, lambda b: b.get_label() == "Add host")[0]
        add.clicked()
        self.assertEqual(len(self.win.session.working.hosts), 2)
        remove = self._find("accounts", Gtk.Button, lambda b: b.get_tooltip_text() == "Remove")
        remove[-1].clicked()
        self.assertEqual([h.hostname for h in self.win.session.working.hosts], ["github.com"])

    def test_account_field_sets_the_user(self):
        user = self._find(
            "accounts", Gtk.Entry, lambda e: e.get_placeholder_text() == "active account"
        )[0]
        user.set_text("@bob")
        self.win.session.commit()
        self.assertEqual(self.saved[-1].hosts[0].account_id, "bob@github.com")

    def test_browsers_are_picked_by_name(self):
        account, default = self._find("accounts", Gtk.ComboBoxText)
        self.assertEqual(account.get_child().get_text(), "Default browser")
        self.assertEqual(default.get_child().get_text(), "System default")
        account.set_active(2)  # Default browser, Google Chrome, Google Chrome — Trabajo
        self.assertEqual(account.get_child().get_text(), "Google Chrome — Trabajo")
        default.set_active(1)
        self.win.session.commit()
        self.assertEqual(self.saved[-1].hosts[0].browser, _BROWSERS[2].command)
        self.assertEqual(self.saved[-1].browser.command, "google-chrome")

    def test_sound_pack_is_picked_and_previewed(self):
        combo = next(
            c for c in self._find("notifications", Gtk.ComboBoxText) if c.get_active_id() == "retro"
        )
        with (
            tempfile.TemporaryDirectory() as cache,
            mock.patch("awaitray.sounds.cache_dir", return_value=Path(cache)),
            mock.patch.object(self.prefs.notifier, "play_sound") as play,
        ):
            combo.set_active_id("chimes")
        self.assertTrue(play.call_args.args[0].endswith("review-v1.wav"))
        self.win.session.commit()
        self.assertEqual(self.saved[-1].sounds.pack, "chimes")

    def test_an_event_gets_its_own_sound(self):
        combos = [
            c
            for c in self._find("notifications", Gtk.ComboBoxText)
            if any(r[0] == "Victory fanfare (WIN)" for r in c.get_model())
        ]
        self.assertEqual(len(combos), len(sounds.EVENTS))  # one per event of your PRs
        approved = combos[0]
        labels = [r[0] for r in approved.get_model()]
        with (
            tempfile.TemporaryDirectory() as cache,
            mock.patch("awaitray.sounds.cache_dir", return_value=Path(cache)),
            mock.patch.object(self.prefs.notifier, "play_sound") as play,
        ):
            approved.set_active(labels.index("Victory fanfare (WIN)"))
        self.assertTrue(play.call_args.args[0].endswith("victory-v1.wav"))
        self.assertEqual(labels[-1], "Choose a file…")
        self.win.session.commit()
        self.assertEqual(self.saved[-1].sounds.events, {"approved": "fx:victory"})

    def test_sounds_folder_is_shown(self):
        panel = self._find("notifications", self.prefs.SoundsFolderPanel)
        self.assertEqual(len(panel), 1)

    def test_account_browser(self):
        field = self._find("accounts", Gtk.ComboBoxText)[0].get_child()
        field.set_text('firefox -P "Work')
        self.assertIsNotNone(field.get_icon_name(Gtk.EntryIconPosition.SECONDARY))
        self.assertFalse(self.win.session.pending)  # not written while invalid
        field.set_text('firefox -P "Work"')
        self.assertIsNone(field.get_icon_name(Gtk.EntryIconPosition.SECONDARY))
        self.win.session.commit()
        self.assertEqual(self.saved[-1].hosts[0].browser, 'firefox -P "Work"')

    def test_reload_replaces_everything(self):
        fresh = cfg_mod.Config(hosts=[cfg_mod.HostConfig("ghe.corp")])
        self.win.reload(fresh)
        entries = self._find("accounts", Gtk.Entry)
        # host, account, its browser, the default browser
        self.assertEqual(
            [e.get_text() for e in entries], ["ghe.corp", "", "Default browser", "System default"]
        )
        self.assertFalse(self.win.session.pending)


if __name__ == "__main__":
    unittest.main()
