import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awaitray import config as cfg_mod


class _TmpHomeMixin:
    """Reload the config module against a tmp XDG_CONFIG_HOME for each test.

    The module computes CONFIG_DIR at import time, so we patch the constants
    rather than re-importing.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)
        self._patch_dir = mock.patch.object(cfg_mod, "CONFIG_DIR", tmp_path / "awaitray")
        self._patch_path = mock.patch.object(
            cfg_mod, "CONFIG_PATH", tmp_path / "awaitray" / "config.json"
        )
        self._patch_legacy = mock.patch.object(cfg_mod, "LEGACY_CONFIG_DIR", tmp_path / "devtray")
        self._patch_dir.start()
        self._patch_path.start()
        self._patch_legacy.start()
        cache = mock.patch.object(cfg_mod, "LEGACY_CACHE_DIR", tmp_path / "cache" / "devtray")
        cache.start()
        self.addCleanup(cache.stop)
        self.addCleanup(self._patch_dir.stop)
        self.addCleanup(self._patch_path.stop)
        self.addCleanup(self._patch_legacy.stop)
        self._patch_warning = mock.patch.object(cfg_mod, "last_load_warning", None)
        self._patch_warning.start()
        self.addCleanup(self._patch_warning.stop)


class LoadSaveRoundtripTests(_TmpHomeMixin, unittest.TestCase):
    def test_load_creates_default_when_missing(self):
        cfg = cfg_mod.load()
        self.assertEqual(cfg.hosts[0].hostname, "github.com")
        self.assertTrue(cfg_mod.CONFIG_PATH.exists())

    def test_roundtrip_preserves_known_fields(self):
        original = cfg_mod.Config(
            hosts=[cfg_mod.HostConfig("ghe.example.com", enabled=False)],
            repos=[cfg_mod.RepoConfig(path="/tmp/x", auto_discover=True)],
            intervals=cfg_mod.Intervals(pr_check_minutes=7, fetch_minutes=21),
            filters=cfg_mod.Filters(
                ignored_authors=["foo[bot]"], ignored_repos=["*/legacy-*"], include_drafts=False
            ),
            paused=True,
        )
        cfg_mod.save(original)
        loaded = cfg_mod.load()

        self.assertEqual(loaded.hosts[0].hostname, "ghe.example.com")
        self.assertFalse(loaded.hosts[0].enabled)
        self.assertEqual(loaded.repos[0].path, "/tmp/x")
        self.assertTrue(loaded.repos[0].auto_discover)
        self.assertEqual(loaded.intervals.pr_check_minutes, 7)
        self.assertEqual(loaded.intervals.fetch_minutes, 21)
        self.assertEqual(loaded.filters.ignored_authors, ["foo[bot]"])
        self.assertFalse(loaded.filters.include_drafts)
        self.assertTrue(loaded.paused)


# Fields whose load step normalizes free values back to a known choice.
_CHOICES = {"error_policy.on_error": "retry", "tray_icon.sound_trigger": "every"}


class EveryFieldRoundtripTests(_TmpHomeMixin, unittest.TestCase):
    """Load/save are derived from the dataclasses: a new field must need no
    registration to survive a round trip."""

    def _changed(self, key, value):
        if key in _CHOICES:
            return _CHOICES[key]
        if isinstance(value, bool):
            return not value
        if isinstance(value, int):
            return value + 1
        if isinstance(value, str):
            return "/changed" if value.startswith("/") or key == "clone_root" else "changed"
        if key == "tray_icon.buckets":
            return [cfg_mod.Bucket(up_to=2, emoji="x", sound="Glass")]
        if isinstance(value, list):
            return ["changed"]
        if isinstance(value, dict):
            return {"changed": "changed"}
        raise AssertionError(f"no test value for {key} ({type(value).__name__})")

    def _mutate(self, obj, prefix=""):
        for f in dataclasses.fields(obj):
            key = f"{prefix}{f.name}"
            value = getattr(obj, f.name)
            if f.name.startswith("_") or key in ("hosts", "repos", "snoozed", "sections"):
                continue
            if dataclasses.is_dataclass(value):
                self._mutate(value, key + ".")
            else:
                setattr(obj, f.name, self._changed(key, value))

    def test_every_field_survives_save_and_load(self):
        cfg = cfg_mod.Config(
            hosts=[cfg_mod.HostConfig("ghe.corp", enabled=False)],
            repos=[cfg_mod.RepoConfig("/r", False, "up", True, True, ["/r/x"])],
            snoozed={"h/o/r#1": cfg_mod.SnoozeEntry("2030-01-01T00:00:00", "abc", "t", True)},
            sections=[
                cfg_mod.CustomSection("Mentions", "mentions:@me", "ghe.corp", False, True, True)
            ],
        )
        self._mutate(cfg)
        self.assertNotEqual(cfg, cfg_mod.Config())
        cfg_mod.save(cfg)
        self.assertEqual(cfg_mod.load(), cfg)


class AdditiveSchemaTests(_TmpHomeMixin, unittest.TestCase):
    def test_unknown_top_level_key_is_preserved(self):
        cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg_mod.CONFIG_PATH.write_text(
            json.dumps({"hosts": [{"hostname": "github.com"}], "future_field": {"foo": 1}})
        )
        loaded = cfg_mod.load()
        cfg_mod.save(loaded)
        rewritten = json.loads(cfg_mod.CONFIG_PATH.read_text())
        self.assertEqual(rewritten["future_field"], {"foo": 1})

    def test_unknown_nested_key_is_ignored_without_crash(self):
        cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg_mod.CONFIG_PATH.write_text(
            json.dumps(
                {
                    "intervals": {"pr_check_minutes": 4, "shiny_new_field": 99},
                }
            )
        )
        loaded = cfg_mod.load()
        self.assertEqual(loaded.intervals.pr_check_minutes, 4)

    def test_corrupt_json_falls_back_to_default(self):
        cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg_mod.CONFIG_PATH.write_text("{ this is not json")
        loaded = cfg_mod.load()
        self.assertEqual(loaded.hosts[0].hostname, "github.com")


class ErrorPolicyNormalizationTests(unittest.TestCase):
    def test_unknown_action_falls_back_to_wait(self):
        p = cfg_mod.ErrorPolicy(on_error="explode").normalized()
        self.assertEqual(p.on_error, "wait")

    def test_clamps_numeric_fields(self):
        p = cfg_mod.ErrorPolicy(
            max_retries=99, retry_backoff_seconds=99999, pause_after_failures=999
        ).normalized()
        self.assertEqual(p.max_retries, 10)
        self.assertEqual(p.retry_backoff_seconds, 3600)
        self.assertEqual(p.pause_after_failures, 50)


class LegacyConfigMigrationTests(_TmpHomeMixin, unittest.TestCase):
    def _write_legacy(self, payload: dict) -> None:
        cfg_mod.LEGACY_CONFIG_DIR.mkdir(parents=True)
        (cfg_mod.LEGACY_CONFIG_DIR / "config.json").write_text(json.dumps(payload))

    def test_legacy_dir_is_moved_on_first_load(self):
        self._write_legacy({"hosts": [{"hostname": "ghe.example.com"}]})
        cfg = cfg_mod.load()
        self.assertEqual(cfg.hosts[0].hostname, "ghe.example.com")
        self.assertTrue(cfg_mod.CONFIG_PATH.exists())
        self.assertFalse(cfg_mod.LEGACY_CONFIG_DIR.exists())

    def test_existing_new_config_wins_over_legacy(self):
        self._write_legacy({"hosts": [{"hostname": "old.example.com"}]})
        cfg_mod.CONFIG_DIR.mkdir(parents=True)
        cfg_mod.CONFIG_PATH.write_text(json.dumps({"hosts": [{"hostname": "new.example.com"}]}))
        cfg = cfg_mod.load()
        self.assertEqual(cfg.hosts[0].hostname, "new.example.com")
        self.assertTrue(cfg_mod.LEGACY_CONFIG_DIR.exists())

    def test_legacy_icon_cache_is_deleted(self):
        icons = cfg_mod.LEGACY_CACHE_DIR / "icons"
        icons.mkdir(parents=True)
        (icons / "devtray-idle.svg").write_text("<svg/>")
        cfg_mod.load()
        self.assertTrue(icons.exists())  # load() never touches a cache
        cfg_mod.drop_legacy_cache()
        self.assertFalse(cfg_mod.LEGACY_CACHE_DIR.exists())

    def test_legacy_cache_with_foreign_files_is_left_alone(self):
        cfg_mod.LEGACY_CACHE_DIR.mkdir(parents=True)
        (cfg_mod.LEGACY_CACHE_DIR / "notes.txt").write_text("mine")
        cfg_mod.drop_legacy_cache()
        self.assertTrue((cfg_mod.LEGACY_CACHE_DIR / "notes.txt").exists())

    def test_legacy_config_keeps_counting_team_requests(self):
        # devtray counted every review request; the split must not hide them.
        self._write_legacy({"tray_icon": {"show_count": True}})
        self.assertTrue(cfg_mod.load().tray_icon.count_team_requests)

    def test_config_without_tray_icon_keeps_counting_team_requests(self):
        self._write_legacy({"hosts": [{"hostname": "github.com"}]})
        self.assertTrue(cfg_mod.load().tray_icon.count_team_requests)

    def test_saved_choice_is_respected(self):
        cfg_mod.CONFIG_DIR.mkdir(parents=True)
        cfg_mod.CONFIG_PATH.write_text(json.dumps({"tray_icon": {"count_team_requests": False}}))
        self.assertFalse(cfg_mod.load().tray_icon.count_team_requests)

    def test_fresh_config_keeps_the_default(self):
        self.assertFalse(cfg_mod.load().tray_icon.count_team_requests)
        self.assertFalse(cfg_mod.load().tray_icon.count_team_requests)  # after save


class ForwardCompatibilityTests(_TmpHomeMixin, unittest.TestCase):
    """A config written by a newer version must load and survive a save."""

    def _write(self, payload):
        cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg_mod.CONFIG_PATH.write_text(json.dumps(payload))

    def test_hosts_and_users_that_arent_are_left_out(self):
        self._write(
            {
                "hosts": [
                    {"hostname": "github.com", "user": "me_corp"},
                    {"hostname": "ghe.corp:8443"},
                    {"hostname": "github.com\n"},
                    {"hostname": "--web"},
                    {"hostname": "ghe.corp", "user": "-x"},
                    {"hostname": "ghe.corp", "user": "a b"},
                    {"hostname": 7},
                ]
            }
        )
        with mock.patch.object(cfg_mod.log_mod, "warn"):
            hosts = cfg_mod.load().hosts
        self.assertEqual([h.account_id for h in hosts], ["me_corp@github.com", "ghe.corp:8443"])

    def _on_disk(self):
        return json.loads(cfg_mod.CONFIG_PATH.read_text())

    def test_unknown_keys_in_hosts_and_repos_do_not_crash(self):
        self._write(
            {
                "hosts": [{"hostname": "github.com", "user": "bob", "provider": "github"}],
                "repos": [{"path": "/r", "future_flag": True}],
            }
        )
        cfg = cfg_mod.load()
        self.assertEqual(cfg.hosts[0].hostname, "github.com")
        self.assertEqual(cfg.repos[0].path, "/r")

    def test_entries_missing_required_fields_are_skipped(self):
        self._write({"hosts": [{"enabled": True}, "junk", {"hostname": "ghe.corp"}]})
        cfg = cfg_mod.load()
        self.assertEqual([h.hostname for h in cfg.hosts], ["ghe.corp"])

    def test_unknown_keys_survive_a_save_from_fresh_objects(self):
        # Preferences rebuilds HostConfig/RepoConfig from scratch before saving.
        self._write(
            {
                "hosts": [{"hostname": "github.com", "future_proxy": "socks5://x"}],
                "repos": [{"path": "/r", "future_flag": True}],
                "intervals": {"pr_check_minutes": 5, "future_interval": 7},
                "snoozed": {
                    "github.com:a/b#1": {"until_iso": None, "head_oid": "x", "reason": "wait"}
                },
                "future_top_level": {"a": 1},
            }
        )
        cfg = cfg_mod.load()
        rebuilt = cfg_mod.Config(
            hosts=[cfg_mod.HostConfig("github.com", enabled=False)],
            repos=[cfg_mod.RepoConfig("/r")],
            snoozed=cfg.snoozed,
            _extra=cfg._extra,
        )
        cfg_mod.save(rebuilt)
        disk = self._on_disk()
        self.assertEqual(disk["hosts"][0]["future_proxy"], "socks5://x")
        self.assertFalse(disk["hosts"][0]["enabled"])  # known keys come from the new config
        self.assertTrue(disk["repos"][0]["future_flag"])
        self.assertEqual(disk["intervals"]["future_interval"], 7)
        self.assertEqual(disk["snoozed"]["github.com:a/b#1"]["reason"], "wait")
        self.assertEqual(disk["future_top_level"], {"a": 1})

    def test_two_accounts_on_one_host_keep_their_own_unknown_keys(self):
        self._write(
            {
                "hosts": [
                    {"hostname": "github.com", "user": "bob", "future": "b"},
                    {"hostname": "github.com", "user": "work", "future": "w"},
                ]
            }
        )
        cfg = cfg_mod.load()
        self.assertEqual([h.account_id for h in cfg.hosts], ["bob@github.com", "work@github.com"])
        cfg_mod.save(cfg)
        self.assertEqual([h["future"] for h in self._on_disk()["hosts"]], ["b", "w"])

    def test_removed_entries_stay_removed(self):
        self._write(
            {
                "hosts": [{"hostname": "github.com"}, {"hostname": "old.corp", "user": "x"}],
                "snoozed": {"github.com:a/b#1": {"head_oid": "x"}},
            }
        )
        cfg = cfg_mod.load()
        cfg.hosts = [h for h in cfg.hosts if h.hostname != "old.corp"]
        cfg.snoozed.clear()
        cfg_mod.save(cfg)
        disk = self._on_disk()
        self.assertEqual([h["hostname"] for h in disk["hosts"]], ["github.com"])
        self.assertEqual(disk["snoozed"], {})


class CorruptConfigTests(_TmpHomeMixin, unittest.TestCase):
    def _write_raw(self, text):
        cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg_mod.CONFIG_PATH.write_text(text)

    def _quarantined(self):
        return list(cfg_mod.CONFIG_DIR.glob("config.json.corrupt-*"))

    def test_corrupt_file_is_moved_aside_not_overwritten(self):
        self._write_raw('{"hosts": [ broken')
        cfg = cfg_mod.load()
        self.assertEqual(cfg.hosts[0].hostname, "github.com")  # defaults
        kept = self._quarantined()
        self.assertEqual(len(kept), 1)
        self.assertIn("broken", kept[0].read_text())
        cfg_mod.save(cfg)
        self.assertIn("broken", kept[0].read_text())  # the next save doesn't touch it
        self.assertIsNotNone(cfg_mod.last_load_warning)

    def test_wrong_top_level_type_is_quarantined(self):
        self._write_raw("[1, 2, 3]")
        cfg_mod.load()
        self.assertEqual(len(self._quarantined()), 1)

    def test_wrong_section_type_does_not_crash(self):
        self._write_raw(json.dumps({"hosts": "github.com", "intervals": [1]}))
        cfg = cfg_mod.load()
        self.assertEqual(cfg.hosts, [])
        self.assertEqual(cfg.intervals.pr_check_minutes, 5)

    def test_valid_file_sets_no_warning(self):
        self._write_raw(json.dumps({"hosts": [{"hostname": "github.com"}]}))
        cfg_mod.load()
        self.assertIsNone(cfg_mod.last_load_warning)
        self.assertEqual(self._quarantined(), [])


class XdgDirTests(unittest.TestCase):
    def test_empty_or_relative_means_the_default(self):
        home = str(Path.home())
        for value in ("", "relative/dir"):
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": value}):
                self.assertEqual(
                    str(cfg_mod.xdg_dir("XDG_CONFIG_HOME", ".config")), f"{home}/.config"
                )
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/x/conf"}):
            self.assertEqual(str(cfg_mod.xdg_dir("XDG_CONFIG_HOME", ".config")), "/x/conf")


if __name__ == "__main__":
    unittest.main()
