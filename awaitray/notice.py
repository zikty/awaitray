"""A notification the controller should show, as data.

The sync modules decide *what* to notify and return `Notice`s; the controller
decides *whether* (quiet hours) and hands them to the backend.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    title: str
    body: str = ""
    urgency: str = "normal"  # low | normal | critical
    icon: str = "dialog-information"
    # Which sound of the chosen pack plays with it (sounds.SLOTS), "" for none.
    sound: str = ""
    # Your PR's event (sounds.EVENTS) when it's about one: its sound can be
    # chosen on its own.
    event: str = ""
