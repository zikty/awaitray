"""Toolkit-agnostic model of the Preferences window (docs/ui-spec.md).

The six sections, their groups and fields, validation, and how a value is
read from / written to a `Config`. Both backends render simple fields
(bool, int, text, time, choice, lines, color) generically from this model
and provide their own widgets only for the `CUSTOM` editors (hosts, repos,
tray-icon buckets, backup, logs).

Editing works on a *working copy* of the config: widgets mutate it through
`set_value`, and the backend commits it (save + `Controller.on_prefs_saved`)
after a short debounce. Nothing rebuilds a `Config` from widgets, so fields
a window doesn't show (a repo's exclusions, snoozes, keys from newer
versions) are never lost.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import account_info, ai_usage, sounds
from . import config as cfg_mod

# Field kinds
BOOL = "bool"
INT = "int"
TEXT = "text"
TIME = "time"  # "HH:MM"
CHOICE = "choice"
LINES = "lines"  # list[str], one per line
COLOR = "color"  # "" (off) or "#RRGGBB"
BROWSER = "browser"  # a browser command; picked from browsers.installed() or typed
SOUND = "sound"  # "" (the pack's), a pack slot, "none", or an audio file's path
CUSTOM = "custom"  # backend-specific editor, see `custom`

# Custom editors
HOSTS = "hosts"
REPOS = "repos"
BUCKETS = "buckets"
BACKUP = "backup"
LOGS = "logs"
SECTIONS_EDITOR = "sections"
SOUNDS_FOLDER = "sounds_folder"  # a button that opens ~/.config/awaitray/sounds

# Run-time choice lists (Field.choices_from)
SOUND_PACKS = "sound_packs"

COMMIT_DELAY_MS = 600  # debounce between the last edit and applying it

# Config fields changed from the menu, not from Preferences. On commit they're
# taken from the live config, so an open window never reverts a snooze or a
# pause made meanwhile.
LIVE_KEYS = ("snoozed", "paused")


@dataclass(frozen=True)
class Field:
    key: str  # dotted path into Config, e.g. "intervals.fetch_minutes"
    label: str
    kind: str
    help: str = ""
    minimum: int | None = None
    maximum: int | None = None
    step: int = 1
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    placeholder: str = ""
    depends_on: str = ""  # key of a BOOL that must be on for this to matter
    custom: str = ""  # editor id when kind == CUSTOM
    # CHOICE whose options are found at run time (see choices_for), e.g. the
    # sound packs installed; `choices` is then empty.
    choices_from: str = ""


@dataclass(frozen=True)
class Group:
    title: str
    fields: tuple[Field, ...]
    help: str = ""


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    icon: str  # SF Symbol name (macOS); Linux maps it to a themed icon
    groups: tuple[Group, ...] = field(default_factory=tuple)


def _color(key: str, label: str, help: str = "") -> Field:
    return Field(f"colors.{key}", label, COLOR, help=help)


SECTIONS: tuple[Section, ...] = (
    Section(
        "accounts",
        "Accounts",
        "person.crop.circle",
        (
            Group(
                "GitHub hosts",
                (Field("hosts", "Hosts", CUSTOM, custom=HOSTS),),
                help="Sign-in is handled by the gh CLI; awaitray never stores tokens.",
            ),
            Group(
                "In the menu",
                (
                    Field(
                        "account_info.inbox",
                        "Unread GitHub notifications",
                        BOOL,
                        help="Each unread thread can be opened, marked as read or done, or "
                        "unsubscribed from the menu.",
                    ),
                    Field(
                        "account_info.github_status",
                        "GitHub incidents",
                        BOOL,
                        help="A line while githubstatus.com reports an incident or Git, "
                        "the API, pull requests, Actions or Copilot degraded, and a "
                        "notification for major ones. Checked with every PR poll.",
                    ),
                ),
                help=f"The inbox is checked every {account_info.ACCOUNT_MINUTES} minutes, "
                "per account.",
            ),
            Group(
                "AI assistants",
                tuple(
                    Field(f"ai_usage.{p.id}", p.label, BOOL, help=p.help)
                    for p in ai_usage.PROVIDERS
                ),
                help="How much of each one's quota is left, right under the menu's status "
                f"line, checked every {account_info.ACCOUNT_MINUTES} minutes; a "
                f"notification when {ai_usage.LOW_PERCENT}% or less is left. Each asks its "
                "own CLI with its own login: awaitray never reads their tokens.",
            ),
            Group(
                "Browser",
                (
                    Field(
                        "browser.command",
                        "Open links with",
                        BROWSER,
                        placeholder="System default",
                        help=(
                            "For accounts without a browser of their own. Pick one (each "
                            "profile is listed) or type a command."
                        ),
                    ),
                ),
            ),
        ),
    ),
    Section(
        "pull_requests",
        "Pull requests",
        "arrow.triangle.pull",
        (
            Group(
                "Your pull requests",
                (
                    Field(
                        "my_prs.enabled",
                        "Show your open PRs",
                        BOOL,
                        help=(
                            "Adds Needs your action, Ready to merge, Waiting for review and Drafts."
                        ),
                    ),
                    Field(
                        "my_prs.include_drafts",
                        "Include your drafts",
                        BOOL,
                        depends_on="my_prs.enabled",
                    ),
                ),
            ),
            Group(
                "Filters",
                (
                    Field(
                        "filters.ignored_authors",
                        "Ignore authors",
                        LINES,
                        help="One per line. Exact names or globs: dependabot[bot], *-bot.",
                    ),
                    Field(
                        "filters.ignored_repos",
                        "Ignore repositories",
                        LINES,
                        help="One per line, as owner/repo. Globs work: acme/legacy-*.",
                    ),
                    Field("filters.include_drafts", "Include draft review requests", BOOL),
                ),
            ),
            Group(
                "Custom sections",
                (Field("sections", "Sections", CUSTOM, custom=SECTIONS_EDITOR),),
                help=(
                    "Menu sections from your own GitHub searches, e.g. mentions:@me or "
                    "repo:acme/core label:urgent. Open PRs only."
                ),
            ),
            Group(
                "Waiting time",
                (
                    Field(
                        "waiting.long_wait_days",
                        "Flag reviews waiting for (days)",
                        INT,
                        minimum=0,
                        maximum=60,
                        help="Marks them with ⏳. 0 turns it off.",
                    ),
                    Field(
                        "waiting.long_wait_alert",
                        "Turn the icon red for them",
                        BOOL,
                        help="Like a high-priority repo.",
                    ),
                    Field(
                        "waiting.stale_days",
                        "Dim PRs with no activity for (days)",
                        INT,
                        minimum=0,
                        maximum=365,
                        help="0 turns it off.",
                    ),
                    Field("waiting.count_stale", "Count dimmed PRs in the menu-bar number", BOOL),
                ),
            ),
            Group(
                "Priorities",
                (
                    Field(
                        "priorities.high_priority_repos",
                        "High-priority repositories",
                        LINES,
                        help="A waiting PR from one of these turns the icon red. Example: acme/*",
                    ),
                ),
            ),
            Group(
                "Rows",
                (
                    Field("features.show_ci_status", "Show CI and review state", BOOL),
                    Field(
                        "features.mark_local_review_requests", "Mark PRs you have cloned (📂)", BOOL
                    ),
                ),
            ),
        ),
    ),
    Section(
        "notifications",
        "Notifications",
        "bell",
        (
            Group(
                "Notify me about",
                (
                    Field("notify_on_new_pr", "Review requests for you", BOOL),
                    Field("notify_team_requests", "Review requests for your teams", BOOL),
                    Field("notify_my_prs", "New PRs of yours", BOOL),
                    Field("notify_local_repos", "Local repos behind or fast-forwarded", BOOL),
                    Field("notify_on_fetch_error", "Git fetch errors", BOOL),
                ),
            ),
            Group(
                "When one of your PRs changes",
                (
                    Field("my_pr_events.approved", "Approved", BOOL, depends_on="my_prs.enabled"),
                    Field(
                        "my_pr_events.ready_to_merge",
                        "Ready to merge",
                        BOOL,
                        depends_on="my_prs.enabled",
                    ),
                    Field(
                        "my_pr_events.changes_requested",
                        "Changes requested",
                        BOOL,
                        depends_on="my_prs.enabled",
                    ),
                    Field(
                        "my_pr_events.ci_failed", "Checks failed", BOOL, depends_on="my_prs.enabled"
                    ),
                    Field(
                        "my_pr_events.ci_passed",
                        "Checks back to green",
                        BOOL,
                        depends_on="my_prs.enabled",
                    ),
                    Field(
                        "my_pr_events.conflict", "Merge conflict", BOOL, depends_on="my_prs.enabled"
                    ),
                    Field(
                        "my_pr_events.new_comments",
                        "New comments or review threads",
                        BOOL,
                        depends_on="my_prs.enabled",
                    ),
                ),
                help=(
                    "Needs “Show your open PRs” (Pull requests). "
                    "Each change notifies once per commit."
                ),
            ),
            Group(
                "Reminders",
                (
                    Field(
                        "reminders.enabled",
                        "Remind me about reviews waiting too long",
                        BOOL,
                        help="Uses the “Flag reviews waiting for” days in Pull requests.",
                    ),
                    Field(
                        "reminders.cooldown_hours",
                        "At most every (hours)",
                        INT,
                        minimum=1,
                        maximum=168,
                        depends_on="reminders.enabled",
                    ),
                    Field("digest.enabled", "Daily summary", BOOL),
                    Field(
                        "digest.time",
                        "At",
                        TIME,
                        placeholder="09:00",
                        depends_on="digest.enabled",
                    ),
                    Field(
                        "digest.weekdays_only",
                        "Weekdays only",
                        BOOL,
                        depends_on="digest.enabled",
                    ),
                ),
                help="Both wait for quiet hours to end.",
            ),
            Group(
                "Quiet hours",
                (
                    Field(
                        "quiet_hours.enabled",
                        "Mute notifications on a schedule",
                        BOOL,
                        help=(
                            "The icon and menu keep updating; "
                            "only notifications and sounds are muted."
                        ),
                    ),
                    Field(
                        "quiet_hours.start",
                        "From",
                        TIME,
                        placeholder="20:00",
                        depends_on="quiet_hours.enabled",
                    ),
                    Field(
                        "quiet_hours.end",
                        "To",
                        TIME,
                        placeholder="08:00",
                        depends_on="quiet_hours.enabled",
                    ),
                    Field(
                        "quiet_hours.quiet_weekends",
                        "All weekend",
                        BOOL,
                        depends_on="quiet_hours.enabled",
                    ),
                ),
            ),
            Group(
                "Sounds",
                (
                    Field(
                        "sounds.enabled",
                        "Play a sound with notifications",
                        BOOL,
                        help="A different sound for review requests, good news, bad news "
                        "and the rest.",
                    ),
                    Field(
                        "sounds.pack",
                        "Sound pack",
                        CHOICE,
                        choices_from=SOUND_PACKS,
                        depends_on="sounds.enabled",
                        help="Picking one plays it. Your own: a folder in "
                        "~/.config/awaitray/sounds/ with review, good, bad and info "
                        "(.wav, .ogg or .aiff).",
                    ),
                    Field(
                        "tray_icon.sound_trigger",
                        "Play the tier sound",
                        CHOICE,
                        choices=(
                            ("transition", "When the tier changes"),
                            ("every", "On every new review request"),
                        ),
                        help="Sounds are set per tier in Appearance.",
                    ),
                ),
            ),
            Group(
                "Sounds for your PRs",
                tuple(
                    Field(f"sounds.events.{event}", label, SOUND, depends_on="sounds.enabled")
                    for event, label in sounds.EVENTS.items()
                ),
                help=(
                    "An effect (victory, game over…), silent, or any audio file of yours. "
                    "“Pack's own” plays the pack's sound for that change. Picking one "
                    "plays it."
                ),
            ),
            Group(
                "Your sounds",
                (Field("sounds_folder", "Sounds folder", CUSTOM, custom=SOUNDS_FOLDER),),
                help=(
                    "A pack of yours is a folder in here: review, good, bad and info "
                    "files, and optionally one per change (approved.wav, "
                    "new_comments.wav…). .wav, .ogg or .aiff."
                ),
            ),
        ),
    ),
    Section(
        "repositories",
        "Repositories",
        "folder",
        (
            Group(
                "Local repositories",
                (Field("repos", "Repositories", CUSTOM, custom=REPOS),),
                help="A repo, or a folder whose subfolders are git repos (one level deep).",
            ),
            Group(
                "Fetching",
                (
                    Field(
                        "intervals.fetch_minutes",
                        "Fetch every (minutes)",
                        INT,
                        minimum=1,
                        maximum=720,
                    ),
                    Field("features.show_behind_ahead", "Compute ahead/behind after fetch", BOOL),
                ),
            ),
            Group(
                "Menu",
                (
                    Field("local_repos.enabled", "Show the Local repos submenu", BOOL),
                    Field(
                        "local_repos.only_with_review",
                        "Only repos with a review request",
                        BOOL,
                        depends_on="local_repos.enabled",
                    ),
                    Field(
                        "local_repos.show_clean",
                        "Include up-to-date repos",
                        BOOL,
                        depends_on="local_repos.enabled",
                    ),
                    Field(
                        "local_repos.count_in_badge",
                        "Count repos behind/ahead in the icon",
                        BOOL,
                        depends_on="local_repos.enabled",
                    ),
                ),
            ),
            Group(
                "Clone and editor",
                (
                    Field(
                        "clone_root",
                        "Clone new repos into",
                        TEXT,
                        placeholder="~/code",
                        help="Empty: the first folder of repos above.",
                    ),
                    Field(
                        "repo_paths",
                        "Where repos live",
                        LINES,
                        placeholder="acme/* = ~/code/acme/*",
                        help=(
                            "One per line, owner/repo = folder. Globs work on the left and * "
                            "on the right is the repo name. Used to open and clone PRs; "
                            "without a match, a repo folder with the same name is used."
                        ),
                    ),
                    Field("editor.enabled", "Offer Open in editor", BOOL),
                    Field(
                        "editor.command",
                        "Editor command",
                        TEXT,
                        placeholder="code",
                        help="The repo path is passed as the last argument.",
                        depends_on="editor.enabled",
                    ),
                ),
            ),
        ),
    ),
    Section(
        "appearance",
        "Appearance",
        "paintpalette",
        (
            Group(
                "Tray icon",
                (
                    Field("tray_icon.count_team_requests", "Count team review requests", BOOL),
                    Field(
                        "tray_icon.unseen_dot",
                        "Dot for PRs you haven't seen yet",
                        BOOL,
                        help="Opening the menu clears it. Works during quiet hours too.",
                    ),
                    Field("tray_icon.enabled", "Use emoji tiers by count", BOOL),
                    Field(
                        "tray_icon.show_count",
                        "Show the number next to the emoji",
                        BOOL,
                        depends_on="tray_icon.enabled",
                    ),
                    Field(
                        "tray_icon.buckets",
                        "Tiers",
                        CUSTOM,
                        custom=BUCKETS,
                        depends_on="tray_icon.enabled",
                    ),
                ),
            ),
            Group(
                "Colors",
                (
                    _color("to_review", "Review requests"),
                    _color("my_prs", "Your PRs"),
                    _color("local_repos", "Local repos"),
                    _color("draft", "Drafts"),
                    _color("approved", "Approved"),
                    _color("changes_requested", "Changes requested"),
                    _color("ci_failure", "CI failing"),
                    _color("high_priority", "High-priority repos"),
                ),
                help="Off uses the system color. PR state colors win over section colors.",
            ),
        ),
    ),
    Section(
        "advanced",
        "Advanced",
        "gearshape",
        (
            Group(
                "Polling",
                (
                    Field(
                        "intervals.pr_check_minutes",
                        "Check PRs every (minutes)",
                        INT,
                        minimum=1,
                        maximum=240,
                    ),
                ),
            ),
            Group(
                "When every host fails",
                (
                    Field(
                        "error_policy.on_error",
                        "Then",
                        CHOICE,
                        choices=(
                            ("wait", "Wait for the next check"),
                            ("retry", "Retry with backoff"),
                            ("pause", "Pause after repeated failures"),
                        ),
                    ),
                    Field("error_policy.max_retries", "Retries", INT, minimum=1, maximum=10),
                    Field(
                        "error_policy.retry_backoff_seconds",
                        "First retry after (seconds)",
                        INT,
                        minimum=5,
                        maximum=3600,
                        step=5,
                    ),
                    Field(
                        "error_policy.pause_after_failures",
                        "Pause after (failures)",
                        INT,
                        minimum=1,
                        maximum=50,
                    ),
                ),
            ),
            Group(
                "Backup",
                (Field("backup", "Backup", CUSTOM, custom=BACKUP),),
                help="Move your settings to another computer. Snapshots are also taken daily.",
            ),
            Group("Logs", (Field("logs", "Logs", CUSTOM, custom=LOGS),)),
        ),
    ),
)


def all_fields() -> list[Field]:
    return [f for s in SECTIONS for g in s.groups for f in g.fields]


def section(section_id: str) -> Section:
    return next(s for s in SECTIONS if s.id == section_id)


# ----- reading and writing values -----


def get_value(cfg: cfg_mod.Config, key: str) -> Any:
    obj: Any = cfg
    for part in key.split("."):
        # A dict setting (sounds.events) holds one value per key; absent = "".
        obj = obj.get(part, "") if isinstance(obj, dict) else getattr(obj, part)
    return obj


def set_value(cfg: cfg_mod.Config, key: str, value: Any) -> None:
    """Write `value` at `key` into `cfg` (in place: the working copy)."""
    *path, last = key.split(".")
    obj: Any = cfg
    for part in path:
        obj = getattr(obj, part)
    if isinstance(obj, dict):
        # A dict setting: "" is the default, so it isn't stored.
        if value == "":
            obj.pop(last, None)
        else:
            obj[last] = value
        return
    setattr(obj, last, value)


_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def validate(f: Field, value: Any) -> str | None:
    """None when valid, else a short message to show next to the field."""
    if f.kind == INT:
        if not isinstance(value, int) or isinstance(value, bool):
            return "Enter a whole number"
        if f.minimum is not None and value < f.minimum:
            return f"At least {f.minimum}"
        if f.maximum is not None and value > f.maximum:
            return f"At most {f.maximum}"
    elif f.kind == TIME:
        if not isinstance(value, str) or not _TIME.match(value.strip()):
            return "Use HH:MM, e.g. 08:30"
    elif f.kind == COLOR:
        if value and not (isinstance(value, str) and _HEX.match(value)):
            return "Use #RRGGBB"
    elif f.kind == CHOICE:
        if value not in {v for v, _ in choices_for(f)}:
            return "Pick one of the options"
    elif f.kind == LINES and f.key == "repo_paths":
        for line in value or []:
            pattern, sep, path = str(line).partition("=")
            if not (sep and pattern.strip() and path.strip()):
                return f"Use owner/repo = folder: {line}"
    elif f.kind == BROWSER:
        return browser_error(str(value))
    elif f.kind == SOUND:
        allowed = {v for v, _ in sound_choices()}
        path = str(value)
        if path not in allowed and not (
            os.path.isabs(path) and path.lower().endswith(sounds.EXTENSIONS)
        ):
            return "Pick a sound or an audio file (.wav, .ogg, .aiff)"
    elif f.kind == TEXT and f.key == "editor.command":
        if not str(value).strip():
            return "Enter a command, e.g. code"
    return None


def browser_error(command: str) -> str | None:
    """A browser command that can't be run as typed; "" is the default."""
    try:
        shlex.split(command)
    except ValueError:
        return "Close the quotes"
    return None


