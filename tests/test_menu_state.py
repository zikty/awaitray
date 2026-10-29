"""menu_model.build is a pure function of a MenuState: no controller needed."""

import ast
import unittest
from datetime import datetime
from pathlib import Path

from awaitray import menu_model as mm
from awaitray.config import Config, CustomSection, MyPRsConfig
from awaitray.github import PRDetails, PullRequest

NOW = datetime(2026, 9, 25, 12, 0)
UPDATED = datetime(2026, 9, 25, 11, 58)


def _pr(n, direct=True, repo="acme/web"):
    return PullRequest(
        "github.com",
        repo,
        n,
        f"PR {n}",
        f"https://github.com/{repo}/pull/{n}",
        "alice",
        False,
        created_at="2026-09-23T12:00:00Z",
        direct_request=direct,
    )


def _state(**kw):
    kw.setdefault("cfg", Config())
    kw.setdefault(
        "status",
        mm.StatusInfo(enabled_accounts=("github.com",), last_success_at=UPDATED, direct_count=1),
    )
    return mm.MenuState(**kw)


def _labels(menu):
    return [i.label for i in menu.items]


class PureBuildTests(unittest.TestCase):
    def test_sections_and_rows_come_from_the_state(self):
        direct, team = _pr(1), _pr(2, direct=False)
        menu = mm.build(
            _state(
                review_prs=(direct, team),
                direct=(direct,),
                team=(team,),
                views={direct.key: mm.PrView(review_marker="📂 ", stale=True)},
            ),
            NOW,
        )
        labels = _labels(menu)
        self.assertEqual(labels[0], "1 to review · updated 11:58")
        self.assertIn("NEEDS YOUR REVIEW · 1", labels)
        row = next(i for i in menu.items if i.kind == mm.PR and i.action.args[1] == direct.key)
        self.assertIn("📂 web#1", row.title)
        self.assertIn("stale", row.meta)

    def test_the_same_state_builds_the_same_menu(self):
        state = _state(review_prs=(_pr(1),), direct=(_pr(1),))
        self.assertEqual(mm.build(state, NOW).signature, mm.build(state, NOW).signature)

    def test_a_view_drives_the_pr_actions(self):
        pr = _pr(1)
        cfg = Config(my_prs=MyPRsConfig(enabled=True))
        failing = PRDetails(
            pr.url,
            "abc",
            "FAILURE",
            checks=(("test", "FAILURE", "https://github.com/acme/web/actions/runs/7/job/1"),),
        )
        menu = mm.build(
            _state(cfg=cfg, mine=(pr,), views={pr.key: mm.PrView(details=failing, can_clone=True)}),
            NOW,
        )
        row = next(i for i in menu.items if i.kind == mm.PR)
        actions = [i.label for i in row.alternate]
        self.assertIn("Clone here", actions)
        self.assertIn("Re-run failed checks", actions)
        self.assertIn("NEEDS YOUR ACTION · 1", _labels(menu))

    def test_custom_sections_say_what_snoozing_hides(self):
        sec = CustomSection(title="Bots", query="author:app/dependabot")
        menu = mm.build(_state(custom=(mm.CustomSectionView(sec, (), snoozed=2),)), NOW)
        labels = _labels(menu)
        self.assertIn("BOTS · 0", labels)
        self.assertIn("Nothing here · 2 snoozed", labels)

    def test_setup_problems_replace_the_sections(self):
        status = mm.StatusInfo(
            enabled_accounts=("github.com",), auth_failed=("github.com",), text="x"
        )
        labels = _labels(mm.build(_state(status=status), NOW))
        self.assertEqual(labels[0], "Not logged in to github.com")
        self.assertIn("Log in to github.com…", labels)
        self.assertNotIn("NEEDS YOUR REVIEW · 0", labels)


class LayerTests(unittest.TestCase):
    def test_menu_model_never_reaches_for_the_controller(self):
        tree = ast.parse(Path(mm.__file__).read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("controller", modules)
        self.assertNotIn("Controller", identifiers)


if __name__ == "__main__":
    unittest.main()
