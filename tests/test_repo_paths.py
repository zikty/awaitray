"""Repo → local folder mapping."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awaitray import backup, repo_sync
from awaitray import prefs_model as pm
from awaitray import repos as repo_mod
from awaitray.config import Config, RepoConfig
from awaitray.github import PullRequest


def _pr(repo):
    return PullRequest("github.com", repo, 1, "t", "u", "a")


class RepoPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for d in ("acme/api", "legacy-monorepo", "other/api"):
            (self.root / d).mkdir(parents=True)
        p = mock.patch.object(
            repo_mod, "discover_repos", return_value=[str(self.root / "other/api")]
        )
        p.start()
        self.addCleanup(p.stop)

    def _cfg(self, *rules):
        return Config(
            repos=[RepoConfig(str(self.root / "other"), auto_discover=True)],
            repo_paths=list(rules),
        )

    def test_exact_beats_glob_beats_basename(self):
        cfg = self._cfg(
            f"acme/* = {self.root}/acme/*",
            f"acme/legacy = {self.root}/legacy-monorepo",
        )
        self.assertEqual(
            repo_sync.local_path_for(cfg, _pr("acme/legacy")), str(self.root / "legacy-monorepo")
        )
        self.assertEqual(
            repo_sync.local_path_for(cfg, _pr("Acme/api")), str(self.root / "acme/api")
        )
        # No mapping: basename, as before.
        self.assertEqual(
            repo_sync.local_path_for(cfg, _pr("team-b/api")), str(self.root / "other/api")
        )

    def test_a_missing_mapped_folder_never_falls_back_to_another_owners_repo(self):
        cfg = self._cfg(f"acme/* = {self.root}/acme/*")
        self.assertIsNone(repo_sync.local_path_for(cfg, _pr("acme/web")))
        self.assertEqual(repo_sync.clone_target(cfg, _pr("acme/web")), str(self.root / "acme/web"))

    def test_home_is_expanded_and_malformed_lines_are_ignored(self):
        cfg = self._cfg("nonsense", "acme/* = ~/code/*")
        self.assertEqual(
            repo_sync.mapped_path(cfg, _pr("acme/web")), os.path.expanduser("~/code/web")
        )

    def test_clone_target_without_mapping_uses_the_clone_root(self):
        cfg = Config(clone_root="/src")
        self.assertEqual(repo_sync.clone_target(cfg, _pr("acme/web")), "/src/web")
        self.assertIsNone(repo_sync.clone_target(Config(), _pr("acme/web")))

    def test_preferences_validate_the_format(self):
        field = next(f for f in pm.all_fields() if f.key == "repo_paths")
        self.assertIsNone(pm.validate(field, ["acme/* = ~/code/*"]))
        self.assertIn("owner/repo = folder", pm.validate(field, ["acme/*"]))

    def test_backups_carry_portable_paths(self):
        raw = {"repo_paths": [f"acme/* = {Path.home()}/code/*", "junk"]}
        backup._map_paths(raw, backup.collapse_home)
        self.assertEqual(raw["repo_paths"], ["acme/* = ~/code/*", "junk"])


if __name__ == "__main__":
    unittest.main()
