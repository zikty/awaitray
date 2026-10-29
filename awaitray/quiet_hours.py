"""Quiet hours: a window during which notifications are suppressed.

Timers keep running so the menu and icon stay current — only `notifier.notify`
is gated on this. Windows wrapping past midnight (e.g. 20:00 → 08:00) are
supported.
"""

from __future__ import annotations

from datetime import datetime, time

from .config import QuietHours


def _parse_hm(s: str) -> time:
    try:
        hh, mm = s.split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return time(0, 0)


def is_quiet(qh: QuietHours, now: datetime | None = None) -> bool:
    if not qh.enabled:
        return False
    now = now or datetime.now()
    if qh.quiet_weekends and now.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return True

    start = _parse_hm(qh.start)
    end = _parse_hm(qh.end)
    cur = now.time()

    if start == end:
        return False  # zero-length window

    if start < end:
        return start <= cur < end

    # Wraps past midnight (e.g. 20:00 → 08:00)
    return cur >= start or cur < end