def choices_for(f: Field) -> tuple[tuple[str, str], ...]:
    """A CHOICE field's (value, label) options, found now if they depend on
    what's installed."""
    if f.choices_from == SOUND_PACKS:
        return tuple((p.id, p.label) for p in sounds.packs())
    return f.choices


def preview_sound(f: Field, value: Any, cfg: cfg_mod.Config) -> str | None:
    """A file the window plays when this field changes to `value` (hearing a
    sound pack or an event's sound as you pick it), or None."""
    if not isinstance(value, str):
        return None
    if f.choices_from == SOUND_PACKS:
        return sounds.path_for(value, sounds.REVIEW)
    if f.kind == SOUND:
        event = f.key.rsplit(".", 1)[-1]
        trial = cfg_mod.SoundsConfig(True, cfg.sounds.pack, {event: value})
        return sounds.resolve(trial, sounds.EVENT_SLOT.get(event, sounds.INFO), event)
    return None


def sound_choices() -> tuple[tuple[str, str], ...]:
    """A SOUND field's choices: the pack's sounds, every effect, silent. The
    window adds "Choose a file…" (and the chosen file)."""
    return (
        ("", "Pack's own"),
        (sounds.REVIEW, "Pack: review request"),
        (sounds.GOOD, "Pack: good news"),
        (sounds.BAD, "Pack: bad news"),
        (sounds.INFO, "Pack: info"),
        *((sounds.FX_PREFIX + fx, label) for fx, label in sounds.FX.items()),
        (sounds.SILENT, "Silent"),
    )


