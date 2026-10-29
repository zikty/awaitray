"""State changes of your own PRs, worth a notification.

Each poll reduces a PR's details to `Facts` and compares them with the
previous poll's. Pure functions: the controller keeps the facts (persisted in
state.json with the rest of the runtime state) and the keys already sent.

Rules:

- A PR seen for the first time (or after a restart without saved facts) only
  records a baseline: nothing is news yet.
- CI is compared by its last *settled* result (success / failure). A re-run
  shows up as PENDING, which keeps the previous result, so a flaky
  passed → running → passed cycle on the same commit notifies nothing.
- Every event is sent at most once per head commit (`sent_key`), so
  approved → dismissed → approved on the same commit notifies once.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import pr_state, sounds
from .github import PRDetails, PullRequest
from .notice import Notice

APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"
CI_FAILED = "ci_failed"
CI_PASSED = "ci_passed"
READY_TO_MERGE = "ready_to_merge"
CONFLICT = "conflict"
NEW_COMMENTS = "new_comments"

EVENTS = (
    APPROVED,
    CHANGES_REQUESTED,
    CI_FAILED,
    CI_PASSED,
    READY_TO_MERGE,
    CONFLICT,
    NEW_COMMENTS,
)

_SETTLED_CI = ("SUCCESS", "FAILURE")


@dataclass(frozen=True)
class Facts:
    """What a PR looked like on one poll, reduced to what can change."""

    head_oid: str | None = None
    review: str | None = None  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | None
    ci: str | None = None  # last settled result: SUCCESS | FAILURE | None
    ci_oid: str | None = None  # the commit `ci` settled on
    ready: bool = False  # pr_state.READY_TO_MERGE
    conflict: bool = False
    comments: int = 0  # conversation comments + unresolved review threads


def facts(pr: PullRequest, details: PRDetails, previous: Facts | None) -> Facts:
    ci: str | None
    ci_oid: str | None
    if details.ci_state in _SETTLED_CI:
        ci, ci_oid = details.ci_state, details.head_oid
    elif details.ci_state == "PENDING" and previous is not None:
        ci, ci_oid = previous.ci, previous.ci_oid  # still running: keep the last result
    else:
        ci, ci_oid = None, None
    return Facts(
        head_oid=details.head_oid,
        review=details.review_decision,
        ci=ci,
        ci_oid=ci_oid,
        ready=pr_state.classify_mine(pr, details) == pr_state.READY_TO_MERGE,
        conflict=details.mergeable == "CONFLICTING",
        comments=(details.comment_count or 0) + (details.unresolved_threads or 0),
    )


def events(prev: Facts | None, cur: Facts) -> list[str]:
    """Events between two polls, most important first."""
    if prev is None:
        return []
    out: list[str] = []
    if cur.ready and not prev.ready:
        out.append(READY_TO_MERGE)  # says "approved" too: don't send both
    elif cur.review == "APPROVED" and prev.review != "APPROVED":
        out.append(APPROVED)
    if cur.review == "CHANGES_REQUESTED" and prev.review != "CHANGES_REQUESTED":
        out.append(CHANGES_REQUESTED)
    # A failure on a commit that hadn't failed yet (a new push, or the first
    # result for it); a re-run failing again on the same commit isn't news.
    if cur.ci == "FAILURE" and (prev.ci != "FAILURE" or prev.ci_oid != cur.ci_oid):
        out.append(CI_FAILED)
    if cur.ci == "SUCCESS" and prev.ci == "FAILURE":
        out.append(CI_PASSED)
    if cur.conflict and not prev.conflict:
        out.append(CONFLICT)
    if cur.comments > prev.comments:
        out.append(NEW_COMMENTS)
    return out


def sent_key(pr: PullRequest, event: str, head_oid: str | None) -> str:
    return f"{pr.key}|{event}|{head_oid or '-'}"


_TITLES = {
    APPROVED: "✓ Approved",
    CHANGES_REQUESTED: "Changes requested",
    CI_FAILED: "✗ Checks failed",
    CI_PASSED: "✓ Checks back to green",
    READY_TO_MERGE: "✓ Ready to merge",
    CONFLICT: "Merge conflict",
    NEW_COMMENTS: "New comments",
}


def notice(pr: PullRequest, event: str) -> Notice:
    sound = sounds.EVENT_SLOT[event]
    urgency = "normal" if sound == sounds.BAD else "low"
    return Notice(
        f"{_TITLES[event]}: {pr.repo} #{pr.number}", pr.title, urgency, sound=sound, event=event
    )
