"""Triage the GitHub inbox from the menu."""

import unittest
from unittest import mock

from awaitray import account_info as ai
from awaitray import github as gh
from awaitray import inbox
from awaitray import menu_model as mm
from awaitray.config import Config, HostConfig
from awaitray.controller import Controller
from awaitray.runner import ImmediateRunner

API = "https://api.github.com/repos"


def _thread(tid, kind="PullRequest", path="pulls/12", reason="review_requested"):
    return {
        "id": tid,
        "reason": reason,
        "subject": {"title": f"Thread {tid}", "type": kind, "url": f"{API}/acme/web/{path}"},
        "repository": {"full_name": "acme/web", "html_url": "https://github.com/acme/web"},
    }


class ParseTests(unittest.TestCase):
    def test_threads_link_to_their_pages(self):
        items = inbox.parse(
            "github.com",
            [
                _thread("1"),
                _thread("2", "Issue", "issues/7", "mention"),
                _thread("3", "Release", "releases/99", "subscribed"),
            ],
        )
        self.assertEqual(
            [i.url for i in items],
            [
                "https://github.com/acme/web/pull/12",
                "https://github.com/acme/web/issues/7",
                "https://github.com/acme/web",  # no page of its own: the repo
            ],
        )
        self.assertEqual(items[0].label(), "web: Thread 1 · review requested")
        self.assertEqual(items[1].label(), "web: Thread 2 · mentioned")

    def test_ghes_api_urls(self):
        raw = _thread("1")
        raw["subject"]["url"] = "https://ghe.corp/api/v3/repos/acme/web/pulls/5"
        self.assertEqual(inbox.parse("ghe.corp", [raw])[0].url, "https://ghe.corp/acme/web/pull/5")

    def test_odd_shapes_are_skipped(self):
        self.assertEqual(inbox.parse("github.com", {"message": "x"}), ())
        items = inbox.parse("github.com", [None, {"no": "id"}, {"id": "../../user"}, {"id": 5}])
        self.assertEqual([(i.thread_id, i.title) for i in items], [("5", "(no title)")])

    def test_a_thread_id_that_isnt_one_never_reaches_the_api(self):
        with mock.patch.object(gh, "rest") as rest:
            for tid in ("../../user", "1/subscription", "", "\u0661"):
                with self.assertRaises(gh.GhError):
                    inbox.act("github.com", inbox.DONE, tid)
        rest.assert_not_called()


class ActTests(unittest.TestCase):
    def _calls(self, fn, *args):
        with mock.patch.object(gh, "rest") as rest:
            fn(*args)
        return [(c.args[0], c.args[1], c.args[2], c.kwargs["method"]) for c in rest.call_args_list]

    def test_each_action_as_the_account(self):
        acct = "me-corp@github.com"
        thread = "notifications/threads/42"
        self.assertEqual(
            self._calls(inbox.act, acct, inbox.READ, "42"),
            [("github.com", thread, "me-corp", "PATCH")],
        )
        self.assertEqual(
            self._calls(inbox.act, acct, inbox.DONE, "42"),
            [("github.com", thread, "me-corp", "DELETE")],
        )
        self.assertEqual(
            self._calls(inbox.act, "ghe.corp", inbox.UNSUBSCRIBE, "42"),
            [
                ("ghe.corp", f"{thread}/subscription", "", "DELETE"),
                ("ghe.corp", thread, "", "DELETE"),
            ],
        )

    def test_mark_all_only_up_to_when_it_was_read(self):
        with mock.patch.object(gh, "rest") as rest:
            inbox.mark_all_read("github.com", "2026-10-01T07:00:00Z")
        self.assertEqual(rest.call_args.kwargs["method"], "PUT")
        self.assertEqual(rest.call_args.kwargs["body"]["last_read_at"], "2026-10-01T07:00:00Z")


class _Host:
    def __init__(self):
        self.notifications = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append(body)

    def run_on_main(self, fn, *args):
        fn(*args)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.host = _Host()
        self.ctl = Controller(
            ui=self.host,
            host=self.host,
            cfg=Config(hosts=[HostConfig("github.com")]),
            runner=ImmediateRunner(),
        )
        threads = inbox.parse("github.com", [_thread(str(n)) for n in range(1, 13)])
        self.ctl.inbox.apply(
            [ai.AccountInfo("github.com", "github.com", unread=12, inbox=threads, inbox_as_of="T0")]
        )

    def _inbox(self):
        for item in self.ctl.menu_model().items:
            if item.label.startswith("GitHub inbox"):
                return item
        raise AssertionError("no inbox line")

    def test_a_submenu_of_threads_capped_with_more(self):
        menu = self._inbox()
        self.assertEqual(menu.kind, mm.SUBMENU)
        self.assertEqual(menu.label, "GitHub inbox · 12 unread")
        labels = [c.label for c in menu.children]
        self.assertEqual(labels[:2], ["Open GitHub inbox", "Mark all as read"])
        self.assertEqual(labels[3], "web: Thread 1 · review requested")
        self.assertEqual(labels[-1], "2 more on GitHub…")
        thread = menu.children[3]
        self.assertEqual(
            [c.label for c in thread.children],
            ["Open", "Mark as read", "Mark as done", "Unsubscribe"],
        )

    def test_done_leaves_the_menu_at_once(self):
        done = self._inbox().children[3].children[2].action
        with mock.patch.object(gh, "rest") as rest:
            self.ctl.perform(done)
        self.assertEqual(rest.call_args.kwargs["method"], "DELETE")
        menu = self._inbox()
        self.assertEqual(menu.label, "GitHub inbox · 11 unread")
        self.assertNotIn("web: Thread 1 · review requested", [c.label for c in menu.children])

    def test_open_opens_the_page_and_marks_it_read(self):
        open_ = self._inbox().children[3].children[0].action
        with (
            mock.patch.object(gh, "rest") as rest,
            mock.patch("awaitray.features.links.webbrowser.open") as browser,
        ):
            self.ctl.perform(open_)
        browser.assert_called_once_with("https://github.com/acme/web/pull/12")
        self.assertEqual(rest.call_args.kwargs["method"], "PATCH")

    def test_mark_all_read_empties_it(self):
        with mock.patch.object(gh, "rest") as rest:
            self.ctl.perform(self._inbox().children[1].action)
        self.assertEqual(rest.call_args.kwargs["body"]["last_read_at"], "T0")
        menu = self._inbox()
        self.assertEqual((menu.kind, menu.label), (mm.ACTION, "GitHub inbox · all read"))

    def test_github_says_no_tells_why_and_reloads(self):
        read = self._inbox().children[3].children[1].action
        with (
            mock.patch.object(gh, "rest", side_effect=gh.GhError("HTTP 403")),
            mock.patch.object(ai, "fetch", return_value=[]) as reload,
        ):
            self.ctl.perform(read)
        self.assertEqual(self.host.notifications, ["HTTP 403"])
        reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
