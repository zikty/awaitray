# awaitray

[![CI](https://github.com/zikty/awaitray/actions/workflows/ci.yml/badge.svg)](https://github.com/zikty/awaitray/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**The pull requests awaiting your review, one glance away in your tray.**
For **Linux** and **macOS**, across every GitHub host you use.

- 🔔 Notifies you when someone requests your review, and shows the count in the
  tray / menu bar.
- 🏢 Works with **github.com and any number of GitHub Enterprise hosts** at
  once, and **several accounts on the same host** (personal + work). Auth is
  delegated to the [`gh`](https://cli.github.com/) CLI: awaitray never stores
  a token.
- 😴 **Snooze** a PR for a while, **until its next commit** or **until new
  activity**, or ignore it for good.
- 🔄 Keeps your local clones fresh with a periodic `git fetch`, and shows
  ahead/behind counts.
- 🐧 Native on both platforms: GTK + AppIndicator on Linux, a PyObjC menu-bar
  item on macOS. No Electron, no background service to sign up for.

## Features

**Review requests.** Open PRs where your review is requested, grouped by repo
(and host, when you use several). *PR actions* lists each PR's checks,
failing first, each opening its run. Each PR opens in the browser, in your editor
if you have a local clone, or can be cloned on demand into a folder you choose.
Clones are found by folder name, or by rules like `acme/* = ~/code/acme/*`
when two owners share a repo name. Each account can open its PRs in its own
browser or profile (a corporate account behind a VPN in its own Chrome
profile, say), with a default browser for the rest: pick them from the
browsers and profiles awaitray finds installed.

**Your own PRs.** An optional section listing your open PRs, with CI and
review status (click opens the latest comment or review), and a notification when one changes: approved, ready to
merge, changes requested, checks failed or back to green, merge conflict,
new comments. Each change notifies once per commit (a flaky re-run doesn't),
and every kind can be turned off. While CI runs, **Notify when checks
finish** (in *PR actions*) sends one notification on the next regular poll
after the checks complete.

**Local repos.** Periodic `git fetch` of a list of repos (or every repo one
level under a parent folder), with ahead/behind counts in the menu and
optional notifications when a fetch fails.

**Snooze.** Per PR: for 1 or 3 hours, until tomorrow or Monday 09:00, until
new commits, or **until new activity** (it comes back when someone comments,
reviews or pushes). **Ignore this PR** hides it for good. Snoozed and ignored
PRs are listed in their own submenu and can be restored.

**Custom sections.** Your own menu sections from GitHub searches
(`mentions:@me`, `repo:acme/core label:urgent`), per host or on all of them,
each with its own "notify new PRs" and "count in the menu bar". Preferences
has a *Test query* button.

**Waiting time.** Review rows show how long they have waited on you (since
your review was requested). Reviews waiting more than 2 days get a ⏳ and
turn the icon red; PRs with no activity for 30 days are dimmed and leave the
count. Both thresholds are configurable, or off. Long waits also get a
**reminder**, at most once a day per PR, and an optional **daily summary**
(`3 reviews waiting (1 for 2+ days) · 2 of your PRs need action`) goes out at
the time you pick. Both wait for quiet hours to end.

**Priorities and colors.** Glob patterns (`myorg/*`, `*core*`) mark repos as
high priority, which turns the tray icon red. Menu colors per section and per
PR state (draft, approved, changes requested), all configurable.

**Filters.** Ignore authors (`dependabot[bot]`, `renovate[bot]` and
`github-actions[bot]` by default) and repos, with shell-style globs.

**Tray icon.** Count thresholds with their own glyph and optional sound (e.g.
calm up to 2 PRs, alarmed from 5), played on tier change or on every new PR.

**Sound packs.** A different sound for a new review request, good news
(approved, ready, checks green), bad news (changes requested, checks failed,
conflict) and the rest. Four packs come built in (Arcade, Retro 8-bit,
Sci-fi, Chimes; synthesized by awaitray itself). Each change to your own PRs
(approved, checks failed, new comments…) can have its own sound: an effect
(victory fanfare, game over, level up, coin, sad trombone…), silence, or any
audio file of yours. Your own pack is a folder in
`~/.config/awaitray/sounds/<name>/` with `review`, `good`, `bad` and `info`
files, and optionally one per change (`approved.wav`…); `.wav`, `.ogg` or
`.aiff`. Picking a pack or a sound in Preferences plays it.

**Quiet hours.** Mute notifications and sounds in a time window (wraps
midnight). The icon and menu keep updating, and a **blue dot** on the icon
marks review requests you haven't seen yet: opening the menu clears it.

**AI assistants and inbox.** Right under the status line: how much of each
AI assistant's quota is left and when it resets (with a notification when
20% or less is left), and your unread GitHub notifications. Turn each one
on or off in Preferences → Accounts:

- **GitHub Copilot**, per github.com account, through `gh`. It comes from
  the unofficial endpoint the Copilot quota tools read, so it may stop
  working without notice; then the line just goes away.
- **OpenAI Codex**: the 5-hour and weekly limits, asked to the `codex` CLI
  (`app-server`, read-only: it spends no quota).
- **Google Antigravity**: what's left per model group, asked to `agy`
  (1.1.11 or later).

Each one asks its own CLI, with its own login: awaitray never reads another
app's token. Not installed or not logged in means no line.

**Triage your inbox from the menu.** The inbox line lists your unread
threads (the first 10, then a link to the rest). Each one can be opened
(which marks it as read), marked as read or done, or unsubscribed from;
*Mark all as read* clears what was listed, never something newer. The menu
updates at once; if GitHub refuses, a notification says why.

**GitHub incidents.** While githubstatus.com reports an incident, or Git,
the API, pull requests, Actions or Copilot as degraded, a `⚠ GitHub: …` line
links to it, and a major incident notifies once. Read with every PR poll,
from the public status page (no API calls), for github.com accounts.

**Fix it from the menu.** On your PRs, *Re-run failed checks* re-runs the
failed jobs of their GitHub Actions runs, and *Request Copilot review*
(github.com) adds Copilot as a reviewer. A notification says how it went.

**Error handling.** Per-host failures are shown in the menu without stopping
the other hosts. Preferences → Accounts shows each host's remaining API
budget, and a host close to its limit pauses until the budget resets. When every host fails: wait for the next tick, retry with
exponential backoff, or pause.

Everything is configured from the **Preferences** window. There is nothing to
edit by hand.

## Install

### Packages

From the first release on:

- **Debian / Ubuntu:** download `awaitray_<version>_all.deb` from
  [Releases](https://github.com/zikty/awaitray/releases) and
  `sudo apt install ./awaitray_*_all.deb`. Start it with `awaitray`, or at
  login with `systemctl --user enable --now awaitray.service`.
- **Arch:** `awaitray` on the AUR.
- **macOS:** `brew install zikty/awaitray/awaitray`, then
  `brew services start awaitray`.

Or run it from a checkout:

### Linux (Debian / Ubuntu, Fedora, Arch)

```bash
git clone https://github.com/zikty/awaitray.git
cd awaitray
./install.sh
```

`install.sh` first checks what awaitray needs at runtime:

- Python can load GTK 3 and AyatanaAppIndicator3
- `git`, `gh` and `notify-send` are on PATH

It only installs packages if something is missing, using `apt`, `dnf` or `pacman`. Then it registers awaitray to start at login: a systemd user service, or a `.desktop` autostart entry when systemd isn't available.

> **Other distros:** `install.sh` lists what's missing instead of guessing.
> awaitray runs anywhere with Python 3.10+, PyGObject, GTK 3, Ayatana
> AppIndicator, `notify-send`, `git` and `gh`. On older distros `gh` may need
> [its own repository](https://cli.github.com).

GNOME needs the *AppIndicator and KStatusNotifierItem Support* extension to
show tray icons (`sudo apt install gnome-shell-extension-appindicator`).

### macOS

```bash
brew install gh git
git clone https://github.com/zikty/awaitray.git
cd awaitray
./install-macos.sh
```

Creates a local `.venv` with `pyobjc-framework-Cocoa` and registers a
LaunchAgent (`com.awaitray.agent`) so awaitray starts at login.

## Connect your GitHub hosts

Once per host:

```bash
gh auth login --hostname github.com
gh auth login --hostname github.your-company.com   # GitHub Enterprise
```

Then add the hostname in **Preferences → Accounts**. It shows whether `gh` is
installed and logged in for each host, your remaining API budget, and can
launch `gh auth login` for you.

**Several accounts on one host** (e.g. personal and work on github.com) need
`gh` 2.40 or newer. Log in to each with `gh auth login` (gh keeps them all),
then add the host once per account in Preferences → Accounts, with the
account's login in the *account* field. An empty account field means
whichever account is active in `gh`.

For those extra accounts awaitray asks `gh auth token --user <login>` for the
account's token on each poll and passes it to the next `gh` call through its
environment (`GH_TOKEN`, or `GH_ENTERPRISE_TOKEN` for Enterprise Server), the
way `gh` itself picks tokens per host. It is never written to disk, logged or
kept after the poll. A PR two accounts can see is listed once, with both.

## Usage

Click the tray icon (Linux) or the menu-bar item (macOS):

- The **status line** (how many reviews wait, last update), then, when there
  is something to say: a **⚠ GitHub incident**, one line per **AI
  assistant** you turned on, and your **GitHub inbox ▸**.
- **Needs your review** / **Needs your team's review**: review requests
  addressed to you, and those that only reach you through a team. The icon
  counts yours.
- With *My PRs* on: **Needs your action** (changes requested, failing checks
  or a conflict), **Ready to merge**, **Waiting for review** and **Drafts**.
- **Click a PR to open it.** Each row shows CI state (`✓ ✗ ● ○`, `⚠` = your
  move), repo#number, title, author and age.
- Your **custom sections**, after those, in the order you set.
- **PR actions ▸**: open in your editor, clone, copy as Markdown, the PR's
  checks, snooze; on your own PRs also **Notify when checks finish**,
  **Re-run failed checks** and **Request Copilot review**. On macOS, hold
  **⌥** over a PR for the same actions.
- **Snoozed ▸** / **Local repos ▸**: restore snoozed PRs; see your clones'
  ahead/behind state and fetch them.
- **Pause**, **Refresh now** (or middle-click the icon on Linux),
  **Preferences…**

Run in the foreground (logs to stderr) with `./run.sh` on Linux or
`./run-macos.sh` on macOS.

## Configuration file

Settings are stored in `~/.config/awaitray/config.json`. You can script it, but
the Preferences window covers everything:

```json
{
  "hosts": [{ "hostname": "github.com", "enabled": true }],
  "repos": [{ "path": "/home/me/code/some-repo", "enabled": true, "remote": "origin" }],
  "intervals": { "pr_check_minutes": 5, "fetch_minutes": 15 },
  "notify_on_new_pr": true,
  "paused": false
}
```

The schema is additive: unknown keys are preserved on save, so older and newer
versions can share the same file.

## Update

```bash
./update.sh                     # Linux: snapshot settings, git pull, refresh autostart, restart
git pull && ./install-macos.sh  # macOS: snapshot settings, refresh the venv and LaunchAgent
```

Both scripts copy your settings into the snapshots folder before touching
anything (`config-<date>-pre-update.json`), so an update can always be rolled
back:

```bash
python3 -m awaitray config snapshots                 # newest first
python3 -m awaitray config import <snapshot>.json    # restore one
```

Snapshots stay on this machine and only the newest 10 are kept. For a copy
you keep elsewhere (or to move to another computer), export a portable
backup as well:

```bash
python3 -m awaitray config export ~/awaitray-backup.json
```

## Moving to a new computer

```bash
# on the old machine
python3 -m awaitray config export ~/awaitray-backup.json

# on the new machine (quit awaitray first)
python3 -m awaitray config import ~/awaitray-backup.json --dry-run
python3 -m awaitray config import ~/awaitray-backup.json --clone-missing
```

- Paths are stored relative to your home folder, so the backup works across
  usernames and between Linux and macOS.
- `--dry-run` shows what will happen first: which hosts need
  `gh auth login`, which repos are missing, and whether your editor is
  installed.
- `--clone-missing` clones the repos you had on the old machine, from their
  remotes (`https://`, `ssh://`, `git://` or `user@host:` only), into folders
  inside your home that aren't hidden (never `~/.config`, `~/Library`…);
  anything else is skipped and listed.
- The editor and browser commands awaitray runs when you click are **not
  imported** unless you pass `--keep-commands` (or tick the box in
  Preferences): `--dry-run` lists the ones that differ from this machine.
- Any `config.json` also works as a backup, including one from an older
  version.
- The backup holds **no tokens** (auth stays in `gh`; credentials typed into
  a remote URL are left out), but it lists your hosts and paths, so it's
  written readable by you only. Keep it private.

awaitray also takes **automatic snapshots** of your settings (one a day,
before every import and before every update) in
`~/.config/awaitray/backups/`, keeping the last 10.
`python3 -m awaitray config snapshots` lists them, and
`python3 -m awaitray config snapshot [label]` takes one by hand.

## Command line

`python3 -m awaitray status` prints the last known counts, for status bars
(waybar, polybar, i3status, tmux…). It reads what the running app saved and
never calls GitHub itself, so you can run it as often as you like.

```bash
python3 -m awaitray status                        # 3 to review · updated 2m ago
python3 -m awaitray status --format '{review}'    # 3
python3 -m awaitray status --json                 # counts + PR list + staleness
```

```jsonc
// waybar
"custom/awaitray": {
  "exec": "python3 -m awaitray status --format '  {review}'",
  "interval": 60
}
```

```tmux
set -g status-right '#(cd ~/awaitray && python3 -m awaitray status --format "PR {review}")'
```

Run it from the awaitray folder, or with that folder on `PYTHONPATH`. With
no arguments, `python3 -m awaitray` starts the tray app as before.

## Uninstall

```bash
# Linux
./uninstall.sh           # stop and remove autostart, keep config
./uninstall.sh --purge   # also delete ~/.config/awaitray

# macOS
./uninstall-macos.sh           # unload and remove the LaunchAgent, keep config
./uninstall-macos.sh --purge   # also delete ~/.config/awaitray
```

System packages (`gh`, `git`, …) are left alone. Delete the project folder
yourself if you want it gone.

## Troubleshooting

- **"gh CLI missing"** in the Hosts tab: install `gh` (`sudo apt install gh` /
  `brew install gh`).
- **"not logged in"**: click *Login (gh auth)* in the Hosts tab, or run
  `gh auth login --hostname <host>`.
- **No tray icon on GNOME**: enable the AppIndicator extension (see Install).
- **No notifications on Linux**: install `libnotify-bin`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the architecture and how to run the
tests. The roadmap lives in the issue tracker.

## License

[MIT](LICENSE)
