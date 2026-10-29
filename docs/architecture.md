# Architecture

awaitray is a cross-platform tray / menu-bar helper with two jobs:

1. Poll GitHub (any host) for pull requests awaiting your review, and your
   own PRs' state.
2. Run `git fetch` periodically on a list of local repositories.

It runs on **Linux** (GTK 3 + AppIndicator) and **macOS** (PyObjC menu
bar). Everything is configured from the GUI; settings persist at
`~/.config/awaitray/config.json`. awaitray stores no tokens: auth is
delegated to the `gh` CLI.

This document is the reference for how the code is organised and why. The
hard rules and the day-to-day workflow (running, testing, pull requests)
are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Stack

- **Shared core**: Python 3, no toolkit imports. The `controller` is the
  composition root and owns the PR poll / fetch orchestration and the error
  policy; the features under `features/` and the core modules (`config`,
  `github`, `repos`, `filters`, `snooze`, `quiet_hours`, `log`…) are reused
  by both backends unchanged.
- **Linux backend**: GTK 3 via PyGObject (`gi`) + `AyatanaAppIndicator3`,
  `notify-send` for notifications. Zero PyPI deps.
- **macOS backend**: pure **PyObjC** — `NSStatusItem` menu bar + `NSWindow`
  preferences (`NSTabView`), `osascript` for notifications. No rumps. Needs
  `pip install pyobjc-framework-Cocoa` (see `requirements-macos.txt`), run
  from a venv.
- Backend is chosen at runtime by `backends.select()` (by `sys.platform`).
- `gh` CLI as the GitHub client (multi-host, OAuth, rate limits handled).
- `git` CLI as a subprocess for fetches.

## Module layout

