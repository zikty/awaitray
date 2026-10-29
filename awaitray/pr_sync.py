"""Polling GitHub for PRs, and what a poll result means.

`poll()` runs on a worker thread and returns a `PollResult`; everything else
here is pure: notification baselines, which PRs are new, and the notices
they produce. The controller owns the state and applies results on the main
thread (`Controller.apply_poll_result`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import config as cfg_mod
from . import github as gh
from . import log as log_mod
from .notice import Notice
from .sounds import INFO, REVIEW

# Why nothing can be polled at all (Controller.setup_problem).
SETUP_GH_MISSING = "gh_missing"
SETUP_NO_HOSTS = "no_hosts"
SETUP_MESSAGES = {
    SETUP_GH_MISSING: "GitHub CLI (gh) not found",
    SETUP_NO_HOSTS: "No GitHub host enabled",
}

# More new review requests than this in one poll → one summary notification.
NOTIFY_BATCH_LIMIT = 3


@dataclass
class PollResult:
    """Everything one poll learned, across every enabled host."""

    review_prs: list[gh.PullRequest] = field(default_factory=list)
    my_prs: list[gh.PullRequest] = field(default_factory=list)
    details: dict[str, gh.PRDetails] = field(default_factory=dict)  # url -> details
    errors: list[str] = field(default_factory=list)
    failed_hosts: set[str] = field(default_factory=set)  # review query failed
    my_failed_hosts: set[str] = field(default_factory=set)  # "my PRs" query failed
    host_errors: dict[str, str] = field(default_factory=dict)  # host -> gh.ERR_* (first)
    # Every enabled host failed: keep the last-known lists, apply the error policy.
    total_failure: bool = False
    # SETUP_* when polling couldn't even start; everything else is empty then.
    setup_problem: str | None = None
    # Custom sections, by CustomSection.sid, and the hosts whose query
    # for a section failed (their PRs keep their baseline).
    section_prs: dict[str, list[gh.PullRequest]] = field(default_factory=dict)
    section_failed_hosts: dict[str, set[str]] = field(default_factory=dict)


def poll(cfg: cfg_mod.Config) -> PollResult:
    """Query every enabled account. Blocking: call it from a worker thread.

    Errors are keyed by account id (`user@host`, or the host for its active
    account); failed_hosts holds *hostnames*, so a PR seen through a failing
    account keeps its baseline even when another account on the same host
    still works (it never re-notifies).
    """
    accounts = [h for h in cfg.hosts if h.enabled]
    if not gh.gh_available():
        return PollResult(setup_problem=SETUP_GH_MISSING)
    if not accounts:
        return PollResult(setup_problem=SETUP_NO_HOSTS)

    result = PollResult()
    successes = 0
    for acct in accounts:
        host, user, aid = acct.hostname, acct.user, acct.account_id
        limit = gh.rate_limit(aid)
        if limit is not None and limit.low and _reset_ahead(limit):
            # Back off until the budget resets. Counts as a failed account, so
            # its PRs stay on screen and the error policy applies if it's the only one.
            _fail(
                result,
                acct,
                f"{aid}: API rate limit nearly used up "
                f"({limit.remaining} left), resuming at {limit.reset_local()}",
                gh.ERR_OTHER,
            )
            result.my_failed_hosts.add(host)
            continue
        ok = False
        try:
            prs, det = gh.review_requested_prs(host, user=user)
            result.review_prs.extend(prs)
            result.details.update(det)
            ok = True
        except gh.GhError as e:
            _fail(result, acct, f"{aid}: {e}", e.kind)
        if cfg.my_prs.enabled:
            try:
                prs, det = gh.my_open_prs(host, user=user)
                result.my_prs.extend(prs)
                result.details.update(det)
                ok = True
            except gh.GhError as e:
                _fail(result, acct, f"{aid} (my): {e}", e.kind, mine=True)
        _poll_sections(cfg, acct, result)
        if ok:
            successes += 1
    result.total_failure = successes == 0
    result.review_prs = merge_accounts(result.review_prs)
    result.my_prs = merge_accounts(result.my_prs)
    result.section_prs = {sid: merge_accounts(prs) for sid, prs in result.section_prs.items()}
    return result


def _fail(
    result: PollResult, acct: cfg_mod.HostConfig, message: str, kind: str, *, mine: bool = False
) -> None:
    result.errors.append(message)
    (result.my_failed_hosts if mine else result.failed_hosts).add(acct.hostname)
    result.host_errors.setdefault(acct.account_id, kind)
    log_mod.warn(message)


def merge_accounts(prs: list[gh.PullRequest]) -> list[gh.PullRequest]:
    """One entry per PR key: a PR two accounts see is shown once, listing
    both, and counts as a direct request if it is one for either."""
    merged: dict[str, gh.PullRequest] = {}
    for pr in prs:
        seen = merged.get(pr.key)
        if seen is None:
            merged[pr.key] = pr
            continue
        accounts = [a for a in seen.account.split(", ") if a]
        if pr.account and pr.account not in accounts:
            accounts.append(pr.account)
        merged[pr.key] = replace(
            seen,
            account=", ".join(accounts),
            direct_request=seen.direct_request or pr.direct_request,
        )
    return list(merged.values())


def _poll_sections(cfg: cfg_mod.Config, acct: cfg_mod.HostConfig, result: PollResult) -> None:
    """Custom sections for one account. A failing query (often a typo in it)
    is reported but doesn't make the account count as failed."""
    host = acct.hostname
    for section in cfg.sections:
        if not section.enabled or section.host not in ("", host):
            continue
        prs = result.section_prs.setdefault(section.sid, [])
        try:
            found, det = gh.search_prs(host, section.query, user=acct.user)
        except gh.GhError as e:
            result.errors.append(f"{section.title}: {e}")
            result.section_failed_hosts.setdefault(section.sid, set()).add(host)
            log_mod.warn(f"section {section.title!r} error on {host}: {e}")
            continue
        prs.extend(found)
        result.details.update(det)


