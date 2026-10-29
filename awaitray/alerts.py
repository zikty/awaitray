"""What reaches the user: notifications and sounds, behind the quiet-hours
gate and the one-sound-at-a-time limit.

- `notify`: news (a new review request, a failed check, low quota). Muted
  in quiet hours, with its sound from the pack.
- `tell`: the answer to something the user just did (a click that failed).
  Shown even in quiet hours, which are for news, not for replies; silent.
- Tray-icon tier sounds: "every" plays the tier's sound for each batch of
  new requests; "transition" when the count moves into another tier.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from . import config as cfg_mod
from . import icon as icon_mod
from . import log as log_mod
from . import quiet_hours as qh_mod
from . import sounds
from .notice import Notice

SOUND_GAP_SECONDS = 2.0
_UNSET = object()  # no tier seen yet: the first one is no transition


class Speaker(Protocol):
    def notify(self, title: str, body: str, urgency: str, icon: str) -> None: ...
    def play_sound(self, sound: str) -> None: ...


class Alerts:
    def __init__(self, ui: Speaker, config: Callable[[], cfg_mod.Config]) -> None:
        self._ui = ui
        self._config = config
        self._last_sound_at = -SOUND_GAP_SECONDS  # monotonic time of the last sound
        self._last_tier: object = _UNSET

    def tell(self, notice: Notice) -> None:
        self._ui.notify(notice.title, notice.body, notice.urgency, notice.icon)

    def notify(self, notice: Notice) -> None:
        cfg = self._config()
        if qh_mod.is_quiet(cfg.quiet_hours):
            log_mod.info(f"suppressed (quiet hours): {notice.title} — {notice.body}")
            return
        self._ui.notify(notice.title, notice.body, notice.urgency, notice.icon)
        if notice.sound and cfg.sounds.enabled:
            path = sounds.resolve(cfg.sounds, notice.sound, notice.event)
            if path:
                self.play(path)

    def play(self, sound: str) -> None:
        """Play a pack or tier sound, gated by quiet hours (like a
        notification). One per SOUND_GAP_SECONDS: a poll that brings several
        notices, or a notice plus a tier change, plays the first, not a pile."""
        if not sound or qh_mod.is_quiet(self._config().quiet_hours):
            return
        now = time.monotonic()
        if now - self._last_sound_at < SOUND_GAP_SECONDS:
            return
        self._last_sound_at = now
        try:
            self._ui.play_sound(sound)
        except Exception as e:  # never let audio break the refresh path
            log_mod.warn(f"play_sound failed: {e}")

    def new_requests(self, count: int) -> None:
        """New review requests arrived: in "every" mode, the tier's sound."""
        tray = self._config().tray_icon
        if tray.sound_trigger == "every":
            bucket = icon_mod.pick_bucket(count, tray.buckets)
            if bucket is not None:
                self.play(bucket.sound)

    def count_changed(self, count: int) -> None:
        """In "transition" mode, play the new tier's sound when the count
        moves into a different one. Always tracks the tier, so switching to
        that mode later doesn't fire spuriously."""
        tray = self._config().tray_icon
        bucket = icon_mod.pick_bucket(count, tray.buckets)
        tier = None if bucket is None else (bucket.up_to, bucket.emoji)
        changed = self._last_tier is not _UNSET and tier != self._last_tier
        self._last_tier = tier
        if tray.sound_trigger == "transition" and changed and bucket is not None:
            self.play(bucket.sound)
