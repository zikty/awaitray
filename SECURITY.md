# Security

## Reporting a vulnerability

Please report it privately, through GitHub's **Report a vulnerability**
button in this repository's Security tab, not in a public issue. You'll get
an answer within a week. Once a fix is released, the advisory is published
with credit to you, unless you'd rather not be named.

## What's in scope

awaitray runs on your machine with your GitHub access, so these matter most:

- **Tokens.** awaitray stores none; `gh` does. The one place a token passes
  through awaitray is `github._account_env`, which hands an extra account's
  token to the next `gh` call in its environment. Anything that puts a token
  on a command line, in a log, on disk, or keeps it longer is a bug.
- **Links it opens.** PR, check and status links come from GitHub and from
  whoever wrote them; only `http(s)` links may reach a browser.
- **Commands it runs.** `gh`, `git`, the configured editor and browser, the
  AI assistants' CLIs, and the commands in an imported settings backup.
- **Settings backups.** An import shows what it will change and asks first
  (unless `--yes`), and a backup never holds a token. It does carry editor
  and browser commands that run when you click, so the README says to
  import only backups you trust.

Out of scope: problems in `gh`, `git` or the AI CLIs themselves (report
those upstream), and anything that needs someone who can already run code
as you.
