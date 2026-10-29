"""Settings backup, restore and automatic snapshots.

A backup is the raw ``config.json`` object (unknown keys included) wrapped in a
small envelope, with local paths written relative to ``~`` so it can be
restored on another machine, OS or username. Import also accepts a bare
``config.json`` from any awaitray (or devtray) version.

``config.json`` itself keeps absolute paths on purpose: older versions don't
expand ``~``, and a downgrade must keep working.

Everything here is toolkit-agnostic: the CLI and the Preferences UI both build
on ``read_backup`` → ``plan_import`` (a report, no side effects) →
``apply_import``.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import config as cfg_mod
from . import log as log_mod

FORMAT = "awaitray-backup"
FORMAT_VERSION = 1
SNAPSHOT_KEEP = 10


class BackupError(ValueError):
    pass


# ----- Paths -----


def _home() -> str:
    return str(Path.home())


def collapse_home(path: str) -> str:
    """``/home/me/code/x`` → ``~/code/x`` (paths outside $HOME unchanged)."""
    if not isinstance(path, str) or not path:
        return path
    home = _home().rstrip(os.sep)
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def expand_home(path: str) -> str:
    return os.path.expanduser(path) if isinstance(path, str) and path else path


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _map_rule(line: Any, fn: Callable[[str], str]) -> Any:
    """`owner/repo = path`: map the path side only."""
    if not isinstance(line, str) or "=" not in line:
        return line
    pattern, _, path = line.partition("=")
    return f"{pattern.strip()} = {fn(path.strip())}"


def _map_paths(raw: dict[str, Any], fn: Callable[[str], str]) -> None:
    """Apply `fn` to every local path field of a raw config dict, in place."""
    if isinstance(raw.get("clone_root"), str):
        raw["clone_root"] = fn(raw["clone_root"])
    if isinstance(raw.get("repo_paths"), list):
        raw["repo_paths"] = [_map_rule(line, fn) for line in raw["repo_paths"]]
    for repo in raw.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        if isinstance(repo.get("path"), str):
            repo["path"] = fn(repo["path"])
        if isinstance(repo.get("excluded"), list):
            repo["excluded"] = [fn(p) if isinstance(p, str) else p for p in repo["excluded"]]


# ----- Export -----


def _git_remote_url(path: str, remote: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "-C", path, "remote", "get-url", remote],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = res.stdout.strip()
    return url if res.returncode == 0 and url else None


def strip_credentials(url: str) -> str:
    """A remote URL without the secret it may embed: `https://user:token@h/r`
    → `https://h/r`. ssh keeps its user (`ssh://git@h/r`), never a password;
    the scp form (`git@h:r`) has no secret. Anything else is unchanged."""
    if "://" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user = userinfo.partition(":")[0] if parts.scheme in ("ssh", "git+ssh") else ""
    netloc = f"{user}@{hostport}" if user else hostport
    return urlunsplit(parts._replace(netloc=netloc))


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, readable by you only (a backup
    lists your hosts and folders, and once held remote URLs as typed)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # an existing tmp keeps its old mode otherwise
    tmp.replace(path)


def _discover(parent: str) -> list[str]:
    from . import repos as repo_mod  # local import: repos is only needed here

    return repo_mod.discover_repos(parent)


def _collect_remotes(raw: dict[str, Any]) -> dict[str, str]:
    """Remote URL per configured clone (auto-discover parents: each child),
    keyed by the ``~``-relative path, so a restore can clone what's missing."""
    remotes: dict[str, str] = {}
    for repo in raw.get("repos") or []:
        if not isinstance(repo, dict) or not isinstance(repo.get("path"), str):
            continue
        remote = repo.get("remote") or "origin"
        excluded = set(repo.get("excluded") or [])
        paths = _discover(repo["path"]) if repo.get("auto_discover") else [repo["path"]]
        for path in paths:
            if path in excluded:
                continue
            url = _git_remote_url(path, remote)
            if url:
                remotes[collapse_home(path)] = strip_credentials(url)
    return remotes


def _read_current_raw() -> dict[str, Any]:
    try:
        raw = json.loads(cfg_mod.CONFIG_PATH.read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        cfg_mod.save(cfg_mod.load())  # materialize defaults
        raw = json.loads(cfg_mod.CONFIG_PATH.read_text())
    return _as_dict(raw)


def build_backup(include_remotes: bool = True, app_version: str = "") -> dict[str, Any]:
    raw = _read_current_raw()
    remotes = _collect_remotes(raw) if include_remotes else {}
    portable = copy.deepcopy(raw)
    _map_paths(portable, collapse_home)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": app_version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"platform": _platform()},
        "config": portable,
        "repo_remotes": remotes,
    }