```
awaitray/
├── __main__.py        entry point; runs backends.select().run()
├── controller.py      the composition root and the facade the backends and the
│                      menu model talk to: builds the features, owns the PR poll
│                      and fetch orchestration (`apply_poll_result(PollResult)`,
│                      `apply_fetch_results`), the error policy, persistence,
│                      the tray state and the action registry. Talks to the GUI
│                      via a UIPort (refresh_icon, rebuild_menu, notify,
│                      present_prefs) and schedules via a HostPort
│                      (timeout_add_seconds / source_remove / run_on_main /
│                      default_opener) the backend implements.
├── features/          each owns its state, menu actions and background work,
│                      and sees the app only through a `Context` (context.py):
│                      links (browser per account, editor, clone), snoozes,
│                      pr_actions (re-run, Copilot review), inbox (triage),
│                      usage (AI quotas + GitHub status). Reached as
│                      `ctl.links`, `ctl.snoozes`, `ctl.pr_actions`,
│                      `ctl.inbox`, `ctl.usage`
├── alerts.py          notifications and sounds: quiet-hours gate, one sound per
│                      SOUND_GAP_SECONDS, tray-icon tier sounds
├── pr_store.py        PrState: the PR lists, details and every "notify once"
│                      record (baselines, sent transitions, watches, seen,
│                      reminders, digest); take_poll() returns the notices,
│                      persists as a state.Snapshot. No I/O, no config writes
├── pr_sync.py         poll() (worker) → PollResult; baselines, which PRs are new,
│                      the notices they produce, the status line (pure)
├── repo_sync.py       fetch_all()/clone() (worker); expanded repos, Local repos
│                      rows, editor matching, fetch notices (pure)
├── runner.py          Runner: background(job, then) — every blocking job runs
│                      through it (ThreadRunner in the app, ImmediateRunner
│                      in tests)
├── scheduler.py       Scheduler: every timer handle (periodic + one retry);
│                      on_total_failure(): the error policy as a pure decision
├── notice.py          Notice: a notification as data (sync modules return them)
├── browser.py         which browser opens a link: the PR's account's, the
│                      default one, or the system's; argv per platform, launch
├── browsers.py        installed browsers + their profiles, offered in
│                      Preferences (plists / .desktop files, Local State,
│                      profiles.ini; pure file reads)
├── account_info.py    unread notifications per account, every 30 min on a
│                      worker (gh.rest_get)
├── ai_usage.py        AI assistants' quota left: PROVIDERS registry (Copilot
│                      via gh, Codex via `codex app-server` JSON-RPC,
│                      Antigravity via `agy -p /usage`), a Usage of Windows
│                      each; menu lines + a low-quota warning
├── inbox.py          unread threads (parse) + triage: read, done, unsubscribe,
│                      mark all read up to when they were read (gh.rest)
├── pr_actions.py     one-click fixes on your PRs: re-run failed Actions jobs,
│                      request a Copilot review (gh.rest, on a worker)
├── github_status.py   githubstatus.com incidents/degraded parts (public
│                      Statuspage JSON, read with each PR poll; no API calls)
├── sounds.py          sound packs: a slot per Notice (review/good/bad/info),
│                      built-in packs synthesized to WAV in the cache, your
│                      own packs in ~/.config/awaitray/sounds/<name>/
├── transitions.py     your PRs' state changes: Facts per poll, events,
│                      once-per-commit dedup keys (pure)
├── nudges.py          reminders for long waits and the daily digest
│                      (pure decisions; the controller keeps the timestamps)
├── config.py          dataclasses + JSON load/save (additive: preserves unknown keys)
├── menu_model.py      the tray menu as immutable data (docs/ui-spec.md):
│                      build(MenuState) is pure, the controller takes the
│                      snapshot (menu_state); rendered by each backend; clicks
│                      come back as Actions → Controller.perform
├── pr_state.py        sections/glyphs/labels/age for a PR (pure functions)
├── prefs_model.py     Preferences as data: 6 sections → groups → fields,
│                      validation, EditSession (working copy + debounced
│                      commit); both prefs windows render it
├── state.py           runtime state in ~/.cache/awaitray/state.json: notification
│                      baselines + last PR lists/details (not settings)
├── backup.py          portable settings export/import (paths as ~/…, git remote
│                      URLs to re-clone), automatic snapshots in config/backups
├── cli.py             `python -m awaitray <command>`: status (reads state.json,
│                      never polls), config export/import/snapshot(s)/path
├── github.py          gh CLI wrapper: one GraphQL search per host/section
│                      returns PRs + details (CI, review, head oid,
│                      mergeable); `pr view` only as a single-PR fallback
├── repos.py           git fetch + ahead/behind + auto-discover of subdirs
├── filters.py         fnmatch-based PR filtering
├── snooze.py          snooze entries (time, commit, activity, ignore) and the
│                      menu's CHOICES / entry_for
├── quiet_hours.py     time-window check (wraps midnight)
├── text.py            clean(): drops control/bidi characters from text that
│                      others wrote (titles, logins, check and incident names)
├── log.py             in-memory ring buffer (200 entries) + stderr mirror
└── backends/
    ├── __init__.py    select() → linux or macos backend module (has run())
    ├── linux/         GTK indicator (app.py), menu renderer (menu.py),
    │                  icon_renderer (SVG), notifier (notify-send),
    │                  ui/prefs (sidebar window rendered from prefs_model)
    └── macos/         PyObjC NSStatusItem (app.py), menu renderer (menu.py),
                       status_icon (SF Symbol), notifier (osascript),
                       ui/prefs_window (toolbar-style Settings from prefs_model)
```

Each backend's `app.py` implements the controller's `UIPort` + `HostPort`
and owns its event loop. **The menu is shared**: `Controller.menu_model()`
returns a `menu_model.Menu` and each backend only renders it
(`backends/linux/menu.py`, `backends/macos/menu.py`), skipping the rebuild
when its signature didn't change. **Preferences are shared too**:
`prefs_model` defines every section and field, and each backend renders
simple fields generically; only the custom editors (hosts, repos, tray-icon
tiers, backup, logs) exist once per backend and **must be mirrored in both**.

`install.sh` / `update.sh` / `uninstall.sh` / `run.sh` are the Linux scripts
(`.desktop` / systemd autostart). `install-macos.sh` / `uninstall-macos.sh` /
`run-macos.sh` are the macOS scripts (venv + `com.awaitray.agent` LaunchAgent).

## Module graph

