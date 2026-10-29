"""Your GitHub inbox (unread notifications), triaged from the menu.

`parse` turns `GET /notifications` into InboxItems (pure); the actions block
(one `gh api` call each) and run as the account that owns the thread, on a
worker thread: mark as read, mark as done, unsubscribe, mark all as read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import config as cfg_mod
from . import github as gh
from . import text

MENU_CAP = 10  # threads listed in the menu; the rest are one click away

# What the reason field means, in the menu's words.
_REASONS = {
    "review_requested": "review requested",
    "mention": "mentioned",
    "team_mention": "team mentioned",
    "assign": "assigned",
    "author": "yours",
    "comment": "commented",
    "ci_activity": "CI",
    "state_change": "state change",
    "security_alert": "security",
}
# API URL of a PR or issue → its page: .../repos/o/r/pulls/12 → o/r/pull/12.
_ID = re.compile(r"[0-9]+")
_SUBJECT = re.compile(r"/repos/([^/]+/[^/]+)/(pulls|issues)/(\d+)$")


@dataclass(frozen=True)
class InboxItem:
    thread_id: str
    title: str
    repo: str  # owner/name
    kind: str  # PullRequest, Issue, Release, CheckSuite, Discussion…
    reason: str
    url: str  # the page to open

    def label(self) -> str:
        repo = self.repo.rsplit("/", 1)[-1]
        why = _REASONS.get(self.reason, self.reason.replace("_", " "))
        return f"{repo}: {self.title}" + (f" · {why}" if why else "")


def _page(host: str, subject: dict[str, Any], repo: dict[str, Any]) -> str:
    match = _SUBJECT.search(str(subject.get("url") or ""))
    if match:
        path = "pull" if match.group(2) == "pulls" else "issues"
        return f"https://{host}/{match.group(1)}/{path}/{match.group(3)}"
    return str(repo.get("html_url") or f"https://{host}/notifications")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse(host: str, payload: Any) -> tuple[InboxItem, ...]:
    """`GET /notifications` → the unread threads, newest first (as given).
    Anything that doesn't look like a thread is skipped."""
    if not isinstance(payload, list):
        return ()
    items = []
    for raw in payload:
        # The id ends up in an API path: digits only.
        if not isinstance(raw, dict) or not _ID.fullmatch(str(raw.get("id") or "")):
            continue
        subject = _dict(raw.get("subject"))
        repo = _dict(raw.get("repository"))
        items.append(
            InboxItem(
                thread_id=str(raw["id"]),
                title=text.clean(subject.get("title") or "(no title)", 200),
                repo=text.clean(repo.get("full_name") or "", 200),
                kind=str(subject.get("type") or ""),
                reason=str(raw.get("reason") or ""),
                url=_page(host, subject, repo),
            )
        )
    return tuple(items)


# ----- Actions (blocking: call them from a worker thread) -----

READ, DONE, UNSUBSCRIBE = "read", "done", "unsubscribe"
ACTIONS = (READ, DONE, UNSUBSCRIBE)


def act(account: str, action: str, thread_id: str) -> None:
    """Raises gh.GhError when GitHub says no."""
    if not _ID.fullmatch(thread_id):
        raise gh.GhError(f"not a notification thread: {thread_id!r}")
    user, host = cfg_mod.split_account(account)
    thread = f"notifications/threads/{thread_id}"
    if action == READ:
        gh.rest(host, thread, user, method="PATCH")
    elif action == DONE:
        gh.rest(host, thread, user, method="DELETE")
    elif action == UNSUBSCRIBE:
        # Stop following it, and clear it from the inbox like Gitify does.
        gh.rest(host, f"{thread}/subscription", user, method="DELETE")
        gh.rest(host, thread, user, method="DELETE")
    else:
        raise ValueError(action)


def mark_all_read(account: str, as_of: str) -> None:
    """Mark everything up to `as_of` (when the inbox was read) as read, so a
    notification that arrived since isn't swept away unseen."""
    user, host = cfg_mod.split_account(account)
    gh.rest(host, "notifications", user, method="PUT", body={"last_read_at": as_of, "read": True})
