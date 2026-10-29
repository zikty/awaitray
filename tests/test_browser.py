"""Which browser opens a link: per account, global, system."""

import copy
import unittest
from unittest import mock

from awaitray import browser
from awaitray import menu_model as mm
from awaitray.config import BrowserConfig, Config, HostConfig
from awaitray.controller import Controller, PollResult
from awaitray.github import PullRequest
from awaitray.runner import ImmediateRunner

WORK = "ghe.corp"


def _pr(n=1, host="github.com", account="github.com"):
    return PullRequest(
        host=host,
        repo="acme/web",
        number=n,
        title=f"PR {n}",
        url=f"https://{host}/acme/web/pull/{n}",
        author="alice",
        is_draft=False,
        updated_at="2026-09-29T10:00:00Z",
        account=account,
    )


def _cfg(*hosts, default=""):
    return Config(hosts=list(hosts), browser=BrowserConfig(default))


class CommandsForTests(unittest.TestCase):
    def test_nothing_set_means_the_system_browser(self):
        self.assertEqual(browser.commands_for(_cfg(HostConfig("github.com")), _pr()), [])

    def test_the_accounts_browser_then_the_default(self):
        cfg = _cfg(
            HostConfig("github.com"), HostConfig(WORK, browser="Google Chrome"), default="Firefox"
        )
        pr = _pr(host=WORK, account=WORK)
        self.assertEqual(
            browser.commands_for(cfg, pr), [(WORK, "Google Chrome"), (browser.GLOBAL, "Firefox")]
        )
        self.assertEqual(browser.commands_for(cfg, _pr()), [(browser.GLOBAL, "Firefox")])
        self.assertEqual(browser.commands_for(cfg, None), [(browser.GLOBAL, "Firefox")])

    def test_two_accounts_on_one_host(self):
        cfg = _cfg(
            HostConfig("github.com"),
            HostConfig("github.com", user="me-corp", browser="chrome --profile-directory=Work"),
        )
        corp = _pr(account="me-corp@github.com")
        self.assertEqual(
            browser.commands_for(cfg, corp),
            [("me-corp@github.com", "chrome --profile-directory=Work")],
        )
        self.assertEqual(browser.commands_for(cfg, _pr()), [])
        # Links that aren't a PR's (inbox, Copilot) name their account.
        self.assertEqual(
            browser.commands_for(cfg, None, "me-corp@github.com"),
            [("me-corp@github.com", "chrome --profile-directory=Work")],
        )
        self.assertEqual(browser.commands_for(cfg, None, "github.com"), [])

    def test_a_pr_both_accounts_see_follows_the_order_of_accounts(self):
        cfg = _cfg(
            HostConfig("github.com", user="b", browser="B"),
            HostConfig("github.com", user="a", browser="A"),
            default="B",
        )
        pr = _pr(account="a@github.com, b@github.com")
        self.assertEqual(
            browser.commands_for(cfg, pr), [("b@github.com", "B"), ("a@github.com", "A")]
        )

    def test_a_pr_without_accounts_falls_back_to_its_host(self):
        cfg = _cfg(HostConfig(WORK, browser="Chrome"))
        self.assertEqual(browser.commands_for(cfg, _pr(host=WORK, account="")), [(WORK, "Chrome")])


