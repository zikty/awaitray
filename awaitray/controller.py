"""Toolkit-agnostic orchestration shared by every platform backend.

Holds the live PR/fetch state, the poll/fetch timers, the worker
threads, the error policy, snooze handling, and the quiet-hours
notification gate. It never imports a GUI toolkit: it talks to the UI
through a `UIPort` the backend implements, and schedules timers / posts
work back to the main thread through a `HostPort` the backend implements
(GLib on Linux, NSTimer + the main NSOperationQueue on macOS).

That keeps the PR/fetch behaviour in one place so the Linux (GTK) and
macOS (PyObjC) backends can't drift apart — only the menu, icon, and
notification rendering live in the backends.

The logic itself lives in modules the controller glues together:
`pr_sync` (polling, baselines, which PRs notify), `repo_sync` (clones,
fetches, Local repos rows), `scheduler` (timers, retries, error policy) and
`snooze`. The controller holds the live state, applies worker results on the
main thread and is the one facade the backends and the menu model talk to.
"""

from __future__ import annotations

import fnmatch
import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from . import config as cfg_mod
from . import github as gh
from . import log as log_mod
from . import (
    menu_model,
    nudges,
    pr_state,
    pr_store,
    pr_sync,
    repo_sync,
    sounds,
)
from . import quiet_hours as qh_mod
from . import repos as repo_mod
from . import scheduler as scheduler_mod
from . import state as state_mod
from .alerts import Alerts
from .features.context import Context
from .features.inbox import Inbox
from .features.links import Links
from .features.pr_actions import PrActions
from .features.snoozes import Snoozes
from .features.usage import Usage
from .menu_model import Action, CustomSectionView, MenuState, PrView, StatusInfo
from .notice import Notice
from .pr_sync import SETUP_GH_MISSING, SETUP_MESSAGES, SETUP_NO_HOSTS, PollResult
from .repo_sync import LocalRepoStatus
from .runner import Runner, ThreadRunner
from .scheduler import HostPort, Scheduler

__all__ = [
    "SETUP_GH_MISSING",
    "SETUP_NO_HOSTS",
    "TRAY_COUNT",
    "TRAY_ERROR",
    "TRAY_IDLE",
    "TRAY_OFFLINE",
    "TRAY_PAUSED",
    "Controller",
    "HostPort",
    "LocalRepoStatus",
    "PollResult",
    "TrayState",
    "UIPort",
]


# What the tray icon shows (Controller.tray_state), most important first.
TRAY_PAUSED = "paused"
TRAY_OFFLINE = "offline"
TRAY_ERROR = "error"  # gh missing, not logged in, other failures
TRAY_COUNT = "count"  # PRs waiting for you
TRAY_IDLE = "idle"  # nothing waiting


@dataclass(frozen=True)
class TrayState:
    kind: str
    count: int = 0
    alert: bool = False  # a high-priority repo has a waiting PR
    unseen: bool = False  # a counted PR arrived since the menu was last opened


class UIPort(Protocol):
    """What the controller needs from whichever GUI toolkit is active."""

    def refresh_icon(self) -> None: ...
    def rebuild_menu(self) -> None: ...
    def notify(self, title: str, body: str, urgency: str, icon: str) -> None: ...
    def play_sound(self, sound: str) -> None: ...
    def present_prefs(self) -> None: ...


