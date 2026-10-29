"""How much of each AI assistant's quota is left, for the menu.

A provider per assistant, each turned on or off in Preferences
(`cfg.ai_usage.<id>`), each answering a `Usage`: its plan and one `Window`
per limit (a 5-hour window, a week, a month…). Fetched on a worker thread
every ACCOUNT_MINUTES with the account info; best effort throughout: a
provider that isn't installed, isn't logged in or answers something
unexpected just shows no line (the reason goes to the log).

awaitray never reads another app's token. Each provider asks its own CLI,
which uses its own login:
- Copilot: `gh` (the unofficial `GET /copilot_internal/user`, see
  `parse_copilot`), per github.com account.
- Codex: `codex app-server`'s JSON-RPC `account/rateLimits/read`; read-only,
  it starts no turn and spends no quota.
- Antigravity: `agy -p /usage --output-format json` (1.1.11+: older ones
  could send the slash command to a model as a prompt).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import github as gh
from . import log as log_mod

LOW_PERCENT = 20  # notify once per window and period at or below this
CLI_TIMEOUT = 60  # seconds a CLI gets to answer
MAX_OUTPUT = 1_000_000  # characters read from a CLI: its answer is a few KB
# Resets a CLI may report: anything else is garbage (and some dates can't
# be shown in the local zone at all).
_EARLIEST = datetime(1970, 1, 2, tzinfo=timezone.utc)
_LATEST = datetime(2200, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Window:
    """One limit: how much is left of it and when it starts over."""

    name: str  # "5h", "week", "month", a model group…; "" when it's the only one
    percent_left: float | None = None
    reset: datetime | None = None  # aware
    remaining: float | None = None  # in the plan's units, when known
    entitlement: float | None = None


@dataclass(frozen=True)
class Usage:
    provider: str  # a PROVIDERS id
    title: str  # "Copilot Business", "Codex Plus", "Antigravity"
    account: str = ""  # whose, when a provider has several (Copilot)
    windows: tuple[Window, ...] = ()
    unlimited: bool = False
    url: str = ""  # where to see it; "" when there's no page

    def tightest(self) -> Window | None:
        """The window with the least left (the one that will stop you)."""
        known = [w for w in self.windows if w.percent_left is not None]
        return min(known, key=lambda w: w.percent_left or 0.0) if known else None

    def describe(self, now: datetime | None = None) -> str:
        parts = [self.title]
        if self.unlimited:
            parts.append("unlimited")
        shown = [w for w in self.windows if w.percent_left is not None]
        for w in shown:
            text = f"{w.percent_left:.0f}% left"
            if w.remaining is not None and w.entitlement:
                text += f" ({w.remaining:,.0f} of {w.entitlement:,.0f})"
            parts.append(f"{w.name} {text}" if len(shown) > 1 and w.name else text)
        tight = self.tightest()
        reset = tight.reset if tight else next((w.reset for w in self.windows if w.reset), None)
        if reset is not None and not self.unlimited:
            parts.append(f"resets {format_reset(reset, now)}")
        return " · ".join(parts)

    @property
    def low(self) -> Window | None:
        tight = self.tightest()
        if self.unlimited or tight is None or (tight.percent_left or 0) > LOW_PERCENT:
            return None
        return tight


def format_reset(when: datetime, now: datetime | None = None) -> str:
    """A time within the next day, else a date. Midnight-UTC resets (monthly
    quotas) always read as a date."""
    now = now or datetime.now(timezone.utc)
    try:
        utc = when.astimezone(timezone.utc)
        local = when.astimezone()
    except (OverflowError, ValueError, OSError):
        return f"{when.strftime('%b')} {when.day}"
    if (utc.hour, utc.minute) != (0, 0) and timedelta(0) <= when - now < timedelta(days=1):
        return local.strftime("%H:%M")
    day = utc if (utc.hour, utc.minute) == (0, 0) else local
    return f"{day.strftime('%b')} {day.day}"


# ----- Small parsing helpers -----


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _when(value: Any) -> datetime | None:
    """ISO 8601 (a date alone is midnight UTC) or Unix seconds → aware."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not _EARLIEST.timestamp() <= value <= _LATEST.timestamp():
            return None
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed if _EARLIEST <= parsed <= _LATEST else None
    except (ValueError, OverflowError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# ----- Copilot (via gh) -----

_COPILOT_PLANS = {
    "free_limited_copilot": "Free",
    "copilot_pro": "Pro",
    "copilot_pro_plus": "Pro+",
    "copilot_for_business": "Business",
    "copilot_enterprise": "Enterprise",
}


def parse_copilot(payload: Any, account: str = "") -> Usage | None:
    """`copilot_internal/user` → Usage, or None when it doesn't look like a
    Copilot account (or the shape changed)."""
    if not isinstance(payload, dict):
        return None
    sku = str(payload.get("access_type_sku") or "")
    raw_plan = str(payload.get("copilot_plan") or "")
    if not sku and not raw_plan:
        return None
    title = f"Copilot {_COPILOT_PLANS.get(sku) or raw_plan.replace('_', ' ').title()}"
    reset = _when(payload.get("quota_reset_date_utc") or payload.get("quota_reset_date"))
    snapshots = _dict(payload.get("quota_snapshots"))
    snap = next(
        (
            snapshots[n]
            for n in ("premium_interactions", "chat")
            if isinstance(snapshots.get(n), dict)
        ),
        None,
    )
    base = Usage("copilot", title, account, url="https://github.com/settings/copilot")
    if snap is None:
        return replace(base, windows=(Window("", reset=reset),) if reset else ())
    window = Window(
        "",
        percent_left=_number(snap.get("percent_remaining")),
        reset=_when(snap.get("quota_reset_at")) or reset,
        remaining=_number(snap.get("remaining")),
        entitlement=_number(snap.get("entitlement")),
    )
    return replace(base, windows=(window,), unlimited=bool(snap.get("unlimited")))


def _copilot(cfg: cfg_mod.Config) -> list[Usage]:
    accounts = [h for h in cfg.hosts if h.enabled and h.hostname.lower() == "github.com"]
    if not accounts or not gh.gh_available():
        return []
    out = []
    for acct in accounts:
        try:
            payload = gh.rest_get(acct.hostname, "copilot_internal/user", acct.user)
        except gh.NotFoundError:
            continue  # no Copilot on this account
        except gh.GhError as e:
            log_mod.info(f"copilot quota for {acct.account_id} unavailable: {e}")
            continue
        usage = parse_copilot(payload, acct.account_id)
        if usage is not None:
            out.append(usage)
    return out


# ----- Finding a CLI (a LaunchAgent's PATH is short) -----

_EXTRA_DIRS = (
    "~/.local/bin",
    "~/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def find_cli(name: str) -> str | None:
    dirs = [os.environ.get("PATH", ""), *(os.path.expanduser(d) for d in _EXTRA_DIRS)]
    return shutil.which(name, path=os.pathsep.join(d for d in dirs if d))


def _env_for(exe: str) -> dict[str, str]:
    """The CLI's own folder first on PATH: an npm-installed one runs
    `#!/usr/bin/env node`, and node usually lives next to it."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(exe).parent), env.get("PATH", "")])
    return env


# ----- Codex (codex app-server, JSON-RPC over stdio) -----

_CODEX_PLANS = {"plus": "Plus", "pro": "Pro", "team": "Team", "business": "Business"}


def _window_name(minutes: Any) -> str:
    mins = _number(minutes)
    if mins is None:
        return ""
    if mins == 10080:
        return "week"
    if mins % 1440 == 0:
        return f"{mins / 1440:.0f}d"
    return f"{mins / 60:.0f}h" if mins % 60 == 0 else f"{mins:.0f}m"


def parse_codex(result: Any) -> Usage | None:
    """`account/rateLimits/read`'s result → Usage."""
    snapshot = _dict(_dict(result).get("rateLimits"))
    windows = []
    for key in ("primary", "secondary"):
        raw = _dict(snapshot.get(key))
        used = _number(raw.get("usedPercent", raw.get("used_percent")))
        if used is None:
            continue
        windows.append(
            Window(
                _window_name(raw.get("windowDurationMins", raw.get("window_minutes"))),
                percent_left=max(0.0, 100.0 - used),
                reset=_when(raw.get("resetsAt", raw.get("resets_at"))),
            )
        )
    if not windows:
        return None
    plan = str(snapshot.get("planType") or snapshot.get("plan_type") or "")
    title = f"Codex {_CODEX_PLANS.get(plan.lower(), plan.title())}".strip()
    return Usage(
        "codex", title, windows=tuple(windows), url="https://chatgpt.com/codex/settings/usage"
    )


def _rpc(argv: list[str], calls: list[tuple[str, Any]], timeout: float) -> Any:
    """Run a newline-delimited JSON-RPC server, call `calls` in order (each
    waits for its answer; `initialized` follows `initialize`) and return the
    last call's result. Raises OSError/ValueError/TimeoutError."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_env_for(argv[0]),
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        read = 0
        with contextlib.suppress(OSError, ValueError):  # closed under us: we're done
            while read <= MAX_OUTPUT:  # past that, it isn't the answer
                line = proc.stdout.readline(MAX_OUTPUT)
                if not line:
                    break
                read += len(line)
                lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = datetime.now() + timedelta(seconds=timeout)
    try:
        assert proc.stdin is not None
        result: Any = None
        for n, (method, params) in enumerate(calls, start=1):
            message = {"jsonrpc": "2.0", "id": n, "method": method, "params": params}
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            result = _await(lines, n, deadline)
            if method == "initialize":
                proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
                proc.stdin.flush()
        return result
    finally:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()


def _await(lines: queue.Queue[str | None], wanted: int, deadline: datetime) -> Any:
    while True:
        left = (deadline - datetime.now()).total_seconds()
        if left <= 0:
            raise TimeoutError("no answer in time")
        try:
            line = lines.get(timeout=left)
        except queue.Empty:
            raise TimeoutError("no answer in time") from None
        if line is None:
            raise OSError("the CLI exited")
        try:
            message = json.loads(line)
        except ValueError:
            continue  # a log line, not a message
        if not isinstance(message, dict) or message.get("id") != wanted:
            continue  # a notification, or an answer to something else
        if "error" in message:
            raise ValueError(str(_dict(message["error"]).get("message") or message["error"]))
        return message.get("result")


def _codex(_cfg: cfg_mod.Config) -> list[Usage]:
    exe = find_cli("codex")
    if exe is None:
        return []
    calls = [
        ("initialize", {"clientInfo": {"name": "awaitray", "version": "1"}}),
        ("account/rateLimits/read", None),
    ]
    try:
        result = _rpc([exe, "-s", "read-only", "-a", "never", "app-server"], calls, CLI_TIMEOUT)
    except (OSError, ValueError, TimeoutError) as e:
        log_mod.info(f"codex usage unavailable: {e}")
        return []
    usage = parse_codex(result)
    if usage is None:
        log_mod.info("codex usage: no rate limits in the answer (logged in to Codex?)")
    return [usage] if usage else []


# ----- Antigravity (agy -p /usage) -----

_MIN_AGY = (1, 1, 11)


def parse_agy_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _fraction(bucket: dict[str, Any]) -> float | None:
    return _number(bucket.get("remaining_fraction", bucket.get("remainingFraction")))


def parse_antigravity(payload: Any) -> Usage | None:
    """`agy -p /usage --output-format json` → Usage: per model group, the
    bucket with the least left."""
    report = _dict(payload)
    command = _dict(report.get("command"))
    if report.get("status") != "SUCCESS" or command.get("name") != "usage":
        return None
    windows = []
    for group in _dict(command.get("data")).get("groups") or []:
        group = _dict(group)
        buckets = [
            b
            for b in group.get("buckets") or []
            if isinstance(b, dict) and not b.get("disabled") and _fraction(b) is not None
        ]
        if not buckets:
            continue
        tight = min(buckets, key=lambda b: _fraction(b) or 0.0)
        name = str(group.get("name") or group.get("displayName") or "").removesuffix(" Models")
        windows.append(
            Window(
                name,
                percent_left=(_fraction(tight) or 0.0) * 100,
                reset=_when(tight.get("reset_time", tight.get("resetTime"))),
            )
        )
    return Usage("antigravity", "Antigravity", windows=tuple(windows)) if windows else None


def _antigravity(_cfg: cfg_mod.Config) -> list[Usage]:
    exe = find_cli("agy")
    if exe is None:
        return []
    try:
        version = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10, env=_env_for(exe)
        )
        if (parse_agy_version(version.stdout) or (0, 0, 0)) < _MIN_AGY:
            log_mod.info("antigravity usage needs agy 1.1.11 or later")
            return []
        res = subprocess.run(
            [exe, "-p", "/usage", "--output-format", "json", "--print-timeout", f"{CLI_TIMEOUT}s"],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT + 10,
            env=_env_for(exe),
        )
        if len(res.stdout) > MAX_OUTPUT:
            raise ValueError("answer too large")
        usage = parse_antigravity(json.loads(res.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log_mod.info(f"antigravity usage unavailable: {e}")
        return []
    if usage is None:
        log_mod.info("antigravity usage: no quota in the report (logged in to agy?)")
    return [usage] if usage else []


# ----- The registry -----


@dataclass(frozen=True)
class Provider:
    id: str  # also the `cfg.ai_usage` field
    label: str
    help: str
    fetch: Callable[[cfg_mod.Config], list[Usage]]


PROVIDERS = (
    Provider(
        "copilot",
        "GitHub Copilot",
        "Plan, how much is left and when it resets, for each github.com account. Read "
        "through gh from an unofficial GitHub endpoint: it may stop working, and then the "
        "line just goes away.",
        _copilot,
    ),
    Provider(
        "codex",
        "OpenAI Codex",
        "The 5-hour and weekly limits of your ChatGPT plan, asked to the codex CLI "
        "(its own login; it spends no quota). Needs codex installed and logged in.",
        _codex,
    ),
    Provider(
        "antigravity",
        "Google Antigravity",
        "What's left per model group, asked to the agy CLI (1.1.11 or later, its own "
        "login). Needs agy installed and logged in.",
        _antigravity,
    ),
)


def fetch(cfg: cfg_mod.Config) -> list[Usage]:
    """Every enabled provider, in PROVIDERS order. Blocking: call it from a
    worker thread. A provider that fails is left out, never the others."""
    out: list[Usage] = []
    for provider in PROVIDERS:
        if not getattr(cfg.ai_usage, provider.id, False):
            continue
        try:
            out.extend(provider.fetch(cfg))
        except Exception as e:  # a provider's bug must not hide the rest
            log_mod.warn(f"{provider.id} usage failed: {e}")
    return out
