import unittest
from unittest import mock

from awaitray import config as cfg_mod
from awaitray import repo_sync
from awaitray import repos as repo_mod


class FetchWorkerExclusionTests(unittest.TestCase):
    def _run_worker(self, repo_cfg, discovered):
        cfg = cfg_mod.Config(repos=[repo_cfg])
        fetched = []

        def fake_fetch(path, remote, auto_update=False):
            fetched.append(path)
            return repo_mod.FetchResult(path, True, "ok")

        with (
            mock.patch.object(repo_mod, "discover_repos", return_value=list(discovered)),
            mock.patch.object(repo_mod, "fetch", side_effect=fake_fetch),
        ):
            repo_sync.fetch_all(cfg)
        return fetched

    def test_excluded_children_are_skipped(self):
        repo = cfg_mod.RepoConfig(path="/parent", auto_discover=True, excluded=["/parent/b"])
        fetched = self._run_worker(repo, ["/parent/a", "/parent/b", "/parent/c"])
        self.assertEqual(fetched, ["/parent/a", "/parent/c"])

    def test_no_exclusions_fetches_all(self):
        repo = cfg_mod.RepoConfig(path="/parent", auto_discover=True)
        fetched = self._run_worker(repo, ["/parent/a", "/parent/b"])
        self.assertEqual(fetched, ["/parent/a", "/parent/b"])

    def test_excluded_ignored_for_single_repo(self):
        # `excluded` only applies to auto_discover entries.
        repo = cfg_mod.RepoConfig(path="/solo", auto_discover=False, excluded=["/solo"])
        fetched = self._run_worker(repo, [])
        self.assertEqual(fetched, ["/solo"])


class ExcludedConfigRoundtripTests(unittest.TestCase):
    def test_excluded_roundtrips(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "repos": [
                            {
                                "path": "/p",
                                "enabled": True,
                                "remote": "origin",
                                "auto_discover": True,
                                "auto_update": False,
                                "excluded": ["/p/x"],
                            }
                        ],
                    }
                )
            )
            raw = json.loads(path.read_text())
            loaded = cfg_mod.RepoConfig(**raw["repos"][0])
            self.assertEqual(loaded.excluded, ["/p/x"])
            # default when missing
            self.assertEqual(cfg_mod.RepoConfig(path="/q").excluded, [])


if __name__ == "__main__":
    unittest.main()
