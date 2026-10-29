"""Linux backend smoke tests: real GTK widgets, run under Xvfb.

Skipped unless PyGObject, GTK 3 and a display are available. CI runs them
in a job that installs the system packages and wraps the suite in
`xvfb-run`.
"""

import os
import unittest
from unittest import mock

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    _GTK = Gtk.init_check()[0] if hasattr(Gtk, "init_check") else bool(os.environ.get("DISPLAY"))
except Exception:  # pragma: no cover - no PyGObject here
    _GTK = False

from awaitray import menu_model as mm
from awaitray.config import Colors, Config, MyPRsConfig, Waiting
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest


def _pr(n, direct=True, title=None):
    return PullRequest(
        "github.com",
        "acme/web_app",
        n,
        title or f"PR {n}",
        f"https://github.com/acme/web_app/pull/{n}",
        "alice",
        created_at="2026-09-23T12:00:00Z",
        direct_request=direct,
    )


class _UI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _labels(menu):
    return [
        c.get_label() if not isinstance(c, Gtk.SeparatorMenuItem) else "-"
        for c in menu.get_children()
    ]


@unittest.skipUnless(_GTK, "GTK 3 with a display required")
class GtkRenderTests(unittest.TestCase):
    def setUp(self):
        from awaitray.backends.linux import menu as menu_render

        self.render = menu_render.render
        mine = _pr(9)
        cfg = Config(  # fixtures have fixed dates: keep the wall clock out
            my_prs=MyPRsConfig(enabled=True), waiting=Waiting(long_wait_days=0, stale_days=0)
        )
        self.ctl = Controller(ui=_UI(), host=_UI(), cfg=cfg)
        self.ctl.apply_poll_result(
            PollResult(
                review_prs=[_pr(1, title="Fix login"), _pr(2, direct=False)],
                my_prs=[mine],
                details={mine.url: PRDetails(mine.url, "o", "FAILURE", None)},
            )
        )
        self.actions = []

    def _render(self):
        menu = Gtk.Menu()
        by_action = self.render(menu, self.ctl.menu_model().items, self.actions.append)
        return menu, by_action

    def test_structure_matches_the_model(self):
        menu, by_action = self._render()
        labels = _labels(menu)
        model = self.ctl.menu_model()
        self.assertEqual(len(labels), len(model.items))
        self.assertIn("NEEDS YOUR REVIEW · 1", labels)
        self.assertIn("NEEDS YOUR ACTION · 1", labels)
        self.assertIn("refresh", by_action)
        # disabled vs clickable
        children = menu.get_children()
        self.assertFalse(children[0].get_sensitive())  # status line
        row = next(c for c in children if "web_app#1" in (c.get_label() or ""))
        self.assertTrue(row.get_sensitive())
        self.assertIsNone(row.get_submenu())  # leaf: one click opens

    def test_clicks_report_actions(self):
        menu, _ = self._render()
        row = next(c for c in menu.get_children() if "web_app#1" in (c.get_label() or ""))
        row.activate()
        self.assertEqual(self.actions[-1], mm.Action("open_url", (_pr(1).url, _pr(1).key)))

    def test_pr_actions_submenu_is_two_levels(self):
        menu, _ = self._render()
        pr_actions = next(c for c in menu.get_children() if c.get_label() == "PR actions")
        entries = pr_actions.get_submenu().get_children()
        entry = next(c for c in entries if (c.get_label() or "").startswith("web_app#1"))
        leaves = entry.get_submenu().get_children()
        self.assertTrue(any(c.get_label() == "Snooze: For 3 hours" for c in leaves))
        self.assertTrue(
            all(isinstance(c, Gtk.SeparatorMenuItem) or c.get_submenu() is None for c in leaves)
        )

    def test_pause_check_item_reports_toggle(self):
        menu, _ = self._render()
        pause = next(c for c in menu.get_children() if c.get_label() == "Pause")
        self.assertIsInstance(pause, Gtk.CheckMenuItem)
        self.assertFalse(pause.get_active())
        self.assertEqual(self.actions, [])  # building it didn't fire the handler
        pause.set_active(True)
        self.assertEqual(self.actions[-1], mm.Action("toggle_pause"))

    def test_colored_rows_get_a_swatch(self):
        self.ctl.cfg.colors = Colors(to_review="#5B9BD5")
        menu, _ = self._render()
        header = next(c for c in menu.get_children() if c.get_label() == "NEEDS YOUR REVIEW · 1")
        self.assertIsInstance(header, Gtk.ImageMenuItem)


@unittest.skipUnless(_GTK, "GTK 3 with a display required")
class GtkAppTests(unittest.TestCase):
    def test_app_starts_and_rebuilds_only_on_change(self):
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
        except ValueError:
            self.skipTest("AyatanaAppIndicator3 not installed")
        from awaitray.backends.linux import app as linux_app

        with (
            mock.patch.object(linux_app, "Controller") as ctl_cls,
            mock.patch.object(
                linux_app.icon_renderer, "render", return_value=("/tmp", "awaitray-idle")
            ),
        ):
            real = Controller(ui=_UI(), host=_UI(), cfg=Config())
            ctl = ctl_cls.return_value
            ctl.cfg = real.cfg
            ctl.menu_model.side_effect = real.menu_model
            ctl.icon_count.return_value = 0
            ctl.has_priority_match.return_value = False
            ctl.last_error = False
            app = linux_app.AwaitrayApp()
            first = app._menu.get_children()
            self.assertTrue(first)
            app.rebuild_menu()  # nothing changed
            self.assertEqual(app._menu.get_children(), first)
            real.apply_poll_result(PollResult(review_prs=[_pr(1)]))
            app.rebuild_menu()
            self.assertNotEqual(app._menu.get_children(), first)
            app._on_action(mm.Action("refresh"))
            ctl.perform.assert_called_with(mm.Action("refresh"))


if __name__ == "__main__":
    unittest.main()
