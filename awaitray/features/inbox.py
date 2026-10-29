"""Your GitHub inbox in the menu: unread threads per account (fetched by
`account_info`), triaged from there (the gh calls are `awaitray.inbox`)."""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable

from .. import account_info
from .. import github as gh
from .. import inbox as inbox_mod
from ..notice import Notice
from .context import Context


class Inbox:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._infos: list[account_info.AccountInfo] = []
        # Lambdas: the method is looked up at click time.
        ctx.register_action("inbox_open", lambda account, tid: self.open(account, tid))
        # account, inbox.READ/DONE/UNSUBSCRIBE, thread id
        ctx.register_action("inbox", lambda account, what, tid: self.act(account, what, tid))
        ctx.register_action("inbox_read_all", lambda account: self.read_all(account))

    @property
    def infos(self) -> list[account_info.AccountInfo]:
        """The last round, for the menu; empty when it's turned off."""
        return self._infos if self._ctx.config().account_info.inbox else []

    def tick(self) -> None:
        cfg = self._ctx.config()
        self._ctx.runner.background(lambda: account_info.fetch(cfg), self.apply)

    def apply(self, infos: list[account_info.AccountInfo]) -> None:
        """Take a round of account info. Main thread only."""
        self._infos = infos
        self._ctx.rebuild_menu()

    def open(self, account: str, thread_id: str) -> None:
        """Open the thread's page and mark it as read, like GitHub's inbox."""
        for info in self._infos:
            for thread in info.inbox if info.account == account else ():
                if thread.thread_id == thread_id:
                    self._ctx.open_url(thread.url, None, account)
        self.act(account, inbox_mod.READ, thread_id)

    def act(self, account: str, action: str, thread_id: str) -> None:
        self._drop(account, {thread_id})
        job = functools.partial(inbox_mod.act, account, action, thread_id)
        self._ctx.runner.background(functools.partial(_problem, job), self._answer(account))

    def read_all(self, account: str) -> None:
        as_of = self._drop(account, None)
        if not as_of:
            return
        job = functools.partial(inbox_mod.mark_all_read, account, as_of)
        self._ctx.runner.background(functools.partial(_problem, job), self._answer(account))

    def _drop(self, account: str, thread_ids: set[str] | None) -> str:
        """Take threads off the menu right away (None: all of them); the
        call that makes it true on GitHub runs after. Returns the inbox's
        as-of time."""
        as_of = ""
        infos = []
        for info in self._infos:
            if info.account == account:
                as_of = info.inbox_as_of
                keep = (
                    ()
                    if thread_ids is None
                    else tuple(t for t in info.inbox if t.thread_id not in thread_ids)
                )
                gone = len(info.inbox) - len(keep)
                unread = None if info.unread is None else max(0, info.unread - gone)
                info = dataclasses.replace(info, inbox=keep, unread=unread)
            infos.append(info)
        self._infos = infos
        self._ctx.rebuild_menu()
        return as_of

    def _answer(self, account: str) -> Callable[[str | None], None]:
        def answer(problem: str | None) -> None:
            if problem is None:
                return
            # GitHub said no: say why and reload the inbox as it really is.
            self._ctx.tell(Notice(f"GitHub inbox ({account})", problem, icon="dialog-warning"))
            self.tick()

        return answer


def _problem(job: Callable[[], object]) -> str | None:
    """Run a gh call; what went wrong, or None. Blocking."""
    try:
        job()
    except gh.GhError as e:
        return str(e)
    return None
