"""Runtime state persisted between runs (not settings).

Lives in the cache dir, not next to the config: losing it is harmless (the
next poll takes a fresh, silent baseline). It holds:

* the notification baselines (which PR keys were already seen), so a restart
  neither re-notifies known PRs nor misses ones that arrived while the app
  was off;
* the last PR lists and their details, so the menu can render immediately at
  startup while the first poll is still running.

A `scope` signature (hosts + filters + My PRs settings) is stored alongside;
if it doesn't match the current settings, the saved state describes a
different set of PRs and is ignored.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from . import config as cfg_mod
from . import github as gh
from . import log as log_mod
from . import transitions

_T = TypeVar("_T")

FORMAT_VERSION = 1


def default_path() -> Path:
    return cfg_mod.xdg_dir("XDG_CACHE_HOME", ".cache") / "awaitray" / "state.json"


def scope_signature(cfg: cfg_mod.Config) -> str:
    """Hash of the settings that decide *which* PRs a poll can see."""
    scope = {
        "hosts": sorted((h.hostname, h.user, h.enabled) for h in cfg.hosts),
        "filters": asdict(cfg.filters),
        "my_prs": asdict(cfg.my_prs),
    }
    blob = json.dumps(scope, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Snapshot:
    scope: str
    saved_at: str  # ISO 8601, UTC
    review_keys: set[str] | None = None
    my_keys: set[str] | None = None
    review_prs: list[gh.PullRequest] = field(default_factory=list)
    my_prs: list[gh.PullRequest] = field(default_factory=list)
    details: dict[str, gh.PRDetails] = field(default_factory=dict)
    # Your PRs' last-seen state and the transition notices already sent.
    my_facts: dict[str, transitions.Facts] = field(default_factory=dict)  # PR key -> facts
    sent: set[str] = field(default_factory=set)
    watching: set[str] = field(default_factory=set)  # PR keys: notify when checks finish
    seen: set[str] | None = None  # review requests seen in an opened menu
    # PR key -> last reminder, or first sight
    reminded: dict[str, str] = field(default_factory=dict)
    digest_date: str | None = None  # day the last digest went out
    section_keys: dict[str, set[str]] = field(default_factory=dict)  # sid -> baseline

    @property
    def saved_at_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.saved_at)
        except (TypeError, ValueError):
            return None


def _build(cls: type[_T], raw: Any) -> _T | None:
    """Dataclass from a dict, ignoring unknown keys; None if unusable."""
    if not isinstance(raw, dict):
        return None
    names = set(getattr(cls, "__dataclass_fields__"))  # noqa: B009
    try:
        return cls(**{k: v for k, v in raw.items() if k in names})
    except TypeError:
        return None


def _details(raw: Any) -> gh.PRDetails | None:
    det = _build(gh.PRDetails, raw)
    if det is None:
        return None
    raw_checks: Any = det.checks if isinstance(det.checks, (list, tuple)) else ()
    # JSON has no tuples: back to the hashable form the menu model needs.
    checks = tuple(
        (str(c[0]), str(c[1]), str(c[2]))
        for c in raw_checks
        if isinstance(c, (list, tuple)) and len(c) == 3
    )
    return replace(det, checks=checks)


def _dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _keys(raw: Any) -> set[str] | None:
    return {k for k in raw if isinstance(k, str)} if isinstance(raw, list) else None


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or default_path()

    def load(self) -> Snapshot | None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log_mod.warn(f"state: ignoring unreadable {self.path}: {e}")
            return None
        if not isinstance(raw, dict) or raw.get("version") != FORMAT_VERSION:
            return None

        def prs(items: Any) -> list[gh.PullRequest]:
            out = [_build(gh.PullRequest, it) for it in items] if isinstance(items, list) else []
            return [p for p in out if p is not None]

        details_raw = _dict(raw.get("details"))
        details = {url: _details(d) for url, d in details_raw.items()}
        return Snapshot(
            scope=str(raw.get("scope", "")),
            saved_at=str(raw.get("saved_at", "")),
            review_keys=_keys(raw.get("review_keys")),
            my_keys=_keys(raw.get("my_keys")),
            review_prs=prs(raw.get("review_prs")),
            my_prs=prs(raw.get("my_prs")),
            details={u: d for u, d in details.items() if d is not None},
            my_facts={
                k: f
                for k, f in (
                    (k, _build(transitions.Facts, v)) for k, v in _dict(raw.get("my_facts")).items()
                )
                if f is not None
            },
            sent=_keys(raw.get("sent")) or set(),
            watching=_keys(raw.get("watching")) or set(),
            seen=_keys(raw.get("seen")),
            reminded={k: v for k, v in _dict(raw.get("reminded")).items() if isinstance(v, str)},
            digest_date=raw.get("digest_date") if isinstance(raw.get("digest_date"), str) else None,
            section_keys={
                sid: keys
                for sid, keys in ((s, _keys(v)) for s, v in _dict(raw.get("section_keys")).items())
                if keys is not None
            },
        )

    def save(self, snap: Snapshot) -> None:
        payload = {
            "version": FORMAT_VERSION,
            "scope": snap.scope,
            "saved_at": snap.saved_at,
            "review_keys": sorted(snap.review_keys) if snap.review_keys is not None else None,
            "my_keys": sorted(snap.my_keys) if snap.my_keys is not None else None,
            "review_prs": [asdict(p) for p in snap.review_prs],
            "my_prs": [asdict(p) for p in snap.my_prs],
            "details": {u: asdict(d) for u, d in snap.details.items()},
            "my_facts": {k: asdict(f) for k, f in snap.my_facts.items()},
            "sent": sorted(snap.sent),
            "watching": sorted(snap.watching),
            "seen": sorted(snap.seen) if snap.seen is not None else None,
            "reminded": dict(snap.reminded),
            "digest_date": snap.digest_date,
            "section_keys": {sid: sorted(keys) for sid, keys in snap.section_keys.items()},
        }
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)
        except OSError as e:
            log_mod.warn(f"state: could not write {path}: {e}")


class NullStore(StateStore):
    """Persists nothing. Used when the controller gets an injected config
    (tests, tools) so it never touches the user's real cache."""

    def load(self) -> Snapshot | None:
        return None

    def save(self, snap: Snapshot) -> None:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
