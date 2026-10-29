"""PrState: the PRs and the "notify once" bookkeeping, without a controller."""

import unittest

from awaitray import state as state_mod
from awaitray.config import Config, MyPRsConfig
from awaitray.github import PRDetails, PullRequest
from awaitray.pr_store import PrState
from awaitray.pr_sync import PollResult


def _pr(n):
    return PullRequest("github.com", "acme/web", n, f"PR {n}", f"https://x/{n}", "bob")


def _take(st, cfg, **result):
    return st.take_poll(
        cfg, PollResult(**result), fetch_outage=False, multi_account=False, quiet=False
    )


class PrStateTests(unittest.TestCase):
    def test_a_poll_returns_its_notices_and_changes_nothing_else(self):
        cfg = Config()
        st = PrState()
        self.assertEqual(_take(st, cfg, review_prs=[_pr(1)]).notices, [])  # baseline
        news = _take(st, cfg, review_prs=[_pr(1), _pr(2)])
        self.assertEqual([n.title for n in news.notices], ["New PR review request"])
        self.assertEqual([p.number for p in news.new_requests], [2])
        self.assertEqual(cfg, Config())  # never writes the config

    def test_fetch_outage_holds_an_empty_review_list(self):
        cfg, st = Config(), PrState()
        _take(st, cfg, review_prs=[_pr(1)])
        st.take_poll(cfg, PollResult(), fetch_outage=True, multi_account=False, quiet=False)
        self.assertEqual([p.number for p in st.review_prs], [1])

    def test_snapshot_round_trip(self):
        cfg = Config(my_prs=MyPRsConfig(enabled=True))
        st = PrState()
        det = {_pr(3).url: PRDetails(_pr(3).url, "o", "PENDING")}
        _take(st, cfg, review_prs=[_pr(1)], my_prs=[_pr(3)], details=det)
        st.toggle_watch(_pr(3).key)
        back = PrState.from_snapshot(st.snapshot(cfg), cfg)
        self.assertEqual(back.review_prs, st.review_prs)
        self.assertEqual(back.watching, {_pr(3).key})
        self.assertEqual(back.known_review, {_pr(1).key})
        self.assertIsInstance(st.snapshot(cfg), state_mod.Snapshot)


if __name__ == "__main__":
    unittest.main()
