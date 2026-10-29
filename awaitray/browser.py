"""Which browser opens a link, and how to launch it.

Each account can name its own browser: a corporate account behind a VPN often
lives in another browser or profile than the personal one. A global setting
covers accounts without one and links that belong to no PR. Nothing set
anywhere: the system browser, as before.

A browser is written the way the user would launch it:

- Linux: a command, the URL goes last (`firefox -P Work`,
  `google-chrome --profile-directory="Profile 2"`).
- macOS: an application name, optionally followed by arguments for it
  (`Firefox`, `Google Chrome --profile-directory="Profile 2"`).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from . import config as cfg_mod
from .github import PullRequest

GLOBAL = "default"  # the `who` of the global browser in commands_for


def commands_for(
    cfg: cfg_mod.Config, pr: PullRequest | None, account: str = ""
) -> list[tuple[str, str]]:
    """(who, command) pairs to try in order: the PR's accounts (or the given
    account, for links that aren't a PR's) with a browser, in the order of
    `cfg.hosts`, then the global one. Empty means the system browser."""
    out: list[tuple[str, str]] = []
    if pr is not None or account:
        seen_by = {a.strip() for a in (pr.account if pr else account).split(",") if a.strip()}
        host = pr.host if pr else cfg_mod.split_account(account)[1]
        for acct in cfg.hosts:
            command = acct.browser.strip()
            if not command:
                continue
            # A PR without accounts (restored from an older cache) falls back
            # to its host.
            if acct.account_id in seen_by or (not seen_by and acct.hostname == host):
                out.append((acct.account_id, command))
    command = cfg.browser.command.strip()
    if command:
        out.append((GLOBAL, command))
    unique: list[tuple[str, str]] = []
    for who, command in out:
        if all(command != c for _, c in unique):
            unique.append((who, command))
    return unique


def argv(command: str, url: str, platform: str | None = None) -> list[str]:
    """The process to run for `command` and `url`. Raises ValueError on a
    command shlex can't split (an unbalanced quote)."""
    if (platform or sys.platform) == "darwin":
        # The app is everything before the first option; quotes are optional
        # ("Google Chrome" or Google Chrome).
        app_part, sep, rest = command.partition(" -")
        app = " ".join(shlex.split(app_part))
        extra = [_expand(t) for t in shlex.split("-" + rest)] if sep else []
        if extra:
            # -n: a new instance, or a running one would ignore the arguments.
            return ["open", "-na", app, "--args", *extra, url]
        return ["open", "-a", app, url]
    return [_expand(t) for t in shlex.split(command)] + [url]


def _expand(token: str) -> str:
    """`~/x` and `--flag=~/x` (how profile folders are usually written)."""
    name, sep, value = token.partition("=")
    if sep and value.startswith("~") and name.startswith("-"):
        return f"{name}={os.path.expanduser(value)}"
    return os.path.expanduser(token)


def launch(command: str, url: str) -> str | None:
    """Open `url` with `command`. Blocking (on macOS, `open` reports a missing
    application only through its exit status): call it off the main thread.
    Returns None on success, or why it failed."""
    try:
        args = argv(command, url)
    except ValueError as e:
        return f"can't parse {command!r}: {e}"
    mac = sys.platform == "darwin"
    if not mac and shutil.which(args[0]) is None:
        return f"{args[0]!r} is not in PATH"
    try:
        if mac:
            done = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if done.returncode != 0:
                return done.stderr.strip() or f"open exited with {done.returncode}"
        else:
            subprocess.Popen(args, start_new_session=True)
    except subprocess.TimeoutExpired:
        # Found and still starting (a cold start, a first-run dialog): it
        # will show the link; falling back would open it twice.
        return None
    except OSError as e:
        return str(e)
    return None
