"""Snooze logic for PRs.

Each entry in `Config.snoozed` maps a PR key (e.g. `github.com:org/repo#123`)
to a `SnoozeEntry`. There are four kinds of snooze:

* time-based: `until_iso` set; clears automatically once the time passes
* commit-based: `head_oid` set; clears as soon as the PR's head ref changes
* activity-based: `updated_at` set; clears when the PR's updatedAt moves on
  (new commits, comments, reviews…)
* ignored: `ignored` set; stays until removed by hand

`SnoozeEntry` may set several: e.g. snooze for 8h *or* until next commit,
whichever comes first.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .config import SnoozeEntry


def is_active(entry: SnoozeEntry, now: datetime | None = None) -> bool:
    """True if the snooze still hides the PR. Time-based entries auto-expire."""
    if entry.ignored:
        return True
    now = now or datetime.now()
    if entry.until_iso:
        try:
            until = datetime.fromisoformat(entry.until_iso)
        except ValueError:
            return False
        return now < until
    # Commit/activity-based entries stay active until the PR moves.
    return entry.head_oid is not None or entry.updated_at is not None


def expired_by_commit(entry: SnoozeEntry, current_oid: str | None) -> bool:
    """True if a commit-based snooze should be cleared because the head moved."""
    if entry.head_oid is None or current_oid is None:
        return False
    return entry.head_oid != current_oid


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def expired_by_activity(entry: SnoozeEntry, current_updated_at: str | None) -> bool:
    """True if an "until new activity" snooze should clear: the PR was updated
    after it was snoozed."""
    if entry.ignored:
        return False
    then, now = _parse_ts(entry.updated_at), _parse_ts(current_updated_at)
    return then is not None and now is not None and now > then


def for_duration(hours: float) -> SnoozeEntry:
    until = datetime.now() + timedelta(hours=hours)
    return SnoozeEntry(until_iso=until.isoformat(timespec="seconds"))


def until_tomorrow_morning(hour: int = 9) -> SnoozeEntry:
    now = datetime.now()
    target_today = datetime.combine(now.date(), time(hour, 0))
    target = target_today + timedelta(days=1) if now >= target_today else target_today
    return SnoozeEntry(until_iso=target.isoformat(timespec="seconds"))


def until_next_weekday(weekday: int = 0, hour: int = 9, now: datetime | None = None) -> SnoozeEntry:
    """Until the next `weekday` (0 = Monday) at `hour`:00. Today counts if
    that time hasn't passed yet."""
    now = now or datetime.now()
    days = (weekday - now.weekday()) % 7
    target = datetime.combine(now.date() + timedelta(days=days), time(hour, 0))
    if target <= now:
        target += timedelta(days=7)
    return SnoozeEntry(until_iso=target.isoformat(timespec="seconds"))


def until_new_activity(updated_at: str) -> SnoozeEntry:
    return SnoozeEntry(updated_at=updated_at)


def ignore() -> SnoozeEntry:
    return SnoozeEntry(ignored=True)


def until_next_commit(head_oid: str) -> SnoozeEntry:
    return SnoozeEntry(head_oid=head_oid)


# Snooze menu entries (label, kind for `entry_for`), shared by both backends
# through the menu model so the menus can't drift.
CHOICES: list[tuple[str, str]] = [
    ("For 1 hour", "1h"),
    ("For 3 hours", "3h"),
    ("Until tomorrow 09:00", "tomorrow"),
    ("Until Monday 09:00", "monday"),
    ("Until new commits", "next_commit"),
    ("Until new activity", "activity"),
    ("Ignore this PR", "ignore"),
]


def entry_for(
    kind: str, *, updated_at: str | None = None, head_oid: str | None = None
) -> SnoozeEntry | None:
    """The entry for a `CHOICES` kind, or None for an unknown kind.

    Without the data a kind needs (no head oid yet, no updatedAt), falls back
    to one hour rather than hiding the PR forever.
    """
    if kind == "1h":
        return for_duration(1)
    if kind == "3h":
        return for_duration(3)
    if kind == "tomorrow":
        return until_tomorrow_morning()
    if kind == "monday":
        return until_next_weekday(0)
    if kind == "activity":
        return until_new_activity(updated_at) if updated_at else for_duration(1)
    if kind == "ignore":
        return ignore()
    if kind == "next_commit":
        return until_next_commit(head_oid) if head_oid else for_duration(1)
    return None


def describe(entry: SnoozeEntry, now: datetime | None = None) -> str:
    if entry.ignored:
        return "ignored"
    now = now or datetime.now()
    if entry.until_iso:
        try:
            until = datetime.fromisoformat(entry.until_iso)
        except ValueError:
            return "snoozed"
        delta = until - now
        if delta.total_seconds() <= 0:
            return "expired"
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        if h >= 24:
            return until.strftime("until %a %H:%M")
        if h > 0:
            return f"for {h}h{m:02d}"
        return f"for {m}m"
    if entry.head_oid:
        return "until next commit"
    if entry.updated_at:
        return "until new activity"
    return "snoozed"
