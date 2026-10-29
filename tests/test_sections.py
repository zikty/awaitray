"""Custom menu sections from saved searches."""

import unittest
from unittest import mock

from awaitray import github as gh
from awaitray import pr_sync
from awaitray.config import Config, CustomSection, HostConfig, Waiting
from awaitray.controller import Controller, PollResult
from awaitray.github import PullRequest

MENTIONS = CustomSection("Mentions", "mentions:@me", notify=True, count=True)


def _pr(n, repo="acme/web"):
    return PullRequest("github.com", repo, n, f"PR {n}", f"https://x/{n}", "bob")


class _UI:
    def __init__(self):
        self.titles = []

    def notify(self, title, body, urgency, icon):
        self.titles.append(title)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class PollTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("gh_available", True), ("review_requested_prs", ([], {}))):
            p = mock.patch.object(gh, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_queries_each_enabled_section_on_its_hosts(self):
        cfg = Config(
            hosts=[HostConfig("github.com"), HostConfig("ghe.corp")],
            sections=[
                MENTIONS,
                CustomSection("Core", "repo:acme/core", host="ghe.corp"),
                CustomSection("Off", "x", enabled=False),
            ],
        )
        with mock.patch.object(gh, "search_prs", return_value=([_pr(1)], {})) as search:
            result = pr_sync.poll(cfg)
        self.assertEqual(
            [c.args for c in search.call_args_list],
            [
                ("github.com", "mentions:@me"),
                ("ghe.corp", "mentions:@me"),
                ("ghe.corp", "repo:acme/core"),
            ],
        )
        # The mock returns the same PR for both hosts: it's listed once.
        self.assertEqual(len(result.section_prs[MENTIONS.sid]), 1)

    def test_a_bad_query_is_reported_but_the_host_still_works(self):
        cfg = Config(hosts=[HostConfig("github.com")], sections=[MENTIONS])
        with mock.patch.object(gh, "search_prs", side_effect=gh.GhError("Invalid search query")):
            result = pr_sync.poll(cfg)
        self.assertFalse(result.total_failure)
        self.assertEqual(result.errors, ["Mentions: Invalid search query"])
        self.assertEqual(result.section_failed_hosts, {MENTIONS.sid: {"github.com"}})

    def test_test_query_button(self):
        cfg = Config(hosts=[HostConfig("github.com")])
        with mock.patch.object(gh, "search_prs", return_value=([_pr(1), _pr(2)], {})):
            self.assertEqual(pr_sync.test_section(cfg, MENTIONS), "2 open PRs now")
        with mock.patch.object(gh, "search_prs", side_effect=gh.GhError("bad")):
            self.assertEqual(pr_sync.test_section(cfg, MENTIONS), "github.com: bad")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.ui = _UI()
        self.ctl = Controller(
            ui=self.ui,
            host=self.ui,
            cfg=Config(
                hosts=[HostConfig("github.com")],
                sections=[MENTIONS],
                waiting=Waiting(long_wait_days=0, stale_days=0),
            ),
        )

    def _poll(self, section=(), review=(), failed=None):
        self.ctl.apply_poll_result(
            PollResult(
                review_prs=list(review),
                section_prs={MENTIONS.sid: list(section)},
                section_failed_hosts={MENTIONS.sid: failed} if failed else {},
            )
        )

    def _new(self):
        return [t for t in self.ui.titles if t.startswith("New in Mentions")]

    def test_menu_section_with_its_prs(self):
        self._poll(section=[_pr(1)])
        labels = [i.label for i in self.ctl.menu_model().items]
        self.assertIn("MENTIONS · 1", labels)

    def test_notifies_new_prs_after_a_silent_baseline(self):
        self._poll(section=[_pr(1)])
        self._poll(section=[_pr(1), _pr(2)])
        self.assertEqual(self._new(), ["New in Mentions"])

    def test_a_failed_query_keeps_the_baseline(self):
        self._poll(section=[_pr(1)])
        self._poll(section=[], failed={"github.com"})
        self._poll(section=[_pr(1)])
        self.assertEqual(self._new(), [])

    def test_counted_once_even_when_also_a_review_request(self):
        self._poll(section=[_pr(1), _pr(2)], review=[_pr(1)])
        self.assertEqual(self.ctl.tray_state().count, 2)
        self.ctl.cfg.sections[0].count = False
        self.assertEqual(self.ctl.tray_state().count, 1)

    def test_section_prs_can_be_snoozed_from_pr_actions(self):
        self._poll(section=[_pr(3)])
        pr = self.ctl.pr_for_key(_pr(3).key)
        self.assertIsNotNone(pr)
        with mock.patch("awaitray.config.save"):
            self.ctl.snoozes.snooze(pr, "1h")
        labels = [i.label for i in self.ctl.menu_model().items]
        self.assertIn("Nothing here · 1 snoozed", labels)


if __name__ == "__main__":
    unittest.main()
