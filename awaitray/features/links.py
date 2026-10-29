"""Opening things: links in the right browser (per account), local clones in
the editor, and cloning a PR's repo."""

from __future__ import annotations

import functools
import shutil
import subprocess
import webbrowser

from .. import browser, repo_sync
from .. import github as gh
from .. import log as log_mod
from .. import repos as repo_mod
from ..notice import Notice
from .context import Context


class Links:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        on_pr = ctx.on_pr
        # Lambdas: the method is looked up at click time.
        # url, then the PR's key or "", then an account (whose browser)
        ctx.register_action(
            "open_url", lambda url, key="", account="": self.open_url(url, key or None, account)
        )
        # (url, PR key) pairs
        ctx.register_action("open_urls", lambda *links: self.open_urls(list(links)))
        ctx.register_action("open_editor", lambda path: self.open_in_editor(path))
        ctx.register_action("clone", on_pr(lambda pr: self.clone_repo(pr)))

    # ----- Browser -----

    def open_url(self, url: str, pr_key: str | None = None, account: str = "") -> None:
        """Open `url` in the browser of the PR's account, or of `account`
        for a link that isn't a PR's, else the global one, else the system
        browser."""
        self.open_urls([(url, pr_key)], account)

    def open_urls(self, links: list[tuple[str, str | None]], account: str = "") -> None:
        """Open (url, PR key) pairs, in order. Links that go to the same
        browsers share one job, so a broken browser is tried and reported
        once per click, not once per link."""
        cfg = self._ctx.config()
        groups: dict[tuple[tuple[str, str], ...], list[str]] = {}
        for url, key in links:
            # Check and status links come from whoever posted them: only web
            # pages, never a file:, custom scheme or "-option" to a browser.
            if not url.lower().startswith(("https://", "http://")):
                log_mod.warn(f"not opening {url!r}: not a web link")
                continue
            pr = self._ctx.pr_for_key(key) if key else None
            tried = browser.commands_for(cfg, pr, "" if pr else account)
            groups.setdefault(tuple(tried), []).append(url)
        for commands, urls in groups.items():
            if not commands:
                for url in urls:
                    webbrowser.open(url)
                continue
            job = functools.partial(launch_browsers, urls, list(commands))
            self._ctx.runner.background(job, self._browser_failed)

    def _browser_failed(self, failures: list[tuple[str, str, str]]) -> None:
        if not failures:
            return
        for who, command, problem in failures:
            log_mod.warn(f"browser {command!r} for {who}: {problem}")
        who, command, problem = failures[0]
        where = "the default browser" if who == browser.GLOBAL else f"the browser of {who}"
        self._ctx.tell(
            Notice(
                "Browser not found",
                f"{command}: {problem}. Opened the link elsewhere; "
                f"check {where} in Preferences → Accounts.",
                icon="dialog-warning",
            )
        )

    # ----- Local clones and the editor -----

    def local_path_for(self, pr: gh.PullRequest) -> str | None:
        return repo_sync.local_path_for(self._ctx.config(), pr)

    def has_local_clone(self, pr: gh.PullRequest) -> bool:
        return self.local_path_for(pr) is not None

    def review_marker(self, pr: gh.PullRequest) -> str:
        """Prefix shown on a review request that maps to a local clone."""
        if self._ctx.config().features.mark_local_review_requests and self.has_local_clone(pr):
            return "📂 "
        return ""

    def open_in_editor(self, path: str) -> None:
        cmd = self._ctx.config().editor.command.strip() or self._ctx.default_opener()
        bin_name = cmd.split()[0]
        if shutil.which(bin_name) is None:
            self._ctx.tell(
                Notice(
                    "Editor not found",
                    f"`{bin_name}` is not in PATH. Set the editor command in Preferences → Editor.",
                    icon="dialog-warning",
                )
            )
            return
        try:
            subprocess.Popen([*cmd.split(), path])
            log_mod.info(f"opened {path} in {bin_name}")
        except OSError as e:
            log_mod.error(f"failed to open editor: {e}")

    def clone_destination(self) -> str | None:
        return repo_sync.clone_destination(self._ctx.config())

    def can_clone(self, pr: gh.PullRequest) -> bool:
        cfg = self._ctx.config()
        return self.local_path_for(pr) is None and repo_sync.clone_target(cfg, pr) is not None

    def clone_repo(self, pr: gh.PullRequest) -> None:
        target = repo_sync.clone_target(self._ctx.config(), pr)
        if not target:
            self._ctx.tell(
                Notice(
                    "Clone destination not set",
                    "Set a clone root (or add an auto-discover parent) "
                    "in Preferences → Repositories.",
                    icon="dialog-warning",
                )
            )
            return
        self._ctx.runner.background(
            functools.partial(repo_sync.clone, pr, target),
            lambda done: self._after_clone(pr, done[0], done[1], target),
        )

    def _after_clone(self, pr: gh.PullRequest, ok: bool, msg: str, target: str) -> None:
        repo_mod.invalidate_discover_cache()
        if ok:
            self._ctx.tell(Notice(f"Cloned {pr.repo}", f"Into {target}", "low"))
            self._ctx.tick_fetch()
        else:
            self._ctx.tell(Notice(f"Clone failed: {pr.repo}", msg, icon="dialog-warning"))
        self._ctx.rebuild_menu()


def launch_browsers(urls: list[str], commands: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Open each link with the first browser that starts; the ones that
    didn't, as (who, command, problem). Blocking: run it through the Runner."""
    failures: list[tuple[str, str, str]] = []
    for url in urls:
        while commands:
            who, command = commands[0]
            problem = browser.launch(command, url)
            if problem is None:
                break
            failures.append((who, command, problem))
            commands = commands[1:]  # failed once: not retried for the next link
        else:
            webbrowser.open(url)  # never lose the click
    return failures
