"""Menu actions: each kind has exactly one handler, and every kind the menu
can produce reaches one."""

import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from awaitray import ai_usage as au
from awaitray import github as gh
from awaitray import inbox
from awaitray import log as log_mod
from awaitray import menu_model as mm
from awaitray.account_info import AccountInfo
from awaitray.config import Config, HostConfig, LocalReposConfig, MyPRsConfig, RepoConfig
from awaitray.controller import Controller, PollResult
from awaitray.repos import FetchResult
from awaitray.runner import ImmediateRunner
from awaitray.snooze import entry_for

RUN = "https://github.com/acme/web/actions/runs/7/job/1"


class _UI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _pr(n, account="github.com"):
    url = f"https://github.com/acme/web/pull/{n}"
    return gh.PullRequest("github.com", "acme/web", n, f"PR {n}", url, "x", account=account)


def _kinds(items):
    for item in items:
        if item.action is not None:
            yield item.action.kind
        yield from _kinds(item.children or ())
        yield from _kinds(item.alternate or ())


class ActionKindTests(unittest.TestCase):
    def _busy_controller(self):
        """Every section and state the menu has, at once."""
        with tempfile.TemporaryDirectory() as repo:
            cfg = Config(
                hosts=[HostConfig("github.com"), HostConfig("ghe.corp")],
                my_prs=MyPRsConfig(enabled=True),
                local_repos=LocalReposConfig(enabled=True),
                repos=[RepoConfig(repo)],
            )
            ctl = Controller(ui=_UI(), host=_UI(), cfg=cfg, runner=ImmediateRunner())
            mine, theirs = _pr(1), [_pr(2), _pr(3), _pr(5)]
            failing = gh.PRDetails(
                mine.url, "abc", "FAILURE", checks=(("test", gh.CHECK_FAILURE, RUN),)
            )
            pending = gh.PRDetails(theirs[0].url, "abc", "PENDING")
            ctl.apply_poll_result(
                PollResult(
                    review_prs=theirs,
                    my_prs=[mine, _pr(4)],
                    details={mine.url: failing, _pr(4).url: replace(pending, url=_pr(4).url)},
                    host_errors={"ghe.corp": gh.ERR_AUTH},
                    failed_hosts={"ghe.corp"},
                )
            )
            cfg.snoozed[theirs[1].key] = entry_for("3h")
            ctl.apply_fetch_results([FetchResult(repo, True, "ok")])
            threads = inbox.parse(
                "github.com",
                [{"id": "9", "subject": {"title": "T", "url": "x/repos/a/b/pulls/1"}}],
            )
            ctl.inbox.apply([AccountInfo("github.com", "github.com", 1, threads, "T0")])
            ctl.usage.apply_ai_usage([au.Usage("codex", "Codex", url="https://chatgpt.com")])
            return ctl, set(_kinds(ctl.menu_model().items))

    def test_every_kind_the_menu_produces_has_a_handler(self):
        ctl, kinds = self._busy_controller()
        # Not vacuous: the busy menu really carries these.
        expected = {
            "open_url",
            "open_urls",
            "snooze",
            "unsnooze",
            "watch_checks",
            "rerun_failed",
            "copilot_review",
            "inbox",
            "inbox_open",
            "inbox_read_all",
            "login",
            "fetch",
            "refresh",
            "toggle_pause",
            mm.COPY,
            mm.PREFS,
            mm.QUIT,
        }
        self.assertLessEqual(expected, kinds)
        self.assertEqual({k for k in kinds if not ctl.handles(k)}, set())

    def test_a_kind_is_handled_once(self):
        ctl = Controller(ui=_UI(), host=_UI(), cfg=Config(), runner=ImmediateRunner())
        with self.assertRaises(ValueError):
            ctl.register_action("refresh", lambda: None)
        with self.assertRaises(ValueError):
            ctl.register_action(mm.QUIT, lambda: None)  # the backends' own

    def test_an_unknown_kind_is_logged_not_raised(self):
        ctl = Controller(ui=_UI(), host=_UI(), cfg=Config(), runner=ImmediateRunner())
        with mock.patch.object(log_mod, "warn") as warn:
            ctl.perform(mm.Action("nope"))
        self.assertIn("nope", warn.call_args.args[0])

    def test_a_feature_can_register_its_own(self):
        ctl = Controller(ui=_UI(), host=_UI(), cfg=Config(), runner=ImmediateRunner())
        seen = []
        ctl.register_action("hello", seen.append)
        ctl.perform(mm.Action("hello", ("world",)))
        self.assertEqual(seen, ["world"])


if __name__ == "__main__":
    unittest.main()