Generated from the imports by `scripts/arch_graph.py`: CI fails when it's
out of date. The layer rules it shows are checked by
`tests/test_architecture.py`:

- the core (everything outside `backends/`) imports no backend and no
  toolkit (`gi`, `AppKit`, `Foundation`, `objc`); only `backends.select()`
  picks a backend, and a backend never imports the other;
- `menu_model` and `prefs_model` don't import the controller;
- a feature module imports neither another feature nor the controller: it
  gets what it needs through its `Context`;
- only `github`, `repos`, `repo_sync`, `ai_usage`, `browser`, `backup`,
  `runner`, `log` (its lock) and `features.links` (it launches the editor)
  start processes or threads.

<!-- arch-graph:begin (generated by scripts/arch_graph.py; don't edit) -->
```mermaid
flowchart LR
  subgraph core
    account_info["account_info"]
    ai_usage["ai_usage"]
    alerts["alerts"]
    backup["backup"]
    browser["browser"]
    browsers["browsers"]
    cli["cli"]
    config["config"]
    controller["controller"]
    filters["filters"]
    github["github"]
    github_status["github_status"]
    icon["icon"]
    inbox["inbox"]
    log["log"]
    menu_model["menu_model"]
    notice["notice"]
    nudges["nudges"]
    pr_actions["pr_actions"]
    pr_state["pr_state"]
    pr_store["pr_store"]
    pr_sync["pr_sync"]
    prefs_model["prefs_model"]
    quiet_hours["quiet_hours"]
    repo_sync["repo_sync"]
    repos["repos"]
    runner["runner"]
    scheduler["scheduler"]
    snooze["snooze"]
    sounds["sounds"]
    state["state"]
    text["text"]
    transitions["transitions"]
  end
  subgraph features
    features["features"]
    features_context["features.context"]
    features_inbox["features.inbox"]
    features_links["features.links"]
    features_pr_actions["features.pr_actions"]
    features_snoozes["features.snoozes"]
    features_usage["features.usage"]
  end
  subgraph backends
    backends["backends"]
    backends_linux["backends.linux"]
    backends_macos["backends.macos"]
  end
  account_info --> config
  account_info --> github
  account_info --> inbox
  account_info --> log
  ai_usage --> config
  ai_usage --> github
  ai_usage --> log
  alerts --> config
  alerts --> icon
  alerts --> log
  alerts --> notice
  alerts --> quiet_hours
  alerts --> sounds
  backends --> backends_linux
  backends --> backends_macos
  backends_linux --> backup
  backends_linux --> browsers
  backends_linux --> config
  backends_linux --> controller
  backends_linux --> github
  backends_linux --> icon
  backends_linux --> log
  backends_linux --> menu_model
  backends_linux --> pr_sync
  backends_linux --> prefs_model
  backends_linux --> repos
  backends_linux --> sounds
  backends_macos --> backup
  backends_macos --> browsers
  backends_macos --> config
  backends_macos --> controller
  backends_macos --> github
  backends_macos --> icon
  backends_macos --> log
  backends_macos --> menu_model
  backends_macos --> pr_sync
  backends_macos --> prefs_model
  backends_macos --> repos
  backends_macos --> sounds
  backup --> config
  backup --> github
  backup --> log
  backup --> repos
  browser --> config
  browser --> github
  cli --> backup
  cli --> config
  cli --> github
  cli --> snooze
  cli --> state
  config --> backup
  config --> log
  controller --> alerts
  controller --> config
  controller --> features_context
  controller --> features_inbox
  controller --> features_links
  controller --> features_pr_actions
  controller --> features_snoozes
  controller --> features_usage
  controller --> github
  controller --> log
  controller --> menu_model
  controller --> notice
  controller --> nudges
  controller --> pr_state
  controller --> pr_store
  controller --> pr_sync
  controller --> quiet_hours
  controller --> repo_sync
  controller --> repos
  controller --> runner
  controller --> scheduler
  controller --> sounds
  controller --> state
  features_context --> config
  features_context --> github
  features_context --> notice
  features_context --> runner
  features_inbox --> account_info
  features_inbox --> features_context
  features_inbox --> github
  features_inbox --> inbox
  features_inbox --> notice
  features_links --> browser
  features_links --> features_context
  features_links --> github
  features_links --> log
  features_links --> notice
  features_links --> repo_sync
  features_links --> repos
  features_pr_actions --> features_context
  features_pr_actions --> github
  features_pr_actions --> notice
  features_pr_actions --> pr_actions
  features_snoozes --> config
  features_snoozes --> features_context
  features_snoozes --> github
  features_snoozes --> log
  features_snoozes --> snooze
  features_usage --> ai_usage
  features_usage --> features_context
  features_usage --> github_status
  features_usage --> notice
  features_usage --> sounds
  filters --> config
  filters --> github
  github --> config
  github --> text
  github_status --> config
  github_status --> log
  github_status --> text
  icon --> config
  icon --> controller
  inbox --> config
  inbox --> github
  inbox --> text
  menu_model --> account_info
  menu_model --> ai_usage
  menu_model --> config
  menu_model --> github
  menu_model --> github_status
  menu_model --> inbox
  menu_model --> pr_actions
  menu_model --> pr_state
  menu_model --> pr_sync
  menu_model --> quiet_hours
  menu_model --> repo_sync
  menu_model --> snooze
  nudges --> config
  nudges --> github
  nudges --> notice
  nudges --> sounds
  pr_actions --> config
  pr_actions --> github
  pr_actions --> notice
  pr_state --> github
  pr_store --> config
  pr_store --> filters
  pr_store --> github
  pr_store --> log
  pr_store --> notice
  pr_store --> nudges
  pr_store --> pr_sync
  pr_store --> sounds
  pr_store --> state
  pr_store --> transitions
  pr_sync --> config
  pr_sync --> github
  pr_sync --> log
  pr_sync --> notice
  pr_sync --> sounds
  prefs_model --> account_info
  prefs_model --> ai_usage
  prefs_model --> config
  prefs_model --> sounds
  quiet_hours --> config
  repo_sync --> config
  repo_sync --> github
  repo_sync --> log
  repo_sync --> notice
  repo_sync --> repos
  repo_sync --> sounds
  runner --> log
  scheduler --> config
  snooze --> config
  sounds --> config
  state --> config
  state --> github
  state --> log
  state --> transitions
  transitions --> github
  transitions --> notice
  transitions --> pr_state
  transitions --> sounds
```
<!-- arch-graph:end -->

## Packaging

`packaging/` holds the recipes; `docs/releasing.md` the release steps.
`packaging/install-tree.sh` is the one Linux file layout (package in
`/usr/lib/awaitray`, `/usr/bin/awaitray` launcher running the distro
`python3`, desktop entry, icon, systemd user unit), used by
`packaging/deb/build.sh` and the AUR `PKGBUILD`. The Homebrew formula
installs into a virtualenv with PyObjC (the pip-installable package comes
from `pyproject.toml`'s setuptools backend) and supports `brew services`.
The **Packaging** workflow installs all three from the checkout when
`packaging/` changes; **Release** (tag `vX.Y.Z`) publishes the `.deb`.
A new file the app needs at runtime must live inside `awaitray/`.

## Threading model

- The toolkit main loop (`Gtk.main()` / `NSApplication.run()`) runs on the
  main thread.
- Network/IO work (`gh`, `git fetch`, a CLI) goes through the injected
  `Runner`: `runner.background(job, then)` runs `job()` on a daemon thread
  and posts `then(result)` back via `host.run_on_main` — `GLib.idle_add` on
  Linux, `NSOperationQueue.mainQueue().addOperationWithBlock_` on macOS.
  Never start a `threading.Thread` in the controller, and never touch
  widgets from a job. A job that raises is logged and `then` isn't called,
  so a failure a feature must report is a result (e.g. `str | None`).
- Periodic ticks use `host.timeout_add_seconds`. The HostPort preserves GLib
  semantics exactly: a callback returning truthy keeps firing, falsy stops it
  (the macOS host wraps a repeating `NSTimer` and invalidates on falsy), so the
  same controller callbacks work on both backends.

## Design decisions

Each one with its reason: change them only with a better reason.

- **`gh` is the auth boundary.** awaitray never stores or refreshes tokens.
  Adding a host means launching `gh auth login --hostname <host>` in a
  terminal. If `gh` is missing or not logged in, the Accounts tab says so.
  The one exception to "never touches a token" is a host entry with a `user`
  (several accounts per host): `github._account_env` gets that
  account's token from `gh auth token --user` and hands it to the next `gh`
  subprocess in its environment only (GH_TOKEN, or GH_ENTERPRISE_TOKEN +
  GH_HOST for GHES). Never on a command line, in a log, on disk, or kept
  past the search.
- **Accounts, not hosts, are the polling unit.** Errors, rate limits and
  login items are keyed by account id (`user@host`, or the host for its
  active account). PR keys stay `host:repo#n`, so a PR two accounts see is
  merged (`pr_sync.merge_accounts`) and shown once; `failed_hosts` stays per
  hostname, so a failing account keeps its whole host's baseline and nothing
  re-notifies when it comes back.
- **Config schema is additive, at every level.** `config.load()` ignores
  unknown keys (list entries go through `_load_entries`, which also skips
  entries missing required fields instead of crashing), and `config.save()`
  re-reads the file and keeps every key this version doesn't know: top-level,
  inside dataclass sections, inside `hosts`/`repos` entries (matched by
  hostname/path) and inside snoozes. That's what lets a newer config, or an
  imported backup, survive an older version. Removed entries stay removed.
- **An unreadable config is quarantined, never overwritten.** `load()` moves
  it to `config.json.corrupt-<timestamp>`, starts from defaults and sets
  `config.last_load_warning`, which the controller notifies once at startup.
- **Icons are generated on demand.** Each state (idle / count N / paused /
  error) is rendered as an SVG into `~/.cache/awaitray/icons/` and loaded via
  `set_icon_theme_path(dir)` + `set_icon_full(name)`. Passing absolute paths
  to `set_icon_full` is unreliable across AppIndicator forks; don't go back
  to that.
- **One GraphQL query per host and section.** `gh api graphql` with a
  `search(type: ISSUE)` returns the PRs and their details together. Never go
  back to a `gh pr view` per PR: that was N subprocesses + API calls per poll.
  Stick to long-standing schema fields so older GHE versions keep working.
- **Notification baselines are `Optional[set]`.** `None` = take a silent
  baseline on the next poll; an empty set = the queue was empty (0 → 1 must
  notify). Hosts that failed a round keep their previous keys, and changing
  hosts/filters/My PRs in Preferences re-baselines silently.
- **Runtime state is persisted separately from settings.** After each
  successful poll the controller writes `state.json` (baselines, PR lists,
  details) to the cache dir. At startup it restores it: the menu renders
  immediately ("from HH:MM, refreshing…"), nothing is notified from cache, and
  the first poll compares against the saved baselines, so PRs that arrived
  while the app was off notify and known ones don't. The snapshot carries a
  scope hash (hosts + filters + My PRs); if it differs, it's ignored. A
  controller built with an injected config uses `NullStore` unless given a
  store, so tests never touch the real cache.
- **`snoozed` and `paused` live in `config.json`, not `state.json`.** They're
  user decisions, not cache: they must survive a cleared cache and travel in
  a backup. `state.json` only holds what the next poll can rebuild. The menu
  owns them, so Preferences takes them from the live config on commit.
- **More than 3 new review requests in one poll → one summary notification.**
- **Backups are portable, `config.json` isn't rewritten.** Exports write
  paths as `~/…` and record each clone's remote URL; imports expand `~` for
  the new machine. `config.json` keeps absolute paths so older versions still
  work, and `load()` accepts `~` too. `save()` snapshots the file once a day
  and whenever it changed on disk since this process read it (an import or a
  hand edit), so a running app can't silently discard it.
- **Whatever others wrote is data, never code or layout.** PR titles,
  logins, check names, incident names and backups come from other people:
  `text.clean` at parse time, `--` before notify-send's arguments, links
  opened only if they're `http(s)`, re-runs only for the PR's own repo,
  hosts and logins checked on load. A backup never brings commands that run
  on click unless the user opts in, and never clones outside home or into a
  hidden folder.
- **"Total failure" = every enabled host failed.** Partial failures are
  surfaced in the menu header but do *not* trigger the error policy. This
  matters because broken hosts shouldn't pause the app while others still
  work.
- **Error policy: wait | retry | pause.** Retry uses exponential backoff;
  exhausted retries fall back to "wait" until the next regular tick.
- **Quiet hours gate notifications, not timers.** The icon and menu always
  reflect current state; only `_notify` is muted.
- **Filters use fnmatch (glob), not regex.** Easier for users to write
  (`*[bot]`, `*/legacy-*`).
- **Auto-discover is one level deep, not recursive.** `parent/subdir/.git` —
  yes; `parent/foo/bar/.git` — no. Predictable beats clever.
- **Snooze is per PR, four flavors.** Time-based (`until_iso`) auto-expires;
  commit-based (`head_oid`) clears when a poll reports a new head ref;
  activity-based (`updated_at`) clears when the PR's updatedAt moves on;
  `ignored` stays until removed by hand. The menu choices and how each one
  becomes an entry live in `snooze.CHOICES` / `snooze.entry_for`.
- **Renamed from `devtray` (2026-09).** `config.load()` moves a legacy
  `~/.config/devtray/` into place on first run (only if the new dir doesn't
  exist), the app deletes `~/.cache/devtray/` at startup when it only holds
  devtray's icons (`drop_legacy_cache`, never from `load()`: tests and the
  CLI load configs), and the install/update/uninstall scripts on both platforms stop and
  remove the old `devtray.service` / `.desktop` / `com.devtray.agent`. Keep
  that compat code for a while; it's cheap and it's what saves people's config.
  A config without `tray_icon.count_team_requests` predates the direct/team
  split (every save writes it), so `load()` turns it on: devtray counted team
  requests, and an upgrade must not drop them from the icon silently.
- **Links open in the PR's account's browser.** Menu actions carry
  the PR key (`open_url` args: url, key); `browser.commands_for` tries the
  PR's accounts in `cfg.hosts` order, then `browser.command`, then the
  system browser. A browser that can't start is reported once and the link
  still opens: the click is never lost. On macOS `open -a` only reports a
  missing app through its exit status, so launching runs on a worker thread.
  Preferences never ask for a command: `browsers.installed()` lists each
  browser and profile by name (`BROWSER` field kind, an editable combo in
  both windows) and stores the command; typing one still works.
- **Sounds come with notices, from a pack.** Each `Notice` names a slot
  (`sounds.REVIEW/GOOD/BAD/INFO`, or none); `Alerts.notify` plays the
  pack's file for it after the quiet-hours gate, at most one sound per
  `SOUND_GAP_SECONDS` (tier sounds share the limit), so a busy poll isn't a
  cacophony. Built-in packs are synthesized in code (no audio files, no
  third-party licenses): never ship sounds from games or films; users add
  their own files. A new notice kind picks its slot where the Notice is
  built; click replies (`_tell`) and fetch errors stay silent. Notices about
  your PRs also carry their `event`: `sounds.resolve` plays the event's own
  choice (`sounds.events`: a slot, `fx:<id>`, "none" or a file), else the
  pack's file for the event, else the slot. Dict settings like
  `sounds.events` get one Field per key (`sounds.events.approved`);
  `get_value`/`set_value` handle dicts, and "" isn't stored.
- **AI quotas come from each assistant's own CLI, never its token.**
  `ai_usage.PROVIDERS` is the registry: a provider is an id (also its
  `cfg.ai_usage` switch and its Preferences field, generated from the
  registry), a label, a help text and a blocking `fetch(cfg) -> [Usage]`.
  Codex asks `codex -s read-only -a never app-server` (JSON-RPC
  `initialize`, `initialized`, `account/rateLimits/read`); Antigravity
  `agy -p /usage --output-format json`, only from 1.1.11 (older ones could
  send the slash command to a model). CLIs are looked up beyond a
  LaunchAgent's short PATH (`find_cli`). Reading another app's OAuth token
  (Claude's Keychain item, `~/.codex/auth.json`, browser cookies) is what
  other tools do; here it would be a separate, opt-in source, never the
  default. A provider that fails is logged and left out, never the others.
  Copilot is one provider: `GET /copilot_internal/user` (what VS Code's
  Copilot status bar and every quota tool read) answers the token `gh`
  already has and covers personal plans and organization seats; the
  documented billing API needs an extra scope, only sees self-paid usage and
  doesn't give the allowance. So: github.com accounts only, 404 = no
  Copilot, anything unparseable = no line, "unofficial" said in
  Preferences. All of it is polled every `ACCOUNT_INFO_SECONDS` (not on
  every PR poll), on its own worker so a slow CLI never holds up the inbox.
- **Settings shows in the Dock while it's open (macOS).** The app is an
  accessory (menu bar only); `prefs_window._show_in_dock` makes it regular
  while Settings is open, with an icon drawn in code and a main menu whose
  Edit items give text fields ⌘C/⌘V/⌘A, then accessory again on close.
- **Editor match: mapping first, then basename.** `repo_paths` lines
  (`acme/* = ~/code/acme/*`, exact entries beat globs, `*` on the right is
  the repo name) say where a repo lives; a mapped folder that doesn't exist
  means "not cloned" (clone goes there) and never falls back to a same-named
  repo of another owner. Without a mapping, PR `org/webapp` matches a
  configured repo folder named `webapp`, so nothing needs declaring.

## Platform gotchas

The macOS backend is pure PyObjC (no `rumps`): `NSStatusItem` for the menu
bar, an `NSWindow` toolbar-style Settings window, `osascript` for
notifications. The Linux backend is GTK 3 + AppIndicator, rendering the
menu over DBusMenu.

- **Don't escape `_` in menu labels.** libdbusmenu-gtk already does
  (`web_app` → `web__app`, checked in `tests/e2e/linux_dbusmenu.sh`). GNOME's
  AppIndicator extension only un-escapes the first `__` of a label, which is
  an upstream bug.
- **Tray icon state is shared, rendering isn't.** `Controller.tray_state()`
  picks paused / offline / error / count / idle (+ count, alert). Linux
  (`icon_renderer.py`) writes monochrome SVGs using `currentColor` + KDE's
  `ColorScheme-Text` class with a `#888888` fallback. Don't rename them
  `-symbolic`: GTK 3 would fill the stroked glyphs. macOS (`status_icon.py`)
  shows an SF Symbol template image + the count, from the pure
  `icon.menubar_presentation()`.
- **Knowing when the Linux menu opens.** With AppIndicator the `Gtk.Menu` is
  never shown (no `show`/`map` when a shell opens it); the desktop draws it
  from DBusMenu. The indicator's `dbus-menu-server` property gives the
  DbusmenuServer, whose `root-node` emits `about-to-show` and
  `event("opened")` (no Dbusmenu typelib needed). `AwaitrayApp` hooks both
  for `Controller.menu_opened` (the unseen dot); macOS uses
  `menuWillOpen:`. The e2e test opens the menu over DBus.
- macOS notifications via `display notification` ignore urgency and icon
  (accepted for API parity, then dropped). No bundle/signing needed because
  we shell out to `osascript` rather than using the bundled-app API.
- The macOS menu uses one `_Trampoline` NSObject per actionable item to
  hold the Python callback; they're kept alive in `self._trampolines`,
  which is reset on every `rebuild_menu`.
- Colored menu labels: Linux uses color-swatch icons (DBusMenu strips Pango
  markup); macOS uses `NSAttributedString` foreground color. Both read the
  hex from `controller.pr_color`.
- `gh auth login` is launched in a terminal from the Hosts prefs tab —
  via a terminal emulator on Linux, via `osascript … tell Terminal` on macOS.

## How to add a feature

### A new config field
1. Add it to a dataclass in `config.py` (or create one and add it to
   `Config`). That's all persistence needs: `load()`/`save()`, the known-key
   set and the unknown-key preservation are derived from the dataclasses,
   and `tests/test_config.py` round-trips every field. Only a field whose
   stored form isn't plain JSON of its dataclass needs a `_LOADERS` entry
   (entry lists, `~` expansion, normalization).
2. Add a `Field` for it in `prefs_model.SECTIONS` (kind, label, help, bounds,
   `depends_on`). Both Preferences windows render it with no backend code,
   and `tests/test_prefs_model.py` fails if a setting has no place there.
   Only a new *kind* of editor needs code in both backends.
3. Preferences edit a working copy (`EditSession`) and commit it debounced;
   never rebuild a `Config` from widgets (that's how the old Linux window
   dropped repo exclusions). Menu-owned fields (`snoozed`, `paused`) are
   taken from the live config on commit.

### A new AI assistant (usage line)
1. In `ai_usage.py`, write `parse_<name>(payload) -> Usage | None` (pure:
   unknown shapes give `None`) and `_<name>(cfg) -> list[Usage]`, which
   finds the CLI with `find_cli`, runs it read-only with a timeout and
   bounded output (`MAX_OUTPUT`), and returns `[]` on any failure.
2. Add a `Provider(id, label, help, fetch)` to `PROVIDERS`, and a bool
   field named like its `id` in `config.AiUsageConfig`. Preferences lists
   it and the menu shows its line with no further code.
3. Test the parser with a recorded payload and the fetch with a fake CLI
   (`tests/test_ai_usage.py` has `_executable`).

### A new feature
- A module in `awaitray/features/` with one class that takes a `Context`:
  it keeps its own state, registers its menu actions in `__init__`
  (`ctx.register_action`, lambdas so the method is looked up at click
  time; `ctx.on_pr` for handlers whose first arg is a PR key), and runs
  blocking work with `ctx.runner.background(job, then)`. What it needs
  from the rest of the app is a `Context` callable; add one there (wired
  in `Controller.__init__`) rather than importing another feature or the
  controller (`tests/test_features.py` fails if a feature does).
- Build it in `Controller.__init__` (`self.thing = Thing(ctx)`), call its
  tick from the timer or poll that feeds it, put what the menu shows of
  it in `MenuState` (filled in `Controller.menu_state`), and render it in
  `menu_model.build`. The controller doesn't grow beyond those lines. Test it through `ctl.thing` with `runner=ImmediateRunner()`.

### A new menu entry
- Add it in `menu_model.build` as an `Item`, with an `Action(kind, args)`.
  `build` reads only its `MenuState`: if it needs something new, add a
  field there and fill it in `Controller.menu_state` (`menu_model` never
  sees the controller; `tests/test_menu_state.py` fails if it does).
  Register a handler for the kind with `register_action(kind, handler)`
  from the feature that owns it (the controller's own few kinds are in
  `Controller._register_actions`); the handler gets the Action's args and
  `perform` only looks it up. Toolkit-only kinds (`menu_model.BACKEND_ACTIONS`: clipboard, windows,
  quit) are each backend's `_on_action`. `tests/test_actions.py` fails if
  the menu can produce a kind nobody handles. Both backends render it
  with no further changes. Keep within two submenu levels (Linux).
- Add a test in `tests/test_menu_state.py` from a hand-made `MenuState`,
  or in `tests/test_menu_model.py` through a controller. `tests/test_linux_backend.py` and
  `tests/test_macos_menu.py` render real menus in CI, and
  `tests/e2e/linux_dbusmenu.sh` reads the menu the app exports over DBusMenu.

### A new periodic task
- Add a timer in `Scheduler.reschedule` (it cancels and recreates every
  periodic timer, and starts none while paused). Return `True` from the
  callback to keep it firing. Blocking work goes on a worker thread that
  posts its result back with `host.run_on_main`. Works on both backends
  with no per-platform code.

### Testing controller logic
- Drive the controller through its public seam: build a `PollResult` and
  call `apply_poll_result`, or `apply_fetch_results`. Workers are just
  `pr_sync.poll(cfg)` / `repo_sync.fetch_all(cfg)` posted to the main thread,
  so tests call those directly with `gh`/`git` mocked. Don't reach into
  underscored attributes. Pass `runner=ImmediateRunner()` to run background
  jobs synchronously; never patch `threading`.