def export_backup(dest: Path, include_remotes: bool = True) -> Path:
    payload = build_backup(include_remotes=include_remotes)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_private(dest, json.dumps(payload, indent=2))
    return dest


def default_export_name() -> str:
    return f"awaitray-backup-{datetime.now().strftime('%Y%m%d')}.json"


def _platform() -> str:
    import sys

    return (
        "macos"
        if sys.platform == "darwin"
        else "linux"
        if sys.platform.startswith("linux")
        else sys.platform
    )


# ----- Import -----


@dataclass
class Backup:
    config: dict[str, Any]
    repo_remotes: dict[str, str] = field(default_factory=dict)
    format_version: int | None = None  # None = bare config.json
    source_platform: str = ""
    warnings: list[str] = field(default_factory=list)


def read_backup(path: str | Path) -> Backup:
    """Parse a backup envelope or a bare config.json (any version)."""
    try:
        raw = json.loads(Path(path).read_text())
    except OSError as e:
        raise BackupError(f"cannot read {path}: {e}") from e
    except ValueError as e:
        raise BackupError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise BackupError(f"{path} doesn't contain a settings object")

    if raw.get("format") == FORMAT:
        config = raw.get("config")
        if not isinstance(config, dict):
            raise BackupError("backup has no 'config' object")
        version = raw.get("format_version")
        warnings = []
        if not isinstance(version, int) or version > FORMAT_VERSION:
            warnings.append(
                f"backup format {version!r} is newer than this version understands "
                f"({FORMAT_VERSION}); settings are imported, unknown parts are kept as-is"
            )
        remotes = _as_dict(raw.get("repo_remotes"))
        return Backup(
            config=config,
            repo_remotes={
                k: v for k, v in remotes.items() if isinstance(k, str) and isinstance(v, str)
            },
            format_version=version if isinstance(version, int) else None,
            source_platform=str((raw.get("source") or {}).get("platform", "")),
            warnings=warnings,
        )

    # A bare config.json: sanity-check it looks like one.
    if not ({"hosts", "repos", "intervals"} & raw.keys()):
        raise BackupError(f"{path} doesn't look like an awaitray backup or config.json")
    return Backup(config=raw)


@dataclass
class RepoCheck:
    path: str  # expanded, for this machine
    exists: bool
    auto_discover: bool
    remote_url: str | None = None


@dataclass
class ImportPlan:
    hosts: list[tuple[str, bool]]  # (hostname, logged in with gh here)
    repos: list[RepoCheck]
    missing_clones: list[tuple[str, str]]  # (path, url) clonable from repo_remotes
    editor_command: str | None = None  # set when the editor isn't on PATH
    missing_sounds: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Commands the backup sets that differ from this machine's, as (where,
    # command): they'd run when you click, so they're imported only on request.
    new_commands: list[tuple[str, str]] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [f"! {w}" for w in self.warnings]
        for host, ok in self.hosts:
            lines.append(
                f"host {host}: "
                + ("logged in" if ok else f"needs `gh auth login --hostname {host}`")
            )
        for r in self.repos:
            kind = "parent folder" if r.auto_discover else "repo"
            lines.append(f"{kind} {r.path}: " + ("found" if r.exists else "MISSING"))
        for path, url in self.missing_clones:
            lines.append(f"can clone {url} → {path}")
        if self.editor_command:
            lines.append(f"editor `{self.editor_command}` not found on PATH")
        for where, command in self.new_commands:
            lines.append(
                f"! {where} command `{command}` runs when you click; "
                "kept as on this machine unless you choose to import commands"
            )
        for s in self.missing_sounds:
            lines.append(f"sound file not found here, will be reset to silent: {s}")
        return lines


def _localized(backup: Backup) -> dict[str, Any]:
    raw = copy.deepcopy(backup.config)
    _map_paths(raw, expand_home)
    return raw


def _is_logged_in(host: str) -> bool:
    from . import github as gh

    return gh.is_authenticated(host)


