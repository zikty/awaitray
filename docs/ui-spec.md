# awaitray UI spec

How the tray menu and the preferences window work, on both backends: the
menu, preferences, icons and empty states. The decisions come from reviewing
13 comparable tools and the source of the Linux tray stack; change them here
first, then in the code.

## Principles

1. **One structure, native rendering.** Both backends render the same menu
   model built in `controller.py`. Feature parity is mandatory; pixel parity
   isn't (macOS can show richer rows).
2. **The core action is one click.** Clicking a PR opens it.
3. **Always tell the user where they stand.** Sections are always shown;
   empty ones say so. Status and freshness are always visible.
4. **Plain text first.** Anything that doesn't survive a Linux tray menu
   (tooltips, secondary text, markup) can't carry essential information.

## Platform constraints (verified in source)

| | Linux (AppIndicator → DBusMenu, GNOME / KDE) | macOS (NSStatusItem + NSMenu) |
|---|---|---|
| Item content | label, icon (right-aligned on GNOME), check/radio, enabled, visible, separator | attributed title (fonts, colors, two lines), image, state, alternates |
| Submenus | nested submenus work (GNOME renders inline accordions); an item with a submenu **can't be clicked** | normal cascading submenus |
| Tooltips / secondary text | none on GNOME | yes, but not needed for essentials |
| Icon label | shown on GNOME only (KDE ignores it) | status item title |
| Clicks on the icon | left = menu; middle = secondary target; `activate` only with libayatana ≥ 0.6 | click = menu |
| Rebuild while open | property updates apply live; structural rebuilds collapse open submenus | rebuild in `menuNeedsUpdate:` |
| Labels | `_` is a mnemonic marker: escape dynamic text | literal |

Rule for both: **at most two submenu levels.**

## Menu

```
3 to review · 2 team · updated 14:32             ← status line (disabled)
⚠ GitHub: Actions runs delayed                  ← only during an incident (githubstatus.com)
Copilot Pro · 38% left (570 of 1,500) · resets Nov 1   ← one line per AI assistant
Codex Plus · 5h 84% left · week 61% left · resets Oct 7    turned on (ai_usage.py); opens
                                                   its usage page when it has one
GitHub inbox · 4 unread ▸                        ← Open GitHub inbox / Mark all as read /
                                                   each thread ▸ Open / Mark as read /
                                                   Mark as done / Unsubscribe (inbox.py);
                                                   a plain link when nothing is unread
─────────────────────────────
NEEDS YOUR REVIEW · 3                            ← section header (disabled)
  ✓ web#412  Fix login redirect · alice · 2d     ← PR row: click opens the PR
  ✗ web#398  Migrate to Vite · bob · 5d
  ● api#77   Rate limit headers · carol · 3h
NEEDS YOUR TEAM'S REVIEW · 0
  Nothing to review                              ← empty line (disabled)
NEEDS YOUR ACTION · 1                            ← my PRs (only if My PRs is on)
  ⚠ web#501  Payments retry · changes requested · 1d
READY TO MERGE · 0
  Nothing ready to merge
WAITING FOR REVIEW · 2
  …
DRAFTS · 1
  …
─────────────────────────────
PR actions ▸                                     ← per PR ▸ Open / Open in editor /
                                                   Clone / Copy as Markdown /
                                                   Snooze choices (inline)
Snoozed (2) ▸                                    ← per PR ▸ Open / Unsnooze
Local repos (5) ▸                                ← per repo ▸ Open in editor / its PRs
─────────────────────────────
☐ Pause
Refresh now
Preferences…
Quit
```

### Sections

In this order. My-PR sections only appear when "My PRs" is enabled.

| Section | Content | Empty line |
|---|---|---|
| Needs your review | review requested from **you** (`user-review-requested:@me`) | Nothing to review |
| Needs your team's review | requested from one of your teams only | Nothing to review |
| Needs your action | my PRs: changes requested, CI failing, or merge conflict | Nothing needs your action |
| Ready to merge | my PRs: approved (or no review required), CI green, no conflict | Nothing ready to merge |
| Waiting for review | my PRs: everything else that isn't a draft | Nothing waiting |
| Drafts | my draft PRs | No drafts |

- Header: `UPPERCASE TITLE · N`.
- Rows are grouped by repo only through the `repo#n` prefix. There are no
  repo submenus.
- **Cap**: at most 8 rows per section (setting), then `N more… ▸` with the rest.
- **Hidden items**: when snoozes or filters hide PRs of a section, the empty
  line (or the header) says so: `Nothing to review · 2 snoozed`.