def parse_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def normalize(f: Field, value: Any) -> Any:
    """Clean a widget value before storing it (trim text, time as HH:MM)."""
    if f.kind in (TEXT, TIME, BROWSER) and isinstance(value, str):
        value = value.strip()
        if f.kind == TIME and _TIME.match(value):
            h, m = value.split(":")
            value = f"{int(h):02d}:{m}"
    if f.kind == COLOR and isinstance(value, str):
        value = value.upper() if value else ""
    return value


def apply(cfg: cfg_mod.Config, f: Field, value: Any) -> str | None:
    """Validate, normalize and write one field. Returns an error or None."""
    value = normalize(f, value)
    error = validate(f, value)
    if error is None:
        set_value(cfg, f.key, value)
    return error


def is_active(cfg: cfg_mod.Config, f: Field) -> bool:
    """False when the field depends on a switch that is off (render dimmed)."""
    return not f.depends_on or bool(get_value(cfg, f.depends_on))


def exclusions_after_choice(
    previous: list[str], discovered: list[str], excluded_now: list[str]
) -> list[str]:
    """A folder's exclusion list after the user picked among the repos found
    in it now. Exclusions for subfolders not on disk right now are kept."""
    on_disk = set(discovered)
    return [p for p in previous if p not in on_disk] + sorted(excluded_now)