class ArgvTests(unittest.TestCase):
    URL = "https://github.com/acme/web/pull/1"

    def test_linux_runs_the_command_with_the_url_last(self):
        with mock.patch.dict("os.environ", {"HOME": "/home/me"}):
            args = browser.argv(
                'chromium --user-data-dir=~/corp --profile-directory="Profile 2"', self.URL, "linux"
            )
        self.assertEqual(
            args,
            [
                "chromium",
                "--user-data-dir=/home/me/corp",
                "--profile-directory=Profile 2",
                self.URL,
            ],
        )
        with mock.patch.dict("os.environ", {"HOME": "/home/me"}):
            self.assertEqual(browser.argv("~/bin/ff", self.URL, "linux")[0], "/home/me/bin/ff")

    def test_macos_opens_an_app_by_name(self):
        self.assertEqual(
            browser.argv("Google Chrome", self.URL, "darwin"),
            ["open", "-a", "Google Chrome", self.URL],
        )

    def test_macos_app_with_arguments(self):
        self.assertEqual(
            browser.argv('Google Chrome --profile-directory="Profile 2"', self.URL, "darwin"),
            ["open", "-na", "Google Chrome", "--args", "--profile-directory=Profile 2", self.URL],
        )

    def test_macos_app_name_may_be_quoted(self):
        self.assertEqual(
            browser.argv('"Google Chrome"', self.URL, "darwin"),
            ["open", "-a", "Google Chrome", self.URL],
        )

    def test_macos_expands_home_in_arguments(self):
        with mock.patch.dict("os.environ", {"HOME": "/Users/me"}):
            args = browser.argv("Google Chrome --user-data-dir=~/ChromeWork", self.URL, "darwin")
        self.assertIn("--user-data-dir=/Users/me/ChromeWork", args)

    def test_unbalanced_quotes(self):
        for platform in ("linux", "darwin"):
            with self.assertRaises(ValueError):
                browser.argv('firefox -P "Work', self.URL, platform)
            with self.assertRaises(ValueError):
                browser.argv('"Google Chrome', self.URL, platform)


class LaunchTests(unittest.TestCase):
    URL = "https://x/1"

    def test_linux_missing_command(self):
        with (
            mock.patch("sys.platform", "linux"),
            mock.patch.object(browser.shutil, "which", return_value=None),
            mock.patch.object(browser.subprocess, "Popen") as popen,
        ):
            self.assertIn("not in PATH", browser.launch("nope", self.URL))
        popen.assert_not_called()

    def test_linux_starts_it_detached(self):
        with (
            mock.patch("sys.platform", "linux"),
            mock.patch.object(browser.shutil, "which", return_value="/usr/bin/firefox"),
            mock.patch.object(browser.subprocess, "Popen") as popen,
        ):
            self.assertIsNone(browser.launch("firefox -P Work", self.URL))
        self.assertEqual(popen.call_args.args[0], ["firefox", "-P", "Work", self.URL])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_macos_missing_app_is_reported_by_open(self):
        done = mock.Mock(returncode=1, stderr="Unable to find application named 'Nope'\n")
        with (
            mock.patch("sys.platform", "darwin"),
            mock.patch.object(browser.subprocess, "run", return_value=done),
        ):
            self.assertEqual(
                browser.launch("Nope", self.URL), "Unable to find application named 'Nope'"
            )

    def test_macos_slow_start_is_not_a_failure(self):
        # Falling back would open the link a second time.
        timeout = browser.subprocess.TimeoutExpired("open", 15)
        with (
            mock.patch("sys.platform", "darwin"),
            mock.patch.object(browser.subprocess, "run", side_effect=timeout),
        ):
            self.assertIsNone(browser.launch("Firefox", self.URL))

    def test_bad_quotes_are_reported_and_nothing_runs(self):
        for platform in ("linux", "darwin"):
            with (
                mock.patch("sys.platform", platform),
                mock.patch.object(browser.subprocess, "run") as run,
                mock.patch.object(browser.subprocess, "Popen") as popen,
            ):
                self.assertIn("can't parse", browser.launch('Firefox "', self.URL), platform)
            run.assert_not_called()
            popen.assert_not_called()


