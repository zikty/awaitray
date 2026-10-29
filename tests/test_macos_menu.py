"""macOS menu renderer: builds real NSMenus from the model (no app needed).

Runs on the macOS CI job (PyObjC installed); skipped elsewhere.
"""

import sys
import unittest

from awaitray import menu_model as mm
from awaitray.config import Colors, Config, MyPRsConfig, Waiting
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest


def _pr(n, direct=True):
    return PullRequest(
        "github.com",
        "acme/web",
        n,
        f"PR {n}",
        f"https://github.com/acme/web/pull/{n}",
        "alice",
        created_at="2026-09-23T12:00:00Z",
        direct_request=direct,
    )


class _UI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


@unittest.skipUnless(sys.platform == "darwin", "macOS backend")
class MacMenuRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import importlib

            from awaitray.backends.macos import menu as mac_menu

            importlib.import_module("awaitray.backends.macos.app")  # imports cleanly
        except Exception as e:  # pragma: no cover
            raise unittest.SkipTest(f"PyObjC unavailable: {e}") from e
        cls.mac_menu = mac_menu

    def setUp(self):
        mine = _pr(9)
        self.ctl = Controller(
            ui=_UI(),
            host=_UI(),
            cfg=Config(
                waiting=Waiting(long_wait_days=0, stale_days=0),  # fixtures have fixed dates
                my_prs=MyPRsConfig(enabled=True),
                colors=Colors(to_review="#5B9BD5"),
            ),
        )
        self.ctl.apply_poll_result(
            PollResult(
                review_prs=[_pr(1), _pr(2, direct=False)],
                my_prs=[mine],
                details={mine.url: PRDetails(mine.url, "o", "FAILURE", None)},
            )
        )
        self.actions = []
        self.renderer = self.mac_menu.Renderer(self.actions.append)
        self.menu = self.renderer.render(self.ctl.menu_model().items)

    def _items(self, menu=None):
        menu = menu or self.menu
        return [menu.itemAtIndex_(i) for i in range(menu.numberOfItems())]

    def test_structure_and_enabled_states(self):
        items = self._items()
        titles = [i.title() for i in items]
        self.assertIn("NEEDS YOUR REVIEW · 1", titles)
        self.assertFalse(items[0].isEnabled())  # status line
        row = next(i for i in items if i.title().startswith("○ web#1"))
        self.assertTrue(row.isEnabled())
        self.assertIsNone(row.submenu())  # leaf: click opens
        self.assertIn("\n", row.attributedTitle().string())  # two-line row

    def test_option_alternate_follows_each_pr_row(self):
        items = self._items()
        idx = next(n for n, i in enumerate(items) if i.title().startswith("○ web#1"))
        alt = items[idx + 1]
        self.assertTrue(alt.isAlternate())
        self.assertEqual(alt.keyEquivalentModifierMask(), 1 << 19)
        self.assertIsNotNone(alt.submenu())

    def test_click_reports_the_action(self):
        row = next(i for i in self._items() if i.title().startswith("○ web#1"))
        row.target().invoke_(row)
        self.assertEqual(self.actions[-1], mm.Action("open_url", (_pr(1).url, _pr(1).key)))

    def test_pause_is_a_check_item(self):
        pause = next(i for i in self._items() if i.title() == "Pause")
        self.assertEqual(pause.state(), 0)


if __name__ == "__main__":
    unittest.main()
