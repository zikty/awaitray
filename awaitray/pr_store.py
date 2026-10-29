"""The PRs as the app knows them, and what it has already told you.

`PrState` holds the last lists (review requests, your PRs, custom sections),
their details, the notification baselines and the bookkeeping behind every
"only once" rule: transition keys sent, checks being watched, PRs seen in an
opened menu, reminder timestamps, the digest date. It persists as a
`state.Snapshot`.

It does no I/O and never writes the config: applying a poll returns the
notices to send, and the controller decides whether (quiet hours) and how.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import config as cfg_mod
from . import filters as filters_mod
from . import github as gh
from . import log as log_mod
from . import nudges, pr_sync, transitions
from . import state as state_mod
from .notice import Notice
from .pr_sync import PollResult
from .sounds import BAD, GOOD


@dataclass
class PollNews:
    """What a poll means for the user."""

    notices: list[Notice] = field(default_factory=list)
    new_requests: list[gh.PullRequest] = field(default_factory=list)  # for "every" sounds


@dataclass
class PrState:
    review_prs: list[gh.PullRequest] = field(default_factory=list)
    my_prs: list[gh.PullRequest] = field(default_factory=list)
    section_prs: dict[str, list[gh.PullRequest]] = field(default_factory=dict)  # by sid
    details: dict[str, gh.PRDetails] = field(default_factory=dict)  # by URL

    # Keys seen on the previous successful poll. None = no baseline yet: the
    # next poll records one silently instead of notifying. Distinct from an
    # empty set, which means "the queue was empty" and must notify on 0 -> 1.
    known_review: set[str] | None = None
    known_mine: set[str] | None = None
    known_sections: dict[str, set[str]] = field(default_factory=dict)  # no entry = no baseline

    my_facts: dict[str, transitions.Facts] = field(default_factory=dict)  # by PR key
    sent: set[str] = field(default_factory=set)  # transition notices sent (sent_key)
    watching: set[str] = field(default_factory=set)  # "notify when checks finish"
    # Review requests seen in an opened menu; None = the next poll's.
    seen: set[str] | None = None
    reminded: dict[str, str] = field(default_factory=dict)  # last reminder or first sight
    digest_date: str | None = None  # day the last digest went out

    # ----- persistence -----

    @classmethod
    def from_snapshot(cls, snap: state_mod.Snapshot, cfg: cfg_mod.Config) -> PrState:
        mine = cfg.my_prs.enabled
        st = cls(
            review_prs=pr_sync.sort_prs(snap.review_prs),
            my_prs=pr_sync.sort_prs(snap.my_prs) if mine else [],
            known_review=snap.review_keys,
            known_mine=snap.my_keys if mine else None,
            known_sections={k: set(v) for k, v in snap.section_keys.items()},
            seen=snap.seen,
            reminded=dict(snap.reminded),
            digest_date=snap.digest_date,
        )
        if mine:
            st.my_facts = dict(snap.my_facts)
            st.sent = set(snap.sent)
            st.watching = set(snap.watching)
        live = {p.url for p in (*st.review_prs, *st.my_prs)}
        st.details = {u: d for u, d in snap.details.items() if u in live}
        return st

    def snapshot(self, cfg: cfg_mod.Config) -> state_mod.Snapshot:
        return state_mod.Snapshot(
            scope=state_mod.scope_signature(cfg),
            saved_at=state_mod.now_iso(),
            review_keys=self.known_review,
            my_keys=self.known_mine,
            review_prs=list(self.review_prs),
            my_prs=list(self.my_prs),
            details=dict(self.details),
            my_facts=dict(self.my_facts),
            sent=set(self.sent),
            watching=set(self.watching),
            seen=set(self.seen) if self.seen is not None else None,
            reminded=dict(self.reminded),
            digest_date=self.digest_date,
            section_keys={k: set(v) for k, v in self.known_sections.items()},
        )

    # ----- lookups -----

    def all_section_prs(self) -> list[gh.PullRequest]:
        return [pr for prs in self.section_prs.values() for pr in prs]

    def pr_for_key(self, key: str) -> gh.PullRequest | None:
        for pr in (*self.review_prs, *self.my_prs, *self.all_section_prs()):
            if pr.key == key:
                return pr
        return None

    # ----- a poll -----

    def take_poll(
        self,
        cfg: cfg_mod.Config,
        result: PollResult,
        *,
        fetch_outage: bool,
        multi_account: bool,
        quiet: bool,
    ) -> PollNews:
        """Apply a successful poll (not a total failure).

        `fetch_outage`: every git fetch is failing, so an empty review list is
        more likely a network artifact (SSO/IP allowlists) than the truth:
        keep the previous one. `quiet`: quiet hours, so a finished check
        watch waits for later.
        """
        news = PollNews()
        review_prs = filters_mod.filter_prs(result.review_prs, cfg.filters)
        my_prs = pr_sync.sort_prs(
            filters_mod.filter_prs(result.my_prs, pr_sync.my_prs_filters(cfg))
        )
        if fetch_outage and not review_prs and self.review_prs:
            self.my_prs = my_prs
            log_mod.info("preserving previous PR list during fetch outage")
        else:
            self._take_review(cfg, review_prs, result, multi_account, news)
            self.my_prs = my_prs
            self._take_mine(cfg, my_prs, result.my_failed_hosts, news)
            self._take_sections(cfg, result, multi_account, news)
            self._track_my_changes(cfg, my_prs, result.details, result.my_failed_hosts, quiet, news)

        # Prune against what's on screen, not the raw result: during a fetch
        # outage the previous review list is kept and must keep its details.
        live = {p.url for p in (*self.review_prs, *self.my_prs, *self.all_section_prs())}
        self.details = {u: d for u, d in self.details.items() if u in live}
        self.details.update(result.details)  # they come with the search itself
        return news

    def _take_review(
        self,
        cfg: cfg_mod.Config,
        prs: list[gh.PullRequest],
        result: PollResult,
        multi_account: bool,
        news: PollNews,
    ) -> None:
        previous = self.known_review
        current = {p.key for p in prs}
        self.review_prs = pr_sync.sort_prs(prs)
        self.known_review = pr_sync.next_baseline(previous, current, result.failed_hosts)
        # First poll: everything is seen. Later, keep "seen" to what's still
        # there (a re-requested PR is new again), and a failed host's keys.
        if self.seen is None:
            self.seen = set(current)
        else:
            self.seen = pr_sync.next_baseline(self.seen, self.seen & current, result.failed_hosts)
        if cfg.notify_on_new_pr and previous is not None:
            new = pr_sync.new_review_requests(cfg, prs, current - previous)
            news.notices += pr_sync.review_request_notices(new, multi_account)
            news.new_requests = new

    def _take_mine(
        self,
        cfg: cfg_mod.Config,
        prs: list[gh.PullRequest],
        failed_hosts: set[str],
        news: PollNews,
    ) -> None:
        previous = self.known_mine
        current = {p.key for p in prs}
        # Not queried when off: no baseline, so re-enabling starts silently.
        self.known_mine = (
            pr_sync.next_baseline(previous, current, failed_hosts) if cfg.my_prs.enabled else None
        )
        if cfg.notify_my_prs and previous is not None:
            news.notices += pr_sync.my_pr_notices([p for p in prs if p.key not in previous])

    def _take_sections(
        self, cfg: cfg_mod.Config, result: PollResult, multi_account: bool, news: PollNews
    ) -> None:
        section_prs: dict[str, list[gh.PullRequest]] = {}
        baselines: dict[str, set[str]] = {}
        for section in cfg.sections:
            if not section.enabled:
                continue
            sid = section.sid
            prs = pr_sync.sort_prs(
                filters_mod.filter_prs(result.section_prs.get(sid, []), cfg.filters)
            )
            section_prs[sid] = prs
            previous = self.known_sections.get(sid)
            current = {p.key for p in prs}
            failed = result.failed_hosts | result.section_failed_hosts.get(sid, set())
            baselines[sid] = pr_sync.next_baseline(previous, current, failed)
            if section.notify and previous is not None:
                new = [p for p in prs if p.key not in previous and p.key not in cfg.snoozed]
                news.notices += pr_sync.section_notices(section.title, new, multi_account)
        self.section_prs = section_prs  # removed or disabled sections drop out
        self.known_sections = baselines

    def _track_my_changes(
        self,
        cfg: cfg_mod.Config,
        prs: list[gh.PullRequest],
        details: dict[str, gh.PRDetails],
        failed_hosts: set[str],
        quiet: bool,
        news: PollNews,
    ) -> None:
        """State changes of your PRs: approved, CI failed, conflict…."""
        if not cfg.my_prs.enabled:
            self.my_facts, self.sent, self.watching = {}, set(), set()
            return
        # A host that failed this round keeps its PRs' facts for next time.
        prefixes = tuple(f"{h}:" for h in failed_hosts)
        facts = {k: f for k, f in self.my_facts.items() if prefixes and k.startswith(prefixes)}
        toggles = cfg.my_pr_events
        for pr in prs:
            prev = self.my_facts.get(pr.key)
            det = details.get(pr.url)
            if det is None:
                if prev is not None:
                    facts[pr.key] = prev
                continue
            cur = transitions.facts(pr, det, prev)
            facts[pr.key] = cur
            self._check_watch(pr, det, cur.ready, quiet, news)
            if pr.key in cfg.snoozed:
                continue
            for event in transitions.events(prev, cur):
                key = transitions.sent_key(pr, event, cur.head_oid)
                if key in self.sent or not getattr(toggles, event):
                    continue
                self.sent.add(key)
                news.notices.append(transitions.notice(pr, event))
        self.my_facts = facts
        self.sent = {k for k in self.sent if k.split("|", 1)[0] in facts}
        self.watching &= set(facts)  # merged or closed: nothing left to watch

    # ----- "notify when checks finish" -----

    def toggle_watch(self, key: str) -> bool:
        """Returns whether the PR is watched now."""
        self.watching ^= {key}
        return key in self.watching

    def _check_watch(
        self, pr: gh.PullRequest, det: gh.PRDetails, ready: bool, quiet: bool, news: PollNews
    ) -> None:
        if pr.key not in self.watching or det.ci_state not in ("SUCCESS", "FAILURE"):
            return  # a new push while watching just keeps watching its checks
        # The watch notice says it all: don't also send the generic CI change,
        # or "ready to merge" when the checks were the last thing missing.
        for event in (transitions.CI_FAILED, transitions.CI_PASSED, transitions.READY_TO_MERGE):
            self.sent.add(transitions.sent_key(pr, event, det.head_oid))
        if quiet:
            return  # keep watching: it notifies on the first poll after quiet hours
        self.watching.discard(pr.key)
        passed = det.ci_state == "SUCCESS"
        what = "✗ Checks failed"
        if passed:
            what = "✓ Checks passed, ready to merge" if ready else "✓ Checks passed"
        news.notices.append(
            Notice(
                f"{what}: {pr.repo} #{pr.number}",
                pr.title,
                "low" if passed else "normal",
                sound=GOOD if passed else BAD,
                event="checks_finished",
            )
        )

    # ----- unseen dot -----

    def unseen(self, counted: set[str]) -> set[str]:
        return set() if self.seen is None else counted - self.seen

    def mark_all_seen(self) -> None:
        self.seen = (self.seen or set()) | {p.key for p in self.review_prs}

    # ----- reminders and digest -----

    def due_reminders(
        self, cfg: cfg_mod.Config, long_waiting: list[gh.PullRequest], now: datetime
    ) -> tuple[list[gh.PullRequest], bool]:
        """Reminders due now (already recorded as sent) and whether the
        bookkeeping changed. First sight counts as a reminder: the first real
        one comes a cooldown later, never next to "new review request" or as
        a burst right after an install, an upgrade or a settings change."""
        stamp = now.isoformat(timespec="seconds")
        live = {p.key for p in self.review_prs}
        reminded = {k: v for k, v in self.reminded.items() if k in live}
        reminded.update({k: stamp for k in live - reminded.keys()})
        changed = reminded != self.reminded
        self.reminded = reminded
        due = nudges.reminders_due(cfg, long_waiting, self.reminded, now)
        if due:
            self.reminded.update({p.key: stamp for p in due})
            changed = True
        return due, changed

    def digest_due(self, cfg: cfg_mod.Config, now: datetime) -> bool:
        return nudges.digest_due(cfg.digest, now, self.digest_date)

    def mark_digest(self, now: datetime) -> None:
        self.digest_date = now.date().isoformat()
