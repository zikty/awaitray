"""Toolkit-agnostic model of the tray menu (docs/ui-spec.md).

The controller snapshots what the menu shows into a `MenuState`, and
`build(state, now)` turns it into a `Menu`: a pure function, so the menu
can be tested from a hand-made state. Each backend renders the `Menu` with
its own toolkit and sends clicks back through `Controller.perform(action)`.
Everything is an immutable, hashable dataclass, so backends can skip a
rebuild when nothing changed (`Menu.signature`), and the whole structure is
unit-testable without GTK or AppKit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import account_info
from . import inbox as inbox_mod
from . import pr_actions as actions_mod
from . import pr_state as ps
from . import quiet_hours as qh_mod
from . import snooze as snooze_mod
from .config import Config
from .github import PRDetails, PullRequest
from .pr_sync import SETUP_GH_MISSING, SETUP_NO_HOSTS

if TYPE_CHECKING:  # pragma: no cover
    from .ai_usage import Usage
    from .config import CustomSection
    from .github_status import Status
    from .repo_sync import LocalRepoStatus

SECTION_CAP = 8  # rows per section before "N more… ▸"
TITLE_MAX = 60  # characters of the PR title (only the title is cut)
CHECKS_CAP = 8  # checks listed per PR before "All checks…"


# Kinds the backends handle themselves (clipboard, windows, the app's life);
# every other kind goes to Controller.perform.
COPY, PREFS, QUIT = "copy", "prefs", "quit"
BACKEND_ACTIONS = frozenset({COPY, PREFS, QUIT})


@dataclass(frozen=True)
class Action:
    """What a click does. `kind` is handled by `Controller.perform`, except
    `copy`, `prefs` and `quit`, which the backend handles itself."""

    kind: str
    args: tuple[Any, ...] = ()


# Item kinds
STATUS = "status"  # first line, disabled
HEADER = "header"  # section header, disabled
EMPTY = "empty"  # "Nothing to review", disabled
PR = "pr"  # PR row, click = action
ACTION = "action"  # plain clickable item
SUBMENU = "submenu"  # item holding children (not clickable itself)
CHECK = "check"  # check item (Pause)
SEPARATOR = "separator"


@dataclass(frozen=True)
class Item:
    kind: str
    label: str = ""  # one-line text (Linux, and macOS fallback)
    title: str = ""  # macOS two-line rows: first line
    meta: str = ""  # macOS two-line rows: second line
    color: str | None = None  # "#RRGGBB" swatch / tint
    action: Action | None = None
    children: tuple[Item, ...] = ()
    checked: bool = False
    # For PR rows: the same actions as the PR's entry in "PR actions ▸"
    # (macOS shows them as a ⌥-alternate submenu on the row).
    alternate: tuple[Item, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.kind in (PR, ACTION, SUBMENU, CHECK)


SEP = Item(SEPARATOR)


@dataclass(frozen=True)
class Menu:
    items: tuple[Item, ...] = field(default_factory=tuple)

    @property
    def signature(self) -> int:
        return hash(self.items)


# ----- What the menu shows -----


@dataclass(frozen=True)
class PrView:
    """What the menu shows about one PR, besides the PR itself."""

    details: PRDetails | None = None
    color: str | None = None  # its state's color; None: the section's
    review_marker: str = ""  # "📂 " when there's a local clone
    long_wait: bool = False
    stale: bool = False
    local_path: str | None = None
    can_clone: bool = False
    watching_checks: bool = False


@dataclass(frozen=True)
class StatusInfo:
    """What the status line says, from the last poll."""

    setup_problem: str | None = None
    text: str = ""  # the last status message: errors, "refreshing…", "starting…"
    offline: bool = False
    enabled_accounts: tuple[str, ...] = ()
    auth_failed: tuple[str, ...] = ()
    error: bool = False
    showing_cached: bool = False
    last_success_at: datetime | None = None
    direct_count: int = 0
    team_count: int = 0


@dataclass(frozen=True)
class CustomSectionView:
    section: CustomSection
    prs: tuple[PullRequest, ...]  # visible ones
    snoozed: int = 0


@dataclass(frozen=True)
class MenuState:
    """Everything `build` reads: a snapshot taken by `Controller.menu_state`."""

    cfg: Config
    status: StatusInfo
    review_prs: tuple[PullRequest, ...] = ()  # all, snoozed ones too
    direct: tuple[PullRequest, ...] = ()  # visible, addressed to you
    team: tuple[PullRequest, ...] = ()  # visible, only through a team
    mine: tuple[PullRequest, ...] = ()  # empty unless My PRs is on
    custom: tuple[CustomSectionView, ...] = ()
    views: Mapping[str, PrView] = field(default_factory=dict)  # by PR key
    snoozed_prs: Mapping[str, PullRequest | None] = field(default_factory=dict)
    multi_account: bool = False
    status_page: Status | None = None
    ai_usages: tuple[Usage, ...] = ()
    inboxes: tuple[account_info.AccountInfo, ...] = ()
    local_repos: tuple[tuple[LocalRepoStatus, str], ...] = ()  # (row, status text)
    local_repos_title: str = "Local repos"

    def view(self, pr: PullRequest) -> PrView:
        return self.views.get(pr.key) or PrView()


# ----- helpers -----


def _truncate(text: str, limit: int = TITLE_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _short_names(prs: list[PullRequest]) -> dict[tuple[str, str], str]:
    """(host, owner/repo) → display name: the short repo name, unless two
    visible repos share it (then owner/repo, then host:owner/repo)."""
    keys = {(p.host, p.repo) for p in prs}
    by_short: dict[str, set[tuple[str, str]]] = {}
    for host, repo in keys:
        by_short.setdefault(repo.rsplit("/", 1)[-1], set()).add((host, repo))
    by_full: dict[str, set[str]] = {}
    for host, repo in keys:
        by_full.setdefault(repo, set()).add(host)
    out = {}
    for host, repo in keys:
        short = repo.rsplit("/", 1)[-1]
        if len(by_short[short]) == 1:
            out[(host, repo)] = short
        elif len(by_full[repo]) == 1:
            out[(host, repo)] = repo
        else:
            out[(host, repo)] = f"{host}:{repo}"
    return out


_CHECK_GLYPHS = {
    "FAILURE": ps.GLYPH_FAILING,
    "PENDING": ps.GLYPH_RUNNING,
    "SUCCESS": ps.GLYPH_GREEN,
}


def _open_target(pr: PullRequest, det: PRDetails | None, is_mine: bool) -> str:
    if is_mine and det is not None and det.latest_activity_url:
        return det.latest_activity_url
    return pr.url


def markdown_link(pr: PullRequest) -> str:
    # Brackets in the title would end the link text early.
    title = re.sub(r"([\\\[\]])", r"\\\1", pr.title)
    return f"[{pr.repo}#{pr.number}: {title}]({pr.url})"


# ----- builder -----


def build(state: MenuState, now: datetime | None = None) -> Menu:
    cfg = state.cfg
    now = now or datetime.now()
    snoozed = cfg.snoozed
    all_review = list(state.review_prs)
    mine = list(state.mine)
    custom = [(c.section, list(c.prs)) for c in state.custom]
    names = _short_names([*all_review, *mine, *(p for _s, prs in custom for p in prs)])
    show_glyph = cfg.features.show_ci_status
    multi_account = state.multi_account

    def pr_row(pr: PullRequest, *, is_mine: bool, default_color: str | None) -> Item:
        view = state.view(pr)
        det = view.details
        glyph = ps.glyph(pr, det, mine=is_mine) if show_glyph else ""
        marker = "" if is_mine else view.review_marker
        if not is_mine and view.long_wait:
            marker = "⏳ " + marker
        head = f"{names[(pr.host, pr.repo)]}#{pr.number}"
        title = f"{glyph + ' ' if glyph else ''}{marker}{head}  {_truncate(pr.title)}"
        who = ps.state_label(pr, det) if is_mine else pr.author
        # Review requests: how long it has waited on you; yours: its age.
        since = pr.created_at if is_mine else ps.waiting_since(pr)
        flags = (
            "draft" if pr.is_draft and not is_mine else "",
            "stale" if view.stale else "",
            pr.account if multi_account else "",  # which account sees it
        )
        meta = " · ".join(x for x in (who, ps.age(since), *flags) if x)
        return Item(
            PR,
            label=f"{title} · {meta}" if meta else title,
            title=title,
            meta=meta,
            color=view.color or default_color,
            # Yours open where the conversation is (latest comment or review).
            action=Action("open_url", (_open_target(pr, det, is_mine), pr.key)),
            alternate=pr_actions(pr, snoozable=not is_mine),
        )

    def pr_actions(pr: PullRequest, *, snoozable: bool) -> tuple[Item, ...]:
        items = [Item(ACTION, label="Open in browser", action=Action("open_url", (pr.url, pr.key)))]
        view = state.view(pr)
        local = view.local_path
        if cfg.editor.enabled and local:
            editor = cfg.editor.command.split()[0] if cfg.editor.command.strip() else "editor"
            items.append(
                Item(ACTION, label=f"Open in {editor}", action=Action("open_editor", (local,)))
            )
        elif view.can_clone:
            items.append(Item(ACTION, label="Clone here", action=Action("clone", (pr.key,))))
        items.append(
            Item(ACTION, label="Copy as Markdown", action=Action("copy", (markdown_link(pr),)))
        )
        watching = view.watching_checks
        det = view.details
        if not snoozable and (watching or (det is not None and det.ci_state == "PENDING")):
            items.append(
                Item(
                    CHECK,
                    label="Notify when checks finish",
                    checked=watching,
                    action=Action("watch_checks", (pr.key,)),
                )
            )
        if not snoozable:  # yours: one-click fixes (pr_actions.py)
            runs = actions_mod.failed_runs(pr, det)
            if runs:
                label = (
                    "Re-run failed checks"
                    if len(runs) == 1
                    else f"Re-run failed checks ({len(runs)} runs)"
                )
                items.append(Item(ACTION, label=label, action=Action("rerun_failed", (pr.key,))))
            if actions_mod.can_request_copilot(pr):
                items.append(
                    Item(
                        ACTION,
                        label="Request Copilot review",
                        action=Action("copilot_review", (pr.key,)),
                    )
                )
        items.extend(check_items(pr))
        if snoozable:
            items.append(SEP)
            items.extend(
                Item(ACTION, label=f"Snooze: {label}", action=Action("snooze", (pr.key, kind)))
                for label, kind in snooze_mod.CHOICES
            )
        return tuple(items)

    def check_items(pr: PullRequest) -> list[Item]:
        """The PR's checks, failing first; inline (Linux allows two submenu
        levels, and PR actions ▸ PR ▸ already uses both)."""
        det = state.view(pr).details
        if not show_glyph or det is None or not det.checks:
            return []
        failing = sum(1 for c in det.checks if c[1] == "FAILURE")
        head = f"Checks · {failing} failing" if failing else f"Checks · {len(det.checks)}"
        out = [SEP, Item(HEADER, label=head)]
        for name, outcome, url in det.checks[:CHECKS_CAP]:
            label = f"{_CHECK_GLYPHS.get(outcome, ps.GLYPH_NONE)} {_truncate(name, 40)}"
            if url:
                out.append(Item(ACTION, label=label, action=Action("open_url", (url, pr.key))))
            else:
                out.append(Item(EMPTY, label=label))
        out.append(
            Item(
                ACTION,
                label="All checks…",
                action=Action("open_url", (f"{pr.url}/checks", pr.key)),
            )
        )
        return out

    def section(
        title: str,
        prs: list[PullRequest],
        empty: str,
        *,
        hidden: int,
        is_mine: bool,
        color: str | None,
    ) -> list[Item]:
        out = [Item(HEADER, label=f"{title.upper()} · {len(prs)}", color=color)]
        rows = [pr_row(p, is_mine=is_mine, default_color=color) for p in prs]
        if not rows:
            text = empty + (f" · {hidden} snoozed" if hidden else "")
            out.append(Item(EMPTY, label=text))
            return out
        out.extend(rows[:SECTION_CAP])
        if len(rows) > SECTION_CAP:
            rest = rows[SECTION_CAP:]
            out.append(Item(SUBMENU, label=f"{len(rest)} more…", children=tuple(rest)))
        return out

    items: list[Item] = [Item(STATUS, label=status_line(cfg, state.status, now))]

    # Nothing can be polled, or no host is logged in: say how to fix it
    # instead of showing empty sections that would read as "all clear".
    st = state.status
    fixes = setup_items(st)
    enabled = st.enabled_accounts
    blocked = st.setup_problem is not None or (
        enabled and set(st.auth_failed) >= set(enabled) and not state.review_prs
    )
    if blocked:
        return Menu(tuple([*items, SEP, *fixes, *_footer(cfg)]))
    # Right under the status: GitHub incidents, Copilot, the inbox.
    items += [*fixes, *account_items(state), SEP]

    # Review requests
    direct, team = list(state.direct), list(state.team)
    snoozed_direct = sum(1 for p in all_review if p.key in snoozed and p.direct_request)
    snoozed_team = sum(1 for p in all_review if p.key in snoozed and not p.direct_request)
    review_color = cfg.colors.to_review or None
    items += section(
        "Needs your review",
        direct,
        "Nothing to review",
        hidden=snoozed_direct,
        is_mine=False,
        color=review_color,
    )
    items += section(
        "Needs your team's review",
        team,
        "Nothing to review",
        hidden=snoozed_team,
        is_mine=False,
        color=review_color,
    )

    # My PRs, by state
    if cfg.my_prs.enabled:
        buckets: dict[str, list[PullRequest]] = {s: [] for s in ps.MY_SECTIONS}
        for pr in mine:
            buckets[ps.classify_mine(pr, state.view(pr).details)].append(pr)
        my_color = cfg.colors.my_prs or None
        for key, title, empty in (
            (ps.NEEDS_ACTION, "Needs your action", "Nothing needs your action"),
            (ps.READY_TO_MERGE, "Ready to merge", "Nothing ready to merge"),
            (ps.WAITING, "Waiting for review", "Nothing waiting"),
            (ps.DRAFTS, "Drafts", "No drafts"),
        ):
            items += section(title, buckets[key], empty, hidden=0, is_mine=True, color=my_color)

    # Custom sections, in the order configured
    for view in state.custom:
        items += section(
            view.section.title,
            list(view.prs),
            "Nothing here",
            hidden=view.snoozed,
            is_mine=False,
            color=review_color,
        )

    items.append(SEP)

    # PR actions ▸ (per PR ▸ actions); review requests first, then mine
    action_prs = [(p, True) for p in [*direct, *team]] + [(p, False) for p in mine]
    listed = {p.key for p, _ in action_prs}
    for _sec, prs in custom:
        for p in prs:
            if p.key not in listed:
                action_prs.append((p, True))
                listed.add(p.key)
    if action_prs:
        children = []
        visible_urls = tuple((p.url, p.key) for p in [*direct, *team])
        if len(visible_urls) > 1:
            children += [
                Item(
                    ACTION,
                    label=f"Open all {len(visible_urls)} review requests",
                    action=Action("open_urls", visible_urls),
                ),
                SEP,
            ]
        for pr, snoozable in action_prs:
            head = f"{names[(pr.host, pr.repo)]}#{pr.number}  {_truncate(pr.title, 40)}"
            children.append(Item(SUBMENU, label=head, children=pr_actions(pr, snoozable=snoozable)))
        items.append(Item(SUBMENU, label="PR actions", children=tuple(children)))

    # Snoozed ▸
    if snoozed:
        children = []
        for key, entry in sorted(snoozed.items()):
            found = state.snoozed_prs.get(key)
            name = f"{found.repo}#{found.number}  {_truncate(found.title, 40)}" if found else key
            actions: list[Item] = []
            if found:
                actions.append(
                    Item(
                        ACTION,
                        label="Open in browser",
                        action=Action("open_url", (found.url, found.key)),
                    )
                )
            actions.append(Item(ACTION, label="Unsnooze", action=Action("unsnooze", (key,))))
            children.append(
                Item(
                    SUBMENU,
                    label=f"{name} ({snooze_mod.describe(entry, now)})",
                    children=tuple(actions),
                )
            )
        items.append(Item(SUBMENU, label=f"Snoozed ({len(snoozed)})", children=tuple(children)))

    # Local repos ▸
    if cfg.local_repos.enabled:
        repos = state.local_repos
        children = [Item(ACTION, label="Fetch all repos now", action=Action("fetch")), SEP]
        editor = cfg.editor.command.split()[0] if cfg.editor.command.strip() else "editor"
        for repo, status_text in repos:
            sub: list[Item] = []
            if cfg.editor.enabled:
                sub.append(
                    Item(
                        ACTION,
                        label=f"Open in {editor}",
                        action=Action("open_editor", (repo.path,)),
                    )
                )
            if sub and repo.review_prs:
                sub.append(SEP)
            sub += [
                pr_row(pr, is_mine=False, default_color=cfg.colors.local_repos or None)
                for pr in repo.review_prs
            ]
            label = f"{repo.name}  {status_text}"
            if sub:
                children.append(
                    Item(
                        SUBMENU,
                        label=label,
                        color=cfg.colors.local_repos or None,
                        children=tuple(sub),
                    )
                )
            else:
                children.append(Item(EMPTY, label=label))
        items.append(
            Item(
                SUBMENU,
                label=f"{state.local_repos_title} ({len(repos)})",
                children=tuple(children),
            )
        )

    items += _footer(cfg)
    return Menu(tuple(items))


def account_items(state: MenuState) -> list[Item]:
    """GitHub incidents (github_status.py), each AI assistant's quota left
    (ai_usage.py), then unread notifications per account (account_info.py),
    right under the status line. An account is named only when there are
    several."""
    out: list[Item] = []
    status = state.status_page
    if status is not None and not status.ok:
        out.append(Item(ACTION, label=status.label(), action=Action("open_url", (status.url,))))
    usages = state.ai_usages
    for usage in usages:
        several = sum(1 for u in usages if u.provider == usage.provider) > 1
        label = (f"{usage.account}: " if several and usage.account else "") + usage.describe()
        if usage.url:
            # In the account's browser, when it's an account's.
            action = Action("open_url", (usage.url, "", usage.account))
            out.append(Item(ACTION, label=label, action=action))
        else:
            out.append(Item(EMPTY, label=label))
    infos = state.inboxes
    named = len(infos) > 1
    for info in infos:
        prefix = f"{info.account}: " if named else ""
        if info.unread is not None:
            out.append(inbox_item(info, prefix + account_info.inbox_label(info.unread)))
    return out


def inbox_item(info: account_info.AccountInfo, label: str) -> Item:
    """The inbox line: a link when there's nothing to triage, else a
    submenu with each thread's actions (inbox.py), two levels deep."""
    page = f"https://{info.host}/notifications"
    if not info.inbox:
        return Item(ACTION, label=label, action=Action("open_url", (page, "", info.account)))
    acct = info.account
    children = [
        Item(
            ACTION, label="Open GitHub inbox", action=Action("open_url", (page, "", info.account))
        ),
        Item(ACTION, label="Mark all as read", action=Action("inbox_read_all", (acct,))),
        SEP,
    ]
    for thread in info.inbox[: inbox_mod.MENU_CAP]:
        tid = thread.thread_id
        children.append(
            Item(
                SUBMENU,
                label=_truncate(thread.label()),
                children=(
                    Item(ACTION, label="Open", action=Action("inbox_open", (acct, tid))),
                    Item(ACTION, label="Mark as read", action=Action("inbox", (acct, "read", tid))),
                    Item(ACTION, label="Mark as done", action=Action("inbox", (acct, "done", tid))),
                    Item(
                        ACTION,
                        label="Unsubscribe",
                        action=Action("inbox", (acct, "unsubscribe", tid)),
                    ),
                ),
            )
        )
    more = len(info.inbox) - inbox_mod.MENU_CAP
    if more > 0:
        plus = "+" if len(info.inbox) >= account_info.INBOX_CAP else ""
        children.append(
            Item(
                ACTION,
                label=f"{more}{plus} more on GitHub…",
                action=Action("open_url", (page, "", info.account)),
            )
        )
    return Item(SUBMENU, label=label, children=tuple(children))


