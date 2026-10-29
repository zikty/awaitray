import unittest
from datetime import datetime

from awaitray.config import QuietHours
from awaitray.quiet_hours import is_quiet

# Picked dates with known weekdays:
#   2026-04-29 = Wednesday
#   2026-05-02 = Saturday


class QuietHoursTests(unittest.TestCase):
    def test_disabled_is_never_quiet(self):
        qh = QuietHours(enabled=False, start="00:00", end="23:59")
        self.assertFalse(is_quiet(qh, datetime(2026, 4, 29, 12, 0)))

    def test_simple_window_inside(self):
        qh = QuietHours(enabled=True, start="09:00", end="17:00")
        self.assertTrue(is_quiet(qh, datetime(2026, 4, 29, 12, 0)))

    def test_simple_window_outside(self):
        qh = QuietHours(enabled=True, start="09:00", end="17:00")
        self.assertFalse(is_quiet(qh, datetime(2026, 4, 29, 8, 59)))
        self.assertFalse(is_quiet(qh, datetime(2026, 4, 29, 17, 0)))

    def test_wraps_past_midnight(self):
        qh = QuietHours(enabled=True, start="20:00", end="08:00")
        self.assertTrue(is_quiet(qh, datetime(2026, 4, 29, 22, 0)))
        self.assertTrue(is_quiet(qh, datetime(2026, 4, 29, 1, 0)))
        self.assertFalse(is_quiet(qh, datetime(2026, 4, 29, 9, 0)))

    def test_zero_length_window_is_not_quiet(self):
        qh = QuietHours(enabled=True, start="09:00", end="09:00")
        self.assertFalse(is_quiet(qh, datetime(2026, 4, 29, 9, 0)))

    def test_invalid_time_strings_dont_crash(self):
        qh = QuietHours(enabled=True, start="banana", end="apricot")
        # Both parse to 00:00 → zero-length window → not quiet.
        self.assertFalse(is_quiet(qh, datetime(2026, 4, 29, 12, 0)))

    def test_quiet_weekend_overrides_window(self):
        qh = QuietHours(enabled=True, start="09:00", end="17:00", quiet_weekends=True)
        self.assertTrue(is_quiet(qh, datetime(2026, 5, 2, 3, 0)))  # Saturday at 3am


if __name__ == "__main__":
    unittest.main()
