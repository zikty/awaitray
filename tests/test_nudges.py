"""Reminders for long-waiting reviews and the daily digest."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from awaitray import nudges
from awaitray import quiet_hours as qh
from awaitray.config import (
    Config,
    DigestConfig,
    HostConfig,
    MyPRsConfig,
    Reminders,
    SnoozeEntry,
    Waiting,
)
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest

# apply_poll_result also runs the nudges on the real clock, so the
# controller tests work relative to it; the pure digest tests use a Monday.
NOW = datetime.now()
MONDAY = datetime(2026, 9, 28, 10, 0)


def _pr(n, waited_days=3):
    requested = datetime.now(timezone.utc) - timedelta(days=waited_days)
    return PullRequest(
        "github.com",
        "acme/web",
        n,
        f"PR {n}",
        f"https://x/{n}",
        "bob",
        requested_at=requested.isoformat(),
    )


class _UI:
    def __init__(self):
        self.titles = []

    def notify(self, title, body, urgency, icon):
        self.titles.append(title)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.ui = _UI()
        self.ctl = Controller(
            ui=self.ui, host=self.ui, cfg=Config(hosts=[HostConfig("github.com")])
        )

    def _poll(self, *prs):
        self.ctl.apply_poll_result(PollResult(review_prs=list(prs)))

    def _reminders(self):
        return [t for t in self.ui.titles if "waiting for" in t]

    def test_first_reminder_a_cooldown_after_first_sight_then_every_cooldown(self):
        self._poll(_pr(1))
        self.ctl.run_nudges(NOW)
        self.assertEqual(self._reminders(), [])  # never on first sight
        self.ctl.run_nudges(NOW + timedelta(hours=25))
        self.assertEqual(self._reminders(), ["Review waiting for 2+ days: acme/web #1"])
        self.ctl.run_nudges(NOW + timedelta(hours=30))
        self.assertEqual(len(self._reminders()), 1)
        self.ctl.run_nudges(NOW + timedelta(hours=50))
        self.assertEqual(len(self._reminders()), 2)

    def test_recent_requests_snoozed_prs_and_disabled_setting_never_remind(self):
        self._poll(_pr(1, waited_days=0.5), _pr(2))
        self.ctl.cfg.snoozed[_pr(2).key] = SnoozeEntry(ignored=True)
        self.ctl.run_nudges(NOW)
        self.ctl.run_nudges(NOW + timedelta(days=2))
        self.assertEqual(self._reminders(), [])
        self.ctl.cfg.snoozed.clear()
        self.ctl.cfg.reminders = Reminders(enabled=False)
        self.ctl.run_nudges(NOW + timedelta(days=4))
        self.assertEqual(self._reminders(), [])

    def test_quiet_hours_defer_and_many_become_one_summary(self):
        self._poll(*[_pr(n) for n in range(1, 6)])
        self.ctl.run_nudges(NOW)
        with mock.patch.object(qh, "is_quiet", return_value=True):
            self.ctl.run_nudges(NOW + timedelta(days=2))
        self.assertEqual(self._reminders(), [])
        self.ctl.run_nudges(NOW + timedelta(days=2, hours=1))
        self.assertEqual(self._reminders(), ["5 reviews waiting for 2+ days"])

    def test_long_wait_off_means_no_reminders(self):
        self.ctl.cfg.waiting = Waiting(long_wait_days=0)
        self._poll(_pr(1, waited_days=30))
        self.ctl.run_nudges(NOW)
        self.ctl.run_nudges(NOW + timedelta(days=3))
        self.assertEqual(self._reminders(), [])


class DigestTests(unittest.TestCase):
    def test_due_once_a_day_from_its_time_on_weekdays(self):
        cfg = DigestConfig(enabled=True, time="09:00")
        self.assertFalse(nudges.digest_due(cfg, MONDAY.replace(hour=8, minute=59), None))
        self.assertTrue(nudges.digest_due(cfg, MONDAY, None))
        self.assertFalse(nudges.digest_due(cfg, MONDAY, MONDAY.date().isoformat()))
        saturday = MONDAY + timedelta(days=5)
        self.assertFalse(nudges.digest_due(cfg, saturday, None))
        self.assertTrue(nudges.digest_due(replace(cfg, weekdays_only=False), saturday, None))
        self.assertFalse(nudges.digest_due(replace(cfg, enabled=False), MONDAY, None))

    def test_text(self):
        notice = nudges.digest_notice(
            nudges.DigestCounts(
                reviews=3, long_waiting=1, long_wait_days=2, needs_action=2, ready_to_merge=1
            )
        )
        self.assertEqual(
            notice.body,
            "3 reviews waiting (1 for 2+ days) · 2 of your PRs need action · 1 ready to merge",
        )
        self.assertIsNone(nudges.digest_notice(nudges.DigestCounts()))

    def test_controller_sends_it_once_with_fresh_data(self):
        ui = _UI()
        cfg = Config(
            hosts=[HostConfig("github.com")],
            my_prs=MyPRsConfig(enabled=True),
            digest=DigestConfig(enabled=True, time="00:00", weekdays_only=False),
        )
        ctl = Controller(ui=ui, host=ui, cfg=cfg)

        class _Frozen(datetime):  # the whole test happens at MONDAY 10:00
            @classmethod
            def now(cls, tz=None):
                return MONDAY.replace(tzinfo=tz) if tz else MONDAY

        with mock.patch("awaitray.controller.datetime", _Frozen):
            ctl.run_nudges()  # no poll yet: nothing to summarize
            self.assertEqual(ui.titles, [])
            mine = replace(_pr(9), author="me")
            det = {mine.url: PRDetails(mine.url, "o", "FAILURE")}
            ctl.apply_poll_result(PollResult(review_prs=[_pr(1)], my_prs=[mine], details=det))
            ctl.run_nudges()  # later the same day
        self.assertEqual(ui.titles.count("awaitray: today"), 1)


if __name__ == "__main__":
    unittest.main()