class EditSession:
    """A working copy of the config plus debounced commits.

    Backends call `edit()` for model fields and `changed()` after a custom
    editor mutated `working` in place. After `COMMIT_DELAY_MS` without edits
    the working copy is saved and handed to `on_commit` (a deep copy, so the
    controller never shares objects with the open window).
    `schedule(ms, fn) -> handle` / `cancel(handle)` come from the toolkit.
    """

    def __init__(
        self,
        cfg: cfg_mod.Config,
        on_commit: Callable[[cfg_mod.Config], Any],
        schedule: Callable[[int, Callable[[], Any]], object],
        cancel: Callable[[object], None],
        live: Callable[[], cfg_mod.Config] | None = None,
    ) -> None:
        import copy

        self._copy = copy.deepcopy
        self.working = copy.deepcopy(cfg)
        self._live = live  # () -> the controller's current Config
        self._on_commit = on_commit
        self._schedule = schedule
        self._cancel = cancel
        self._pending: object | None = None

    def edit(self, f: Field, value: Any) -> str | None:
        error = apply(self.working, f, value)
        if error is None:
            self.changed()
        return error

    def changed(self) -> None:
        if self._pending is not None:
            self._cancel(self._pending)
        self._pending = self._schedule(COMMIT_DELAY_MS, self._fire)

    def _fire(self) -> bool:
        self._pending = None
        self.commit()
        return False  # one-shot (GLib semantics)

    def commit(self) -> None:
        """Apply now (also used when the window closes with edits pending)."""
        if self._pending is not None:
            self._cancel(self._pending)
            self._pending = None
        snapshot = self._copy(self.working)
        if self._live is not None:
            live = self._live()
            for key in LIVE_KEYS:
                setattr(snapshot, key, self._copy(getattr(live, key)))
        cfg_mod.save(snapshot)
        self._on_commit(snapshot)

    def reset(self, cfg: cfg_mod.Config) -> None:
        """Drop pending edits and start over from `cfg` (e.g. after an import
        wrote config.json: the old working copy must not overwrite it)."""
        if self._pending is not None:
            self._cancel(self._pending)
            self._pending = None
        self.working = self._copy(cfg)

    @property
    def pending(self) -> bool:
        return self._pending is not None
