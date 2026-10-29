"""Wrapper around the `gh` CLI.

We delegate auth, multi-host, tokens and rate-limit handling to `gh`. Users
add hosts with `gh auth login --hostname <host>` (the preferences window
exposes this). awaitray never stores a token; for an extra account on a host
it forwards the one gh gives it to the next gh call (see `_account_env`).
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from . import config as cfg_mod
from . import text


@dataclass(frozen=True)
class PullRequest:
    host: str
    repo: str
    number: int
    title: str
    url: str
    author: str
    is_draft: bool = False
    created_at: str | None = None  # ISO 8601, from the GraphQL search
    updated_at: str | None = None
    # Review requested from the viewer personally (not only via a team).
    # True when unknown, so older cached data keeps counting as before.
    direct_request: bool = True
    # When your review was requested (you, or else one of your teams), from
    # the PR's timeline. None when unknown: fall back to created_at.
    requested_at: str | None = None
    # The configured account(s) that see it, "user@host" or "host";
    # several joined with ", " when two accounts both see the same PR.
    account: str = ""

    @property
    def key(self) -> str:
        return f"{self.host}:{self.repo}#{self.number}"


@dataclass(frozen=True)
class PRDetails:
    """Per-PR state. Comes with every search (GraphQL); `pr_details` refetches
    a single PR on demand."""

    url: str
    head_oid: str | None
    ci_state: str | None  # "SUCCESS" | "FAILURE" | "PENDING" | None
    review_decision: str | None = (
        None  # "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | None
    )
    mergeable: str | None = None  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN" | None
    comment_count: int | None = None
    # Review threads not yet resolved (of the first 50; enough to spot new ones).
    unresolved_threads: int | None = None
    # Each check of the head commit: (name, CHECK_* state, url), failing first.
    checks: tuple[tuple[str, str, str], ...] = ()
    # Permalink of the latest comment or review (None: open the PR itself).
    latest_activity_url: str | None = None


@dataclass(frozen=True)
class RateLimit:
    """GraphQL API budget of a host, as of its last query."""

    remaining: int
    limit: int
    reset_at: str  # ISO 8601, UTC
    cost: int = 0  # points the last query used

    @property
    def low(self) -> bool:
        """Close enough to the limit that polling should wait for the reset."""
        return self.remaining < max(50, self.limit // 50)

    def reset_local(self) -> str:
        try:
            when = datetime.fromisoformat(self.reset_at.replace("Z", "+00:00"))
        except ValueError:
            return "?"
        return when.astimezone().strftime("%H:%M")

    def describe(self) -> str:
        return f"API {self.remaining:,}/{self.limit:,} left · resets {self.reset_local()}"


# Last rate limit seen per host. Written by the poll worker, read by the UI
# (a dict assignment is atomic; readers only ever see a whole RateLimit).
_RATE_LIMITS: dict[str, RateLimit] = {}


def rate_limit(account: str) -> RateLimit | None:
    """By account id: `user@host`, or the host for its active account."""
    return _RATE_LIMITS.get(account)


def sign_in_status(host: str, user: str = "") -> str:
    """The Hosts editor's status line: sign-in, plus the API budget when a
    poll has seen it. Blocking (runs gh): call off the main thread."""
    if user:
        try:
            _account_env(host, user)
        except GhError as e:
            return str(e)
    elif not is_authenticated(host):
        return "not signed in"
    limit = rate_limit(f"{user}@{host}" if user else host)
    return f"signed in · {limit.describe()}" if limit else "signed in"


# A search result: the PRs plus their details keyed by URL.
SearchResult = tuple[list[PullRequest], dict[str, PRDetails]]


# What went wrong, so the UI can say what to do about it.
ERR_MISSING = "missing"  # gh isn't installed
ERR_AUTH = "auth"  # not logged in / bad or expired credentials
ERR_NETWORK = "network"  # offline, DNS, timeouts, VPN/SSO proxies
ERR_OTHER = "other"

_AUTH_HINTS = (
    "gh auth login",
    "not logged in",
    "authentication",
    "bad credentials",
    "http 401",
    "requires authentication",
    "token",
    "saml",
    "sso",
)
_NETWORK_HINTS = (
    "could not resolve",
    "no such host",
    "dial tcp",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "timeout",
    "timed out",
    "tls handshake",
    "temporary failure in name resolution",
    "eof",
)


def classify_error(message: str) -> str:
    text = (message or "").lower()
    if "gh cli not found" in text:
        return ERR_MISSING
    if any(h in text for h in _NETWORK_HINTS):
        return ERR_NETWORK
    if any(h in text for h in _AUTH_HINTS):
        return ERR_AUTH
    return ERR_OTHER


class GhError(RuntimeError):
    @property
    def kind(self) -> str:
        return classify_error(str(self))


def gh_available() -> bool:
    return shutil.which("gh") is not None


def is_authenticated(host: str) -> bool:
    if not gh_available():
        return False
    res = subprocess.run(
        ["gh", "auth", "status", "--hostname", host],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return res.returncode == 0


# One query returns the PRs *and* the per-PR state (CI, review decision, head
# commit, mergeability), replacing a `gh pr view` per PR. Only fields that
# have been in the schema for years, so older GHE versions keep working.
_SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  viewer { login }
  rateLimit { remaining limit resetAt cost }
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        createdAt
        updatedAt
        author { login }
        repository { nameWithOwner }
        headRefOid
        reviewDecision
        mergeable
        comments { totalCount }
        reviewThreads(first: 50) { nodes { isResolved } }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 30) {
                  nodes {
                    ... on CheckRun { name status conclusion detailsUrl }
                    ... on StatusContext { context state targetUrl }
                  }
                }
              }
            }
          }
        }
        lastComment: comments(last: 1) { nodes { url createdAt } }
        lastReview: reviews(last: 1) { nodes { url createdAt } }
        reviewRequests(first: 25) {
          nodes { requestedReviewer { __typename ... on User { login } } }
        }
        timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 10) {
          nodes {
            ... on ReviewRequestedEvent {
              createdAt
              requestedReviewer { __typename ... on User { login } }
            }
          }
        }
      }
    }
  }
}
"""

