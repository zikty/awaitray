"""Config persistence for awaitray.

Stored as JSON at ~/.config/awaitray/config.json. Schema is intentionally
additive — unknown keys are preserved on save so older versions don't clobber
settings written by newer ones.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import MISSING, Field, asdict, dataclass, field, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from . import log as log_mod

_T = TypeVar("_T")


def xdg_dir(var: str, fallback: str) -> Path:
    """An XDG base directory. Unset, empty or relative means the default
    (~/<fallback>), as the spec says: an empty XDG_CONFIG_HOME must never
    turn into a path relative to wherever the app was started."""
    value = os.environ.get(var, "")
    return Path(value) if os.path.isabs(value) else Path.home() / fallback


CONFIG_DIR = xdg_dir("XDG_CONFIG_HOME", ".config") / "awaitray"
CONFIG_PATH = CONFIG_DIR / "config.json"
# Pre-rename location (the project was called "devtray"). Migrated on first load.
LEGACY_CONFIG_DIR = CONFIG_DIR.parent / "devtray"
# Its cache (tray icons): only deleted, nothing in it is worth keeping.
LEGACY_CACHE_DIR = xdg_dir("XDG_CACHE_HOME", ".cache") / "devtray"
_LEGACY_CACHE_ENTRIES = {"icons", "state.json"}


# ----- Hosts / Repos -----


@dataclass
class HostConfig:
    hostname: str
    enabled: bool = True
    # A gh account on this host. Empty: whichever account is active in
    # gh. The same hostname may appear once per account.
    user: str = ""
    # Browser for this account's PRs; "" = the global one. See browser.py.
    browser: str = ""

    @property
    def account_id(self) -> str:
        return account_id(self.hostname, self.user)


_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?")
# GitHub logins, plus the `_shortcode` of managed users and GHES's dots.
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def valid_hostname(host: object) -> bool:
    """A host (optionally with a port) as gh takes it, nothing else: it ends
    up on command lines and, on macOS, in a Terminal script."""
    return isinstance(host, str) and _HOSTNAME.fullmatch(host) is not None


def valid_user(user: object) -> bool:
    """A login, or "" for the host's active account."""
    return isinstance(user, str) and (user == "" or _USER.fullmatch(user) is not None)


def account_id(hostname: str, user: str = "") -> str:
    """`user@hostname`, or just `hostname` for the host's active account."""
    return f"{user}@{hostname}" if user else hostname


def split_account(aid: str) -> tuple[str, str]:
    """`user@hostname` → (user, hostname); a bare hostname → ("", hostname)."""
    user, sep, host = aid.rpartition("@")
    return (user, host) if sep else ("", aid)


@dataclass
class RepoConfig:
    path: str
    enabled: bool = True
    remote: str = "origin"
    auto_discover: bool = False  # treat `path` as a parent dir; expand to subdirs with .git
    auto_update: bool = False  # fast-forward to upstream on fetch when the worktree is clean
    excluded: list[str] = field(
        default_factory=list
    )  # discovered child paths to skip (auto_discover only)


@dataclass
class CustomSection:
    """A menu section from a saved GitHub search."""

    title: str
    query: str  # search qualifiers, e.g. "mentions:@me" (is:pr is:open is implied)
    host: str = ""  # "" = every enabled host
    enabled: bool = True
    notify: bool = False  # notify when a PR appears in it
    count: bool = False  # add its PRs to the menu-bar number

    @property
    def sid(self) -> str:
        """Identity for results and baselines: a new query starts fresh."""
        return f"{self.host}|{self.query.strip()}"


# ----- Intervals -----


@dataclass
class Intervals:
    pr_check_minutes: int = 5
    fetch_minutes: int = 15


# ----- Error policy -----

ERROR_ACTIONS = ("wait", "retry", "pause")


@dataclass
class ErrorPolicy:
    """How to react when *every* enabled GitHub host fails."""

    on_error: str = "wait"
    max_retries: int = 3
    retry_backoff_seconds: int = 30
    pause_after_failures: int = 3

    def normalized(self) -> ErrorPolicy:
        action = self.on_error if self.on_error in ERROR_ACTIONS else "wait"
        return ErrorPolicy(
            on_error=action,
            max_retries=max(1, min(int(self.max_retries), 10)),
            retry_backoff_seconds=max(5, min(int(self.retry_backoff_seconds), 3600)),
            pause_after_failures=max(1, min(int(self.pause_after_failures), 50)),
        )


