"""State changes of your PRs and their toggles."""

import tempfile
import unittest
from pathlib import Path

from awaitray import state as state_mod
from awaitray import transitions as tr
from awaitray.config import Config, HostConfig, MyPrEvents, MyPRsConfig, SnoozeEntry
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest

PR = PullRequest("github.com", "acme/web", 7, "Fix login", "https://x/7", "me")


def _det(oid="a", ci=None, review=None, mergeable="MERGEABLE", comments=0, threads=0):
    return PRDetails(PR.url, oid, ci, review, mergeable, comments, threads)


def _sequence(*states):
    """Events for each poll after the first (the baseline)."""
    prev, out = None, []
    for det in states:
        cur = tr.facts(PR, det, prev)
        out.append(tr.events(prev, cur))
        prev = cur
    return out[1:]


class EventTests(unittest.TestCase):
    def test_first_sight_is_a_silent_baseline(self):
        self.assertEqual(tr.events(None, tr.facts(PR, _det(ci="FAILURE"), None)), [])

    def test_review_changes(self):
        self.assertEqual(
            _sequence(
                _det(ci="PENDING", review="REVIEW_REQUIRED"),
                _det(ci="PENDING", review="CHANGES_REQUESTED"),
                _det(ci="PENDING", review="APPROVED"),
            ),
            [[tr.CHANGES_REQUESTED], [tr.APPROVED]],
        )

    def test_ready_to_merge_replaces_approved(self):
        self.assertEqual(
            _sequence(
                _det(ci="SUCCESS", review="REVIEW_REQUIRED"), _det(ci="SUCCESS", review="APPROVED")
            ),
            [[tr.READY_TO_MERGE]],
        )

    def test_ci_failed_then_back_to_green_through_a_rerun(self):
        self.assertEqual(
            _sequence(
                _det(ci="PENDING", review="REVIEW_REQUIRED"),
                _det(ci="FAILURE", review="REVIEW_REQUIRED"),
                _det(ci="PENDING", review="REVIEW_REQUIRED"),
                _det(ci="SUCCESS", review="REVIEW_REQUIRED"),
            ),
            [[tr.CI_FAILED], [], [tr.CI_PASSED]],
        )

    def test_flaky_reruns_on_the_same_commit_are_quiet(self):
        green = _det(ci="SUCCESS", review="REVIEW_REQUIRED")
        red = _det(ci="FAILURE", review="REVIEW_REQUIRED")
        running = _det(ci="PENDING", review="REVIEW_REQUIRED")
        self.assertEqual(_sequence(green, running, green), [[], []])
        self.assertEqual(_sequence(red, running, red), [[], []])

    def test_a_new_commit_failing_again_notifies(self):
        self.assertEqual(
            _sequence(
                _det("a", ci="FAILURE", review="REVIEW_REQUIRED"),
                _det("b", ci="PENDING", review="REVIEW_REQUIRED"),
                _det("b", ci="FAILURE", review="REVIEW_REQUIRED"),
            ),
            [[], [tr.CI_FAILED]],
        )

    def test_conflict_and_comments(self):
        self.assertEqual(
            _sequence(
                _det(ci="PENDING", comments=1),
                _det(ci="PENDING", comments=1, mergeable="CONFLICTING"),
                _det(ci="PENDING", comments=1, threads=1, mergeable="CONFLICTING"),
            ),
            [[tr.CONFLICT], [tr.NEW_COMMENTS]],
        )


