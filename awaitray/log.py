"""Lightweight in-memory ring buffer for runtime events.

Used by the Logs tab in preferences. Also mirrors entries to stderr so
launching with `./run.sh` lets you tail them in the terminal.
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

_CAPACITY = 200
_LOCK = threading.Lock()
_BUFFER: deque[LogEntry] = deque(maxlen=_CAPACITY)


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    level: str
    message: str

    def render(self) -> str:
        return f"[{self.timestamp}] {self.level:<5} {self.message}"


def log(level: str, message: str) -> None:
    entry = LogEntry(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        level=level.upper(),
        message=message,
    )
    with _LOCK:
        _BUFFER.append(entry)
    print(entry.render(), file=sys.stderr, flush=True)


def info(message: str) -> None:
    log("info", message)


def warn(message: str) -> None:
    log("warn", message)


def error(message: str) -> None:
    log("error", message)


def snapshot() -> list[LogEntry]:
    with _LOCK:
        return list(_BUFFER)


def clear() -> None:
    with _LOCK:
        _BUFFER.clear()
