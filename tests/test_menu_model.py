import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from awaitray import menu_model as mm
from awaitray.config import (
    Colors,
    Config,
    FeatureToggles,
    LocalReposConfig,
    MyPRsConfig,
    SnoozeEntry,
    Waiting,
)
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest
from awaitray.snooze import CHOICES as SNOOZE_CHOICES

NOW = datetime(2026, 9, 25, 12, 0)


def _pr(
    n, repo="acme/web", host="github.com", direct=True, draft=False, title=None, author="alice"
):
    return PullRequest(
        host=host,
        repo=repo,
        number=n,
        title=title or f"PR {n}",
        url=f"https://{host}/{repo}/pull/{n}",
        author=author,
        is_draft=draft,
        created_at="2026-09-23T12:00:00Z",
        direct_request=direct,
    )


class _UI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _ctl(review=(), mine=(), details=None, **cfg):
    # Fixtures have fixed dates: keep the wall clock out unless a test opts in.
    cfg.setdefault("waiting", Waiting(long_wait_days=0, stale_days=0))
    cfg.setdefault(
        "colors",
        Colors(
            to_review="",
            my_prs="",
            local_repos="",
            draft="",
            approved="",
            changes_requested="",
            ci_failure="",
            high_priority="",
        ),
    )
    ctl = Controller(ui=_UI(), host=_UI(), cfg=Config(**cfg))
    ctl.apply_poll_result(
        PollResult(review_prs=list(review), my_prs=list(mine), details=details or {})
    )
    return ctl


def _labels(items):
    return [i.label for i in items]


def _find(items, prefix):
    return next(i for i in items if i.label.startswith(prefix))


class StructureTests(unittest.TestCase):
    def test_sections_are_always_shown_with_empty_lines(self):
        menu = _ctl().menu_model()
        labels = _labels(menu.items)
        self.assertEqual(menu.items[0].kind, mm.STATUS)
        self.assertIn("NEEDS YOUR REVIEW · 0", labels)
        self.assertIn("NEEDS YOUR TEAM'S REVIEW · 0", labels)
        self.assertIn("Nothing to review", labels)
        self.assertNotIn("NEEDS YOUR ACTION · 0", labels)  # My PRs is off
        self.assertEqual(labels[-5:], ["Pause", "Refresh now", "Preferences…", "", "Quit"])

    def test_direct_and_team_requests_go_to_their_sections(self):
        menu = _ctl(review=[_pr(1), _pr(2, direct=False)]).menu_model()
        labels = _labels(menu.items)
        i_direct = labels.index("NEEDS YOUR REVIEW · 1")
        i_team = labels.index("NEEDS YOUR TEAM'S REVIEW · 1")
        self.assertIn("web#1", labels[i_direct + 1])
        self.assertIn("web#2", labels[i_team + 1])

    def test_my_prs_are_bucketed_by_state(self):
        mine = [_pr(10), _pr(11), _pr(12, draft=True), _pr(13)]
        det = {
            mine[0].url: PRDetails(mine[0].url, "o", "FAILURE", None),
            mine[1].url: PRDetails(mine[1].url, "o", "SUCCESS", "APPROVED"),
            mine[3].url: PRDetails(mine[3].url, "o", "PENDING", "REVIEW_REQUIRED"),
        }
        menu = _ctl(mine=mine, details=det, my_prs=MyPRsConfig(enabled=True)).menu_model()
        labels = _labels(menu.items)
        for header, number in (
            ("NEEDS YOUR ACTION · 1", 10),
            ("READY TO MERGE · 1", 11),
            ("WAITING FOR REVIEW · 1", 13),
            ("DRAFTS · 1", 12),
        ):
            self.assertIn(f"web#{number}", labels[labels.index(header) + 1])
        action_row = menu.items[labels.index("NEEDS YOUR ACTION · 1") + 1]
        self.assertIn("checks failing", action_row.meta)
        self.assertTrue(action_row.title.startswith("⚠"))


class RowTests(unittest.TestCase):
    def test_row_format_and_click_action(self):
        pr = _pr(412, title="Fix login redirect")
        det = {pr.url: PRDetails(pr.url, "o", "SUCCESS", None)}
        with mock.patch("awaitray.pr_state.age", return_value="2d"):
            row = _find(_ctl(review=[pr], details=det).menu_model().items, "✓")
        self.assertEqual(row.kind, mm.PR)
        self.assertEqual(row.title, "✓ web#412  Fix login redirect")
        self.assertEqual(row.meta, "alice · 2d")
        self.assertEqual(row.label, "✓ web#412  Fix login redirect · alice · 2d")
        self.assertEqual(row.action, mm.Action("open_url", (pr.url, pr.key)))
        self.assertTrue(row.alternate)  # ⌥ actions on macOS

    def test_only_the_title_is_truncated(self):
        pr = _pr(1, title="x" * 200)
        row = _find(_ctl(review=[pr]).menu_model().items, "○")
        self.assertTrue(row.title.endswith("…"))
        self.assertLessEqual(len(row.title), len("○ web#1  ") + mm.TITLE_MAX)
        self.assertIn("alice", row.label)

    def test_glyphs_hidden_when_ci_status_is_off(self):
        menu = _ctl(review=[_pr(1)], features=FeatureToggles(show_ci_status=False)).menu_model()
        row = next(i for i in menu.items if i.kind == mm.PR)
        self.assertTrue(row.title.startswith("web#1"))

    def test_ambiguous_short_names_use_owner(self):
        menu = _ctl(
            review=[_pr(1, repo="a/api"), _pr(2, repo="b/api"), _pr(3, repo="c/web")]
        ).menu_model()
        titles = [i.title for i in menu.items if i.kind == mm.PR]
        self.assertTrue(any("a/api#1" in t for t in titles))
        self.assertTrue(any("b/api#2" in t for t in titles))
        self.assertTrue(any(" web#3" in t for t in titles))


