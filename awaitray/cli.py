"""Command-line interface: ``python3 -m awaitray <command>``.

Without arguments ``python3 -m awaitray`` starts the tray app. The commands
never poll GitHub: ``status`` reads the state the running app persisted, so
status bars can call it as often as they like.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import backup as backup_mod
from . import config as cfg_mod
from . import snooze as snooze_mod
from . import state as state_mod
from .github import PullRequest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="awaitray", description="PRs awaiting your review, in your tray."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", help="print the last known PR counts (for status bars)")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.add_argument("--format", help="template with {review} {mine} {total} {age} {stale}")

    cfg = sub.add_parser("config", help="back up, restore and inspect settings")
    cfg_sub = cfg.add_subparsers(dest="action", required=True)

    exp = cfg_sub.add_parser("export", help="write a portable settings backup")
    exp.add_argument(
        "file", nargs="?", help=f"output file (default: ./{backup_mod.default_export_name()})"
    )
    exp.add_argument(
        "--no-remotes", action="store_true", help="don't record git remote URLs of your repos"
    )

    imp = cfg_sub.add_parser("import", help="restore settings from a backup or a config.json")
    imp.add_argument("file")
    imp.add_argument("--dry-run", action="store_true", help="only show what would happen")
    imp.add_argument(
        "--clone-missing", action="store_true", help="clone repos missing on this machine"
    )
    imp.add_argument(
        "--keep-commands",
        action="store_true",
        help="also import the backup's editor and browser commands (they run when you click)",
    )
    imp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    snap = cfg_sub.add_parser("snapshot", help="copy the current settings into the snapshots")
    snap.add_argument("reason", nargs="?", default="manual", help="label in the file name")
    cfg_sub.add_parser("snapshots", help="list automatic snapshots (newest first)")
    cfg_sub.add_parser("path", help="print where settings and snapshots live")

    args = parser.parse_args(argv)
    if args.command == "status":
        return _status(args.json, args.format)
    if args.action == "export":
        return _export(args.file, not args.no_remotes)
    if args.action == "import":
        return _import(
            Path(args.file), args.dry_run, args.clone_missing, args.yes, args.keep_commands
        )
    if args.action == "snapshot":
        return _snapshot(args.reason)
    if args.action == "snapshots":
        for p in backup_mod.list_snapshots():
            print(p)
        return 0
    if args.action == "path":
        print(f"settings:  {cfg_mod.CONFIG_PATH}")
        print(f"snapshots: {backup_mod.backups_dir()}")
        print(f"state:     {state_mod.default_path()}")
        return 0
    return 2


# ----- status -----


def _status(as_json: bool, template: str | None) -> int:
    cfg = cfg_mod.load()
    snap = state_mod.StateStore().load()
    if snap is None:
        if as_json:
            print(json.dumps({"available": False}))
        else:
            print("awaitray: no data yet (is the app running?)")
        return 1

    def active(prs: list[PullRequest]) -> list[PullRequest]:
        return [p for p in prs if not _snoozed(cfg, p.key)]

    review, mine = active(snap.review_prs), active(snap.my_prs)
    saved = snap.saved_at_dt
    age_s = (datetime.now(timezone.utc) - saved).total_seconds() if saved else None
    stale = age_s is None or age_s > 2 * max(1, cfg.intervals.pr_check_minutes) * 60
    data = {
        "available": True,
        "review": len(review),
        "mine": len(mine),
        "total": len(review) + len(mine),
        "updated_at": snap.saved_at,
        "age_seconds": int(age_s) if age_s is not None else None,
        "stale": stale,
        "review_prs": [_pr_json(p, snap) for p in review],
        "my_prs": [_pr_json(p, snap) for p in mine],
    }
    if as_json:
        print(json.dumps(data, indent=2))
    elif template:
        print(
            template.format(
                review=data["review"],
                mine=data["mine"],
                total=data["total"],
                age=_human_age(age_s),
                stale="stale" if stale else "",
            )
        )
    else:
        line = f"{data['review']} to review"
        if cfg.my_prs.enabled:
            line += f" · {data['mine']} of yours"
        line += f" · updated {_human_age(age_s)} ago" + (" (stale)" if stale else "")
        print(line)
    return 0


def _snoozed(cfg: cfg_mod.Config, key: str) -> bool:
    entry = cfg.snoozed.get(key)
    return entry is not None and snooze_mod.is_active(entry)


def _pr_json(pr: PullRequest, snap: state_mod.Snapshot) -> dict[str, Any]:
    det = snap.details.get(pr.url)
    return {
        "key": pr.key,
        "repo": pr.repo,
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "author": pr.author,
        "draft": pr.is_draft,
        "ci": det.ci_state if det else None,
        "review": det.review_decision if det else None,
    }


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 90 * 60:
        return f"{int(seconds // 60)}m"
    if seconds < 36 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


# ----- config export / import -----


def _snapshot(reason: str) -> int:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", reason).strip("-") or "manual"
    path = backup_mod.snapshot(label)
    if path is None:
        print("nothing to snapshot (no settings file yet, or it could not be copied)")
        return 0  # not an error: update scripts call this on fresh installs too
    print(path)
    return 0


def _export(file: str | None, include_remotes: bool) -> int:
    dest = Path(file) if file else Path.cwd() / backup_mod.default_export_name()
    backup_mod.export_backup(dest, include_remotes=include_remotes)
    print(f"Settings exported to {dest}")
    print("It contains no tokens (auth stays in gh), but it does list your hosts and")
    print("local paths: keep it private.")
    return 0


def _import(
    file: Path, dry_run: bool, clone_missing: bool, yes: bool, keep_commands: bool = False
) -> int:
    try:
        bk = backup_mod.read_backup(file)
    except backup_mod.BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    plan = backup_mod.plan_import(bk)
    print(f"Import plan for {file}:")
    for line in plan.summary_lines():
        print(f"  {line}")
    if plan.missing_clones and not clone_missing:
        print("  (use --clone-missing to clone the missing repos)")
    if plan.new_commands and not keep_commands:
        print("  (use --keep-commands to import those commands; only for a backup you trust)")
    if dry_run:
        return 0

    print()
    print("This REPLACES your current settings (a snapshot is taken first).")
    print("Quit awaitray before importing and start it again afterwards; a running")
    print("app would overwrite the imported settings on its next save.")
    if not yes:
        if not sys.stdin.isatty():
            print("error: refusing to import without confirmation; pass --yes", file=sys.stderr)
            return 1
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    for line in backup_mod.apply_import(
        bk, plan, clone_missing=clone_missing, keep_commands=keep_commands
    ):
        print(line)
    for host, ok in plan.hosts:
        if not ok:
            print(f"next: gh auth login --hostname {host}")
    return 0