def plan_import(backup: Backup, check_hosts: Callable[[str], bool] = _is_logged_in) -> ImportPlan:
    """What importing would do on this machine. No side effects besides
    read-only checks (paths, PATH lookups, `gh auth status`)."""
    raw = _localized(backup)
    # Once per hostname: `gh auth login` is per host, whatever the accounts.
    names = dict.fromkeys(
        h["hostname"]
        for h in raw.get("hosts") or []
        if isinstance(h, dict) and isinstance(h.get("hostname"), str) and h.get("enabled", True)
    )
    # Loading the config leaves out what isn't a hostname: say so up front.
    hosts = [(name, check_hosts(name)) for name in names if cfg_mod.valid_hostname(name)]
    bad_hosts = [
        f"won't add {name!r}: not a hostname" for name in names if not cfg_mod.valid_hostname(name)
    ]

    repos: list[RepoCheck] = []
    for repo in raw.get("repos") or []:
        if not isinstance(repo, dict) or not isinstance(repo.get("path"), str):
            continue
        path = repo["path"]
        repos.append(
            RepoCheck(
                path=path,
                exists=Path(path).is_dir(),
                auto_discover=bool(repo.get("auto_discover")),
                remote_url=backup.repo_remotes.get(collapse_home(path)),
            )
        )

    missing_clones = []
    refused = []
    for rel, url in sorted(backup.repo_remotes.items()):
        path = expand_home(rel)
        if Path(path).exists():
            continue
        problem = clone_problem(url, path)
        if problem:
            refused.append(f"won't clone {url!r} into {path!r}: {problem}")
        else:
            missing_clones.append((path, url))

    editor = _as_dict(raw.get("editor"))
    editor_missing = None
    if (
        editor.get("enabled")
        and isinstance(editor.get("command"), str)
        and editor["command"].strip()
    ):
        exe = editor["command"].split()[0]
        if not shutil.which(exe):
            editor_missing = editor["command"]

    missing_sounds = [s for s in _bucket_sounds(raw) if os.path.isabs(s) and not Path(s).exists()]

    warnings = [*backup.warnings, *bad_hosts, *refused]
    if backup.source_platform and backup.source_platform != _platform():
        warnings.append(f"backup comes from {backup.source_platform}; this is {_platform()}")

    current = _commands(_read_current_raw())
    new_commands = [
        (where, cmd) for where, cmd in _commands(raw).items() if current.get(where) != cmd
    ]

    return ImportPlan(
        hosts=hosts,
        repos=repos,
        missing_clones=missing_clones,
        editor_command=editor_missing,
        missing_sounds=missing_sounds,
        warnings=warnings,
        new_commands=new_commands,
    )


def _account(host: dict[str, Any]) -> str:
    user = host.get("user") if isinstance(host.get("user"), str) else ""
    return f"{user}@{host.get('hostname')}" if user else str(host.get("hostname"))


def _commands(raw: dict[str, Any]) -> dict[str, str]:
    """The commands a config runs when you click, by where they're set:
    the editor, the default browser and each account's browser."""
    out: dict[str, str] = {}
    for key in ("editor", "browser"):
        command = _as_dict(raw.get(key)).get("command")
        if isinstance(command, str) and command.strip():
            out[key] = command.strip()
    for host in raw.get("hosts") or []:
        browser = host.get("browser") if isinstance(host, dict) else None
        if isinstance(browser, str) and browser.strip():
            out[f"browser for {_account(host)}"] = browser.strip()
    return out


def _keep_current_commands(raw: dict[str, Any]) -> None:
    """Put this machine's commands back into an imported config, so nothing
    from the backup runs unless asked for (`keep_commands`)."""
    current = _commands(_read_current_raw())
    for key in ("editor", "browser"):
        section = raw.get(key)
        if isinstance(section, dict) and "command" in section:
            section["command"] = current.get(key, "")
    for host in raw.get("hosts") or []:
        if isinstance(host, dict) and "browser" in host:
            host["browser"] = current.get(f"browser for {_account(host)}", "")


def _bucket_sounds(raw: dict[str, Any]) -> list[str]:
    tray = _as_dict(raw.get("tray_icon"))
    sounds = []
    for b in tray.get("buckets") or []:
        if isinstance(b, dict) and isinstance(b.get("sound"), str) and b["sound"]:
            sounds.append(b["sound"])
    return sounds


