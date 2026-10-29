"""Unread GitHub notifications per account (the inbox, inbox.py).

Fetched on a worker thread every ACCOUNT_MINUTES (not on every PR poll),
shown right under the menu's status line, and best effort throughout:
anything that fails just isn't shown. Uses the documented
`GET /notifications`. AI assistants' quotas are ai_usage.py's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import config as cfg_mod
from . import github as gh
from . import inbox as inbox_mod
from . import log as log_mod

ACCOUNT_MINUTES = 30
INBOX_CAP = 50  # unread notifications counted ("50+" beyond)


@dataclass(frozen=True)
class AccountInfo:
    account: str  # account id: user@host, or the host
    host: str
    unread: int | None = None  # None: not known
    inbox: tuple[inbox_mod.InboxItem, ...] = ()  # the unread threads (inbox.py)
    inbox_as_of: str = ""  # when they were read, ISO 8601 UTC (for "mark all")


def _inbox(acct: cfg_mod.HostConfig) -> tuple[int, tuple[inbox_mod.InboxItem, ...], str] | None:
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        items = gh.rest_get(acct.hostname, f"notifications?per_page={INBOX_CAP}", acct.user)
    except gh.GhError as e:
        log_mod.info(f"notifications for {acct.account_id} unavailable: {e}")
        return None
    if not isinstance(items, list):
        return None
    return len(items), inbox_mod.parse(acct.hostname, items), as_of


def fetch(cfg: cfg_mod.Config) -> list[AccountInfo]:
    """For every enabled account. Blocking: call it from a worker thread."""
    if not cfg.account_info.inbox or not gh.gh_available():
        return []
    out = []
    for acct in cfg.hosts:
        if not acct.enabled:
            continue
        inbox = _inbox(acct)
        out.append(
            AccountInfo(
                account=acct.account_id,
                host=acct.hostname,
                unread=inbox[0] if inbox else None,
                inbox=inbox[1] if inbox else (),
                inbox_as_of=inbox[2] if inbox else "",
            )
        )
    return out


def inbox_label(unread: int) -> str:
    count = f"{INBOX_CAP}+" if unread >= INBOX_CAP else str(unread)
    return f"GitHub inbox · {count} unread" if unread else "GitHub inbox · all read"