class CapAndHiddenTests(unittest.TestCase):
    def test_sections_are_capped_with_more_submenu(self):
        prs = [_pr(i) for i in range(1, 12)]
        menu = _ctl(review=prs).menu_model()
        more = _find(menu.items, "3 more…")
        self.assertEqual(more.kind, mm.SUBMENU)
        self.assertEqual(len(more.children), 3)
        self.assertEqual(sum(1 for i in menu.items if i.kind == mm.PR), mm.SECTION_CAP)

    def test_snoozed_prs_are_acknowledged_in_the_empty_line(self):
        pr = _pr(1)
        ctl = _ctl(review=[pr])
        ctl.cfg.snoozed[pr.key] = SnoozeEntry(head_oid="x")
        labels = _labels(ctl.menu_model().items)
        self.assertIn("Nothing to review · 1 snoozed", labels)
        snoozed = _find(ctl.menu_model().items, "Snoozed (1)")
        self.assertEqual(snoozed.children[0].children[-1].action, mm.Action("unsnooze", (pr.key,)))


class ActionsTests(unittest.TestCase):
    def test_pr_actions_submenu_has_open_copy_and_inline_snooze(self):
        prs = [_pr(1), _pr(2)]
        menu = _ctl(review=prs).menu_model()
        pr_actions = _find(menu.items, "PR actions")
        self.assertEqual(pr_actions.children[0].action.kind, "open_urls")
        entry = next(c for c in pr_actions.children if c.label.startswith("web#1"))
        kinds = [c.action.kind for c in entry.children if c.action]
        self.assertEqual(kinds[:2], ["open_url", "copy"])
        self.assertEqual(kinds.count("snooze"), len(SNOOZE_CHOICES))
        for child in entry.children:  # at most two submenu levels
            self.assertFalse(child.children)

    def test_perform_dispatches_to_the_controller(self):
        pr = _pr(1)
        ctl = _ctl(review=[pr])
        with (
            mock.patch.object(ctl.links, "open_url") as open_url,
            mock.patch.object(ctl.snoozes, "snooze") as snooze,
            mock.patch.object(ctl, "tick_prs") as refresh,
        ):
            ctl.perform(mm.Action("open_url", (pr.url,)))
            ctl.perform(mm.Action("snooze", (pr.key, "3h")))
            ctl.perform(mm.Action("refresh"))
        open_url.assert_called_once_with(pr.url, None, "")
        snooze.assert_called_once_with(pr, "3h")
        refresh.assert_called_once()

    def test_local_repos_submenu(self):
        ctl = _ctl(local_repos=LocalReposConfig(enabled=True))
        local = _find(ctl.menu_model().items, "Local repos")
        self.assertEqual(local.children[0].action, mm.Action("fetch"))


class StatusAndSignatureTests(unittest.TestCase):
    def test_status_line(self):
        ctl = _ctl(review=[_pr(1)])
        self.assertRegex(ctl.menu_model().items[0].label, r"^1 to review · updated \d\d:\d\d$")
        ctl.cfg.paused = True
        self.assertEqual(ctl.menu_model().items[0].label, "Paused")

    def test_status_line_shows_team_requests(self):
        # Team-only requests must not read as "0 to review" with nothing else.
        ctl = _ctl(review=[_pr(1), _pr(2, direct=False), _pr(3, direct=False)])
        self.assertRegex(
            ctl.menu_model().items[0].label, r"^1 to review · 2 team · updated \d\d:\d\d$"
        )
        ctl = _ctl(review=[_pr(2, direct=False)])
        self.assertRegex(
            ctl.menu_model().items[0].label, r"^0 to review · 1 team · updated \d\d:\d\d$"
        )

    def test_signature_is_stable_until_something_changes(self):
        ctl = _ctl(review=[_pr(1)])
        a, b = ctl.menu_model(), ctl.menu_model()
        self.assertEqual(a.signature, b.signature)
        ctl.apply_poll_result(PollResult(review_prs=[_pr(1), _pr(2)]))
        self.assertNotEqual(a.signature, ctl.menu_model().signature)

    def test_items_are_hashable_values(self):
        pr = _pr(1)
        # Skip the status line: it carries the poll time.
        self.assertEqual(
            hash(_ctl(review=[pr]).menu_model().items[1:]),
            hash(_ctl(review=[replace(pr)]).menu_model().items[1:]),
        )


