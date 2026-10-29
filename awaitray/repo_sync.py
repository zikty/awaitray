"""Local clones: which ones to fetch, their menu rows, cloning, and the
notices a fetch round produces.

`fetch_all()` and `clone()` block (run them on a worker thread); the rest is
pure logic over the config and the last fetch results.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from . import github as gh
from . import log as log_mod
from . import repos as repo_mod
from .notice import Notice
from .sounds import INFO


@dataclass
class LocalRepoStatus:
    """One row of the "Local repos" menu section: a local clone, its fetch
    status, and the review-request PRs that landed on it (matched by basename)."""

    name: str
    path: str
    ok: bool
    ahead: int
    behind: int
    branch: str
    message: str
    review_prs: list[gh.PullRequest] = field(default_factory=list)


def expanded_repos(cfg: cfg_mod.Config) -> list[tuple[str, cfg_mod.RepoConfig]]:
    """Enabled repo paths, expanding auto-discover parents one level deep.

    Shared by the fetch worker and the "Local repos" menu section so both
    see exactly the same set of clones (dedup + exclusions applied)."""
    out: list[tuple[str, cfg_mod.RepoConfig]] = []
    seen: set[str] = set()
    for r in cfg.repos:
        if not r.enabled:
            continue
        paths = repo_mod.discover_repos(r.path) if r.auto_discover else [r.path]
        excluded = set(r.excluded) if r.auto_discover else set()
        for path in paths:
            if path in seen or path in excluded:
                continue
            seen.add(path)
            out.append((path, r))
    return out


def fetch_all(cfg: cfg_mod.Config) -> list[repo_mod.FetchResult]:
    """`git fetch` every clone. Blocking: call it from a worker thread."""
    return [
        repo_mod.fetch(path, r.remote, auto_update=r.auto_update) for path, r in expanded_repos(cfg)
    ]


def parse_repo_paths(lines: list[str]) -> list[tuple[str, str]]:
    """`owner/repo = path` lines → (pattern, path); malformed lines are skipped."""
    out = []
    for line in lines:
        pattern, sep, path = line.partition("=")
        if sep and pattern.strip() and path.strip():
            out.append((pattern.strip().lower(), path.strip()))
    return out


def mapped_path(cfg: cfg_mod.Config, pr: gh.PullRequest) -> str | None:
    """Where the repo-path mapping puts a PR's repo, existing or not.

    An exact `owner/repo` entry wins over a glob; among globs the first match
    wins. A `*` in the path stands for the repo name (`acme/* = ~/code/*`).
    """
    repo = pr.repo.lower()
    name = pr.repo.split("/")[-1]
    rules = parse_repo_paths(cfg.repo_paths)
    exact = [path for pattern, path in rules if pattern == repo]
    globs = [
        path for pattern, path in rules if pattern != repo and fnmatch.fnmatchcase(repo, pattern)
    ]
    for path in exact + globs:
        return os.path.expanduser(path.replace("*", name))
    return None


def local_path_for(cfg: cfg_mod.Config, pr: gh.PullRequest) -> str | None:
    """The local clone of a PR's repo: where the repo-path mapping says (only
    if that folder exists: a missing mapped clone must not fall back to a
    same-named repo of another owner), else a configured repo folder with
    the same basename."""
    mapped = mapped_path(cfg, pr)
    if mapped is not None:
        return mapped if os.path.isdir(mapped) else None
    repo_basename = pr.repo.split("/")[-1].lower()
    for r in cfg.repos:
        if not r.enabled:
            continue
        paths = repo_mod.discover_repos(r.path) if r.auto_discover else [r.path]
        for p in paths:
            if Path(p).name.lower() == repo_basename:
                return p
    return None


def clone_destination(cfg: cfg_mod.Config) -> str | None:
    """Where 'Clone here' drops a new clone: the configured clone_root, or
    the first enabled auto-discover parent as a fallback."""
    root = cfg.clone_root.strip()
    if root:
        return root
    for r in cfg.repos:
        if r.enabled and r.auto_discover:
            return r.path
    return None


def clone_target(cfg: cfg_mod.Config, pr: gh.PullRequest) -> str | None:
    """Where "Clone here" puts a PR's repo: its mapped folder, else
    <clone destination>/<repo name>. None when neither is configured."""
    mapped = mapped_path(cfg, pr)
    if mapped is not None:
        return mapped
    dest = clone_destination(cfg)
    return os.path.join(dest, pr.repo.split("/")[-1]) if dest else None


def clone(pr: gh.PullRequest, target: str) -> tuple[bool, str]:
    """`gh repo clone` the PR's repo into `target`. Blocking."""
    log_mod.info(f"cloning {pr.repo} → {target}")
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        res = subprocess.run(
            ["gh", "repo", "clone", pr.repo, target],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "GH_HOST": pr.host},
        )
        ok = res.returncode == 0
        msg = "ok" if ok else (res.stderr.strip() or f"exit {res.returncode}")
    except (subprocess.SubprocessError, OSError) as e:
        ok, msg = False, str(e)
    if not ok:
        log_mod.error(f"clone failed for {pr.repo}: {msg}")
    return ok, msg