# Hard cap on results per search, like the old `--limit 100`, with headroom.
_MAX_RESULTS = 200

# One check's state, as the menu shows it.
CHECK_FAILURE = "FAILURE"
CHECK_PENDING = "PENDING"
CHECK_SUCCESS = "SUCCESS"
CHECK_NEUTRAL = "NEUTRAL"  # skipped, neutral, cancelled…

_CHECK_ORDER = {CHECK_FAILURE: 0, CHECK_PENDING: 1, CHECK_SUCCESS: 2, CHECK_NEUTRAL: 3}
_RUN_CONCLUSION = {
    "SUCCESS": CHECK_SUCCESS,
    "FAILURE": CHECK_FAILURE,
    "TIMED_OUT": CHECK_FAILURE,
    "STARTUP_FAILURE": CHECK_FAILURE,
    "ACTION_REQUIRED": CHECK_FAILURE,
}
_CONTEXT_STATE = {
    "SUCCESS": CHECK_SUCCESS,
    "FAILURE": CHECK_FAILURE,
    "ERROR": CHECK_FAILURE,
    "PENDING": CHECK_PENDING,
    "EXPECTED": CHECK_PENDING,
}

# statusCheckRollup.state → the ci_state values the UI knows.
_ROLLUP_STATE = {
    "SUCCESS": "SUCCESS",
    "FAILURE": "FAILURE",
    "ERROR": "FAILURE",
    "PENDING": "PENDING",
    "EXPECTED": "PENDING",
}


def _is_github_cloud(host: str) -> bool:
    host = host.lower()
    return host == "github.com" or host.endswith(".ghe.com")


def _account_env(host: str, user: str) -> dict[str, str] | None:
    """Environment for a gh call as `user` on `host`, or None for the
    host's active account.

    gh stays the auth boundary: it hands us the account's token and we pass
    it straight to the next gh process in its environment, the way gh itself
    picks a token per host type. The token is never stored, logged or kept
    past the search that needs it.
    """
    if not user:
        return None
    res = subprocess.run(
        ["gh", "auth", "token", "--hostname", host, "--user", user],
        capture_output=True,
        text=True,
        timeout=10,
    )
    token = res.stdout.strip()
    if res.returncode != 0 or not token:
        if "unknown flag" in res.stderr:
            raise GhError("several accounts per host need gh 2.40 or newer")
        raise GhError(
            f"account {user} is not logged in on {host}: run gh auth login --hostname {host}"
        )
    env = dict(os.environ)
    if _is_github_cloud(host):
        env["GH_TOKEN"] = token
    else:
        env["GH_ENTERPRISE_TOKEN"] = token
        env["GH_HOST"] = host
    return env


