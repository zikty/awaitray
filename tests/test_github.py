import json
import subprocess
import unittest
from unittest import mock

from awaitray import github as gh
from awaitray import pr_sync
from awaitray.config import Config, HostConfig, MyPRsConfig
from awaitray.controller import Controller


def _node(
    number,
    *,
    repo="acme/web",
    host="github.com",
    rollup="SUCCESS",
    review="APPROVED",
    mergeable="MERGEABLE",
    author="alice",
    draft=False,
):
    commit = {"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://{host}/{repo}/pull/{number}",
        "isDraft": draft,
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedAt": "2026-09-02T10:00:00Z",
        "author": {"login": author} if author else None,
        "repository": {"nameWithOwner": repo},
        "headRefOid": f"oid{number}",
        "reviewDecision": review,
        "mergeable": mergeable,
        "comments": {"totalCount": 3},
        "reviewThreads": {"nodes": [{"isResolved": True}, {"isResolved": False}]},
        "commits": {"nodes": [commit]},
    }


def _page(nodes, *, next_cursor=None):
    return {
        "data": {
            "search": {
                "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
                "nodes": nodes,
            }
        }
    }


def _completed(payload=None, *, returncode=0, stdout=None, stderr=""):
    out = stdout if stdout is not None else json.dumps(payload)
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=out, stderr=stderr)


class _GhTestCase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gh, "gh_available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, *responses):
        run = mock.patch.object(gh.subprocess, "run", side_effect=list(responses))
        self.run_mock = run.start()
        self.addCleanup(run.stop)


