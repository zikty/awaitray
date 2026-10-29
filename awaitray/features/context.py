"""What a feature may use of the app: a narrow set of callables, never the
controller itself."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .. import config as cfg_mod
from .. import github as gh
from ..notice import Notice
from ..runner import Runner


@dataclass(frozen=True)
class Context:
    # The live config. A function, not a value: Preferences replace it.
    config: Callable[[], cfg_mod.Config]
    # Blocking work (gh, git, a CLI) goes here, never on the main thread.
    runner: Runner
    # News, gated by quiet hours and with its sound.
    notify: Callable[[Notice], None]
    # The answer to a click: shown even in quiet hours, silent.
    tell: Callable[[Notice], None]
    rebuild_menu: Callable[[], None]
    # The icon and the menu (what a snooze or a fetch changes).
    refresh_ui: Callable[[], None]
    register_action: Callable[[str, Callable[..., object]], None]
    pr_for_key: Callable[[str], gh.PullRequest | None]
    details_for: Callable[[str], gh.PRDetails | None]
    # Collaborations, wired by the controller.
    open_url: Callable[[str, str | None, str], None]  # url, PR key, account
    refresh_details: Callable[[list[str]], None]  # refetch these PRs now
    tick_fetch: Callable[[], object]  # fetch every clone now
    default_opener: Callable[[], str]  # the platform's "open this file" command

    def on_pr(self, run: Callable[..., object]) -> Callable[..., object]:
        """An action handler whose first arg is a PR key; a PR that's gone
        since the menu was built is ignored."""

        def handler(key: str, *rest: object) -> None:
            pr = self.pr_for_key(key)
            if pr is not None:
                run(pr, *rest)

        return handler