def _graphql(
    host: str,
    variables: dict[str, str],
    timeout: int,
    env: dict[str, str] | None = None,
    account: str = "",
) -> dict[str, Any]:
    cmd = ["gh", "api", "graphql", "--hostname", host, "-f", f"query={_SEARCH_QUERY}"]
    for name, value in variables.items():
        cmd += ["-f", f"{name}={value}"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)

    try:
        payload = json.loads(res.stdout) if res.stdout.strip() else {}
    except json.JSONDecodeError as e:
        if res.returncode != 0:
            raise GhError(res.stderr.strip() or f"gh exited with {res.returncode}") from e
        raise GhError(f"unable to parse gh output: {e}") from e

    errors = payload.get("errors") if isinstance(payload, dict) else None
    if res.returncode != 0 or errors:
        messages = [e.get("message", "") for e in errors or [] if isinstance(e, dict)]
        detail = "; ".join(m for m in messages if m) or res.stderr.strip()
        raise GhError(detail or f"gh exited with {res.returncode}")
    data = payload.get("data") or {}
    _record_rate_limit(account or host, data.get("rateLimit"))
    return data


class NotFoundError(GhError):
    """The endpoint answered 404 (for Copilot: no subscription)."""


def rest_get(host: str, path: str, user: str = "", timeout: int = 15) -> Any:
    """GET a REST endpoint through gh, as `user` on `host` (or the host's
    active account). Blocking: call it off the main thread. Raises
    NotFoundError on a 404 and GhError on any other failure."""
    return rest(host, path, user, timeout=timeout)


