import unittest
from datetime import datetime, timedelta

from awaitray import snooze as snooze_mod
from awaitray.config import SnoozeEntry
from awaitray.snooze import (
    describe,
    expired_by_commit,
    for_duration,
    is_active,
    until_next_commit,
    until_tomorrow_morning,
)


class IsActiveTests(unittest.TestCase):
    def test_time_based_future_is_active(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        self.assertTrue(is_active(SnoozeEntry(until_iso=future)))

    def test_time_based_past_is_inactive(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        self.assertFalse(is_active(SnoozeEntry(until_iso=past)))

    def test_invalid_iso_is_inactive(self):
        self.assertFalse(is_active(SnoozeEntry(until_iso="not-a-date")))

    def test_commit_based_with_oid_stays_active(self):
        self.assertTrue(is_active(SnoozeEntry(head_oid="abc123")))

    def test_empty_entry_is_inactive(self):
        self.assertFalse(is_active(SnoozeEntry()))


class ExpiredByCommitTests(unittest.TestCase):
    def test_oid_changed_is_expired(self):
        self.assertTrue(expired_by_commit(SnoozeEntry(head_oid="abc"), "def"))

    def test_oid_unchanged_is_not_expired(self):
        self.assertFalse(expired_by_commit(SnoozeEntry(head_oid="abc"), "abc"))

    def test_no_head_oid_is_not_expired(self):
        self.assertFalse(expired_by_commit(SnoozeEntry(), "abc"))

    def test_no_current_oid_is_not_expired(self):
        self.assertFalse(expired_by_commit(SnoozeEntry(head_oid="abc"), None))


class FactoriesTests(unittest.TestCase):
    def test_for_duration_sets_future_iso(self):
        entry = for_duration(2)
        self.assertIsNotNone(entry.until_iso)
        until = datetime.fromisoformat(entry.until_iso)
        self.assertGreater(until, datetime.now() + timedelta(minutes=110))

    def test_until_tomorrow_morning_is_in_the_future(self):
        entry = until_tomorrow_morning(hour=9)
        self.assertIsNotNone(entry.until_iso)
        until = datetime.fromisoformat(entry.until_iso)
        self.assertGreater(until, datetime.now())

    def test_until_next_commit_only_sets_oid(self):
        entry = until_next_commit("abc")
        self.assertEqual(entry.head_oid, "abc")
        self.assertIsNone(entry.until_iso)


class DescribeTests(unittest.TestCase):
    def test_describes_minutes_for_short_durations(self):
        future = (datetime.now() + timedelta(minutes=30)).isoformat()
        self.assertIn("m", describe(SnoozeEntry(until_iso=future)))

    def test_describes_commit_snooze(self):
        self.assertEqual(describe(SnoozeEntry(head_oid="abc")), "until next commit")

    def test_describes_expired(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        self.assertEqual(describe(SnoozeEntry(until_iso=past)), "expired")


class NewPresetsTests(unittest.TestCase):
    def test_until_monday_from_midweek(self):
        wed = datetime(2026, 9, 23, 15, 0)  # Wednesday
        entry = snooze_mod.until_next_weekday(0, 9, now=wed)
        self.assertEqual(entry.until_iso, "2026-09-28T09:00:00")

    def test_until_monday_on_monday_before_and_after_nine(self):
        early = datetime(2026, 9, 28, 8, 0)
        late = datetime(2026, 9, 28, 10, 0)
        self.assertEqual(
            snooze_mod.until_next_weekday(0, 9, now=early).until_iso, "2026-09-28T09:00:00"
        )
        self.assertEqual(
            snooze_mod.until_next_weekday(0, 9, now=late).until_iso, "2026-10-05T09:00:00"
        )

    def test_ignored_is_always_active_and_described(self):
        entry = snooze_mod.ignore()
        self.assertTrue(snooze_mod.is_active(entry))
        self.assertEqual(snooze_mod.describe(entry), "ignored")
        self.assertFalse(snooze_mod.expired_by_activity(entry, "2099-01-01T00:00:00Z"))

    def test_activity_snooze_clears_only_on_a_newer_update(self):
        entry = snooze_mod.until_new_activity("2026-09-25T10:00:00Z")
        self.assertTrue(snooze_mod.is_active(entry))
        self.assertEqual(snooze_mod.describe(entry), "until new activity")
        self.assertFalse(snooze_mod.expired_by_activity(entry, "2026-09-25T10:00:00Z"))
        self.assertFalse(snooze_mod.expired_by_activity(entry, None))
        self.assertTrue(snooze_mod.expired_by_activity(entry, "2026-09-25T10:05:00Z"))


if __name__ == "__main__":
    unittest.main()
