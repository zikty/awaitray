import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from awaitray import cli
from awaitray import config as cfg_mod
from awaitray import state as state_mod
from awaitray.github import PRDetails, PullRequest


def _pr(n):
    return PullRequest(
        "github.com", "acme/web", n, f"PR {n}", f"https://github.com/acme/web/pull/{n}", "alice"
    )


class StatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for p in (
            mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}),
            mock.patch.object(cfg_mod, "CONFIG_DIR", root / "cfg"),
            mock.patch.object(cfg_mod, "CONFIG_PATH", root / "cfg" / "config.json"),
            mock.patch.object(cfg_mod, "LEGACY_CONFIG_DIR", root / "legacy"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _state(self, review, saved_at=None, snoozed=()):
        cfg = cfg_mod.load()
        for pr in snoozed:
            cfg.snoozed[pr.key] = cfg_mod.SnoozeEntry(head_oid="x")
        cfg_mod.save(cfg)
        state_mod.StateStore().save(
            state_mod.Snapshot(
                scope="s",
                saved_at=(saved_at or datetime.now(timezone.utc)).isoformat(),
                review_keys={p.key for p in review},
                review_prs=review,
                details={review[0].url: PRDetails(review[0].url, "o", "FAILURE", None)}
                if review
                else {},
            )
        )

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_no_state_yet(self):
        code, out = self._run("status", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"available": False})

    def test_json_counts_exclude_snoozed(self):
        prs = [_pr(1), _pr(2), _pr(3)]
        self._state(prs, snoozed=[prs[2]])
        code, out = self._run("status", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((data["review"], data["total"], data["stale"]), (2, 2, False))
        self.assertEqual(data["review_prs"][0]["ci"], "FAILURE")

    def test_format_template_and_staleness(self):
        self._state([_pr(1)], saved_at=datetime.now(timezone.utc) - timedelta(hours=3))
        _code, out = self._run("status", "--format", "{review}|{stale}|{age}")
        self.assertEqual(out.strip(), "1|stale|3h")

    def test_snapshot_command_copies_settings_before_an_update(self):
        code, out = self._run("config", "snapshot")
        self.assertEqual(code, 0)  # fresh install: nothing to copy, still success
        self.assertIn("nothing to snapshot", out)
        cfg_mod.save(cfg_mod.Config(clone_root="/code"))
        code, out = self._run("config", "snapshot", "pre update!")
        self.assertEqual(code, 0)
        path = Path(out.strip())
        self.assertTrue(path.name.endswith("-pre-update.json"), path.name)
        self.assertEqual(json.loads(path.read_text())["clone_root"], "/code")


if __name__ == "__main__":
    unittest.main()
