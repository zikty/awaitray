"""What a PR's state means for the user: sections, glyphs, labels, age.

Pure functions over `PullRequest` + `PRDetails` (no I/O, no toolkit), shared
by the menu model and anything else that needs the same vocabulary. The
rules are the ones in docs/ui-spec.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .github import PRDetails, PullRequest

# Sections for your own PRs, in menu order.
NEEDS_ACTION = "needs_action"
READY_TO_MERGE = "ready_to_merge"
WAITING = "waiting"
DRAFTS = "drafts"
MY_SECTIONS = (NEEDS_ACTION, READY_TO_MERGE, WAITING, DRAFTS)

GLYPH_ACTION = "⚠"
GLYPH_FAILING = "✗"
GLYPH_RUNNING = "●"
GLYPH_GREEN = "✓"
GLYPH_NONE = "○"


def _ci(details: PRDetails | None) -> str | None:
    return details.ci_state if details else None


def _review(details: PRDetails | None) -> str | None:
    return details.review_decision if details else None


def _conflict(details: PRDetails | None) -> bool:
    return bool(details and details.mergeable == "CONFLICTING")


def classify_mine(pr: PullRequest, details: PRDetails | None) -> str:
    """Section for one of *your* PRs.

    - Drafts: any draft, whatever its state.
    - Needs your action: changes requested, CI failing, or a merge conflict.
    - Ready to merge: approved, or no review required (no decision), with CI
      green or absent and no conflict.
    - Waiting: the rest (review required, CI running).
    """
    if pr.is_draft:
        return DRAFTS
    ci, review = _ci(details), _review(details)
    if review == "CHANGES_REQUESTED" or ci == "FAILURE" or _conflict(details):
        return NEEDS_ACTION
    if review in ("APPROVED", None) and ci in ("SUCCESS", None):
        return READY_TO_MERGE
    return WAITING


def glyph(pr: PullRequest, details: PRDetails | None, mine: bool = False) -> str:
    """One state glyph per row (first match wins, see the spec's legend)."""
    if mine and classify_mine(pr, details) == NEEDS_ACTION:
        return GLYPH_ACTION
    ci = _ci(details)
    if ci == "FAILURE":
        return GLYPH_FAILING
    if ci == "PENDING":
        return GLYPH_RUNNING
    if ci == "SUCCESS":
        return GLYPH_GREEN
    return GLYPH_NONE


def state_label(pr: PullRequest, details: PRDetails | None) -> str:
    """Short state for your own PRs, shown where review rows show the author."""
    section = classify_mine(pr, details)
    ci, review = _ci(details), _review(details)
    if section == DRAFTS:
        return "draft"
    if section == NEEDS_ACTION:
        if review == "CHANGES_REQUESTED":
            return "changes requested"
        if ci == "FAILURE":
            return "checks failing"
        return "conflict"
    if section == READY_TO_MERGE:
        return "approved" if review == "APPROVED" else "ready"
    if ci == "PENDING":
        return "checks running"
    return "review required"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return then if then.tzinfo else then.replace(tzinfo=timezone.utc)


def waiting_since(pr: PullRequest) -> str | None:
    """When a review request started waiting on you: the request itself when
    known, else the PR's creation."""
    return pr.requested_at or pr.created_at


def _days_since(ts: str | None, now: datetime | None) -> float | None:
    then = _parse(ts)
    if then is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - then).total_seconds() / 86400


def is_long_wait(pr: PullRequest, days: int, now: datetime | None = None) -> bool:
    waited = _days_since(waiting_since(pr), now)
    return days > 0 and waited is not None and waited >= days


def is_stale(pr: PullRequest, days: int, now: datetime | None = None) -> bool:
    idle = _days_since(pr.updated_at, now)
    return days > 0 and idle is not None and idle >= days


def age(created_at: str | None, now: datetime | None = None) -> str:
    """Compact age: 45m, 3h, 2d, 5w. Empty when unknown."""
    then = _parse(created_at)
    if then is None:
        return ""
    now = now or datetime.now(timezone.utc)
    seconds = max(0.0, (now - then).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 14 * 86400:
        return f"{int(seconds // 86400)}d"
    return f"{int(seconds // (7 * 86400))}w"
