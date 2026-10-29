"""Several GitHub accounts per host."""

import json
import subprocess
import unittest
from unittest import mock

from awaitray import config as cfg_mod
from awaitray import github as gh
from awaitray import pr_sync
from awaitray.config import Config, HostConfig
from awaitray.controller import Controller, PollResult
from awaitray.github import PullRequest

TOKEN = "gho_secret_do_not_leak"


def _done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _search_page(number=1):
    node = {
        "number": number,
        "title": "t",
        "url": f"https://github.com/acme/web/pull/{number}",
        "repository": {"nameWithOwner": "acme/web"},
        "author": {"login": "alice"},
    }
    return json.dumps({"data": {"viewer": {"login": "bob"}, "search": {"nodes": [node]}}})


class TokenEnvTests(unittest.TestCase):
    def _env(self, host, user="bob", token_result=None):
        with mock.patch.object(gh.subprocess, "run", return_value=token_result or _done(TOKEN)):
            return gh._account_env(host, user)

    def test_active_account_needs_no_token(self):
        self.assertIsNone(gh._account_env("github.com", ""))

    def test_github_com_and_ghe_com_use_gh_token(self):
        for host in ("github.com", "acme.ghe.com"):
            env = self._env(host)
            self.assertEqual(env["GH_TOKEN"], TOKEN)
            self.assertNotIn("GH_ENTERPRISE_TOKEN", env)

    def test_enterprise_server_uses_the_enterprise_variables(self):
        env = self._env("ghe.corp:8443")
        self.assertEqual((env["GH_ENTERPRISE_TOKEN"], env["GH_HOST"]), (TOKEN, "ghe.corp:8443"))

    def test_not_logged_in_and_old_gh_explain_themselves(self):
        with self.assertRaises(gh.GhError) as err:
            self._env("github.com", token_result=_done("", 1, "no oauth token"))
        self.assertIn("account bob is not logged in on github.com", str(err.exception))
        self.assertEqual(err.exception.kind, gh.ERR_AUTH)
        self.assertNotIn(TOKEN, str(err.exception))
        with self.assertRaisesRegex(gh.GhError, "gh 2.40 or newer"):
            self._env("github.com", token_result=_done("", 1, "unknown flag: --user"))

    def test_search_passes_the_token_only_to_gh_and_tags_the_account(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw.get("env")))
            return _done(TOKEN) if cmd[1:3] == ["auth", "token"] else _done(_search_page())

        with (
            mock.patch.object(gh, "gh_available", return_value=True),
            mock.patch.object(gh.subprocess, "run", side_effect=run),
        ):
            prs, _ = gh.review_requested_prs("github.com", user="bob")
        (search_cmd, env) = calls[1]
        self.assertEqual(env["GH_TOKEN"], TOKEN)
        self.assertNotIn(TOKEN, " ".join(search_cmd))  # never on the command line
        self.assertEqual(prs[0].account, "bob@github.com")


def _pr(n, account, direct=True):
    return PullRequest(
        "github.com", "acme/web", n, "t", f"u{n}", "a", direct_request=direct, account=account
    )


class PollTests(unittest.TestCase):
    CFG = Config(
        hosts=[HostConfig("github.com", user="bob"), HostConfig("github.com", user="work")]
    )

    def _poll(self, review):
        with (
            mock.patch.object(gh, "gh_available", return_value=True),
            mock.patch.object(gh, "review_requested_prs", side_effect=review),
        ):
            return pr_sync.poll(self.CFG)

    def test_a_pr_both_accounts_see_is_listed_once_with_both(self):
        def review(host, user=""):
            return [_pr(1, f"{user}@{host}", direct=user == "work")], {}

        result = self._poll(review)
        (pr,) = result.review_prs
        self.assertEqual(pr.account, "bob@github.com, work@github.com")
        self.assertTrue(pr.direct_request)

    def test_one_failing_account_is_a_partial_failure(self):
        def review(host, user=""):
            if user == "work":
                raise gh.GhError("account work is not logged in on github.com: run gh auth login")
            return [_pr(1, f"{user}@{host}")], {}

        result = self._poll(review)
        self.assertFalse(result.total_failure)
        self.assertEqual(result.host_errors, {"work@github.com": gh.ERR_AUTH})
        self.assertEqual(result.failed_hosts, {"github.com"})


class _UI:
    def __init__(self):
        self.notices = []

    def notify(self, title, body, urgency, icon):
        self.notices.append((title, body))

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.ui = _UI()
        self.ctl = Controller(ui=self.ui, host=self.ui, cfg=PollTests.CFG)

    def test_accounts_are_labelled_and_a_blip_does_not_renotify(self):
        self.ctl.apply_poll_result(PollResult(review_prs=[]))
        self.ctl.apply_poll_result(PollResult(review_prs=[_pr(1, "work@github.com")]))
        self.assertEqual(self.ui.notices[-1][1], "acme/web #1 — t · work@github.com")
        row = next(i for i in self.ctl.menu_model().items if i.kind == "pr")
        self.assertIn("work@github.com", row.meta)
        # The account that saw it fails for a round, then comes back.
        self.ctl.apply_poll_result(
            PollResult(
                failed_hosts={"github.com"},
                errors=["work@github.com: timeout"],
                host_errors={"work@github.com": gh.ERR_NETWORK},
            )
        )
        self.ctl.apply_poll_result(PollResult(review_prs=[_pr(1, "work@github.com")]))
        self.assertEqual(len(self.ui.notices), 1)

    def test_login_item_names_the_account_and_logs_in_on_its_host(self):
        self.ctl.apply_poll_result(
            PollResult(
                review_prs=[_pr(1, "bob@github.com")],
                failed_hosts={"github.com"},
                errors=["x"],
                host_errors={"work@github.com": gh.ERR_AUTH},
            )
        )
        item = next(i for i in self.ctl.menu_model().items if i.label.startswith("Log in to"))
        self.assertEqual(item.label, "Log in to work@github.com…")
        with mock.patch.object(gh, "launch_login", return_value=None) as login:
            self.ctl.perform(item.action)
        login.assert_called_once_with("github.com")


class ConfigTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(HostConfig("github.com").account_id, "github.com")
        self.assertEqual(HostConfig("github.com", user="bob").account_id, "bob@github.com")
        self.assertEqual(cfg_mod.split_account("bob@ghe.corp:8443"), ("bob", "ghe.corp:8443"))
        self.assertEqual(cfg_mod.split_account("github.com"), ("", "github.com"))


if __name__ == "__main__":
    unittest.main()