- Hiding and reordering sections is a preferences feature.

### PR row

`<glyph> <repo>#<n>  <title> · <author> · <age>`

Clicking a review request opens the PR; clicking one of yours opens its
latest comment or review (the PR when there's none).

- `repo` is the short name (`web` for `acme/web`). When two configured hosts
  or orgs share a short name, the full `owner/repo` is used.
- Only the title is truncated (default 60 characters, `…`).
- `age`, compact (`45m`, `3h`, `2d`, `5w`): for review requests, time since
  your review was requested (else since creation); for my PRs, since creation.
- Review requests waiting longer than *Waiting time → Flag reviews waiting
  for* (default 2 days) get a `⏳` before the name and can turn the icon red.
  PRs with no activity for *Dim PRs…* days (default 30) get `· stale`, the
  draft color, and leave the menu-bar number.
- My PRs replace the author with their state: `changes requested`,
  `approved`, `checks failing`, `conflict`, `review required`.

| Glyph | Meaning (first match wins) |
|---|---|
| `⚠` | needs your action (my PRs) |
| `✗` | CI failing |
| `●` | CI running |
| `✓` | CI green |
| `○` | no CI / unknown |

Colors: Linux keeps the color swatch icon (`controller.pr_color`); macOS uses
the same color on the glyph.

**macOS rows** are two lines (title / meta in secondary color). Holding **⌥**
turns a row into its actions submenu (`NSMenuItem.setAlternate_`).

### Custom sections

After My PRs, in the configured order: `<TITLE> · N`, rows like review
requests (snoozable, in PR actions), `Nothing here` when empty. A section is
identified by host + query, so editing the query starts a fresh baseline.

### Actions

PR actions submenu (both platforms), per PR:

- Open in browser
- Open in editor (if a local clone is found)
- Clone here (if possible)
- Copy as Markdown
- Notify when checks finish (your PRs, while CI runs; a check item)
- Re-run failed checks (your PRs with failing GitHub Actions checks; re-runs
  the failed jobs of each run) and Request Copilot review (your PRs on
  github.com, not drafts) — `pr_actions.py`, replies as a notification
- Checks (when CI status is shown): a `Checks · N failing` header, each
  check failing first with its glyph (click opens its run), capped at 8,
  then `All checks…`. Inline, not a submenu: Linux allows two levels and
  PR actions ▸ PR ▸ uses both
- the snooze choices from `snooze.CHOICES`, inline

### Status line and modes

- First item, disabled: count of *direct* review requests, then team-only
  ones when there are any, and the last update (`3 to review · 2 team ·
  updated 14:32`); both leave out stale PRs, like the icon. Or the state: `Paused`, `Quiet hours until
  09:00`, `Offline`, `github.com: not logged in`, `from 14:32, refreshing…`.
- `Pause` is a check item.
- **Linux**: middle click on the icon = Refresh now.

### Tray count

Direct review requests only; a setting adds team requests to the number.
Stale PRs don't count unless asked.

A blue dot (Linux SVG) / `•` after the number (macOS) marks counted review
requests that arrived since the menu was last opened. Opening the menu
clears it; it works during quiet hours, and a re-requested PR is new again.

### Rebuilding

The controller builds the model on every change. Backends compare its
signature with the last rendered one and rebuild only if it differs, so
polls that change nothing never collapse an open submenu. macOS rebuilds in
`menuNeedsUpdate:`.

## Preferences

Same six sections on both platforms, each presented natively:

1. **Accounts**: hosts, `gh` status and login (several accounts per host), each
   account's browser and the default browser
2. **Pull requests**: My PRs, filters, priorities, section visibility and order
3. **Notifications**: what notifies, quiet hours, sounds (on/off, the
   sound pack, a sound per change to your PRs, your sounds folder; picking
   a pack or a sound plays it)
4. **Repositories**: local repos, auto-discover, clone root, fetch interval,
   editor, repo path mapping
5. **Appearance**: colors, tray icon thresholds and glyphs
6. **Advanced**: PR check interval, error policy, backup/restore, logs

- **Linux**: libhandy-1 `HdyPreferencesWindow` (system package
  `gir1.2-handy-1`), with a `Gtk.Stack` + `Gtk.StackSidebar` fallback.
- **macOS**: a toolbar-pane Settings window (Apple HIG). ⌘, opens it, and it
  restores the last pane.
- **Instant apply** with per-field validation. Settings that trigger a
  refetch are debounced (~600 ms).
