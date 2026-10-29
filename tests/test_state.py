import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awaitray import state as state_mod
from awaitray.config import Config, Filters, HostConfig
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest


def _pr(number, host="github.com", repo="acme/web"):
    return PullRequest(
        host=host,
        repo=repo,
        number=number,
        title=f"PR {number}",
        url=f"https://{host}/{repo}/pull/{number}",
        author="alice",
        created_at="2026-09-01T10:00:00Z",
    )


class _RecordingUI:
    def __init__(self):
        self.notifications = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append((title, body))

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"
        self.store = state_mod.StateStore(self.path)

    def _run(self, cfg=None):
        """Start an app 'process': a new controller over the same state file."""
        self.ui = _RecordingUI()
        return Controller(ui=self.ui, host=self.ui, cfg=cfg or Config(), state_store=self.store)

    def _poll(self, ctl, review=(), details=None):
        ctl.apply_poll_result(PollResult(review_prs=list(review), details=details or {}))

    def _titles(self):
        return [t for t, _ in self.ui.notifications]


class StateStoreTests(_StoreCase):
    def test_round_trip(self):
        pr = _pr(1)
        det = PRDetails(
            pr.url,
            "oid",
            "SUCCESS",
            "APPROVED",
            "MERGEABLE",
            2,
            checks=(("build", "SUCCESS", "https://ci/1"),),
            latest_activity_url="https://c/1",
        )
        self.store.save(
            state_mod.Snapshot(
                scope="s",
                saved_at="2026-09-25T10:00:00+00:00",
                review_keys={pr.key},
                my_keys=None,
                review_prs=[pr],
                my_prs=[],
                details={pr.url: det},
            )
        )
        snap = self.store.load()
        self.assertEqual(snap.review_keys, {pr.key})
        self.assertIsNone(snap.my_keys)
        self.assertEqual(snap.review_prs, [pr])
        self.assertEqual(snap.details[pr.url], det)
        hash(snap.details[pr.url].checks)  # tuples again: the menu model hashes them

    def test_missing_corrupt_or_foreign_files_load_as_none(self):
        self.assertIsNone(self.store.load())
        self.path.write_text("{ nope")
        self.assertIsNone(self.store.load())
        self.path.write_text(json.dumps({"version": 999}))
        self.assertIsNone(self.store.load())

    def test_unknown_fields_are_ignored(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": state_mod.FORMAT_VERSION,
                    "scope": "s",
                    "saved_at": "",
                    "review_keys": ["k"],
                    "review_prs": [{**_pr(1).__dict__, "future": 1}, {"junk": True}],
                    "details": {"u": {"url": "u", "head_oid": None, "ci_state": None, "extra": 1}},
                }
            )
        )
        snap = self.store.load()
        self.assertEqual([p.number for p in snap.review_prs], [1])
        self.assertIn("u", snap.details)


class RestartTests(_StoreCase):
    def test_restart_with_same_prs_does_not_notify(self):
        first = self._run()
        self._poll(first, [_pr(1), _pr(2)])  # silent baseline, persisted
        second = self._run()
        self._poll(second, [_pr(1), _pr(2)])
        self.assertEqual(self._titles(), [])

    def test_pr_that_arrived_while_off_notifies(self):
        first = self._run()
        self._poll(first, [_pr(1)])
        second = self._run()
        self._poll(second, [_pr(1), _pr(2)])
        self.assertEqual(self._titles(), ["New PR review request"])

    def test_cached_prs_render_before_the_first_poll(self):
        first = self._run()
        pr = _pr(1)
        self._poll(first, [pr], {pr.url: PRDetails(pr.url, "oid", "FAILURE", None)})
        second = self._run()
        self.assertEqual([p.number for p in second.current_prs], [1])
        self.assertEqual(second.details_for(pr.url).ci_state, "FAILURE")
        self.assertIn("refreshing", second.last_status_text)
        self.assertEqual(self._titles(), [])  # cache never notifies

    def test_changed_scope_starts_fresh_and_silent(self):
        first = self._run(Config(filters=Filters(ignored_authors=["alice"])))
        self._poll(first, [])
        second = self._run(Config(filters=Filters(ignored_authors=[])))
        self.assertEqual(second.current_prs, [])
        self._poll(second, [_pr(1)])  # visible now because of the filter change
        self.assertEqual(self._titles(), [])

    def test_empty_queue_is_persisted_as_empty_not_unknown(self):
        first = self._run()
        self._poll(first, [])
        second = self._run()
        self._poll(second, [_pr(1)])  # 0 -> 1 across a restart must notify
        self.assertEqual(self._titles(), ["New PR review request"])

    def test_injected_config_without_store_never_persists(self):
        with mock.patch.object(state_mod, "StateStore") as real_store:
            ctl = Controller(
                ui=_RecordingUI(), host=_RecordingUI(), cfg=Config(hosts=[HostConfig("github.com")])
            )
            self._poll(ctl, [_pr(1)])
        real_store.assert_not_called()


class BatchNotificationTests(_StoreCase):
    def test_many_new_requests_send_one_summary(self):
        ctl = self._run()
        self._poll(ctl, [])
        self._poll(ctl, [_pr(i) for i in range(1, 7)])
        self.assertEqual(len(self.ui.notifications), 1)
        title, body = self.ui.notifications[0]
        self.assertEqual(title, "6 new PR review requests")
        self.assertIn("and 3 more", body)

    def test_small_batches_notify_individually(self):
        ctl = self._run()
        self._poll(ctl, [])
        self._poll(ctl, [_pr(1), _pr(2), _pr(3)])
        self.assertEqual(self._titles(), ["New PR review request"] * 3)


if __name__ == "__main__":
    unittest.main()
