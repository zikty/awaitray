"""Unread notifications per account (AI quotas are test_ai_usage.py's)."""

import unittest
from unittest import mock

from awaitray import account_info as ai
from awaitray import github as gh
from awaitray import menu_model as mm
from awaitray.config import AccountInfoConfig, Config, HostConfig
from awaitray.controller import Controller


class LabelTests(unittest.TestCase):
    def test_inbox_label(self):
        self.assertEqual(ai.inbox_label(0), "GitHub inbox · all read")
        self.assertEqual(ai.inbox_label(7), "GitHub inbox · 7 unread")
        self.assertEqual(ai.inbox_label(50), "GitHub inbox · 50+ unread")


class FetchTests(unittest.TestCase):
    def _fetch(self, cfg, answer):
        def rest_get(host, path, user=""):
            if isinstance(answer, Exception):
                raise answer
            return answer

        with (
            mock.patch.object(gh, "gh_available", return_value=True),
            mock.patch.object(gh, "rest_get", side_effect=rest_get) as call,
        ):
            return ai.fetch(cfg), call

    def test_each_enabled_account(self):
        cfg = Config(
            hosts=[
                HostConfig("github.com"),
                HostConfig("ghe.corp"),
                HostConfig("old.corp", enabled=False),
            ]
        )
        infos, call = self._fetch(cfg, [{}] * 3)
        self.assertEqual(
            [(i.account, i.unread) for i in infos], [("github.com", 3), ("ghe.corp", 3)]
        )
        self.assertEqual({c.args[1].split("?")[0] for c in call.call_args_list}, {"notifications"})

    def test_errors_just_hide_the_line(self):
        infos, _ = self._fetch(Config(hosts=[HostConfig("github.com")]), gh.GhError("x"))
        self.assertIsNone(infos[0].unread)

    def test_turned_off_asks_nothing(self):
        infos, call = self._fetch(Config(account_info=AccountInfoConfig(inbox=False)), [])
        self.assertEqual(infos, [])
        call.assert_not_called()


class RestGetTests(unittest.TestCase):
    def _gh_run(self, code, out="", err=""):
        done = mock.Mock(returncode=code, stdout=out, stderr=err)
        return mock.patch.object(gh.subprocess, "run", return_value=done)

    def test_404_is_not_found_and_other_failures_are_errors(self):
        with self._gh_run(1, err="gh: Not Found (HTTP 404)"), self.assertRaises(gh.NotFoundError):
            gh.rest_get("github.com", "copilot_internal/user")
        with self._gh_run(1, err="HTTP 401"), self.assertRaises(gh.GhError):
            gh.rest_get("github.com", "notifications")

    def test_json_and_account_environment(self):
        with (
            mock.patch.object(gh, "_account_env", return_value={"GH_TOKEN": "t"}) as env,
            self._gh_run(0, out='[{"id": 1}]') as run,
        ):
            self.assertEqual(gh.rest_get("github.com", "notifications", "me"), [{"id": 1}])
        env.assert_called_once_with("github.com", "me")
        self.assertEqual(run.call_args.kwargs["env"], {"GH_TOKEN": "t"})
        self.assertNotIn("t", run.call_args.args[0])  # the token never goes on the command line


class _UI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def _ctl(self, **cfg):
        ui = _UI()
        return Controller(ui=ui, host=ui, cfg=Config(**cfg))

    def _labels(self, ctl):
        return [i.label for i in ctl.menu_model().items]

    def test_the_inbox_line_opens_it_in_the_accounts_browser(self):
        ctl = self._ctl()
        ctl.inbox.apply([ai.AccountInfo("github.com", "github.com", 4)])
        items = {i.label: i for i in ctl.menu_model().items}
        inbox = items["GitHub inbox · 4 unread"]
        self.assertEqual(inbox.action.args, ("https://github.com/notifications", "", "github.com"))

    def test_several_accounts_are_named(self):
        ctl = self._ctl()
        ctl.inbox.apply(
            [
                ai.AccountInfo("github.com", "github.com", unread=1),
                ai.AccountInfo("me-corp@github.com", "github.com", unread=0),
            ]
        )
        labels = self._labels(ctl)
        self.assertIn("github.com: GitHub inbox · 1 unread", labels)
        self.assertIn("me-corp@github.com: GitHub inbox · all read", labels)

    def test_nothing_known_or_turned_off_adds_nothing(self):
        ctl = self._ctl()
        before = self._labels(ctl)
        ctl.inbox.apply([ai.AccountInfo("github.com", "github.com")])
        self.assertEqual(self._labels(ctl), before)
        off = self._ctl(account_info=AccountInfoConfig(inbox=False))
        off.inbox.apply([ai.AccountInfo("github.com", "github.com", unread=3)])
        self.assertNotIn("GitHub inbox · 3 unread", self._labels(off))


class MenuModelTests(unittest.TestCase):
    def test_the_inbox_sits_right_under_the_status_line(self):
        ui = _UI()
        ctl = Controller(ui=ui, host=ui, cfg=Config())
        ctl.inbox.apply([ai.AccountInfo("github.com", "github.com", unread=2)])
        items = ctl.menu_model().items
        self.assertEqual(items[0].kind, mm.STATUS)
        self.assertEqual(items[1].label, "GitHub inbox · 2 unread")
        self.assertEqual(items[2], mm.SEP)  # then the PR sections


if __name__ == "__main__":
    unittest.main()
