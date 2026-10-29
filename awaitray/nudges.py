"""Scheduled nudges: reminders for long-waiting reviews and the daily
digest.

Pure decisions over the config, the current PRs and a clock; the controller
keeps the timestamps (persisted in state.json) and sends the notices. Both
are deferred by quiet hours: the controller simply doesn't ask while quiet,
so they go out on the first check after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config as cfg_mod
from .github import PullRequest
from .notice import Notice
from .sounds import INFO, REVIEW

BATCH_LIMIT = 3  # more reminders than this at once → one summary


def reminders_due(
    cfg: cfg_mod.Config,
    long_waiting: list[PullRequest],
    reminded: dict[str, str],
    now: datetime,
) -> list[PullRequest]:
    """Long-waiting review requests not reminded within the cooldown.
    `reminded` holds the last reminder per PR key, or when it was first seen."""
    if not cfg.reminders.enabled or cfg.waiting.long_wait_days <= 0:
        return []
    cooldown = timedelta(hours=max(1, cfg.reminders.cooldown_hours))
    due = []
    for pr in long_waiting:
        last = _parse(reminded.get(pr.key))
        if last is None or now - last >= cooldown:
            due.append(pr)
    return due


def reminder_notices(prs: list[PullRequest], days: int) -> list[Notice]:
    waiting = f"waiting for {days}+ day{'s' if days != 1 else ''}"
    if len(prs) > BATCH_LIMIT:
        shown = "\n".join(f"{pr.repo} #{pr.number} — {pr.title}" for pr in prs[:BATCH_LIMIT])
        more = len(prs) - BATCH_LIMIT
        return [Notice(f"{len(prs)} reviews {waiting}", f"{shown}\n…and {more} more", sound=REVIEW)]
    return [
        Notice(f"Review {waiting}: {pr.repo} #{pr.number}", pr.title, "normal", sound=REVIEW)
        for pr in prs
    ]


def digest_due(cfg: cfg_mod.DigestConfig, now: datetime, last_date: str | None) -> bool:
    """Once a day, from the configured time on (a missed digest goes out
    late rather than never), weekdays only if asked."""
    if not cfg.enabled or last_date == now.date().isoformat():
        return False
    if cfg.weekdays_only and now.weekday() >= 5:
        return False
    try:
        hour, minute = (int(x) for x in cfg.time.split(":"))
    except ValueError:
        return False
    return (now.hour, now.minute) >= (hour, minute)


@dataclass(frozen=True)
class DigestCounts:
    reviews: int = 0
    long_waiting: int = 0
    long_wait_days: int = 0
    needs_action: int = 0
    ready_to_merge: int = 0


def digest_notice(c: DigestCounts) -> Notice | None:
    """One line of what needs attention; None when nothing does."""
    parts = []
    if c.reviews:
        waiting = f" ({c.long_waiting} for {c.long_wait_days}+ days)" if c.long_waiting else ""
        parts.append(f"{c.reviews} review{'s' if c.reviews != 1 else ''} waiting{waiting}")
    if c.needs_action:
        parts.append(
            f"{c.needs_action} of your PR{'s' if c.needs_action != 1 else ''} need"
            f"{'s' if c.needs_action == 1 else ''} action"
        )
    if c.ready_to_merge:
        parts.append(f"{c.ready_to_merge} ready to merge")
    if not parts:
        return None
    return Notice("awaitray: today", " · ".join(parts), "low", sound=INFO)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None
