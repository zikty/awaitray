"""One-click fixes on your PRs, from the menu: re-run failed checks, ask
Copilot for a review (the gh calls are `awaitray.pr_actions`)."""

from __future__ import annotations

import functools

from .. import github as gh
from .. import pr_actions
from ..notice import Notice
from .context import Context


class PrActions:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        on_pr = ctx.on_pr
        ctx.register_action("rerun_failed", on_pr(lambda pr: self.rerun_failed(pr)))
        ctx.register_action("copilot_review", on_pr(lambda pr: self.request_copilot_review(pr)))

    def rerun_failed(self, pr: gh.PullRequest) -> None:
        runs = pr_actions.failed_runs(pr, self._ctx.details_for(pr.url))
        if runs:
            job = functools.partial(pr_actions.rerun_failed, pr, runs)
            self._ctx.runner.background(job, functools.partial(self._done, pr))

    def request_copilot_review(self, pr: gh.PullRequest) -> None:
        job = functools.partial(pr_actions.request_copilot_review, pr)
        self._ctx.runner.background(job, functools.partial(self._done, pr))

    def _done(self, pr: gh.PullRequest, notice: Notice) -> None:
        self._ctx.tell(notice)
        # Its checks are running again (or Copilot joined): refresh that PR now.
        self._ctx.refresh_details([pr.url])
