"""Per-PR snoozes: set and lift them from the menu, and wake them on time,
on new activity or on a new commit. The entries live in `cfg.snoozed` (a user
decision, so it's saved with the settings, not the cache)."""

from __future__ import annotations

from .. import config as cfg_mod
from .. import github as gh
from .. import log as log_mod
from .. import snooze as snooze_mod
from .context import Context


class Snoozes:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        # Lambdas: the method is looked up at click time.
        ctx.register_action("snooze", ctx.on_pr(lambda pr, kind: self.snooze(pr, kind)))
        ctx.register_action("unsnooze", lambda key: self.unsnooze(key))

    def snooze(self, pr: gh.PullRequest, kind: str) -> None:
        cfg = self._ctx.config()
        details = self._ctx.details_for(pr.url)
        head_oid = details.head_oid if details else None
        entry = snooze_mod.entry_for(kind, updated_at=pr.updated_at, head_oid=head_oid)
        if entry is None:
            return
        if kind == "next_commit" and not head_oid:
            # Snoozed for 1h as a stop-gap; fetch the head so it can be re-snoozed.
            self._ctx.refresh_details([pr.url])
        cfg.snoozed[pr.key] = entry
        cfg_mod.save(cfg)
        log_mod.info(f"snoozed {pr.key} ({kind})")
        self._ctx.refresh_ui()

    def unsnooze(self, key: str) -> None:
        cfg = self._ctx.config()
        if cfg.snoozed.pop(key, None):
            cfg_mod.save(cfg)
            log_mod.info(f"unsnoozed {key}")
            self._ctx.refresh_ui()

    def _unsnooze_many(self, keys: list[str], why: str) -> bool:
        if not keys:
            return False
        cfg = self._ctx.config()
        for key in keys:
            cfg.snoozed.pop(key, None)
        cfg_mod.save(cfg)
        log_mod.info(f"unsnoozed {len(keys)} PR(s) {why}")
        return True

    def wake_on_activity(self, prs: list[gh.PullRequest]) -> None:
        """Wake "until new activity" snoozes whose PR was updated since."""
        snoozed = self._ctx.config().snoozed
        woken = [
            pr.key
            for pr in prs
            if pr.key in snoozed and snooze_mod.expired_by_activity(snoozed[pr.key], pr.updated_at)
        ]
        self._unsnooze_many(woken, "on new activity")

    def wake_on_commit(self, details: dict[str, gh.PRDetails]) -> None:
        """Wake "until next commit" snoozes whose head moved."""
        woken = []
        for key, entry in self._ctx.config().snoozed.items():
            pr = self._ctx.pr_for_key(key)
            if pr is None or pr.url not in details:
                continue
            if snooze_mod.expired_by_commit(entry, details[pr.url].head_oid):
                woken.append(key)
        self._unsnooze_many(woken, "on new commit")

    def sweep_expired(self) -> bool:
        """Drop time-based snoozes that have expired. True if anything changed."""
        expired = [k for k, v in self._ctx.config().snoozed.items() if not snooze_mod.is_active(v)]
        return self._unsnooze_many(expired, "expired")
