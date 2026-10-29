"""Things you might be waiting on besides PRs: each AI assistant's quota
left (`ai_usage`, every account round) and GitHub's own health
(`github_status`, every PR poll). Both warn once when it matters."""

from __future__ import annotations

from .. import ai_usage, github_status, sounds
from ..notice import Notice
from .context import Context


class Usage:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._ai: list[ai_usage.Usage] = []
        # (provider, account, window, reset) already warned about
        self._warned: set[tuple[str, str, str, object]] = set()
        self._status: github_status.Status | None = None
        self._incidents_told: set[str] = set()

    # ----- AI assistants -----

    @property
    def ai_usages(self) -> list[ai_usage.Usage]:
        """The last round, for the menu; a provider turned off since shows
        nothing."""
        enabled = self._ctx.config().ai_usage
        return [u for u in self._ai if getattr(enabled, u.provider, False)]

    def tick_ai(self) -> None:
        cfg = self._ctx.config()
        self._ctx.runner.background(lambda: ai_usage.fetch(cfg), self.apply_ai_usage)

    def apply_ai_usage(self, usages: list[ai_usage.Usage]) -> None:
        """Take a round of AI usage. Main thread only. Warns once per window
        and period when what's left gets low."""
        self._ai = usages
        for usage in usages:
            low = usage.low
            if low is None:
                continue
            key = (usage.provider, usage.account, low.name, low.reset)
            if key in self._warned:
                continue
            self._warned.add(key)
            when = f" until {ai_usage.format_reset(low.reset)}" if low.reset else ""
            whose = f"{usage.account}: " if usage.account else ""
            self._ctx.notify(
                Notice(
                    f"{usage.title}: {low.percent_left:.0f}% left",
                    f"{whose}{usage.describe()}. Usage is limited{when}.",
                    "normal",
                    sound=sounds.INFO,
                )
            )
        self._ctx.rebuild_menu()

    # ----- GitHub's status page -----

    @property
    def status_page(self) -> github_status.Status | None:
        """The last status, for the menu; None when it's turned off or unknown."""
        return self._status if github_status.wanted(self._ctx.config()) else None

    def tick_status(self) -> None:
        if github_status.wanted(self._ctx.config()):
            self._ctx.runner.background(github_status.fetch, self.apply_github_status)

    def apply_github_status(self, status: github_status.Status | None) -> None:
        """Take githubstatus.com's answer (None: it couldn't be read, keep
        the last one). Main thread only. Notifies each major incident once;
        the menu shows any incident while it lasts."""
        if status is None:
            return
        for incident in status.incidents:
            if (
                incident.impact in github_status.NOTIFY_IMPACTS
                and incident.id not in self._incidents_told
            ):
                self._incidents_told.add(incident.id)
                self._ctx.notify(
                    Notice(
                        "GitHub is having problems",
                        incident.name,
                        "normal",
                        icon="dialog-warning",
                        sound=sounds.INFO,
                    )
                )
        if status != self._status:
            self._status = status
            self._ctx.rebuild_menu()
