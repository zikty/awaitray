"""One-click actions on your PRs: re-run failed checks, ask Copilot."""

import unittest
from unittest import mock

from awaitray import github as gh
from awaitray import menu_model as mm
from awaitray import pr_actions
from awaitray.config import Config, MyPRsConfig
from awaitray.controller import Controller, PollResult
from awaitray.runner import ImmediateRunner

RUN = "https://github.com/acme/web/actions/runs/{}/job/{}"


def _pr(n=1, host="github.com", draft=False, account=""):
    url = f"https://{host}/acme/web/pull/{n}"
    return gh.PullRequest(host, "acme/web", n, f"PR {n}", url, "me", draft, account=account)


def _det(pr, *checks):
    return gh.PRDetails(pr.url, "abc", "FAILURE", checks=tuple(checks))


class OfferTests(unittest.TestCase):
    def test_failed_actions_runs_once_each_in_order(self):
        det = _det(
            _pr(),
            ("lint", gh.CHECK_FAILURE, RUN.format(7, 1)),
            ("test", gh.CHECK_FAILURE, RUN.format(7, 2)),
            ("e2e", gh.CHECK_FAILURE, RUN.format(9, 3)),
            ("build", gh.CHECK_SUCCESS, RUN.format(5, 4)),
            ("ci/jenkins", gh.CHECK_FAILURE, "https://jenkins.corp/job/1"),
        )
        self.assertEqual(pr_actions.failed_runs(_pr(), det), [7, 9])
        self.assertEqual(pr_actions.failed_runs(_pr(), None), [])

    def test_only_runs_of_the_prs_own_repo(self):
        pr = _pr()
        det = _det(
            pr,
            ("a", gh.CHECK_FAILURE, "https://evil.example/acme/web/actions/runs/1"),
            ("b", gh.CHECK_FAILURE, "https://github.com/acme/other/actions/runs/2"),
            ("c", gh.CHECK_FAILURE, "https://github.com/x/y?u=/acme/web/actions/runs/3"),
            ("d", gh.CHECK_FAILURE, "https://github.com/acme/web/actions/runs/4x"),
            ("e", gh.CHECK_FAILURE, "https://GitHub.com/Acme/Web/actions/runs/5/job/6"),
        )
        self.assertEqual(pr_actions.failed_runs(pr, det), [5])

    def test_copilot_on_github_com_and_not_drafts(self):
        self.assertTrue(pr_actions.can_request_copilot(_pr()))
        self.assertFalse(pr_actions.can_request_copilot(_pr(host="ghe.corp")))
        self.assertFalse(pr_actions.can_request_copilot(_pr(draft=True)))


class DoTests(unittest.TestCase):
    def test_rerun_each_run_as_the_prs_account(self):
        pr = _pr(account="me-corp@github.com, github.com")
        with mock.patch.object(gh, "rest") as rest:
            notice = pr_actions.rerun_failed(pr, [7, 9])
        paths = [c.args[1] for c in rest.call_args_list]
        self.assertEqual(
            paths,
            [
                "repos/acme/web/actions/runs/7/rerun-failed-jobs",
                "repos/acme/web/actions/runs/9/rerun-failed-jobs",
            ],
        )
        self.assertEqual({c.args[2] for c in rest.call_args_list}, {"me-corp"})
        self.assertEqual(rest.call_args.kwargs["method"], "POST")
        self.assertEqual(notice.title, "Re-running failed checks: acme/web#1")

    def test_rerun_failure_says_why(self):
        with mock.patch.object(gh, "rest", side_effect=gh.GhError("HTTP 403: no")):
            notice = pr_actions.rerun_failed(_pr(), [7])
        self.assertEqual(
            (notice.title, notice.body), ("Couldn't re-run checks: acme/web#1", "HTTP 403: no")
        )

    def test_copilot_review_request(self):
        with mock.patch.object(gh, "rest") as rest:
            notice = pr_actions.request_copilot_review(_pr())
        self.assertEqual(rest.call_args.args[1], "repos/acme/web/pulls/1/requested_reviewers")
        self.assertEqual(
            rest.call_args.kwargs["body"], {"reviewers": ["copilot-pull-request-reviewer[bot]"]}
        )
        self.assertEqual(notice.title, "Copilot review requested: acme/web#1")
        with mock.patch.object(gh, "rest", side_effect=gh.GhError("HTTP 422")):
            self.assertIn("Couldn't ask Copilot", pr_actions.request_copilot_review(_pr()).title)


class RestTests(unittest.TestCase):
    def test_body_goes_on_stdin_as_json(self):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(gh.subprocess, "run", return_value=done) as run:
            gh.rest("github.com", "repos/a/b/x", method="POST", body={"k": ["v"]})
        argv = run.call_args.args[0]
        self.assertEqual(argv[-5:], ["--method", "POST", "--input", "-", "repos/a/b/x"])
        self.assertEqual(run.call_args.kwargs["input"], '{"k": ["v"]}')


class _Host:
    def __init__(self):
        self.notifications = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append(title)

    def run_on_main(self, fn, *args):
        fn(*args)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        for target in (mock.patch.object(gh, "pr_details", side_effect=gh.GhError("offline")),):
            target.start()
            self.addCleanup(target.stop)

    def _ctl(self, mine=(), review=(), details=None):
        host = _Host()
        ctl = Controller(
            ui=host,
            host=host,
            cfg=Config(my_prs=MyPRsConfig(enabled=True)),
            runner=ImmediateRunner(),
        )
        ctl.apply_poll_result(
            PollResult(review_prs=list(review), my_prs=list(mine), details=details or {})
        )
        return ctl, host

    def _actions(self, ctl, pr):
        for item in ctl.menu_model().items:
            if item.kind == mm.PR and item.action.args[-1] == pr.key:
                return {i.label: i.action for i in item.alternate if i.action}
        raise AssertionError(f"{pr.key} not in the menu")

    def test_your_failing_pr_offers_both(self):
        pr = _pr()
        det = _det(pr, ("test", gh.CHECK_FAILURE, RUN.format(7, 1)))
        ctl, host = self._ctl(mine=[pr], details={pr.url: det})
        actions = self._actions(ctl, pr)
        self.assertIn("Request Copilot review", actions)
        with mock.patch.object(gh, "rest") as rest:
            ctl.perform(actions["Re-run failed checks"])
        self.assertEqual(rest.call_args.args[1], "repos/acme/web/actions/runs/7/rerun-failed-jobs")
        self.assertEqual(host.notifications, ["Re-running failed checks: acme/web#1"])

    def test_review_requests_and_green_prs_offer_no_rerun(self):
        theirs, green = _pr(1), _pr(2)
        ctl, _ = self._ctl(
            review=[theirs],
            mine=[green],
            details={theirs.url: _det(theirs, ("t", gh.CHECK_FAILURE, RUN.format(7, 1)))},
        )
        self.assertNotIn("Re-run failed checks", self._actions(ctl, theirs))
        self.assertNotIn("Request Copilot review", self._actions(ctl, theirs))
        self.assertNotIn("Re-run failed checks", self._actions(ctl, green))


if __name__ == "__main__":
    unittest.main()