def apply_import(
    backup: Backup,
    plan: ImportPlan,
    clone_missing: bool = False,
    clone: Callable[[str, str], bool] | None = None,
    keep_commands: bool = False,
) -> list[str]:
    """Replace the current settings with the backup. Returns log lines.

    The current config is snapshotted first, so an import can always be
    undone from the backups folder.
    """
    lines = []
    snap = snapshot("pre-import")
    if snap:
        lines.append(f"previous settings saved to {snap}")

    raw = _localized(backup)
    if plan.new_commands:
        if keep_commands:
            lines.append(f"imported {len(plan.new_commands)} command(s) from the backup")
        else:
            _keep_current_commands(raw)
            lines.append(f"kept this machine's commands ({len(plan.new_commands)} not imported)")
    missing = set(plan.missing_sounds)
    tray = raw.get("tray_icon")
    if isinstance(tray, dict):
        for b in tray.get("buckets") or []:
            if isinstance(b, dict) and b.get("sound") in missing:
                b["sound"] = ""

    cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cfg_mod.CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    tmp.replace(cfg_mod.CONFIG_PATH)
    lines.append(f"settings imported into {cfg_mod.CONFIG_PATH}")

    if clone_missing and plan.missing_clones:
        clone = clone or _git_clone
        for path, url in plan.missing_clones:
            problem = clone_problem(url, path)
            if problem:
                lines.append(f"skipped {url!r} → {path!r}: {problem}")
                log_mod.warn(f"backup: not cloning {url!r} into {path!r}: {problem}")
                continue
            ok = clone(url, path)
            lines.append(("cloned " if ok else "FAILED to clone ") + f"{url} → {path}")
    return lines


# Network remotes only: a backup is data from another machine (or someone
# else), and git also understands `ext::<command>`, `fd::` and local paths.
_REMOTE = re.compile(r"^(https://|ssh://|git://)[^\s]+$|^[\w.-]+@[\w.-]+:[^\s]+$")


def clone_problem(url: str, path: str) -> str | None:
    """Why a clone recorded in a backup won't be run, or None. The URL and
    the path both come from the backup file, so neither may pass as a git
    option, the URL must be a plain network remote, and the folder must be
    an ordinary one in your home: a clone into ~/.bashrc.d, ~/.config/...
    or ~/Library/LaunchAgents would run its code at the next login."""
    if url.startswith("-") or not _REMOTE.match(url):
        return "not an https://, ssh://, git:// or user@host: remote"
    if path.startswith("-") or not os.path.isabs(path):
        return "not an absolute path"
    home = Path(os.path.realpath(_home()))
    try:
        inside = Path(os.path.realpath(path)).relative_to(home).parts
    except ValueError:
        return "outside your home folder"
    if not inside:
        return "your home folder itself"
    if any(part.startswith(".") for part in inside) or inside[0] == "Library":
        return "a hidden or system folder, where files can run at login"
    return None


def _git_clone(url: str, path: str) -> bool:
    if clone_problem(url, path):
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # `--` ends the options; the -c flags refuse local and command transports
    # even if a URL slipped through.
    cmd = [
        "git", "-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never",
        "-c", "protocol.file.allow=never", "clone", "--", url, path,
    ]  # fmt: skip
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        log_mod.warn(f"clone {strip_credentials(url)} failed: {e}")
        return False
    if res.returncode != 0:
        log_mod.warn(f"clone {strip_credentials(url)} failed: {res.stderr.strip()}")
    return res.returncode == 0


# ----- Snapshots -----


def backups_dir() -> Path:
    return cfg_mod.CONFIG_DIR / "backups"


def snapshot(reason: str) -> Path | None:
    """Copy the current config.json into the backups folder; keep the newest
    SNAPSHOT_KEEP. Best-effort: never raises."""
    # Upgrading from devtray, the settings are still in its folder: move them
    # into place first, as load() would, so the riskiest update is backed up.
    cfg_mod.migrate_legacy_dir()
    if not cfg_mod.CONFIG_PATH.exists():
        return None
    try:
        folder = backups_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = folder / f"config-{stamp}-{reason}.json"
        shutil.copy2(cfg_mod.CONFIG_PATH, dest)
        os.chmod(dest, 0o600)
        for old in list_snapshots()[SNAPSHOT_KEEP:]:
            old.unlink(missing_ok=True)
        return dest
    except OSError as e:
        log_mod.warn(f"backup: snapshot failed: {e}")
        return None


def list_snapshots() -> list[Path]:
    """Newest first."""
    folder = backups_dir()
    if not folder.is_dir():
        return []
    return sorted(folder.glob("config-*.json"), reverse=True)


def snapshot_daily() -> Path | None:
    """One snapshot per day, taken before the first save of the day."""
    today = datetime.now().strftime("%Y%m%d")
    if any(p.name.startswith(f"config-{today}-") for p in list_snapshots()):
        return None
    return snapshot("daily")
