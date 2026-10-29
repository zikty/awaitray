"""Background work: run a blocking job off the main thread, hand its result
back on the main thread.

The controller and its features never start threads themselves; they ask
a `Runner`. The app uses `ThreadRunner` (a daemon thread per job, the result
posted through `HostPort.run_on_main`, which is `GLib.idle_add` on Linux and
the main operation queue on macOS). Tests pass `ImmediateRunner`, which does
both right away, so they need no patching of `threading`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from . import log as log_mod

T = TypeVar("T")


class MainThread(Protocol):
    def run_on_main(self, callback: Callable[..., Any], *args: Any) -> None: ...


class Runner(Protocol):
    def background(self, job: Callable[[], T], then: Callable[[T], Any] | None = None) -> None:
        """Run `job()` off the main thread; then, on the main thread,
        `then(result)`. A job that raises is logged and `then` isn't
        called: a failure a feature must report is a result, not an
        exception."""
        ...


def _name(job: Callable[..., Any]) -> str:
    func = getattr(job, "func", job)  # functools.partial
    return getattr(func, "__qualname__", repr(func))


class ThreadRunner:
    def __init__(self, host: MainThread) -> None:
        self._host = host

    def background(self, job: Callable[[], T], then: Callable[[T], Any] | None = None) -> None:
        def work() -> None:
            try:
                result = job()
            except Exception as e:  # a worker's bug must not die silently
                log_mod.warn(f"background job {_name(job)} failed: {e}")
                return
            if then is not None:
                self._host.run_on_main(_deliver, then, result)

        threading.Thread(target=work, daemon=True).start()


def _deliver(then: Callable[[Any], Any], result: Any) -> bool:
    then(result)
    return False  # GLib.idle_add: run once


class ImmediateRunner:
    """Runs the job and its continuation right away, on the caller's thread."""

    def background(self, job: Callable[[], T], then: Callable[[T], Any] | None = None) -> None:
        try:
            result = job()
        except Exception as e:
            log_mod.warn(f"background job {_name(job)} failed: {e}")
            return
        if then is not None:
            then(result)