def test_section(cfg: cfg_mod.Config, section: cfg_mod.CustomSection) -> str:
    """Run a section's query now and say what it finds (the editors' "Test"
    button). Blocking: call it off the main thread."""
    if not section.query.strip():
        return "Enter a query"
    if not gh.gh_available():
        return "gh CLI not installed"
    accounts = [h for h in cfg.hosts if h.enabled and section.host in ("", h.hostname)]
    if not accounts:
        return "No account enabled for this host"
    found: list[gh.PullRequest] = []
    for acct in accounts:
        try:
            prs, _ = gh.search_prs(acct.hostname, section.query, user=acct.user)
        except gh.GhError as e:
            return f"{acct.account_id}: {e}"
        found.extend(prs)
    total = len(merge_accounts(found))
    return f"{total} open PR{'s' if total != 1 else ''} now"


def section_notices(
    title: str, new: list[gh.PullRequest], with_account: bool = False
) -> list[Notice]:
    if len(new) > NOTIFY_BATCH_LIMIT:
        shown = "\n".join(_line(pr, with_account) for pr in new[:NOTIFY_BATCH_LIMIT])
        more = len(new) - NOTIFY_BATCH_LIMIT
        return [Notice(f"{len(new)} new in {title}", f"{shown}\n…and {more} more", sound=REVIEW)]
    return [Notice(f"New in {title}", _line(pr, with_account), sound=REVIEW) for pr in new]


def _reset_ahead(limit: gh.RateLimit) -> bool:
    try:
        reset = datetime.fromisoformat(limit.reset_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return reset > datetime.now(timezone.utc)


def sort_prs(prs: list[gh.PullRequest]) -> list[gh.PullRequest]:
    return sorted(prs, key=lambda p: (p.host, p.repo, p.number))


def my_prs_filters(cfg: cfg_mod.Config) -> cfg_mod.Filters:
    """Filters for your own PRs: the repo globs apply, author filters don't."""
    return cfg_mod.Filters(
        ignored_authors=[],
        ignored_repos=cfg.filters.ignored_repos,
        include_drafts=cfg.my_prs.include_drafts,
    )


def next_baseline(
    previous: set[str] | None,
    current: set[str],
    failed_hosts: set[str] | None,
) -> set[str]:
    """Keys to compare the next poll against.

    PRs from a host that failed this round are missing from `current` only
    because we couldn't see them; keep their previous keys so they don't
    notify again as "new" when the host comes back.
    """
    if not previous or not failed_hosts:
        return set(current)
    # Keys are "host:repo#n"; match on the full "host:" prefix because a GHE
    # hostname may itself contain a port ("ghe.corp:8443").
    prefixes = tuple(f"{h}:" for h in failed_hosts)
    kept = {k for k in previous if k.startswith(prefixes)}
    return set(current) | kept


def new_review_requests(
    cfg: cfg_mod.Config, prs: list[gh.PullRequest], new_keys: set[str]
) -> list[gh.PullRequest]:
    """The newly-arrived review requests worth a notification."""
    return [
        pr
        for pr in prs
        if pr.key in new_keys
        and pr.key not in cfg.snoozed
        and (pr.direct_request or cfg.notify_team_requests)
    ]


def _line(pr: gh.PullRequest, with_account: bool = False) -> str:
    line = f"{pr.repo} #{pr.number} — {pr.title}"
    return f"{line} · {pr.account}" if with_account and pr.account else line


def review_request_notices(new: list[gh.PullRequest], with_account: bool = False) -> list[Notice]:
    """One notice per new request, or one summary when there are many (e.g.
    the first poll after a weekend off). With several accounts, each line
    says which one the request is for."""
    if len(new) > NOTIFY_BATCH_LIMIT:
        shown = "\n".join(_line(pr, with_account) for pr in new[:NOTIFY_BATCH_LIMIT])
        more = len(new) - NOTIFY_BATCH_LIMIT
        return [
            Notice(f"{len(new)} new PR review requests", f"{shown}\n…and {more} more", sound=REVIEW)
        ]
    return [Notice("New PR review request", _line(pr, with_account), sound=REVIEW) for pr in new]


def my_pr_notices(new: list[gh.PullRequest]) -> list[Notice]:
    return [Notice("New PR of yours", _line(pr), urgency="low", sound=INFO) for pr in new]


def status_text(result: PollResult, visible: int, consecutive_failures: int) -> str:
    """The menu's status line after a poll."""
    plural = "s" if visible != 1 else ""
    if result.total_failure:
        return f"PR check failed ({consecutive_failures}× in a row): " + "; ".join(result.errors)
    if result.errors:
        return f"{visible} PR{plural} (partial: {'; '.join(result.errors)})"
    return f"{visible} PR{plural} need review"