# ----- Filters -----

DEFAULT_IGNORED_AUTHORS = [
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
]


@dataclass
class Filters:
    """Glob (fnmatch) patterns; matches are excluded from the menu and notifications."""

    ignored_authors: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORED_AUTHORS))
    ignored_repos: list[str] = field(default_factory=list)  # e.g. "*/legacy-*"
    include_drafts: bool = True


# ----- My PRs (second section) -----


@dataclass
class MyPRsConfig:
    enabled: bool = False
    include_drafts: bool = True


# ----- Local repos (section) -----


@dataclass
class LocalReposConfig:
    """The "Local repos" menu section: your local clones with fetch status,
    cross-referenced with the review requests that land on them."""

    enabled: bool = False
    only_with_review: bool = (
        False  # "Local repos · to review": only clones with a pending review request
    )
    show_clean: bool = True  # include clones with no ahead/behind (ignored when only_with_review)
    count_in_badge: bool = (
        False  # add behind/ahead clones (without a review) to the menu-bar number
    )


# ----- Quiet hours -----


@dataclass
class QuietHours:
    """Silences notifications during the configured window. Timers keep running."""

    enabled: bool = False
    start: str = "20:00"  # HH:MM 24h
    end: str = "08:00"  # may wrap past midnight
    quiet_weekends: bool = False


# ----- Editor (open repo locally) -----


@dataclass
class EditorConfig:
    enabled: bool = False
    command: str = "code"  # invoked as `command <path>`


@dataclass
class BrowserConfig:
    """Browser for links of accounts without their own; "" = the
    system browser. See browser.py for the format."""

    command: str = ""


# ----- Visual toggles -----


@dataclass
class FeatureToggles:
    show_ci_status: bool = True  # CI/review glyphs; data comes with the search
    show_behind_ahead: bool = True  # cheap; computed during fetch
    mark_local_review_requests: bool = True  # 📂 marker on review requests that have a local clone


@dataclass
class AiUsageConfig:
    """The AI assistants whose quota left the menu shows (ai_usage.PROVIDERS)."""

    copilot: bool = True
    codex: bool = True
    antigravity: bool = True


@dataclass
class AccountInfoConfig:
    """Extra lines under the menu's status line, per account (account_info.py)."""

    inbox: bool = True  # unread GitHub notifications
    github_status: bool = True  # githubstatus.com incidents (github_status.py)


@dataclass
class SoundsConfig:
    """A sound with each notification, from a pack (sounds.py)."""

    enabled: bool = False
    pack: str = "retro"  # a built-in pack id or a folder in ~/.config/awaitray/sounds
    # Your PRs' events with a sound of their own (sounds.EVENTS → a pack slot,
    # "none", or an audio file); an event not listed uses the pack.
    events: dict[str, str] = field(default_factory=dict)


# ----- Tray icon -----

# Sound played for a bucket. The first bucket whose `up_to` the count fits in
# wins (catch-all = up_to None). `sound` is a system sound name (macOS:
# /System/Library/Sounds), an absolute path to an audio file, or "" for silent.
SOUND_TRIGGERS = ("transition", "every")


@dataclass
class Bucket:
    up_to: int | None = None  # inclusive max count; None = catch-all (no upper bound)
    emoji: str = ""  # menu-bar glyph (macOS); unused by the Linux SVG icon
    sound: str = ""  # system sound name, absolute file path, or "" for silent


# The first bucket whose threshold the count fits in wins; "*"/None is catch-all.
DEFAULT_ICON_BUCKETS = [
    Bucket(up_to=0, emoji="✅"),
    Bucket(up_to=5, emoji="🔄"),
    Bucket(up_to=15, emoji="⚠️"),
    Bucket(up_to=None, emoji="🚨"),
]


def _copy_default_buckets() -> list[Bucket]:
    return [Bucket(up_to=b.up_to, emoji=b.emoji, sound=b.sound) for b in DEFAULT_ICON_BUCKETS]