def local_repo_rows(
    cfg: cfg_mod.Config,
    fetch_results: list[repo_mod.FetchResult],
    visible_prs: list[gh.PullRequest],
) -> list[LocalRepoStatus]:
    """Every enabled local clone with its fetch status and the review
    requests landing on it (matched by basename, like `local_path_for`).

    Unfiltered: display preferences (only_with_review / show_clean) are
    applied by `shown_rows`; the icon count reads these raw rows so the
    number doesn't shift when you change the view."""
    results_by_path = {r.path: r for r in fetch_results}
    review_by_base: dict[str, list[gh.PullRequest]] = defaultdict(list)
    for pr in visible_prs:
        review_by_base[pr.repo.split("/")[-1].lower()].append(pr)

    out: list[LocalRepoStatus] = []
    for path, _r in expanded_repos(cfg):
        name = Path(path).name
        fr = results_by_path.get(path)
        out.append(
            LocalRepoStatus(
                name=name,
                path=path,
                ok=fr.ok if fr else True,
                ahead=fr.ahead if fr else 0,
                behind=fr.behind if fr else 0,
                branch=fr.branch if fr else "",
                message=fr.message if fr else "",
                review_prs=review_by_base.get(name.lower(), []),
            )
        )
    out.sort(key=lambda s: s.name.lower())
    return out


def shown_rows(cfg: cfg_mod.LocalReposConfig, rows: list[LocalRepoStatus]) -> list[LocalRepoStatus]:
    """The 'Local repos' section rows, with display preferences applied."""
    if not cfg.enabled:
        return []
    out = []
    for st in rows:
        if cfg.only_with_review:
            if not st.review_prs:
                continue
        elif not cfg.show_clean and st.ok and not st.ahead and not st.behind and not st.review_prs:
            continue
        out.append(st)
    return out


def status_text(st: LocalRepoStatus) -> str:
    if not st.ok:
        return "fetch failed"
    parts: list[str] = []
    if st.ahead:
        parts.append(f"↑{st.ahead}")
    if st.behind:
        parts.append(f"↓{st.behind}")
    if st.review_prs:
        parts.append(f"· {len(st.review_prs)} to review")
    return " ".join(parts) if parts else "✓"


def fetch_notices(cfg: cfg_mod.Config, results: list[repo_mod.FetchResult]) -> list[Notice]:
    """Log each fetch result and return the notices the user asked for."""
    notices: list[Notice] = []
    for r in results:
        if not r.ok:
            log_mod.warn(f"fetch failed for {r.path}: {r.message}")
            if cfg.notify_on_fetch_error:
                notices.append(
                    Notice(f"Fetch failed: {r.name}", r.message, "low", "dialog-warning")
                )
        elif r.updated:
            log_mod.info(f"{r.name} ({r.branch or '?'}): fast-forwarded {r.updated} commit(s)")
            if cfg.notify_local_repos:
                notices.append(
                    Notice(
                        f"Updated: {r.name}",
                        f"{r.branch} fast-forwarded {r.updated} commit(s) from upstream",
                        "low",
                        sound=INFO,
                    )
                )
        elif cfg.features.show_behind_ahead and (r.ahead or r.behind):
            msg = []
            if r.behind:
                msg.append(f"{r.behind} behind")
            if r.ahead:
                msg.append(f"{r.ahead} ahead")
            log_mod.info(f"{r.name} ({r.branch or '?'}): {', '.join(msg)}")
            if r.behind and cfg.notify_local_repos:
                notices.append(
                    Notice(
                        f"{r.name}: {r.behind} behind",
                        f"{r.branch} is {r.behind} commit(s) behind upstream"
                        + (f", {r.ahead} ahead" if r.ahead else ""),
                        "low",
                        sound=INFO,
                    )
                )
    return notices
