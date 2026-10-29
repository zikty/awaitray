"""Platform backends. `select()` returns the right one at runtime."""

from __future__ import annotations

import sys
from types import ModuleType


def select() -> ModuleType:
    """Import and return the backend module for the current platform.

    Each backend module must expose ``run() -> None``.
    """
    if sys.platform == "darwin":
        from .macos import app as backend
    else:
        from .linux import app as backend
    return backend