@dataclass
class TrayIcon:
    """Menu-bar glyph (and per-bucket sound) selection by pending-PR count."""

    enabled: bool = True  # use the emoji buckets; off → plain count glyph
    show_count: bool = True  # append the number next to the emoji
    buckets: list[Bucket] = field(default_factory=_copy_default_buckets)
    sound_trigger: str = "transition"  # "transition" (on tier change) | "every" (each new PR)
    count_team_requests: bool = False  # also count review requests sent to your teams
    unseen_dot: bool = True  # dot on the icon for PRs not seen yet; opening the menu clears it


# ----- Priorities -----


@dataclass
class Priorities:
    """Globs (fnmatch) matched against `pr.repo` (owner/name).

    When at least one visible review-requested PR matches a high-priority
    glob, the tray icon turns red.
    """

    high_priority_repos: list[str] = field(default_factory=list)


# ----- Waiting time -----


@dataclass
class Waiting:
    """How long review requests have been waiting. 0 turns a threshold off."""

    long_wait_days: int = 2  # requested this long ago: ⏳ marker
    long_wait_alert: bool = True  # long waits turn the icon red, like priorities
    stale_days: int = 30  # no update this long: dimmed
    count_stale: bool = False  # stale PRs still count in the menu-bar number


# ----- Reminders and daily digest -----


@dataclass
class Reminders:
    """Remind about reviews past the long-wait threshold."""

    enabled: bool = True
    cooldown_hours: int = 24  # at most once per this, per PR


@dataclass
class DigestConfig:
    """One summary notification a day."""

    enabled: bool = False
    time: str = "09:00"  # HH:MM, local
    weekdays_only: bool = True


# ----- Colors -----


@dataclass
class Colors:
    """Hex color overrides applied to menu items by PR state.

    Empty string means "no override" (use the system theme color). Use #RRGGBB
    form: the macOS backend parses it into an NSColor (text + a color dot), the
    Linux backend into a swatch pixbuf.
    """

    my_prs: str = "#3DB371"  # green tint for the "My open PRs" section
    to_review: str = "#5B9BD5"  # neutral blue default for the "Review requests" section
    local_repos: str = "#9B59B6"  # purple tint for the "Local repos" section
    draft: str = "#7F8C8D"  # muted gray for drafts
    approved: str = "#3DB371"  # green for approved PRs
    changes_requested: str = "#E74C3C"  # red for PRs with changes requested
    ci_failure: str = "#E67E22"  # orange for failing CI
    high_priority: str = ""  # off by default; icon already turns red


# ----- Notifications about your own PRs -----


@dataclass
class MyPrEvents:
    """Which state changes of your PRs notify (needs "Show your open PRs")."""

    approved: bool = True
    changes_requested: bool = True
    ci_failed: bool = True
    ci_passed: bool = True  # back to green after a failure
    ready_to_merge: bool = True
    conflict: bool = True
    new_comments: bool = False  # noisy: your own comments count too


# ----- Snooze -----


@dataclass
class SnoozeEntry:
    until_iso: str | None = None  # ISO 8601 datetime; None means until next commit
    head_oid: str | None = None  # head ref oid at snooze time (for "until next commit")
    updated_at: str | None = None  # PR updatedAt at snooze time (for "until new activity")
    ignored: bool = False  # hidden for good until unsnoozed by hand


# ----- Top-level Config -----


