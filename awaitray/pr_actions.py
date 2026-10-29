"""One-click actions on your PRs: re-run failed checks, ask Copilot for a review.

What's offered is pure (from the PR and its last details); doing it blocks
(a `gh api` call), so the controller runs `rerun_failed` and
`request_copilot_review` on a worker thread and tells you how it went.
"""

from __future__ import annotations

import re

from . import config as cfg_mod
from . import github as gh
from .notice import Notice

# Actions check runs link to https://<host>/<repo>/actions/runs/<run id>/job/<job id>.
_RUN_ID = re.compile(r"(\d+)(?:/|$)")
# The reviewer GitHub's docs name for Copilot code review (REST).
COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"


def failed_runs(pr: gh.PullRequest, det: gh.PRDetails | None) -> list[int]:
    """The GitHub Actions runs behind the PR's failing checks, in order.
    Other CI (a status or another app's check) can't be re-run from here.
    Only runs of the PR's own repo count: a check's link is set by whoever
    posts the check, so one pointing elsewhere must not pick a run to
    re-run."""
    if det is None:
        return []
    prefix = f"https://{pr.host}/{pr.repo}/actions/runs/".lower()
    runs: list[int] = []
    for _name, state, url in det.checks:
        own = state == gh.CHECK_FAILURE and url.lower().startswith(prefix)
        match = _RUN_ID.match(url, len(prefix)) if own else None
        if match and int(match.group(1)) not in runs:
            runs.append(int(match.group(1)))
    return runs


def can_request_copilot(pr: gh.PullRequest) -> bool:
    """Copilot code review lives on github.com only, and skips drafts."""
    return pr.host.lower() == "github.com" and not pr.is_draft


def _user(pr: gh.PullRequest) -> str:
    """The account to act as: the first configured one that sees the PR."""
    first = pr.account.split(", ")[0] if pr.account else pr.host
    return cfg_mod.split_account(first)[0]


def rerun_failed(pr: gh.PullRequest, runs: list[int]) -> Notice:
    """Re-run the failed jobs of each run. Blocking."""
    problems = []
    for run in runs:
        try:
            gh.rest(
                pr.host,
                f"repos/{pr.repo}/actions/runs/{run}/rerun-failed-jobs",
                _user(pr),
                method="POST",
            )
        except gh.GhError as e:
            problems.append(str(e))
    head = f"{pr.repo}#{pr.number}"
    if not problems:
        what = "1 workflow run" if len(runs) == 1 else f"{len(runs)} workflow runs"
        return Notice(f"Re-running failed checks: {head}", f"Failed jobs of {what}", "low")
    return Notice(f"Couldn't re-run checks: {head}", problems[0], icon="dialog-warning")


def request_copilot_review(pr: gh.PullRequest) -> Notice:
    """Add Copilot as a reviewer. Blocking."""
    head = f"{pr.repo}#{pr.number}"
    try:
        gh.rest(
            pr.host,
            f"repos/{pr.repo}/pulls/{pr.number}/requested_reviewers",
            _user(pr),
            method="POST",
            body={"reviewers": [COPILOT_REVIEWER]},
        )
    except gh.GhError as e:
        return Notice(f"Couldn't ask Copilot: {head}", str(e), icon="dialog-warning")
    return Notice(f"Copilot review requested: {head}", pr.title, "low")