class _Host:
    """UI + host that runs main-thread callbacks right away."""

    def __init__(self):
        self.notifications = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append((title, body))

    def run_on_main(self, fn, *args):
        fn(*args)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def _ctl(self, cfg, prs):
        host = _Host()
        ctl = Controller(ui=host, host=host, cfg=cfg, runner=ImmediateRunner())
        ctl.apply_poll_result(PollResult(review_prs=list(prs)))
        self.system = self._patch("awaitray.features.links.webbrowser.open")
        self.launch = self._patch("awaitray.browser.launch", return_value=None)
        return ctl, host

    def _patch(self, target, new=mock.DEFAULT, **kwargs):
        patcher = mock.patch(target, new, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_no_browser_set_uses_the_system_one(self):
        pr = _pr()
        ctl, _ = self._ctl(_cfg(HostConfig("github.com")), [pr])
        ctl.perform(mm.Action("open_url", (pr.url, pr.key)))
        self.system.assert_called_once_with(pr.url)
        self.launch.assert_not_called()

    def test_the_prs_account_picks_the_browser(self):
        pr = _pr(host=WORK, account=WORK)
        ctl, _ = self._ctl(_cfg(HostConfig(WORK, browser="Chrome"), default="Firefox"), [pr])
        ctl.perform(mm.Action("open_url", (f"{pr.url}/checks", pr.key)))
        self.launch.assert_called_once_with("Chrome", f"{pr.url}/checks")
        ctl.perform(mm.Action("open_url", ("https://cli.github.com",)))
        self.launch.assert_called_with("Firefox", "https://cli.github.com")
        self.system.assert_not_called()

    def test_a_missing_browser_falls_back_and_says_so(self):
        pr = _pr(host=WORK, account=WORK)
        ctl, host = self._ctl(_cfg(HostConfig(WORK, browser="Chrome"), default="Firefox"), [pr])
        self.launch.side_effect = ["'Chrome' is not in PATH", "'Firefox' is not in PATH"]
        ctl.perform(mm.Action("open_url", (pr.url, pr.key)))
        self.assertEqual(self.launch.call_count, 2)
        self.system.assert_called_once_with(pr.url)  # the click is never lost
        [(title, body)] = host.notifications
        self.assertEqual(title, "Browser not found")
        self.assertIn(f"the browser of {WORK}", body)

    def test_open_all_uses_each_prs_browser(self):
        corp, own = _pr(1, host=WORK, account=WORK), _pr(2)
        ctl, _ = self._ctl(
            _cfg(HostConfig("github.com"), HostConfig(WORK, browser="Chrome")), [corp, own]
        )
        actions = next(i for i in ctl.menu_model().items if i.label == "PR actions")
        ctl.perform(actions.children[0].action)
        self.launch.assert_called_once_with("Chrome", corp.url)
        self.system.assert_called_once_with(own.url)

    def test_open_all_tries_a_broken_browser_once(self):
        prs = [_pr(n, host=WORK, account=WORK) for n in (1, 2, 3)]
        ctl, host = self._ctl(_cfg(HostConfig(WORK, browser="Chrome")), prs)
        self.launch.return_value = "'Chrome' is not in PATH"
        actions = next(i for i in ctl.menu_model().items if i.label == "PR actions")
        ctl.perform(actions.children[0].action)
        self.launch.assert_called_once()
        self.assertEqual([c.args[0] for c in self.system.call_args_list], [p.url for p in prs])
        self.assertEqual(len(host.notifications), 1)

    def test_a_failed_click_is_reported_in_quiet_hours(self):
        pr = _pr(host=WORK, account=WORK)
        ctl, host = self._ctl(_cfg(HostConfig(WORK, browser="Chrome")), [pr])
        self.launch.return_value = "'Chrome' is not in PATH"
        with mock.patch("awaitray.controller.qh_mod.is_quiet", return_value=True):
            ctl.perform(mm.Action("open_url", (pr.url, pr.key)))
        self.assertEqual(len(host.notifications), 1)

    def test_editing_a_browser_keeps_the_notification_baseline(self):
        old = _pr(1)
        ctl, host = self._ctl(_cfg(HostConfig("github.com")), [old])
        edited = copy.deepcopy(ctl.cfg)
        edited.hosts[0].browser = "firefox -P Work"
        with mock.patch.object(ctl, "tick_prs") as poll:
            ctl.on_prefs_saved(edited)
        poll.assert_not_called()  # not a scope change: no extra poll
        new = _pr(2)
        ctl.apply_poll_result(PollResult(review_prs=[old, new]))
        self.assertEqual(len(host.notifications), 1)  # the new request still notifies

    def test_only_web_links_are_opened(self):
        # Check and status links are set by whoever posted them.
        pr = _pr()
        ctl, _ = self._ctl(_cfg(HostConfig("github.com"), default="firefox"), [pr])
        for url in ("file:///etc/passwd", "--renderer-cmd-prefix=sh", "vscode://x", "javascript:1"):
            ctl.perform(mm.Action("open_url", (url, pr.key)))
        self.launch.assert_not_called()
        self.system.assert_not_called()
        ctl.perform(mm.Action("open_url", ("HTTPS://ci.example.com/run/1", pr.key)))
        self.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