def rest(
    host: str,
    path: str,
    user: str = "",
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: int = 15,
) -> Any:
    """Call a REST endpoint through gh (see `rest_get`). `body` goes as JSON
    on stdin, never on the command line."""
    env = _account_env(host, user)
    cmd = ["gh", "api", "--hostname", host, "-H", "Accept: application/vnd.github+json"]
    if method != "GET":
        cmd += ["--method", method]
    if body is not None:
        cmd += ["--input", "-"]
    cmd.append(path)
    res = subprocess.run(
        cmd,
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if res.returncode != 0:
        detail = res.stderr.strip() or f"gh exited with {res.returncode}"
        if "HTTP 404" in detail:
            raise NotFoundError(detail)
        raise GhError(detail)
    try:
        return json.loads(res.stdout) if res.stdout.strip() else None
    except json.JSONDecodeError as e:
        raise GhError(f"unable to parse gh output: {e}") from e


def _record_rate_limit(host: str, raw: object) -> None:
    """GHE with rate limiting disabled returns null: nothing to record."""
    if not isinstance(raw, dict):
        return
    with contextlib.suppress(KeyError, TypeError, ValueError):
        _RATE_LIMITS[host] = RateLimit(
            remaining=int(raw["remaining"]),
            limit=int(raw["limit"]),
            reset_at=str(raw.get("resetAt") or ""),
            cost=int(raw.get("cost") or 0),
        )


def _is_direct_request(node: dict[str, Any], viewer: str | None) -> bool:
    """Whether the viewer is among the PR's requested reviewers as a *user*
    (review-requested:@me also matches requests to any of their teams)."""
    if not viewer:
        return True
    for req in (node.get("reviewRequests") or {}).get("nodes") or []:
        reviewer = (req or {}).get("requestedReviewer") or {}
        if (
            reviewer.get("__typename") == "User"
            and (reviewer.get("login") or "").lower() == viewer.lower()
        ):
            return True
    return False


def _checks(rollup: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    out = []
    for c in (rollup.get("contexts") or {}).get("nodes") or []:
        if not c:
            continue
        if "context" in c:  # a commit status
            name, url = c.get("context") or "?", c.get("targetUrl") or ""
            state = _CONTEXT_STATE.get((c.get("state") or "").upper(), CHECK_NEUTRAL)
        else:  # a check run
            name, url = c.get("name") or "?", c.get("detailsUrl") or ""
            if (c.get("status") or "").upper() != "COMPLETED":
                state = CHECK_PENDING
            else:
                conclusion = (c.get("conclusion") or "").upper()
                state = _RUN_CONCLUSION.get(conclusion, CHECK_NEUTRAL)
        out.append((text.clean(name, 200), state, url))
    out.sort(key=lambda c: (_CHECK_ORDER[c[1]], c[0].lower()))
    return tuple(out)


def _latest_activity(node: dict[str, Any]) -> str | None:
    """Permalink of the newest of the last comment and the last review."""
    items = [
        item
        for field in ("lastComment", "lastReview")
        for item in (node.get(field) or {}).get("nodes") or []
        if item and item.get("url")
    ]
    if not items:
        return None
    return str(max(items, key=lambda item: item.get("createdAt") or "")["url"])


def _unresolved(node: dict[str, Any]) -> int | None:
    threads = node.get("reviewThreads")
    if not isinstance(threads, dict):
        return None
    return sum(1 for t in threads.get("nodes") or [] if t and not t.get("isResolved"))


def _requested_at(node: dict[str, Any], viewer: str | None) -> str | None:
    """Latest review request addressed to the viewer; else the latest one to a
    team (we can't tell which teams are theirs, and the search only returns
    PRs requesting them); else None."""
    events = ((node.get("timelineItems") or {}).get("nodes")) or []
    direct = team = None
    for ev in events:  # oldest first
        if not ev or not ev.get("createdAt"):
            continue
        reviewer = ev.get("requestedReviewer") or {}
        kind = reviewer.get("__typename")
        if kind == "User" and viewer and (reviewer.get("login") or "").lower() == viewer.lower():
            direct = ev["createdAt"]
        elif kind == "Team":
            team = ev["createdAt"]
    return direct or team


def _parse_node(
    host: str, node: dict[str, Any], viewer: str | None = None
) -> tuple[PullRequest, PRDetails] | None:
    if not node or "number" not in node:  # an Issue hit, or a node we can't see
        return None
    url = node.get("url", "")
    pr = PullRequest(
        host=host,
        repo=(node.get("repository") or {}).get("nameWithOwner") or "?",
        number=int(node["number"]),
        title=text.clean(node.get("title") or ""),
        url=url,
        author=text.clean((node.get("author") or {}).get("login") or "ghost"),  # deleted user
        is_draft=bool(node.get("isDraft", False)),
        created_at=node.get("createdAt"),
        updated_at=node.get("updatedAt"),
        direct_request=_is_direct_request(node, viewer),
        requested_at=_requested_at(node, viewer),
    )
    rollup: dict[str, Any] = {}
    commits = (node.get("commits") or {}).get("nodes") or []
    if commits and commits[-1]:
        rollup = (commits[-1].get("commit") or {}).get("statusCheckRollup") or {}
    rollup_state = _ROLLUP_STATE.get((rollup.get("state") or "").upper())
    review_decision = node.get("reviewDecision")
    details = PRDetails(
        url=url,
        head_oid=node.get("headRefOid") or None,
        ci_state=rollup_state,
        review_decision=review_decision.upper() if review_decision else None,
        mergeable=node.get("mergeable") or None,
        comment_count=(node.get("comments") or {}).get("totalCount"),
        unresolved_threads=_unresolved(node),
        checks=_checks(rollup),
        latest_activity_url=_latest_activity(node),
    )
    return pr, details


def _search_prs(host: str, qualifiers: str, timeout: int, user: str = "") -> SearchResult:
    if not gh_available():
        raise GhError("gh CLI not found in PATH")
    env = _account_env(host, user)
    account = f"{user}@{host}" if user else host

    query = f"is:pr is:open archived:false {qualifiers}"
    prs: list[PullRequest] = []
    details: dict[str, PRDetails] = {}
    cursor: str | None = None
    while len(prs) < _MAX_RESULTS:
        variables = {"q": query}
        if cursor:
            variables["cursor"] = cursor
        data = _graphql(host, variables, timeout, env, account)
        viewer = (data.get("viewer") or {}).get("login")
        search = data.get("search") or {}
        for node in search.get("nodes") or []:
            parsed = _parse_node(host, node, viewer)
            if parsed is None:
                continue
            pr, det = parsed
            prs.append(replace(pr, account=account))
            details[pr.url] = det
        page = search.get("pageInfo") or {}
        cursor = page.get("endCursor")
        if not page.get("hasNextPage") or not cursor:
            break
    return prs[:_MAX_RESULTS], details


def review_requested_prs(host: str, timeout: int = 30, user: str = "") -> SearchResult:
    """Open PRs where the authenticated user (or one of their teams) is a
    requested reviewer, with their details."""
    return _search_prs(host, "review-requested:@me", timeout, user)


def my_open_prs(host: str, timeout: int = 30, user: str = "") -> SearchResult:
    """Open PRs authored by the authenticated user, with their details."""
    return _search_prs(host, "author:@me", timeout, user)


def search_prs(host: str, query: str, timeout: int = 30, user: str = "") -> SearchResult:
    """Open PRs matching a user's saved search (custom sections)."""
    return _search_prs(host, query, timeout, user)


def pr_details(url: str, timeout: int = 15) -> PRDetails:
    """Fetch CI rollup state and head ref oid for a PR via `gh pr view`."""
    if not gh_available():
        raise GhError("gh CLI not found in PATH")

    cmd = ["gh", "pr", "view", url, "--json", "statusCheckRollup,headRefOid,reviewDecision"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise GhError(res.stderr.strip() or f"gh exited with {res.returncode}")
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError as e:
        raise GhError(f"unable to parse gh output: {e}") from e

    head_oid = data.get("headRefOid") or None
    review_decision = (data.get("reviewDecision") or None) or None
    if isinstance(review_decision, str):
        review_decision = review_decision.upper()

    rollup = data.get("statusCheckRollup") or []
    ci_state: str | None = None
    if rollup:
        states = []
        for c in rollup:
            s = c.get("conclusion") or c.get("state") or c.get("status")
            if s:
                states.append(s.upper())
        if any(s in ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR") for s in states):
            ci_state = "FAILURE"
        elif any(s in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED") for s in states):
            ci_state = "PENDING"
        elif states and all(s in ("SUCCESS", "NEUTRAL", "SKIPPED") for s in states):
            ci_state = "SUCCESS"
        else:
            ci_state = states[0] if states else None

    return PRDetails(
        url=url,
        head_oid=head_oid,
        ci_state=ci_state,
        review_decision=review_decision,
    )


def login_command(host: str) -> list[str]:
    """Returns the command users should run to authenticate a new host.

    The preferences UI launches this in a terminal so credentials never leave
    the standard `gh` flow.
    """
    return ["gh", "auth", "login", "--hostname", host, "--web"]


_LINUX_TERMINALS = (
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("konsole", ["konsole", "-e"]),
    ("xfce4-terminal", ["xfce4-terminal", "-x"]),
    ("kitty", ["kitty"]),
    ("alacritty", ["alacritty", "-e"]),
    ("xterm", ["xterm", "-e"]),
)


def valid_hostname(host: str) -> bool:
    """A host (optionally with a port) as gh takes it, nothing else: it ends
    up on command lines and, on macOS, in a Terminal script."""
    return cfg_mod.valid_hostname(host)


def launch_login(host: str) -> str | None:
    """Open a terminal running `gh auth login` for `host`.

    Returns None on success, or a message telling the user what to run
    themselves. Shared by the menu and both preferences windows.
    """
    import sys

    if not valid_hostname(host):
        return f"{host!r} is not a hostname (like github.com or ghe.corp:8443)"
    cmd = login_command(host)
    manual = f"Run this in a terminal: {' '.join(cmd)}"
    try:
        if sys.platform == "darwin":
            # `do script` runs the string in a shell: quote it for the shell,
            # then for AppleScript. The hostname is checked above as well.
            script = shlex.join(cmd).replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'tell application "Terminal" to do script "{script}"',
                    "-e",
                    'tell application "Terminal" to activate',
                ]
            )
            return None
        for binary, prefix in _LINUX_TERMINALS:
            if shutil.which(binary):
                subprocess.Popen(prefix + cmd)
                return None
    except OSError as e:
        return f"Could not open a terminal ({e}). {manual}"
    return f"No terminal emulator found. {manual}"
