"""Git fetch worker.

Each call runs `git fetch <remote> --prune` with a timeout. Results are
returned as a list — the caller (the indicator) decides whether to surface
errors via notifications.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_DISCOVER_TTL_SECONDS = 30.0
_discover_cache: dict[str, tuple[float, list[str]]] = {}


def invalidate_discover_cache() -> None:
    """Clear the discover_repos cache. Call when repo configuration changes."""
    _discover_cache.clear()


@dataclass(frozen=True)
class FetchResult:
    path: str
    ok: bool
    message: str
    ahead: int = 0
    behind: int = 0
    branch: str = ""
    updated: int = 0  # commits fast-forwarded by auto-update (0 if none)

    @property
    def name(self) -> str:
        return Path(self.path).name


def is_git_repo(path: str) -> bool:
    p = Path(path)
    if not p.is_dir():
        return False
    return (p / ".git").exists() or (p / "HEAD").exists()


def discover_repos(parent: str) -> list[str]:
    """Return immediate subdirectories of `parent` that look like git repos.

    Sorted alphabetically. Hidden directories are skipped. We do NOT recurse
    into nested folders to keep behavior predictable for users. Results are
    cached for a short TTL because the menu rebuild and PR matching call this
    repeatedly with the same arguments.
    """
    cached = _discover_cache.get(parent)
    if cached and (time.monotonic() - cached[0]) < _DISCOVER_TTL_SECONDS:
        return list(cached[1])

    p = Path(parent)
    if not p.is_dir():
        _discover_cache[parent] = (time.monotonic(), [])
        return []
    out = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            if is_git_repo(str(child)):
                out.append(str(child))
    except OSError:
        _discover_cache[parent] = (time.monotonic(), [])
        return []
    _discover_cache[parent] = (time.monotonic(), list(out))
    return out


# Boilerplate continuation lines git prints after the real cause. They carry no
# information on their own, so the naive "last line of stderr" gave the useless
# "and the repository exists." instead of the actual "Permission denied" cause.
_GIT_ERROR_NOISE = (
    "please make sure you have the correct access rights",
    "and the repository exists",
    "fatal: could not read from remote repository",
    "the requested url returned error",  # kept as fallback only, more specific below
)


def _summarize_git_error(stderr: str, stdout: str, returncode: int) -> str:
    """Turn a failed git invocation into one short, actionable line.

    Classifies the common auth/network failures (which recur and are the whole
    reason a user sees these warnings) into a clear message; otherwise falls
    back to the first informative line of output rather than the last, which
    git reserves for boilerplate ("...and the repository exists.").
    """
    raw = (stderr or stdout or "").strip()
    low = raw.lower()

    if "permission denied (publickey)" in low or (
        "could not read from remote repository" in low and "permission denied" in low
    ):
        return "SSH auth failed (key rejected — run ssh-add / authorize the key for the org)"
    if ("permission denied" in low and "publickey" not in low) or (
        "permission to" in low and "denied to" in low  # GitHub over HTTPS
    ):
        return "permission denied (no access to this repo)"
    if "repository not found" in low:
        # GitHub says this for private repos the account can't see, too.
        return "repository not found (or no access with this account)"
    if (
        "authentication failed" in low
        or "terminal prompts disabled" in low
        or ("could not read username" in low)
    ):
        return "auth failed (check `gh auth` / git credentials)"
    if "could not resolve host" in low or "name or service not known" in low:
        return "network: cannot resolve host (offline?)"
    if (
        "connection timed out" in low
        or "connection refused" in low
        or ("operation timed out" in low)
    ):
        return "network: connection failed"
    if "403" in low and "url returned error" in low:
        return "auth failed (HTTP 403)"

    # Fallback: first non-empty line that isn't pure git boilerplate.
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(noise in line.lower() for noise in _GIT_ERROR_NOISE):
            continue
        return line
    return f"exit {returncode}"


def fetch(
    path: str,
    remote: str = "origin",
    timeout: int = 60,
    auto_update: bool = False,
) -> FetchResult:
    if not is_git_repo(path):
        return FetchResult(path, False, "not a git repository")

    try:
        res = subprocess.run(
            ["git", "fetch", "--prune", "--quiet", "--", remote],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return FetchResult(path, False, f"timeout after {timeout}s")
    except FileNotFoundError:
        return FetchResult(path, False, "git not found")

    if res.returncode != 0:
        return FetchResult(
            path, False, _summarize_git_error(res.stderr, res.stdout, res.returncode)
        )

    ahead, behind, branch = _ahead_behind(path)

    # Auto-update: fast-forward to the upstream we just fetched, but only when
    # it can't possibly conflict — on a tracked branch, strictly behind (not
    # diverged), with a clean worktree. `--ff-only` aborts cleanly otherwise.
    if auto_update and branch and behind > 0 and ahead == 0 and is_clean(path):
        message = _fast_forward(path)
        if message == "ok":
            return FetchResult(path, True, "ok", ahead=0, behind=0, branch=branch, updated=behind)
        return FetchResult(path, True, message, ahead=ahead, behind=behind, branch=branch)

    return FetchResult(path, True, "ok", ahead=ahead, behind=behind, branch=branch)


def is_clean(path: str, timeout: int = 5) -> bool:
    """True when `git status --porcelain` is empty.

    Strict: any modified, staged, or untracked file makes the repo "dirty" so
    auto-update never risks a conflict or clobbering local work.
    """
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return res.returncode == 0 and not res.stdout.strip()


def _fast_forward(path: str, timeout: int = 30) -> str:
    """`git merge --ff-only @{upstream}`. Returns "ok" or an error message."""
    try:
        res = subprocess.run(
            ["git", "merge", "--ff-only", "@{upstream}"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ff timeout after {timeout}s"
    except (subprocess.SubprocessError, OSError) as e:
        return f"ff failed: {e}"
    if res.returncode == 0:
        return "ok"
    return _summarize_git_error(res.stderr, res.stdout, res.returncode)


def _ahead_behind(path: str) -> tuple[int, int, str]:
    """Returns (ahead, behind, branch) for the current HEAD vs its upstream.

    If HEAD has no upstream (detached, no tracking), returns zeros and "".
    """
    try:
        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""

        counts_res = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if counts_res.returncode != 0:
            return 0, 0, branch
        parts = counts_res.stdout.split()
        if len(parts) != 2:
            return 0, 0, branch
        ahead, behind = int(parts[0]), int(parts[1])
        return ahead, behind, branch
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0, 0, ""
