"""PR filters using glob (fnmatch) patterns.

Patterns are matched case-insensitively against:
  * `pr.author` for `ignored_authors`
  * `pr.repo` (a `owner/name` string) for `ignored_repos`
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable

from .config import Filters
from .github import PullRequest


def _matches_any(value: str, patterns: Iterable[str]) -> bool:
    v = value.lower()
    for p in patterns:
        if not p:
            continue
        pl = p.lower()
        if v == pl:
            return True
        if fnmatch.fnmatchcase(v, pl):
            return True
    return False


def keep(pr: PullRequest, filters: Filters) -> bool:
    if not filters.include_drafts and pr.is_draft:
        return False
    if _matches_any(pr.author, filters.ignored_authors):
        return False
    return not _matches_any(pr.repo, filters.ignored_repos)


def filter_prs(prs: Iterable[PullRequest], filters: Filters) -> list[PullRequest]:
    return [p for p in prs if keep(p, filters)]