class _UI:
    def __init__(self):
        self.titles = []

    def notify(self, title, body, urgency, icon):
        self.titles.append(title)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def _ctl(self, store=None, **cfg):
        cfg.setdefault("my_prs", MyPRsConfig(enabled=True))
        self.ui = _UI()
        return Controller(
            ui=self.ui,
            host=self.ui,
            cfg=Config(hosts=[HostConfig("github.com")], **cfg),
            state_store=store,
        )

    def _poll(self, ctl, det, **kw):
        ctl.apply_poll_result(PollResult(my_prs=[PR], details={PR.url: det}, **kw))

    def test_notifies_a_change_once_per_commit(self):
        ctl = self._ctl()
        self._poll(ctl, _det(review="REVIEW_REQUIRED", ci="PENDING"))
        self._poll(ctl, _det(review="APPROVED", ci="PENDING"))
        self._poll(ctl, _det(review="REVIEW_REQUIRED", ci="PENDING"))  # review dismissed
        self._poll(ctl, _det(review="APPROVED", ci="PENDING"))  # re-approved, same commit
        self.assertEqual(self.ui.titles, ["✓ Approved: acme/web #7"])

    def test_toggles_and_snoozes_mute_events(self):
        ctl = self._ctl(my_pr_events=MyPrEvents(ci_failed=False))
        self._poll(ctl, _det(ci="PENDING"))
        self._poll(ctl, _det(ci="FAILURE"))
        self.assertEqual(self.ui.titles, [])
        ctl.cfg.my_pr_events.conflict = True
        ctl.cfg.snoozed[PR.key] = SnoozeEntry(ignored=True)
        self._poll(ctl, _det(ci="FAILURE", mergeable="CONFLICTING"))
        self.assertEqual(self.ui.titles, [])

    def test_a_host_blip_keeps_the_baseline(self):
        ctl = self._ctl()
        self._poll(ctl, _det(ci="PENDING"))
        ctl.apply_poll_result(PollResult(my_failed_hosts={"github.com"}, errors=["x"]))
        self._poll(ctl, _det(ci="FAILURE"))
        self.assertEqual(self.ui.titles, ["✗ Checks failed: acme/web #7"])

    def test_disabled_my_prs_tracks_nothing(self):
        ctl = self._ctl(my_prs=MyPRsConfig(enabled=False))
        self._poll(ctl, _det(ci="PENDING"))
        self._poll(ctl, _det(ci="FAILURE"))
        self.assertEqual(self.ui.titles, [])

    def test_changes_while_the_app_was_off_notify_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            store = state_mod.StateStore(Path(d) / "state.json")
            self._poll(self._ctl(store), _det(ci="PENDING"))
            second = self._ctl(store)
            self._poll(second, _det(ci="FAILURE"))
            self._poll(second, _det(ci="FAILURE"))
        self.assertEqual(self.ui.titles, ["✗ Checks failed: acme/web #7"])


class WatchChecksTests(unittest.TestCase):
    """ "Notify when checks finish"."""

    def setUp(self):
        self.ui = _UI()
        self.ctl = Controller(
            ui=self.ui,
            host=self.ui,
            cfg=Config(hosts=[HostConfig("github.com")], my_prs=MyPRsConfig(enabled=True)),
        )

    def _poll(self, det):
        self.ctl.apply_poll_result(PollResult(my_prs=[PR], details={PR.url: det}))

    def _watch_item(self):

        def walk(items):
            for it in items:
                yield it
                yield from walk(it.children)

        return next(
            (
                i
                for i in walk(self.ctl.menu_model().items)
                if i.label == "Notify when checks finish"
            ),
            None,
        )

    def test_offered_only_while_checks_run(self):
        self._poll(_det(ci="SUCCESS"))
        self.assertIsNone(self._watch_item())
        self._poll(_det(ci="PENDING"))
        item = self._watch_item()
        self.assertFalse(item.checked)
        self.ctl.perform(item.action)
        self.assertTrue(self._watch_item().checked)

    def test_pending_to_success_notifies_once_and_clears(self):
        self._poll(_det(ci="PENDING"))
        self.ctl.toggle_watch_checks(PR.key)
        self._poll(_det(ci="PENDING"))
        self._poll(_det(ci="SUCCESS"))  # no review required: also ready, one notice
        self._poll(_det(ci="SUCCESS"))
        self.assertEqual(self.ui.titles, ["✓ Checks passed, ready to merge: acme/web #7"])
        self.assertFalse(self.ctl.is_watching_checks(PR.key))

    def test_failure_replaces_the_generic_ci_notice(self):
        self._poll(_det(ci="PENDING"))
        self.ctl.toggle_watch_checks(PR.key)
        self._poll(_det(ci="FAILURE"))
        self.assertEqual(self.ui.titles, ["✗ Checks failed: acme/web #7"])

    def test_new_commit_while_watching_keeps_watching(self):
        self._poll(_det("a", ci="PENDING"))
        self.ctl.toggle_watch_checks(PR.key)
        self._poll(_det("b", ci="PENDING"))
        self.assertTrue(self.ctl.is_watching_checks(PR.key))
        self._poll(_det("b", ci="SUCCESS", review="REVIEW_REQUIRED"))
        self.assertEqual(self.ui.titles, ["✓ Checks passed: acme/web #7"])

    def test_quiet_hours_defer_until_they_end(self):
        from unittest import mock

        from awaitray import quiet_hours as qh

        self._poll(_det(ci="PENDING"))
        self.ctl.toggle_watch_checks(PR.key)
        with mock.patch.object(qh, "is_quiet", return_value=True):
            self._poll(_det(ci="SUCCESS", review="REVIEW_REQUIRED"))
        self.assertEqual(self.ui.titles, [])
        self._poll(_det(ci="SUCCESS", review="REVIEW_REQUIRED"))
        self.assertEqual(self.ui.titles, ["✓ Checks passed: acme/web #7"])


if __name__ == "__main__":
    unittest.main()