class SearchParsingTests(_GhTestCase):
    def test_parses_prs_and_details_in_one_call(self):
        self._run(_completed(_page([_node(1), _node(2, rollup="FAILURE", review=None)])))
        prs, details = gh.review_requested_prs("github.com")

        self.assertEqual(self.run_mock.call_count, 1)
        self.assertEqual([p.number for p in prs], [1, 2])
        pr = prs[0]
        self.assertEqual((pr.host, pr.repo, pr.author), ("github.com", "acme/web", "alice"))
        self.assertEqual(pr.created_at, "2026-09-01T10:00:00Z")
        d = details[pr.url]
        self.assertEqual(
            (d.head_oid, d.ci_state, d.review_decision), ("oid1", "SUCCESS", "APPROVED")
        )
        self.assertEqual((d.mergeable, d.comment_count), ("MERGEABLE", 3))
        self.assertEqual(d.unresolved_threads, 1)
        self.assertEqual(details[prs[1].url].ci_state, "FAILURE")
        self.assertIsNone(details[prs[1].url].review_decision)

    def test_query_targets_host_and_qualifier(self):
        self._run(_completed(_page([])))
        gh.review_requested_prs("ghe.corp:8443")
        cmd = self.run_mock.call_args.args[0]
        self.assertEqual(cmd[:5], ["gh", "api", "graphql", "--hostname", "ghe.corp:8443"])
        self.assertIn("q=is:pr is:open archived:false review-requested:@me", cmd)
        self.assertFalse(any(a.startswith("cursor=") for a in cmd))

    def test_my_prs_uses_author_qualifier(self):
        self._run(_completed(_page([])))
        gh.my_open_prs("github.com")
        self.assertIn("q=is:pr is:open archived:false author:@me", self.run_mock.call_args.args[0])

    def test_rollup_states_map_to_ui_states(self):
        nodes = [
            _node(1, rollup="ERROR"),
            _node(2, rollup="EXPECTED"),
            _node(3, rollup="PENDING"),
            _node(4, rollup=None),
        ]
        self._run(_completed(_page(nodes)))
        prs, details = gh.review_requested_prs("github.com")
        self.assertEqual(
            [details[p.url].ci_state for p in prs], ["FAILURE", "PENDING", "PENDING", None]
        )

    def test_checks_failing_first_and_latest_activity(self):
        node = _node(1)
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"] = {
            "nodes": [
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "l"},
                {"name": "build", "status": "IN_PROGRESS", "conclusion": None, "detailsUrl": "b"},
                {"context": "ci/legacy", "state": "ERROR", "targetUrl": "x"},
                {"name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED", "detailsUrl": ""},
                {},
            ]
        }
        node["lastComment"] = {"nodes": [{"url": "c1", "createdAt": "2026-09-02T10:00:00Z"}]}
        node["lastReview"] = {"nodes": [{"url": "r1", "createdAt": "2026-09-03T10:00:00Z"}]}
        self._run(_completed(_page([node])))
        _, details = gh.review_requested_prs("github.com")
        det = next(iter(details.values()))
        self.assertEqual(
            det.checks,
            (
                ("ci/legacy", "FAILURE", "x"),
                ("build", "PENDING", "b"),
                ("lint", "SUCCESS", "l"),
                ("docs", "NEUTRAL", ""),
            ),
        )
        self.assertEqual(det.latest_activity_url, "r1")

    def test_skips_non_pr_and_empty_nodes_and_handles_deleted_author(self):
        self._run(_completed(_page([{}, None, _node(5, author=None)])))
        prs, _ = gh.review_requested_prs("github.com")
        self.assertEqual([(p.number, p.author) for p in prs], [(5, "ghost")])

    def test_hidden_characters_are_dropped_from_titles_and_checks(self):
        node = _node(5, author="al\u200bice")
        node["title"] = "Fix \u202egnp.exe\u202c login\nredirect"
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
            "state": "FAILURE",
            "contexts": {"nodes": [{"context": "ci\u2066/x", "state": "FAILURE", "targetUrl": ""}]},
        }
        self._run(_completed(_page([node])))
        prs, details = gh.review_requested_prs("github.com")
        self.assertEqual((prs[0].title, prs[0].author), ("Fix gnp.exe login redirect", "alice"))
        self.assertEqual(details[prs[0].url].checks[0][0], "ci/x")

    def test_follows_pagination(self):
        self._run(
            _completed(_page([_node(1)], next_cursor="C1")),
            _completed(_page([_node(2)])),
        )
        prs, _ = gh.review_requested_prs("github.com")
        self.assertEqual([p.number for p in prs], [1, 2])
        second_cmd = self.run_mock.call_args_list[1].args[0]
        self.assertIn("cursor=C1", second_cmd)

    def test_caps_results(self):
        page = _completed(_page([_node(i) for i in range(50)], next_cursor="more"))
        self._run(*[page] * 10)
        prs, _ = gh.review_requested_prs("github.com")
        self.assertEqual(len(prs), gh._MAX_RESULTS)
        self.assertEqual(self.run_mock.call_count, gh._MAX_RESULTS // 50)


class DirectVsTeamRequestTests(_GhTestCase):
    def _page_with_viewer(self, nodes, viewer="me"):
        page = _page(nodes)
        page["data"]["viewer"] = {"login": viewer}
        return page

    def _with_requests(self, number, *reviewers):
        node = _node(number)
        node["reviewRequests"] = {"nodes": [{"requestedReviewer": r} for r in reviewers]}
        return node

    def test_direct_vs_team_requests(self):
        direct = self._with_requests(1, {"__typename": "User", "login": "Me"})
        team_only = self._with_requests(2, {"__typename": "Team"})
        both = self._with_requests(3, {"__typename": "Team"}, {"__typename": "User", "login": "me"})
        other_user = self._with_requests(
            4, {"__typename": "User", "login": "bob"}, {"__typename": "Team"}
        )
        self._run(_completed(self._page_with_viewer([direct, team_only, both, other_user])))
        prs, _ = gh.review_requested_prs("github.com")
        self.assertEqual(
            [(p.number, p.direct_request) for p in prs],
            [(1, True), (2, False), (3, True), (4, False)],
        )

    def test_requested_at_prefers_the_request_to_you(self):
        def ev(when, reviewer):
            return {"createdAt": when, "requestedReviewer": reviewer}

        me, bob, team = (
            {"__typename": "User", "login": "me"},
            {"__typename": "User", "login": "bob"},
            {"__typename": "Team"},
        )
        mine = _node(1)
        mine["timelineItems"] = {"nodes": [ev("T1", me), ev("T2", team), ev("T3", bob)]}
        via_team = _node(2)
        via_team["timelineItems"] = {"nodes": [ev("T4", team), ev("T5", bob)]}
        unknown = _node(3)  # older GHE or no events: fall back to createdAt later
        self._run(_completed(self._page_with_viewer([mine, via_team, unknown])))
        prs, _ = gh.review_requested_prs("github.com")
        self.assertEqual([p.requested_at for p in prs], ["T1", "T4", None])

    def test_unknown_viewer_counts_as_direct(self):
        self._run(_completed(_page([self._with_requests(1, {"__typename": "Team"})])))
        prs, _ = gh.review_requested_prs("github.com")
        self.assertTrue(prs[0].direct_request)


class RateLimitTests(_GhTestCase):
    """API budget per host."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(gh._RATE_LIMITS, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def _page_with_limit(self, remaining, reset_in_minutes=30):
        from datetime import datetime, timedelta, timezone

        reset = datetime.now(timezone.utc) + timedelta(minutes=reset_in_minutes)
        page = _page([_node(1)])
        page["data"]["rateLimit"] = {
            "remaining": remaining,
            "limit": 5000,
            "resetAt": reset.isoformat().replace("+00:00", "Z"),
            "cost": 1,
        }
        return page

    def test_each_query_records_the_budget(self):
        self._run(_completed(self._page_with_limit(4812)))
        gh.review_requested_prs("github.com")
        limit = gh.rate_limit("github.com")
        self.assertEqual((limit.remaining, limit.limit, limit.low), (4812, 5000, False))
        self.assertIn("API 4,812/5,000 left · resets", limit.describe())
        with mock.patch.object(gh, "is_authenticated", return_value=True):
            self.assertTrue(gh.sign_in_status("github.com").startswith("signed in · API"))

    def test_ghe_without_rate_limiting_records_nothing(self):
        page = _page([_node(1)])
        page["data"]["rateLimit"] = None
        self._run(_completed(page))
        gh.review_requested_prs("ghe.corp")
        self.assertIsNone(gh.rate_limit("ghe.corp"))

    def test_poll_skips_a_host_until_its_budget_resets(self):
        self._run(_completed(self._page_with_limit(20)), _completed(self._page_with_limit(4999)))
        cfg = Config(hosts=[HostConfig("github.com")])
        first = pr_sync.poll(cfg)  # this query leaves 20 points: low
        self.assertFalse(first.total_failure)
        second = pr_sync.poll(cfg)
        self.assertTrue(second.total_failure)
        self.assertIn("rate limit nearly used up (20 left)", second.errors[0])
        self.assertEqual(self.run_mock.call_count, 1)  # no call while backing off
        # Once the reset time has passed, it polls again.
        gh._RATE_LIMITS["github.com"] = gh.RateLimit(20, 5000, "2000-01-01T00:00:00Z")
        self.assertFalse(pr_sync.poll(cfg).total_failure)


class SearchErrorTests(_GhTestCase):
    def test_graphql_errors_raise_with_message(self):
        payload = {"errors": [{"message": "Could not resolve to a User"}]}
        self._run(_completed(payload, returncode=1))
        with self.assertRaisesRegex(gh.GhError, "Could not resolve"):
            gh.review_requested_prs("github.com")

    def test_errors_with_exit_zero_still_raise(self):
        self._run(_completed({"data": None, "errors": [{"message": "boom"}]}))
        with self.assertRaisesRegex(gh.GhError, "boom"):
            gh.review_requested_prs("github.com")

    def test_non_json_failure_uses_stderr(self):
        self._run(_completed(stdout="", returncode=1, stderr="HTTP 401: Bad credentials"))
        with self.assertRaisesRegex(gh.GhError, "Bad credentials"):
            gh.review_requested_prs("github.com")

    def test_missing_gh(self):
        with (
            mock.patch.object(gh, "gh_available", return_value=False),
            self.assertRaisesRegex(gh.GhError, "not found"),
        ):
            gh.review_requested_prs("github.com")


class _ImmediateHost:
    """Runs main-thread callbacks inline; everything else is a no-op."""

    def run_on_main(self, cb, *args):
        cb(*args)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerUsesSearchDetailsTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gh, "gh_available", return_value=True)  # the worker checks first
        p.start()
        self.addCleanup(p.stop)

    def test_details_from_search_feed_the_controller(self):
        pr = gh.PullRequest("github.com", "acme/web", 1, "t", "https://x/1", "bob")
        mine = gh.PullRequest("github.com", "acme/web", 2, "t", "https://x/2", "me")
        det = {
            pr.url: gh.PRDetails(pr.url, "o1", "FAILURE", "CHANGES_REQUESTED"),
            mine.url: gh.PRDetails(mine.url, "o2", "SUCCESS", "APPROVED"),
        }
        host = _ImmediateHost()
        ctl = Controller(
            ui=host,
            host=host,
            cfg=Config(
                hosts=[HostConfig("github.com")],
                my_prs=MyPRsConfig(enabled=True),
            ),
        )
        with (
            mock.patch.object(
                gh, "review_requested_prs", return_value=([pr], {pr.url: det[pr.url]})
            ),
            mock.patch.object(gh, "my_open_prs", return_value=([mine], {mine.url: det[mine.url]})),
            mock.patch.object(gh, "pr_details") as per_pr,
        ):
            ctl.apply_poll_result(pr_sync.poll(ctl.cfg))

        per_pr.assert_not_called()  # no more one-call-per-PR
        self.assertEqual(ctl.details_for(pr.url).ci_state, "FAILURE")
        self.assertEqual(ctl.details_for(mine.url).review_decision, "APPROVED")

    def test_new_head_commit_from_search_clears_commit_snooze(self):
        from awaitray import config as cfg_mod
        from awaitray.config import SnoozeEntry

        pr = gh.PullRequest("github.com", "acme/web", 1, "t", "https://x/1", "bob")
        host = _ImmediateHost()
        cfg = Config(hosts=[HostConfig("github.com")])
        cfg.snoozed[pr.key] = SnoozeEntry(head_oid="old")
        ctl = Controller(ui=host, host=host, cfg=cfg)
        new_det = {pr.url: gh.PRDetails(pr.url, "new", None, None)}
        with (
            mock.patch.object(gh, "review_requested_prs", return_value=([pr], new_det)),
            mock.patch.object(cfg_mod, "save") as save,
        ):
            ctl.apply_poll_result(pr_sync.poll(ctl.cfg))

        self.assertNotIn(pr.key, ctl.cfg.snoozed)
        save.assert_called()


if __name__ == "__main__":
    unittest.main()
