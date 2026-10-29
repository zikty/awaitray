"""Is GitHub itself having a bad day? From githubstatus.com.

The public Statuspage summary (no auth, CDN-cached, not GitHub's API, so it
costs no rate limit) is read on every PR poll, so an incident shows up as
soon as your PRs would. Only for github.com accounts: GHES has no status
page. Best effort: a failed read keeps the last answer and logs it.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import config as cfg_mod
from . import log as log_mod
from . import text

SUMMARY_URL = "https://www.githubstatus.com/api/v2/summary.json"
PAGE_URL = "https://www.githubstatus.com"
MAX_BYTES = 1_000_000  # the summary is a few KB; more is not it
# The parts of GitHub awaitray (and a PR workflow) depends on.
WATCHED = (
    "Git Operations",
    "API Requests",
    "Pull Requests",
    "Actions",
    "Copilot",
    "Copilot AI Model Providers",
)
# Incidents worth a notification; minor ones only show in the menu.
NOTIFY_IMPACTS = ("major", "critical")


@dataclass(frozen=True)
class Incident:
    id: str
    name: str
    impact: str  # none | minor | major | critical
    url: str


@dataclass(frozen=True)
class Status:
    incidents: tuple[Incident, ...] = ()  # unresolved
    degraded: tuple[str, ...] = ()  # watched components not operational

    @property
    def ok(self) -> bool:
        return not self.incidents and not self.degraded

    def label(self) -> str:
        if self.incidents:
            more = len(self.incidents) - 1
            return f"⚠ GitHub: {self.incidents[0].name}" + (f" (+{more} more)" if more else "")
        if self.degraded:
            return f"⚠ GitHub: {', '.join(self.degraded)} degraded"
        return ""

    @property
    def url(self) -> str:
        return self.incidents[0].url if self.incidents else PAGE_URL


def parse(payload: Any) -> Status | None:
    """summary.json → Status; None when it doesn't look like one."""
    if not isinstance(payload, dict):
        return None
    incidents = []
    for raw in payload.get("incidents") or []:
        if not isinstance(raw, dict) or raw.get("status") in ("resolved", "postmortem"):
            continue
        name = text.clean(raw.get("name") or "", 120)
        if name:
            incidents.append(
                Incident(
                    id=str(raw.get("id") or name),
                    name=name,
                    impact=str(raw.get("impact") or "none"),
                    url=str(raw.get("shortlink") or PAGE_URL),
                )
            )
    degraded = []
    for comp in payload.get("components") or []:
        if not isinstance(comp, dict):
            continue
        part, state = comp.get("name"), comp.get("status")
        if part in WATCHED and state not in (None, "operational"):
            degraded.append(text.clean(part, 60))
    return Status(tuple(incidents), tuple(degraded))


def wanted(cfg: cfg_mod.Config) -> bool:
    return cfg.account_info.github_status and any(
        h.enabled and h.hostname.lower() == "github.com" for h in cfg.hosts
    )


def fetch(timeout: int = 10) -> Status | None:
    """Blocking: call it from a worker thread. None when it can't be read."""
    request = urllib.request.Request(SUMMARY_URL, headers={"User-Agent": "awaitray"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as res:
            body = res.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("summary too large")
        return parse(json.loads(body))
    except (OSError, ValueError) as e:
        log_mod.info(f"githubstatus.com unavailable: {e}")
        return None