def _footer(cfg: Config) -> list[Item]:
    return [
        SEP,
        Item(CHECK, label="Pause", checked=cfg.paused, action=Action("toggle_pause")),
        Item(ACTION, label="Refresh now", action=Action("refresh")),
        Item(ACTION, label="Preferences…", action=Action("prefs")),
        SEP,
        Item(ACTION, label="Quit", action=Action("quit")),
    ]


def setup_items(st: StatusInfo) -> list[Item]:
    """Actionable fixes for setup problems, shown right under the status line."""
    if st.setup_problem == SETUP_GH_MISSING:
        return [
            Item(
                ACTION,
                label="Install the GitHub CLI (gh)…",
                action=Action("open_url", ("https://cli.github.com",)),
            )
        ]
    if st.setup_problem == SETUP_NO_HOSTS:
        return [Item(ACTION, label="Set up awaitray…", action=Action("prefs"))]
    return [
        Item(ACTION, label=f"Log in to {host}…", action=Action("login", (host,)))
        for host in st.auth_failed
    ]


def status_line(cfg: Config, st: StatusInfo, now: datetime) -> str:
    """First line: count + freshness, or the state that matters more."""
    enabled = st.enabled_accounts
    if cfg.paused:
        text = "Paused"
    elif st.setup_problem is not None:
        text = st.text
    elif st.offline:
        text = "Offline, will retry"
    elif enabled and set(st.auth_failed) >= set(enabled):
        text = "Not logged in to " + ", ".join(st.auth_failed)
    elif st.error or st.showing_cached or st.last_success_at is None:
        text = st.text  # error details, "from 14:32, refreshing…", "starting…"
    else:
        n, team = st.direct_count, st.team_count
        text = f"{n} to review" + (f" · {team} team" if team else "")
        text += f" · updated {st.last_success_at.strftime('%H:%M')}"
    if qh_mod.is_quiet(cfg.quiet_hours, now):
        text += " · quiet hours"
    return text
