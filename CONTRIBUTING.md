# Contributing

Thanks for helping. This page is the workflow and the rules;
[docs/architecture.md](docs/architecture.md) explains how the code is
organised and why, with a module graph generated from the code, the layer
rules CI enforces, and step-by-step recipes for adding a feature, a menu
entry, a setting, an AI assistant or a periodic task.

Questions and ideas go to
[Discussions](https://github.com/zikty/awaitray/discussions); issues are for
bugs and concrete proposals. Security problems are reported privately (see
[SECURITY.md](SECURITY.md)). Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md).

New here? Issues tagged
[`good first issue`](https://github.com/zikty/awaitray/labels/good%20first%20issue)
are small and well scoped;
[`help wanted`](https://github.com/zikty/awaitray/labels/help%20wanted) is
everything else contributions are welcome on. For anything larger than a bug
fix, open a Discussion or an issue first, so the approach is agreed and nobody
duplicates work.

## Running locally

On Linux the toolkit is a system package, not pip: install `python3-gi`,
`gir1.2-gtk-3.0`, `gir1.2-ayatanaappindicator3-0.1` and `libnotify-bin`
(Debian/Ubuntu names). On macOS `run-macos.sh` creates a `.venv` with PyObjC
on first run, so there's nothing to install by hand.

```bash
# Linux
./run.sh             # foreground, logs to stderr
python3 -m awaitray   # same thing

# macOS (creates .venv with pyobjc on first run)
./run-macos.sh
```

Logs live only in memory (Logs tab) and on stderr — there is no log file
path beyond what the LaunchAgent/`run.sh` capture.

## Tests

Run with `python3 -m unittest discover -s tests`. The core is tested on any
platform. Toolkit tests skip unless their toolkit is available:
`test_linux_backend.py` / `test_linux_prefs.py` need PyGObject + a display
(`xvfb-run -a /usr/bin/python3 -m unittest …`), `test_macos_*.py` need
PyObjC. CI runs all of them (Linux GTK job under Xvfb, macOS runners), plus
`tests/e2e/linux_dbusmenu.sh`, which starts the real app with a fake `gh`
and reads the menu it exports over DBusMenu, and `tests/e2e/linux_migration.sh`,
which runs the real `update.sh` over a home holding a devtray install (it
fakes `systemctl`/`pgrep`/`pkill`, so it's safe to run on your machine).

Lint, format and type-check (dev-only tools, configured in
`pyproject.toml`; CI runs all of them):

```bash
ruff check . && ruff format --check .
mypy awaitray   # strict on the core; the toolkit backends are excluded
python3 scripts/arch_graph.py   # after changing imports: refreshes the graph in docs/
shellcheck --severity=warning -x *.sh lib/*.sh packaging/*.sh packaging/deb/*.sh tests/e2e/*.sh
```

## Rules

These hold for every change, however it was written. Most are checked by
CI; the rest are checked in review.

- Don't introduce a Python dependency that requires pip install at runtime.
  System packages (`python3-gi`, `gir1.2-ayatanaappindicator3-0.1`,
  `libnotify-bin`) are the install boundary.
- Don't add token handling beyond `_account_env`. If something needs auth,
  add a `gh` command instead.
- Don't poll faster than the configured interval to "feel responsive". The
  user picked the interval.
- Don't retry without a backoff timer. `Scheduler.retry` keeps one pending retry for a
  reason.
- Don't hardcode anything organization-specific. The default ignored authors
  (`dependabot[bot]`, `renovate[bot]`, `github-actions[bot]`) are universally
  noisy; everything else is configured by the user.
- Don't write blocking code from the main thread (no synchronous `gh` or
  `git` from `rebuild_menu`, the prefs window, or signal handlers).

## Pull requests

- One topic per PR, with tests. Logic goes in the core (a feature module or
  the controller), never in a backend: backends only render.
- Anything a user sees in the menu or Preferences must work on both
  platforms. If you can only test one, say so in the PR.
- Run the tests, lint and shellcheck before pushing.
- Reference the issue you're closing.
- Label the PR so it lands in the right release-notes section (`enhancement`,
  `bug`, `security`, `linux`, `macos`, `docs`; the mapping is in
  [`.github/release.yml`](.github/release.yml)). `skip-changelog` leaves it out.
- By opening a PR you agree your contribution is licensed under the project's
  [MIT License](LICENSE).
