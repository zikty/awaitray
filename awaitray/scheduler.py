"""Periodic timers, the retry timer, and the error policy.

`Scheduler` owns every timer handle, so there is exactly one place that
creates and cancels them. `on_total_failure` is the error policy as a pure
decision; the controller carries it out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from . import config as cfg_mod

SNOOZE_SWEEP_SECONDS = 60  # expired snoozes come back within a minute
ACCOUNT_INFO_SECONDS = 30 * 60  # Copilot quota, inbox: slow-moving


class HostPort(Protocol):
    """Scheduling primitives the controller needs from the event loop.

    Semantics mirror GLib exactly so the same callbacks work on both
    backends: a `timeout_add_seconds` callback that returns a truthy value
    keeps firing; returning falsy stops it. `run_on_main` runs a callback
    once on the main thread (used to post worker results back).
    """

    def timeout_add_seconds(self, seconds: int, callback: Callable[[], bool]) -> object: ...
    def source_remove(self, handle: object) -> None: ...
    def run_on_main(self, callback: Callable[..., Any], *args: Any) -> None: ...
    def default_opener(self) -> str: ...


@dataclass(frozen=True)
class FailureAction:
    """What the error policy wants after a total failure."""

    pause: bool = False
    retry_in: int | None = None  # seconds


def on_total_failure(
    policy: cfg_mod.ErrorPolicy, consecutive: int, paused: bool
) -> FailureAction | None:
    """The error policy: nothing (wait for the next tick), pause, or retry
    with exponential backoff until the retries run out."""
    policy = policy.normalized()
    if policy.on_error == "pause":
        if consecutive >= policy.pause_after_failures and not paused:
            return FailureAction(pause=True)
        return None
    if policy.on_error == "retry" and consecutive <= policy.max_retries:
        return FailureAction(retry_in=policy.retry_backoff_seconds * (2 ** (consecutive - 1)))
    return None


class Scheduler:
    def __init__(self, host: HostPort) -> None:
        self._host = host
        self._periodic: list[object] = []
        self._retry: object | None = None

    def reschedule(
        self,
        cfg: cfg_mod.Config,
        *,
        on_prs: Callable[[], bool],
        on_fetch: Callable[[], bool],
        on_sweep: Callable[[], bool],
        on_account: Callable[[], bool] | None = None,
    ) -> None:
        """Cancel every timer (the pending retry too) and, unless paused,
        start the periodic ones from the configured intervals."""
        for handle in self._periodic:
            self._host.source_remove(handle)
        self._periodic = []
        self.cancel_retry()
        if cfg.paused:
            return
        self._periodic = [
            self._host.timeout_add_seconds(max(1, cfg.intervals.pr_check_minutes) * 60, on_prs),
            self._host.timeout_add_seconds(max(1, cfg.intervals.fetch_minutes) * 60, on_fetch),
            self._host.timeout_add_seconds(SNOOZE_SWEEP_SECONDS, on_sweep),
        ]
        if on_account is not None:
            self._periodic.append(self._host.timeout_add_seconds(ACCOUNT_INFO_SECONDS, on_account))

    def retry(self, seconds: int, callback: Callable[[], None]) -> None:
        """Run `callback` once after `seconds`, replacing any pending retry."""
        self.cancel_retry()

        def fire() -> bool:
            self._retry = None
            callback()
            return False

        self._retry = self._host.timeout_add_seconds(seconds, fire)

    def cancel_retry(self) -> None:
        if self._retry is not None:
            self._host.source_remove(self._retry)
            self._retry = None

    @property
    def retry_pending(self) -> bool:
        return self._retry is not None

    def once(self, seconds: int, callback: Callable[[], None]) -> None:
        """Run `callback` once after `seconds` (not cancelled by reschedule)."""

        def fire() -> bool:
            callback()
            return False

        self._host.timeout_add_seconds(seconds, fire)