class Controller:
    def __init__(
        self,
        ui: UIPort,
        host: HostPort,
        cfg: cfg_mod.Config | None = None,
        state_store: state_mod.StateStore | None = None,
        runner: Runner | None = None,
    ) -> None:
        self._ui = ui
        self._host = host
        # Every blocking job (gh, git, a CLI) goes through it: tests pass an
        # ImmediateRunner instead of patching threads.
        self._runner: Runner = runner if runner is not None else ThreadRunner(host)
        self._timers = Scheduler(host)
        self._cfg = cfg if cfg is not None else cfg_mod.load()
        self._alerts = Alerts(ui, lambda: self._cfg)
        # The real cache only when the controller owns its config (the app);
        # an injected config (tests, tools) never touches it unless asked.
        if state_store is None:
            state_store = state_mod.StateStore() if cfg is None else state_mod.NullStore()
        self._state = state_store

        # The PRs, their details and every "notify once" bookkeeping.
        self._prs = pr_store.PrState()
        self._fetch_results: list[repo_mod.FetchResult] = []
        self._last_error = False
        self._consecutive_failures: int = 0
        self._consecutive_fetch_failures: int = 0
        self._fetch_outage: bool = False
        self._last_status_text = "awaitray: starting…"
        self._last_success_at: datetime | None = None  # last poll that reached GitHub
        self._showing_cached = False  # PR lists come from the last run, not a poll yet
        self._setup_problem: str | None = None  # SETUP_* when nothing can be polled
        self._host_errors: dict[str, str] = {}  # host -> gh.ERR_* from the last poll
        self._first_run_prefs_shown = False
        self._actions: dict[str, Callable[..., object]] = {}
        self._register_actions()
        # The features: each owns its state, actions and background work, and
        # sees the app only through this Context (late-bound lambdas, so a
        # replaced config or a patched method is what they get).
        ctx = Context(
            config=lambda: self._cfg,
            runner=self._runner,
            notify=lambda notice: self._notify(notice),
            tell=lambda notice: self._tell(notice),
            rebuild_menu=lambda: self._ui.rebuild_menu(),
            refresh_ui=lambda: self._refresh_ui(),
            register_action=self.register_action,
            pr_for_key=self.pr_for_key,
            details_for=self.details_for,
            open_url=lambda url, key, account: self.links.open_url(url, key, account),
            refresh_details=lambda urls: self._refresh_details(urls),
            tick_fetch=lambda: self.tick_fetch(),
            default_opener=host.default_opener,
        )
        self.links = Links(ctx)
        self.snoozes = Snoozes(ctx)
        self.pr_actions = PrActions(ctx)
        self.inbox = Inbox(ctx)
        self.usage = Usage(ctx)
        self._restore_state()

    # ----- Lifecycle -----

    def start(self) -> None:
        """Schedule the timers and trigger the first poll shortly after the
        event loop starts so the UI has a chance to draw."""
        self._reschedule_timers()
        log_mod.info("awaitray started")
        if cfg_mod.last_load_warning:
            self._notify(Notice("awaitray settings reset", cfg_mod.last_load_warning, "critical"))
        self._timers.once(1, self._initial_kick)

    # ----- State exposed to the backend (menu + icon rendering) -----

    @property
    def cfg(self) -> cfg_mod.Config:
        return self._cfg

    @property
    def my_prs(self) -> list[gh.PullRequest]:
        return self._prs.my_prs

    @property
    def current_prs(self) -> list[gh.PullRequest]:
        """All review requests from the last poll (snoozed ones included)."""
        return self._prs.review_prs

    @property
    def last_status_text(self) -> str:
        return self._last_status_text

    @property
    def last_success_at(self) -> datetime | None:
        return self._last_success_at

    @property
    def showing_cached(self) -> bool:
        return self._showing_cached

    @property
    def setup_problem(self) -> str | None:
        """SETUP_GH_MISSING / SETUP_NO_HOSTS when polling can't even start."""
        return self._setup_problem

    @property
    def auth_failed_hosts(self) -> list[str]:
        """Accounts (ids: `user@host` or `host`) whose last poll failed because
        gh isn't logged in there."""
        return sorted(a for a, kind in self._host_errors.items() if kind == gh.ERR_AUTH)

    @property
    def enabled_accounts(self) -> list[str]:
        return [h.account_id for h in self._cfg.hosts if h.enabled]

    @property
    def multi_account(self) -> bool:
        """More than one enabled account: PRs and notices say which one."""
        return len(self.enabled_accounts) > 1

    @property
    def offline(self) -> bool:
        """Every account failed and every failure looks like a network problem."""
        enabled = self.enabled_accounts
        return bool(enabled) and all(self._host_errors.get(a) == gh.ERR_NETWORK for a in enabled)

    @property
    def last_error(self) -> bool:
        return self._last_error

    def details_for(self, url: str) -> gh.PRDetails | None:
        return self._prs.details.get(url)

    def pr_for_key(self, key: str) -> gh.PullRequest | None:
        return self._prs.pr_for_key(key)

    def custom_sections(self) -> list[tuple[cfg_mod.CustomSection, list[gh.PullRequest]]]:
        """Enabled custom sections with their visible (not snoozed) PRs."""
        snoozed = self._cfg.snoozed
        return [
            (s, [p for p in self._prs.section_prs.get(s.sid, []) if p.key not in snoozed])
            for s in self._cfg.sections
            if s.enabled
        ]

    def section_snoozed_count(self, section: cfg_mod.CustomSection) -> int:
        return sum(
            1 for p in self._prs.section_prs.get(section.sid, []) if p.key in self._cfg.snoozed
        )

    def visible_prs(self) -> list[gh.PullRequest]:
        return [p for p in self._prs.review_prs if p.key not in self._cfg.snoozed]

    def direct_prs(self) -> list[gh.PullRequest]:
        """Visible review requests addressed to you (not only to a team)."""
        return [p for p in self.visible_prs() if p.direct_request]

    def team_prs(self) -> list[gh.PullRequest]:
        """Visible review requests that reach you only through a team."""
        return [p for p in self.visible_prs() if not p.direct_request]

    def _counts(self, pr: gh.PullRequest) -> bool:
        """Whether a visible review request counts in the numbers (stale ones
        don't, unless asked)."""
        w = self._cfg.waiting
        return w.count_stale or not pr_state.is_stale(pr, w.stale_days)

    def is_long_wait(self, pr: gh.PullRequest) -> bool:
        return pr_state.is_long_wait(pr, self._cfg.waiting.long_wait_days)

    def is_stale(self, pr: gh.PullRequest) -> bool:
        return pr_state.is_stale(pr, self._cfg.waiting.stale_days)

    def visible_count(self) -> int:
        """Review requests addressed to you personally (the headline number)."""
        return sum(1 for p in self.direct_prs() if self._counts(p))

    def team_count(self) -> int:
        """Review requests that reach you only through a team, counted like
        `visible_count` (whether or not the icon counts them)."""
        return len(self._counted_team_prs())

    def _counted_team_prs(self) -> list[gh.PullRequest]:
        return [p for p in self.team_prs() if self._counts(p)]

    # ----- Menu -----

    def menu_model(self) -> menu_model.Menu:
        """The tray menu as data (docs/ui-spec.md); backends render it."""
        return menu_model.build(self.menu_state())

    def menu_state(self) -> MenuState:
        """A snapshot of everything the menu shows (`menu_model.build` reads
        nothing else)."""
        cfg = self._cfg
        mine = list(self.my_prs) if cfg.my_prs.enabled else []
        custom = tuple(
            CustomSectionView(sec, tuple(prs), self.section_snoozed_count(sec))
            for sec, prs in self.custom_sections()
        )
        rows = self.local_repos()
        shown = [
            *self.current_prs,
            *mine,
            *(p for c in custom for p in c.prs),
            *(p for row in rows for p in row.review_prs),
        ]
        return MenuState(
            cfg=cfg,
            status=StatusInfo(
                setup_problem=self.setup_problem,
                text=self.last_status_text,
                offline=self.offline,
                enabled_accounts=tuple(self.enabled_accounts),
                auth_failed=tuple(self.auth_failed_hosts),
                error=self.last_error,
                showing_cached=self.showing_cached,
                last_success_at=self.last_success_at,
                direct_count=self.visible_count(),
                team_count=self.team_count(),
            ),
            review_prs=tuple(self.current_prs),
            direct=tuple(self.direct_prs()),
            team=tuple(self.team_prs()),
            mine=tuple(mine),
            custom=custom,
            views={p.key: self._pr_view(p) for p in shown},
            snoozed_prs={key: self.pr_for_key(key) for key in cfg.snoozed},
            multi_account=self.multi_account,
            status_page=self.usage.status_page,
            ai_usages=tuple(self.usage.ai_usages),
            inboxes=tuple(self.inbox.infos),
            local_repos=tuple((row, self.repo_status_text(row)) for row in rows),
            local_repos_title=self.local_repos_section_title(),
        )

    def _pr_view(self, pr: gh.PullRequest) -> PrView:
        return PrView(
            details=self.details_for(pr.url),
            color=self.pr_color(pr, None),
            review_marker=self.links.review_marker(pr),
            long_wait=self.is_long_wait(pr),
            stale=self.is_stale(pr),
            local_path=self.links.local_path_for(pr),
            can_clone=self.links.can_clone(pr),
            watching_checks=self.is_watching_checks(pr.key),
        )

    def perform(self, action: Action) -> None:
        """Run a menu action through its registered handler. `copy`, `prefs`
        and `quit` (menu_model.BACKEND_ACTIONS) are toolkit things the
        backends handle before calling this."""
        handler = self._actions.get(action.kind)
        if handler is None:
            log_mod.warn(f"menu: unknown action {action.kind!r}")
            return
        handler(*action.args)

    def register_action(self, kind: str, handler: Callable[..., object]) -> None:
        """Whoever owns a menu action registers it; `handler` gets the
        Action's args."""
        if kind in self._actions or kind in menu_model.BACKEND_ACTIONS:
            raise ValueError(f"action {kind!r} is already handled")
        self._actions[kind] = handler

    def handles(self, kind: str) -> bool:
        return kind in self._actions or kind in menu_model.BACKEND_ACTIONS

    def _register_actions(self) -> None:
        # The controller's own; each feature registers its own. Lambdas, not
        # bound methods: the method is looked up at click time.
        actions: dict[str, Callable[..., object]] = {
            "watch_checks": lambda key: self.toggle_watch_checks(key),
            "toggle_pause": lambda: self.toggle_pause(),
            "refresh": lambda: self.tick_prs(),
            "fetch": lambda: self.tick_fetch(),
            "login": lambda account: self._login(account),  # an account id
        }
        for kind, handler in actions.items():
            self.register_action(kind, handler)

    def _login(self, account: str) -> None:
        _user, host = cfg_mod.split_account(account)
        problem = gh.launch_login(host)
        if problem:
            self._tell(Notice(f"Log in to {account}", problem))

    def pr_color(self, pr: gh.PullRequest, section_default: str | None) -> str | None:
        """Pick the label color (hex) for an individual PR by priority of states."""
        c = self._cfg.colors
        details = self._prs.details.get(pr.url)
        if details:
            if details.review_decision == "CHANGES_REQUESTED" and c.changes_requested:
                return c.changes_requested
            if details.review_decision == "APPROVED" and c.approved:
                return c.approved
            if details.ci_state == "FAILURE" and c.ci_failure:
                return c.ci_failure
        if pr.is_draft and c.draft:
            return c.draft
        if c.high_priority and self.pr_matches_priority(pr):
            return c.high_priority
        if c.draft and self.is_stale(pr):
            return c.draft  # the muted gray: stale PRs read as dimmed
        return section_default

    def pr_matches_priority(self, pr: gh.PullRequest) -> bool:
        repo = pr.repo.lower()
        return any(
            pat and fnmatch.fnmatchcase(repo, pat.lower())
            for pat in self._cfg.priorities.high_priority_repos
        )

    def has_priority_match(self) -> bool:
        """Turns the icon red: a high-priority repo, or (when enabled) a review
        request waiting too long."""
        long_wait = self._cfg.waiting.long_wait_alert
        return any(
            self.pr_matches_priority(pr) or (long_wait and self.is_long_wait(pr))
            for pr in self.visible_prs()
        )

    # ----- Tray icon -----

    def icon_count(self) -> int:
        """The menu-bar number: review requests addressed to you. Team-only
        requests count when `tray_icon.count_team_requests` is on. Optionally
        adds local repos that are behind/ahead but don't already carry a review
        request (those are counted once, as reviews), only when
        `local_repos.count_in_badge` is on. My open PRs never count."""
        count = self.visible_count()
        if self._cfg.tray_icon.count_team_requests:
            count += self.team_count()
        # Custom sections marked "count": each PR once, even if it's also a
        # review request or in two sections.
        counted = {p.key for p in self.direct_prs()}
        if self._cfg.tray_icon.count_team_requests:
            counted |= {p.key for p in self.team_prs()}
        for section, prs in self.custom_sections():
            if section.count:
                extra = {p.key for p in prs if self._counts(p)} - counted
                count += len(extra)
                counted |= extra
        if self._cfg.local_repos.enabled and self._cfg.local_repos.count_in_badge:
            for st in self._local_repo_rows():
                if not st.review_prs and (st.ahead or st.behind):
                    count += 1
        return count

    def tray_state(self) -> TrayState:
        """The icon's state, shared by both backends (docs/ui-spec.md)."""
        if self._cfg.paused:
            return TrayState(TRAY_PAUSED)
        if self.offline:
            return TrayState(TRAY_OFFLINE)
        if self._last_error or self._setup_problem:
            return TrayState(TRAY_ERROR)
        count = self.icon_count()
        if count <= 0:
            return TrayState(TRAY_IDLE)
        unseen = self._cfg.tray_icon.unseen_dot and bool(self.unseen_keys())
        return TrayState(TRAY_COUNT, count, self.has_priority_match(), unseen)

    def unseen_keys(self) -> set[str]:
        """Counted review requests that arrived since the menu was last opened."""
        counted = [p for p in self.direct_prs() if self._counts(p)]
        if self._cfg.tray_icon.count_team_requests:
            counted += self._counted_team_prs()
        return self._prs.unseen({p.key for p in counted})

    def menu_opened(self) -> None:
        """The backend saw the menu open: everything in it is now seen.
        Only the icon changes; the open menu is left alone."""
        if not self.unseen_keys():
            return
        self._prs.mark_all_seen()
        self._persist_state()
        self._ui.refresh_icon()

    def _refresh_ui(self) -> None:
        self._ui.refresh_icon()
        self._ui.rebuild_menu()

    # ----- Notifications and sounds (quiet hours gate) -----

    def _tell(self, notice: Notice) -> None:
        """The answer to something the user just did (alerts.py)."""
        self._alerts.tell(notice)

    def _notify(self, notice: Notice) -> None:
        """News: muted in quiet hours, with its sound (alerts.py)."""
        self._alerts.notify(notice)

    # ----- Timers and workers -----

    def _reschedule_timers(self) -> None:
        self._timers.reschedule(
            self._cfg,
            on_prs=self.tick_prs,
            on_fetch=self.tick_fetch,
            on_sweep=self._tick_sweep,
            on_account=self.tick_account,
        )

    def _initial_kick(self) -> None:
        if not self._cfg.paused:
            self.tick_prs()
            self.tick_fetch()
            self.tick_account()

    def tick_account(self) -> bool:
        """Unread notifications and AI usage, each its own background job (a
        slow CLI never holds up the inbox)."""
        self.inbox.tick()
        self.usage.tick_ai()
        return True

    def tick_prs(self) -> bool:
        """Poll GitHub on a worker thread; the result comes back through
        `apply_poll_result` on the main thread."""
        cfg = self._cfg
        self._runner.background(lambda: pr_sync.poll(cfg), self.apply_poll_result)
        self.usage.tick_status()
        return True

    def tick_fetch(self) -> bool:
        """Fetch every clone on a worker thread (→ `apply_fetch_results`)."""
        cfg = self._cfg
        self._runner.background(lambda: repo_sync.fetch_all(cfg), self.apply_fetch_results)
        return True

    def _tick_sweep(self) -> bool:
        if self.snoozes.sweep_expired():
            self._refresh_ui()
        self.run_nudges()  # every minute: the digest goes out on time
        return True

    def _refresh_details(self, urls: list[str]) -> None:
        """Refetch these PRs' details now (a snooze needs the head commit, a
        re-run changes the checks)."""
        self._runner.background(functools.partial(_fetch_details, urls), self._apply_pr_details)

    # ----- Poll results (main thread) -----

    def apply_poll_result(self, result: PollResult) -> bool:
        """Take a poll's result: update the lists, notify what's new, apply
        the error policy and refresh the UI. Main thread only; returns False
        so it can be posted as a one-shot idle callback."""
        if result.setup_problem:
            self._apply_setup_problem(result.setup_problem)
            return False

        self._last_error = result.total_failure or self._fetch_outage
        self._setup_problem = None
        self._host_errors = dict(result.host_errors)
        self._maybe_open_prefs_on_first_run()

        if result.total_failure:
            # Preserve last-known PR state — a failed query is not "no PRs".
            # Without this guard, auto-pause would reconstruct the menu from
            # an empty list and look like awaitray forgot everything.
            self._consecutive_failures += 1
        else:
            self._take_prs(result)

        self.snoozes.sweep_expired()
        self._last_status_text = pr_sync.status_text(
            result, self.visible_count(), self._consecutive_failures
        )
        if result.total_failure:
            self._handle_total_failure(result.errors, source="prs")

        self._refresh_ui()
        self._alerts.count_changed(self.icon_count())
        self.run_nudges()
        return False

    def _take_prs(self, result: PollResult) -> None:
        news = self._prs.take_poll(
            self._cfg,
            result,
            fetch_outage=self._fetch_outage,
            multi_account=self.multi_account,
            quiet=qh_mod.is_quiet(self._cfg.quiet_hours),
        )
        for notice in news.notices:
            self._notify(notice)
        if news.new_requests:  # one tier sound per batch, in "every" mode
            self._alerts.new_requests(self.icon_count())

        if not result.errors:
            self._consecutive_failures = 0
            self._timers.cancel_retry()
        self.snoozes.wake_on_commit(result.details)
        self._showing_cached = False
        self._last_success_at = datetime.now()
        self.snoozes.wake_on_activity([*self._prs.review_prs, *self._prs.my_prs])
        self._persist_state()

    def is_watching_checks(self, key: str) -> bool:
        return key in self._prs.watching

    def toggle_watch_checks(self, key: str) -> None:
        """ "Notify when checks finish": checked on the next regular poll,
        never sooner."""
        watching = self._prs.toggle_watch(key)
        log_mod.info(f"{'watching' if watching else 'unwatched'} checks of {key}")
        self._persist_state()
        self._ui.rebuild_menu()

    def run_nudges(self, now: datetime | None = None) -> None:
        """Reminders and the daily digest. Only on fresh data (not
        the cached lists shown at startup), and never during quiet hours:
        whatever is due then goes out on the first check after them."""
        if self._last_success_at is None or self._showing_cached:
            return
        if qh_mod.is_quiet(self._cfg.quiet_hours):
            return
        now = now or datetime.now()

        eligible = self.direct_prs()
        if self._cfg.notify_team_requests:
            eligible += self.team_prs()
        long_waiting = [p for p in eligible if self._counts(p) and self.is_long_wait(p)]
        due, changed = self._prs.due_reminders(self._cfg, long_waiting, now)
        for notice in nudges.reminder_notices(due, self._cfg.waiting.long_wait_days) if due else []:
            self._notify(notice)

        if self._prs.digest_due(self._cfg, now):
            digest = nudges.digest_notice(self._digest_counts())
            if digest is not None:
                self._notify(digest)
            self._prs.mark_digest(now)
            changed = True

        if changed:
            self._persist_state()

    def _digest_counts(self) -> nudges.DigestCounts:
        counted = [p for p in self.direct_prs() if self._counts(p)]
        mine = self._prs.my_prs if self._cfg.my_prs.enabled else []
        sections = [pr_state.classify_mine(p, self._prs.details.get(p.url)) for p in mine]
        return nudges.DigestCounts(
            reviews=len(counted),
            long_waiting=sum(1 for p in counted if self.is_long_wait(p)),
            long_wait_days=self._cfg.waiting.long_wait_days,
            needs_action=sections.count(pr_state.NEEDS_ACTION),
            ready_to_merge=sections.count(pr_state.READY_TO_MERGE),
        )

    def _apply_setup_problem(self, problem: str) -> None:
        self._setup_problem = problem
        self._last_error = True
        self._last_status_text = SETUP_MESSAGES[problem]
        log_mod.warn(f"setup: {self._last_status_text}")
        self._maybe_open_prefs_on_first_run()
        self._refresh_ui()

    def _maybe_open_prefs_on_first_run(self) -> None:
        """On a brand-new install that can't poll, open Preferences once."""
        if self._first_run_prefs_shown or not cfg_mod.created_fresh:
            return
        if self._setup_problem or (self.auth_failed_hosts and not self._prs.review_prs):
            self._first_run_prefs_shown = True
            self._ui.present_prefs()

    def _apply_pr_details(self, details: dict[str, gh.PRDetails]) -> bool:
        """Main-thread callback for the single-PR `gh pr view` fallback."""
        self._prs.details.update(details)
        self.snoozes.wake_on_commit(details)
        self._ui.rebuild_menu()
        return False

    # ----- Persisted state (baselines + last PR lists) -----

    def _restore_state(self) -> None:
        """Start from the last run: baselines, and PR lists to render now.

        Nothing here notifies; the next poll compares against these baselines,
        so PRs that arrived while the app was off do notify, and known ones
        don't. If the settings that decide which PRs are visible changed since
        (hosts, filters, My PRs), the snapshot is ignored and the first poll
        takes a silent baseline instead.
        """
        snap = self._state.load()
        if snap is None:
            return
        if snap.scope != state_mod.scope_signature(self._cfg):
            log_mod.info("state: settings changed since last run; starting fresh")
            return
        self._prs = pr_store.PrState.from_snapshot(snap, self._cfg)
        self._showing_cached = True
        visible = self.visible_count()
        when = snap.saved_at_dt
        stamp = when.astimezone().strftime("%H:%M") if when else "last run"
        self._last_status_text = (
            f"{visible} PR{'s' if visible != 1 else ''} need review (from {stamp}, refreshing…)"
        )

    def _persist_state(self) -> None:
        self._state.save(self._prs.snapshot(self._cfg))

    # ----- Error policy -----

    def _handle_total_failure(self, errors: list[str], *, source: str) -> None:
        if source == "fetch":
            counter, retry, label = (
                self._consecutive_fetch_failures,
                self._retry_fetch,
                "repo fetches",
            )
        else:
            counter, retry, label = self._consecutive_failures, self._retry_prs, "PR checks"
        action = scheduler_mod.on_total_failure(self._cfg.error_policy, counter, self._cfg.paused)
        if action is None:
            return
        if action.pause:
            self._auto_pause(errors, label=label)
        elif action.retry_in is not None:
            self._timers.retry(action.retry_in, retry)
            log_mod.info(f"scheduled {label} retry in {action.retry_in}s (attempt {counter})")

    def _retry_prs(self) -> None:
        if not self._cfg.paused:
            self.tick_prs()

    def _retry_fetch(self) -> None:
        if not self._cfg.paused:
            self.tick_fetch()

    def _auto_pause(self, errors: list[str], *, label: str) -> None:
        self._cfg.paused = True
        self._last_error = False  # paused trumps error in icon priority
        cfg_mod.save(self._cfg)
        self._reschedule_timers()
        self._refresh_ui()
        self._notify(
            Notice(
                "awaitray paused",
                f"Auto-paused after consecutive failed {label}.\n" + "; ".join(errors[-2:]),
                "critical",
                "dialog-warning",
                sound=sounds.BAD,
            )
        )
        log_mod.warn(f"auto-paused after consecutive {label} failures")

    # ----- Fetch results (main thread) -----

    def apply_fetch_results(self, results: list[repo_mod.FetchResult]) -> bool:
        """Take a fetch round: notify, track outages, refresh. Main thread only."""
        if not results:
            return False
        self._fetch_results = list(results)
        for notice in repo_sync.fetch_notices(self._cfg, results):
            self._notify(notice)

        failed = [r for r in results if not r.ok]
        if len(failed) == len(results):
            # Every attempted fetch failed → treat as a network outage and
            # apply the same error policy as a total PR-host failure.
            self._consecutive_fetch_failures += 1
            self._fetch_outage = True
            self._last_error = True
            errors = [f"{r.name}: {r.message}" for r in failed]
            self._last_status_text = (
                f"Fetch failed for all {len(results)} repo(s) "
                f"({self._consecutive_fetch_failures}× in a row): " + "; ".join(errors[:2])
            )
            self._refresh_ui()
            self._handle_total_failure(errors, source="fetch")
        else:
            if self._fetch_outage or self._consecutive_fetch_failures:
                log_mod.info("fetch recovered")
                self._fetch_outage = False
                if self._consecutive_failures == 0:
                    self._last_error = False
            self._consecutive_fetch_failures = 0
            # Refresh so the Local repos section + badge reflect new ahead/behind.
            self._refresh_ui()
        self._alerts.count_changed(self.icon_count())
        return False

    # ----- Local repos section -----

    def _local_repo_rows(self) -> list[LocalRepoStatus]:
        return repo_sync.local_repo_rows(self._cfg, self._fetch_results, self.visible_prs())

    def local_repos(self) -> list[LocalRepoStatus]:
        """The 'Local repos' section rows, with display preferences applied."""
        if not self._cfg.local_repos.enabled:
            return []
        return repo_sync.shown_rows(self._cfg.local_repos, self._local_repo_rows())

    def local_repos_section_title(self) -> str:
        return (
            "Local repos · to review" if self._cfg.local_repos.only_with_review else "Local repos"
        )

    def repo_status_text(self, st: LocalRepoStatus) -> str:
        return repo_sync.status_text(st)

    # ----- Pause and preferences -----

    def toggle_pause(self) -> None:
        self._cfg.paused = not self._cfg.paused
        cfg_mod.save(self._cfg)
        self._reschedule_timers()
        self._refresh_ui()
        log_mod.info("paused" if self._cfg.paused else "resumed")
        if not self._cfg.paused:
            self._consecutive_failures = 0
            self._consecutive_fetch_failures = 0
            self._fetch_outage = False
            self.tick_prs()
            self.tick_fetch()

    def on_prefs_saved(self, new_cfg: cfg_mod.Config) -> None:
        old = self._cfg
        self._cfg = new_cfg
        repo_mod.invalidate_discover_cache()
        self._reschedule_timers()
        self._refresh_ui()
        # Only re-fetch when something fetch-relevant changed. Cosmetic edits
        # (colors, editor command, notifications toggles) shouldn't blank the
        # menu while a new query is in flight.
        if old.repos != new_cfg.repos or old.local_repos != new_cfg.local_repos:
            self.tick_fetch()
        scope_changed = (
            _accounts(old) != _accounts(new_cfg)
            or old.filters != new_cfg.filters
            or old.my_prs != new_cfg.my_prs
        )
        if scope_changed:
            # The set of PRs we can see changed (new host, un-ignored author…):
            # what shows up next isn't "new", so re-baseline silently.
            self._prs.known_review = None
            self._prs.known_mine = None
        if scope_changed or old.intervals != new_cfg.intervals or old.sections != new_cfg.sections:
            self.tick_prs()
        if (
            _accounts(old) != _accounts(new_cfg)
            or old.account_info != new_cfg.account_info
            or old.ai_usage != new_cfg.ai_usage
        ):
            self.tick_account()


def _accounts(cfg: cfg_mod.Config) -> list[tuple[str, str, bool]]:
    """What in `hosts` decides which PRs a poll sees (a browser doesn't)."""
    return [(h.hostname, h.user, h.enabled) for h in cfg.hosts]


# ----- Blocking jobs (run through the Runner, off the main thread) -----


def _fetch_details(urls: list[str]) -> dict[str, gh.PRDetails]:
    details: dict[str, gh.PRDetails] = {}
    for url in urls:
        try:
            details[url] = gh.pr_details(url)
        except gh.GhError as e:
            log_mod.warn(f"pr_details failed for {url}: {e}")
    return details
