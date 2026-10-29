"""Timers, retries and the error policy (wait | retry | pause)."""

import unittest
from unittest import mock

from awaitray import config as cfg_mod
from awaitray import scheduler as sched
from awaitray import snooze as snooze_mod
from awaitray.config import Config, ErrorPolicy, HostConfig, Intervals
from awaitray.controller import Controller, PollResult


class _FakeHost:
    """Records timers; `fire` runs one like the event loop would."""

    def __init__(self):
        self.timers = {}  # handle -> (seconds, callback)
        self._next = 0
        self.notifications = []

    def timeout_add_seconds(self, seconds, callback):
        self._next += 1
        self.timers[self._next] = (seconds, callback)
        return self._next

    def source_remove(self, handle):
        self.timers.pop(handle, None)

    def run_on_main(self, callback, *args):
        callback(*args)

    def default_opener(self):
        return "xdg-open"

    def fire(self, handle):
        _seconds, callback = self.timers[handle]
        if not callback():
            self.timers.pop(handle, None)

    def notify(self, title, body, urgency, icon):
        self.notifications.append(title)

    def __getattr__(self, _name):  # the rest of the UI port
        return lambda *a, **k: None


class FailureDecisionTests(unittest.TestCase):
    def test_wait_does_nothing(self):
        self.assertIsNone(sched.on_total_failure(ErrorPolicy(on_error="wait"), 5, False))

    def test_retry_backs_off_exponentially_until_retries_run_out(self):
        policy = ErrorPolicy(on_error="retry", max_retries=3, retry_backoff_seconds=30)
        delays = [sched.on_total_failure(policy, n, False) for n in (1, 2, 3, 4)]
        self.assertEqual([d and d.retry_in for d in delays], [30, 60, 120, None])

    def test_pause_after_n_failures_once(self):
        policy = ErrorPolicy(on_error="pause", pause_after_failures=3)
        self.assertIsNone(sched.on_total_failure(policy, 2, False))
        self.assertTrue(sched.on_total_failure(policy, 3, False).pause)
        self.assertIsNone(sched.on_total_failure(policy, 4, True))  # already paused


class SchedulerTests(unittest.TestCase):
    def test_reschedule_replaces_timers_and_pause_clears_them(self):
        host = _FakeHost()
        timers = sched.Scheduler(host)
        cfg = Config(intervals=Intervals(pr_check_minutes=2, fetch_minutes=10))
        noop = lambda: True  # noqa: E731
        timers.reschedule(cfg, on_prs=noop, on_fetch=noop, on_sweep=noop)
        timers.reschedule(cfg, on_prs=noop, on_fetch=noop, on_sweep=noop)
        self.assertEqual(sorted(s for s, _ in host.timers.values()), [60, 120, 600])
        cfg.paused = True
        timers.reschedule(cfg, on_prs=noop, on_fetch=noop, on_sweep=noop)
        self.assertEqual(host.timers, {})

    def test_account_info_every_half_hour(self):
        host = _FakeHost()
        timers = sched.Scheduler(host)
        noop = lambda: True  # noqa: E731
        timers.reschedule(Config(), on_prs=noop, on_fetch=noop, on_sweep=noop, on_account=noop)
        self.assertIn(1800, [s for s, _ in host.timers.values()])

    def test_one_pending_retry_at_a_time(self):
        host = _FakeHost()
        timers = sched.Scheduler(host)
        calls = []
        timers.retry(30, lambda: calls.append("a"))
        timers.retry(60, lambda: calls.append("b"))
        (handle,) = host.timers
        host.fire(handle)
        self.assertEqual(calls, ["b"])
        self.assertFalse(timers.retry_pending)
        self.assertEqual(host.timers, {})


class ControllerErrorPolicyTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(cfg_mod, "save")
        p.start()
        self.addCleanup(p.stop)
        self.host = _FakeHost()

    def _ctl(self, policy):
        cfg = Config(hosts=[HostConfig("github.com")], error_policy=policy)
        return Controller(ui=self.host, host=self.host, cfg=cfg)

    def _fail(self, ctl):
        ctl.apply_poll_result(
            PollResult(errors=["github.com: boom"], total_failure=True, failed_hosts={"github.com"})
        )

    def test_retry_policy_schedules_a_poll_and_success_cancels_it(self):
        ctl = self._ctl(ErrorPolicy(on_error="retry", retry_backoff_seconds=30))
        self._fail(ctl)
        self.assertEqual([s for s, _ in self.host.timers.values()], [30])
        with mock.patch.object(ctl, "tick_prs") as tick:
            self.host.fire(next(iter(self.host.timers)))
        tick.assert_called_once()
        self._fail(ctl)
        ctl.apply_poll_result(PollResult())
        self.assertEqual(self.host.timers, {})

    def test_pause_policy_pauses_and_notifies(self):
        ctl = self._ctl(ErrorPolicy(on_error="pause", pause_after_failures=2))
        self._fail(ctl)
        self.assertFalse(ctl.cfg.paused)
        self._fail(ctl)
        self.assertTrue(ctl.cfg.paused)
        self.assertIn("awaitray paused", self.host.notifications)


class SnoozeEntryTests(unittest.TestCase):
    def test_every_menu_choice_builds_an_entry(self):
        for _label, kind in snooze_mod.CHOICES:
            entry = snooze_mod.entry_for(kind, updated_at="2026-01-01T00:00:00Z", head_oid="abc")
            self.assertIsNotNone(entry, kind)
            self.assertTrue(snooze_mod.is_active(entry), kind)

    def test_missing_data_falls_back_to_one_hour(self):
        for kind in ("next_commit", "activity"):
            entry = snooze_mod.entry_for(kind)
            self.assertIsNotNone(entry.until_iso, kind)
        self.assertIsNone(snooze_mod.entry_for("bogus"))


if __name__ == "__main__":
    unittest.main()