@dataclass
class Config:
    hosts: list[HostConfig] = field(default_factory=lambda: [HostConfig("github.com")])
    repos: list[RepoConfig] = field(default_factory=list)
    sections: list[CustomSection] = field(default_factory=list)
    clone_root: str = ""  # where "Clone here" drops new clones; empty → first auto-discover parent
    # "owner/repo = path" lines mapping PRs to local folders; globs work
    # on the left, and a * on the right stands for the repo name.
    repo_paths: list[str] = field(default_factory=list)
    intervals: Intervals = field(default_factory=Intervals)
    error_policy: ErrorPolicy = field(default_factory=ErrorPolicy)
    filters: Filters = field(default_factory=Filters)
    my_prs: MyPRsConfig = field(default_factory=MyPRsConfig)
    local_repos: LocalReposConfig = field(default_factory=LocalReposConfig)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    editor: EditorConfig = field(default_factory=EditorConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    sounds: SoundsConfig = field(default_factory=SoundsConfig)
    account_info: AccountInfoConfig = field(default_factory=AccountInfoConfig)
    ai_usage: AiUsageConfig = field(default_factory=AiUsageConfig)
    features: FeatureToggles = field(default_factory=FeatureToggles)
    tray_icon: TrayIcon = field(default_factory=TrayIcon)
    priorities: Priorities = field(default_factory=Priorities)
    waiting: Waiting = field(default_factory=Waiting)
    reminders: Reminders = field(default_factory=Reminders)
    digest: DigestConfig = field(default_factory=DigestConfig)
    colors: Colors = field(default_factory=Colors)
    snoozed: dict[str, SnoozeEntry] = field(default_factory=dict)
    notify_on_new_pr: bool = True  # review requests addressed to you
    notify_team_requests: bool = False  # review requests addressed to one of your teams
    notify_local_repos: bool = True  # local repo behind/fast-forward notifications
    notify_my_prs: bool = False  # new PRs authored by you
    my_pr_events: MyPrEvents = field(default_factory=MyPrEvents)
    notify_on_fetch_error: bool = True
    paused: bool = False
    _extra: dict[str, Any] = field(default_factory=dict)


# ----- Persistence -----


def _default(f: Field[Any]) -> Any:
    return f.default_factory() if f.default_factory is not MISSING else f.default


# Everything below is derived from the dataclasses: adding a field to `Config`
# (or to one of its sections) is enough for it to load, save and round-trip.
_FIELDS: dict[str, Field[Any]] = {f.name: f for f in fields(Config) if not f.name.startswith("_")}
_KNOWN_KEYS = frozenset(_FIELDS)
# Sections stored as a JSON object built from a dataclass: unknown keys inside
# them survive a save.
_DICT_SECTIONS = tuple(name for name, f in _FIELDS.items() if is_dataclass(_default(f)))
# List sections and what identifies an entry across saves.
_LIST_SECTIONS: dict[str, Any] = {
    "hosts": lambda e: (e.get("hostname"), e.get("user") or ""),
    "repos": lambda e: _expand(e.get("path")),
    "sections": lambda e: e.get("title"),
}


def _load_entries(cls: type[_T], items: Any, section: str) -> list[_T]:
    """Build a list of dataclasses tolerantly.

    Unknown keys are ignored (``save`` keeps them on disk), and entries that
    aren't objects or lack a required field are skipped with a warning instead
    of crashing startup. A newer version may add fields to an entry, and a
    downgrade or an imported backup must still load.
    """
    if not isinstance(items, list):
        return []
    known: dict[str, Field[Any]] = getattr(cls, "__dataclass_fields__")  # noqa: B009
    required = {
        name for name, f in known.items() if f.default is MISSING and f.default_factory is MISSING
    }
    out = []
    for item in items:
        if not isinstance(item, dict) or not required <= item.keys():
            log_mod.warn(f"config: skipping invalid {section} entry: {item!r}")
            continue
        out.append(cls(**{k: v for k, v in item.items() if k in known}))
    return out


def _load_dict(cls: type[_T], raw: Any) -> _T:
    """Build a dataclass from a dict, ignoring unknown keys to keep load forward-compatible."""
    if not isinstance(raw, dict):
        return cls()
    fields_set = set(getattr(cls, "__dataclass_fields__"))  # noqa: B009
    return cls(**{k: v for k, v in raw.items() if k in fields_set})


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_buckets(raw: Any) -> list[Bucket]:
    """Parse the `buckets` list, accepting both the legacy "N: glyph" / "*: glyph"
    strings and the structured {up_to, emoji, sound} dicts. Malformed entries are
    dropped; an empty/invalid result falls back to the defaults."""
    if not isinstance(raw, list):
        return _copy_default_buckets()
    out: list[Bucket] = []
    for item in raw:
        if isinstance(item, str):
            if ":" not in item:
                continue
            key, glyph = (s.strip() for s in item.split(":", 1))
            if not glyph:
                continue
            if key == "*":
                out.append(Bucket(up_to=None, emoji=glyph))
            else:
                n = _safe_int(key)
                if n is not None:
                    out.append(Bucket(up_to=n, emoji=glyph))
        elif isinstance(item, dict):
            up = item.get("up_to", None)
            up_to = None if up in (None, "*", "") else _safe_int(up)
            out.append(
                Bucket(
                    up_to=up_to,
                    emoji=str(item.get("emoji", "")),
                    sound=str(item.get("sound", "")),
                )
            )
    return out or _copy_default_buckets()


def _load_tray_icon(raw: Any) -> TrayIcon:
    if not isinstance(raw, dict):
        return TrayIcon()
    # Plain fields load like any section (so new ones need no code here);
    # only the buckets and the trigger need parsing.
    icon = _load_dict(TrayIcon, {k: v for k, v in raw.items() if k != "buckets"})
    icon.buckets = _parse_buckets(raw.get("buckets"))
    if icon.sound_trigger not in SOUND_TRIGGERS:
        icon.sound_trigger = "transition"
    return icon


def migrate_legacy_dir() -> None:
    """Move the pre-rename config dir into place so users keep their settings.

    Only runs when the new dir doesn't exist yet, so it never overwrites a
    config written by this version. Best-effort: on failure we fall back to
    defaults rather than crash at startup.
    """
    if CONFIG_DIR.exists() or not LEGACY_CONFIG_DIR.is_dir():
        return
    try:
        CONFIG_DIR.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_CONFIG_DIR.rename(CONFIG_DIR)
    except OSError:
        pass


def drop_legacy_cache() -> None:
    """Delete the pre-rename cache dir, but only when it holds nothing but
    what devtray wrote there (the tray icons): never someone else's files.
    Run once when the app starts, not from `load()`: the CLI and the tests
    load configs too, and must never touch a real cache."""
    try:
        entries = {p.name for p in LEGACY_CACHE_DIR.iterdir()}
    except OSError:
        return  # not there (the usual case) or unreadable
    if entries <= _LEGACY_CACHE_ENTRIES:
        shutil.rmtree(LEGACY_CACHE_DIR, ignore_errors=True)


def _keep_team_counting(raw: dict[str, Any], cfg: Config) -> None:
    """Configs from before the direct/team split (e.g. devtray's) counted every
    review request, team ones included. Every save writes the key, so a file
    without it predates the split: keep counting team requests there instead
    of silently dropping them from the icon."""
    tray = raw.get("tray_icon")
    if not isinstance(tray, dict) or "count_team_requests" not in tray:
        cfg.tray_icon.count_team_requests = True


def _move_copilot_toggle(raw: dict[str, Any], cfg: Config) -> None:
    """Copilot's switch moved from `account_info.copilot` to `ai_usage`:
    a config from before keeps its choice."""
    old = raw.get("account_info")
    if "ai_usage" not in raw and isinstance(old, dict) and old.get("copilot") is False:
        cfg.ai_usage.copilot = False


def load() -> Config:
    global created_fresh
    migrate_legacy_dir()
    if not CONFIG_PATH.exists():
        created_fresh = True  # first run: the UI may guide the user through setup
        cfg = Config()
        save(cfg)
        return cfg

    try:
        raw = json.loads(CONFIG_PATH.read_text())
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
        cfg = _build(raw)
        _keep_team_counting(raw, cfg)
        _move_copilot_toggle(raw, cfg)
        _remember_mtime()
        return cfg
    except (ValueError, TypeError, AttributeError, OSError) as e:
        # Never let the next save() overwrite a file we couldn't read: it may be
        # recoverable by hand. Move it aside and start from defaults.
        _quarantine(e)
        return Config()


# Set when load() had to quarantine an unreadable file; the controller surfaces
# it to the user once at startup.
last_load_warning: str | None = None

# True when load() found no config file and created the defaults (first run).
created_fresh: bool = False


def _quarantine(error: Exception) -> None:
    global last_load_warning
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupt-{stamp}")
    try:
        CONFIG_PATH.replace(target)
        kept = f"a copy was kept at {target}"
    except OSError as move_error:
        kept = f"it could not be moved aside ({move_error})"
    last_load_warning = f"Settings file was unreadable ({error}); defaults loaded, {kept}."
    log_mod.error(f"config: {last_load_warning}")


def _expand(path: Any) -> Any:
    """Accept ``~`` in path fields (hand edits, imported backups). Saved back as
    absolute paths, which every version understands."""
    return os.path.expanduser(path) if isinstance(path, str) and path else path


def _load_snoozed(raw: Any) -> dict[str, SnoozeEntry]:
    if not isinstance(raw, dict):
        return {}
    return {k: _load_dict(SnoozeEntry, v) for k, v in raw.items() if isinstance(v, dict)}


def _load_hosts(raw: Any) -> list[HostConfig]:
    """Hosts and logins reach `gh` command lines: anything that isn't one
    (a hand-edited file, an imported backup) is left out."""
    hosts = []
    for h in _load_entries(HostConfig, raw, "hosts"):
        if valid_hostname(h.hostname) and valid_user(h.user):
            hosts.append(h)
        else:
            log_mod.warn(f"config: skipping host {h.hostname!r} (user {h.user!r}): not valid")
    return hosts


def _load_repos(raw: Any) -> list[RepoConfig]:
    repos = _load_entries(RepoConfig, raw, "repos")
    for r in repos:
        r.path = _expand(r.path)
        r.excluded = [_expand(p) for p in r.excluded] if isinstance(r.excluded, list) else []
    return repos


# Fields that need more than the generic loaders below.
_LOADERS: dict[str, Any] = {
    "hosts": lambda raw: _load_hosts(raw),
    "sections": lambda raw: _load_entries(CustomSection, raw, "sections"),
    "repos": _load_repos,
    "clone_root": _expand,
    "error_policy": lambda raw: _load_dict(ErrorPolicy, raw).normalized(),
    "tray_icon": _load_tray_icon,
    "snoozed": _load_snoozed,
}


def _build(raw: dict[str, Any]) -> Config:
    values: dict[str, Any] = {}
    for name, f in _FIELDS.items():
        if name not in raw:
            continue  # the dataclass default applies
        if name in _LOADERS:
            values[name] = _LOADERS[name](raw[name])
        elif name in _DICT_SECTIONS:
            values[name] = _load_dict(type(_default(f)), raw[name])
        else:
            values[name] = raw[name]
    extra = {k: v for k, v in raw.items() if k not in _KNOWN_KEYS}
    return Config(**values, _extra=extra)


def _dump(cfg: Config, name: str) -> Any:
    value = getattr(cfg, name)
    if name == "error_policy":
        value = value.normalized()
    return _plain(value)


def _plain(value: Any) -> Any:
    """Dataclass instances (also inside lists and dicts) as plain JSON data."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {name: _dump(cfg, name) for name in _FIELDS}
    payload.update(cfg._extra)
    _keep_unknown_from_disk(payload)
    from . import backup  # local import: backup depends on this module

    if _changed_on_disk():
        # Someone else wrote the file since we read it (an import, a hand edit,
        # another instance). Keep their version before we overwrite it.
        kept = backup.snapshot("before-overwrite")
        log_mod.warn(f"config: {CONFIG_PATH} changed on disk; previous version kept at {kept}")
    backup.snapshot_daily()  # keep yesterday's settings before today's first change
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(CONFIG_PATH)
    _remember_mtime()


def _keep_unknown_from_disk(payload: dict[str, Any]) -> None:
    """Carry keys this version doesn't know from the current file into payload.

    Every save path (Preferences builds entries from scratch) goes through here,
    so fields written by a newer version survive a round trip through an older
    one. Only *unknown* keys are added: known ones always come from payload, and
    removed entries (a deleted host, an unsnoozed PR) stay removed.
    """
    try:
        on_disk = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(on_disk, dict):
        return

    for key, value in on_disk.items():
        if key not in payload:
            payload[key] = value  # unknown top-level key

    def merge(target: Any, source: Any) -> None:
        if isinstance(target, dict) and isinstance(source, dict):
            for k, v in source.items():
                target.setdefault(k, v)

    for section in _DICT_SECTIONS:
        merge(payload.get(section), on_disk.get(section))

    for section, ident in _LIST_SECTIONS.items():
        old_entries = on_disk.get(section)
        if not isinstance(old_entries, list):
            continue
        by_id = {ident(e): e for e in old_entries if isinstance(e, dict)}
        for entry in payload.get(section) or []:
            merge(entry, by_id.get(ident(entry)))

    old_snoozed = on_disk.get("snoozed")
    if isinstance(old_snoozed, dict):
        for pr_key, entry in (payload.get("snoozed") or {}).items():
            merge(entry, old_snoozed.get(pr_key))


# mtime of the config file as this process last read or wrote it, per path.
_known_mtime: dict[str, int | None] = {}


def _mtime() -> int | None:
    try:
        return CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        return None


def _remember_mtime() -> None:
    _known_mtime[str(CONFIG_PATH)] = _mtime()


def _changed_on_disk() -> bool:
    known = _known_mtime.get(str(CONFIG_PATH))
    return known is not None and _mtime() not in (None, known)