class WaitingTimeTests(unittest.TestCase):
    """PR age from the review request, long waits and stale PRs."""

    def _pr(self, n, requested_days_ago, updated_days_ago=0):
        now = datetime.now(timezone.utc)
        iso = lambda days: (now - timedelta(days=days)).isoformat()  # noqa: E731
        return replace(
            _pr(n),
            created_at=iso(90),
            requested_at=iso(requested_days_ago),
            updated_at=iso(updated_days_ago),
        )

    def _row(self, ctl, n):
        return next(i for i in ctl.menu_model().items if i.kind == mm.PR and f"#{n} " in i.title)

    def test_age_counts_from_the_review_request(self):
        ctl = _ctl([self._pr(1, 0.5)], waiting=Waiting())
        self.assertIn("· 12h", self._row(ctl, 1).meta)

    def test_long_wait_is_marked_and_turns_the_icon_red(self):
        ctl = _ctl([self._pr(1, 3), self._pr(2, 1)], waiting=Waiting(long_wait_days=2))
        self.assertIn("⏳", self._row(ctl, 1).title)
        self.assertNotIn("⏳", self._row(ctl, 2).title)
        self.assertTrue(ctl.tray_state().alert)
        quiet = _ctl([self._pr(1, 3)], waiting=Waiting(long_wait_days=2, long_wait_alert=False))
        self.assertFalse(quiet.tray_state().alert)

    def test_stale_prs_are_dimmed_and_not_counted(self):
        prs = [self._pr(1, 40, updated_days_ago=35), self._pr(2, 1)]
        ctl = _ctl(prs, waiting=Waiting(long_wait_days=0, stale_days=30))
        self.assertIn("stale", self._row(ctl, 1).meta)
        self.assertEqual(ctl.tray_state().count, 1)
        counted = _ctl(prs, waiting=Waiting(long_wait_days=0, stale_days=30, count_stale=True))
        self.assertEqual(counted.tray_state().count, 2)

    def test_zero_turns_the_thresholds_off(self):
        ctl = _ctl([self._pr(1, 300, updated_days_ago=300)], waiting=Waiting(0, True, 0))
        self.assertNotIn("⏳", self._row(ctl, 1).title)
        self.assertNotIn("stale", self._row(ctl, 1).meta)


class DeepLinkTests(unittest.TestCase):
    """Latest activity for your PRs, checks per PR."""

    CHECKS = tuple((f"job{i}", "SUCCESS", f"https://ci/{i}") for i in range(10))

    def _actions_for(self, ctl, number):
        actions = _find(ctl.menu_model().items, "PR actions")
        return _find(actions.children, f"web#{number}").children

    def test_your_pr_opens_the_latest_comment_or_review(self):
        mine, other = _pr(5, author="me"), _pr(6, author="me")
        det = {
            mine.url: PRDetails(mine.url, "o", None, latest_activity_url="https://c/1"),
            other.url: PRDetails(other.url, "o", None),
        }
        ctl = _ctl(mine=[mine, other], details=det, my_prs=MyPRsConfig(enabled=True))
        rows = {i.title: i for i in ctl.menu_model().items if i.kind == mm.PR}
        targets = sorted(i.action.args[0] for i in rows.values())
        self.assertEqual(targets, sorted([other.url, "https://c/1"]))

    def test_review_requests_keep_opening_the_pr(self):
        pr = _pr(1)
        det = {pr.url: PRDetails(pr.url, "o", None, latest_activity_url="https://c/1")}
        row = next(i for i in _ctl([pr], details=det).menu_model().items if i.kind == mm.PR)
        self.assertEqual(row.action, mm.Action("open_url", (pr.url, pr.key)))

    def test_checks_listed_failing_first_and_capped(self):
        pr = _pr(1)
        checks = (("build", "FAILURE", "https://ci/b"), *self.CHECKS)
        ctl = _ctl([pr], details={pr.url: PRDetails(pr.url, "o", "FAILURE", checks=checks)})
        items = self._actions_for(ctl, 1)
        labels = _labels(items)
        head = labels.index("Checks · 1 failing")
        self.assertEqual(labels[head + 1], "✗ build")
        self.assertEqual(items[head + 1].action, mm.Action("open_url", ("https://ci/b", pr.key)))
        listed = labels[head + 1 : labels.index("All checks…")]
        self.assertEqual(len(listed), mm.CHECKS_CAP)
        self.assertEqual(
            _find(items, "All checks").action, mm.Action("open_url", (f"{pr.url}/checks", pr.key))
        )

    def test_checks_hidden_with_ci_status_off(self):
        pr = _pr(1)
        ctl = _ctl(
            [pr],
            details={pr.url: PRDetails(pr.url, "o", "SUCCESS", checks=self.CHECKS)},
            features=FeatureToggles(show_ci_status=False),
        )
        self.assertNotIn("All checks…", _labels(self._actions_for(ctl, 1)))


if __name__ == "__main__":
    unittest.main()
