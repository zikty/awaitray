import copy
import unittest
from dataclasses import fields, is_dataclass

from awaitray import config as cfg_mod
from awaitray import prefs_model as pm


class CatalogTests(unittest.TestCase):
    def test_six_sections_in_spec_order(self):
        self.assertEqual(
            [s.id for s in pm.SECTIONS],
            [
                "accounts",
                "pull_requests",
                "notifications",
                "repositories",
                "appearance",
                "advanced",
            ],
        )

    def test_every_field_resolves_with_the_right_type(self):
        cfg = cfg_mod.Config()
        expected = {
            pm.BOOL: bool,
            pm.INT: int,
            pm.TEXT: str,
            pm.TIME: str,
            pm.CHOICE: str,
            pm.LINES: list,
            pm.COLOR: str,
            pm.BROWSER: str,
            pm.SOUND: str,
        }
        for f in pm.all_fields():
            if f.kind == pm.CUSTOM:
                continue
            value = pm.get_value(cfg, f.key)
            self.assertIsInstance(value, expected[f.kind], f.key)
            if f.depends_on:
                self.assertIsInstance(pm.get_value(cfg, f.depends_on), bool, f.depends_on)

    def test_every_setting_is_reachable_from_preferences(self):
        """Each Config setting must be editable somewhere (no orphan settings)."""
        covered = {f.key for f in pm.all_fields()}
        # Runtime/user data managed elsewhere, not settings.
        not_settings = {"snoozed", "paused", "_extra", "backup", "logs"}

        def leaves(obj, prefix=""):
            for fl in fields(obj):
                key = f"{prefix}{fl.name}"
                value = getattr(obj, fl.name)
                if key in covered or key in not_settings:
                    continue
                if any(c.startswith(key + ".") for c in covered):
                    continue  # a dict setting, one field per key (sounds.events)
                if is_dataclass(value):
                    yield from leaves(value, key + ".")
                else:
                    yield key

        missing = sorted(leaves(cfg_mod.Config()))
        self.assertEqual(missing, [], "settings without a place in Preferences")

    def test_keys_are_unique(self):
        keys = [f.key for f in pm.all_fields()]
        self.assertEqual(len(keys), len(set(keys)))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_mod.Config()
        self.by_key = {f.key: f for f in pm.all_fields()}

    def test_int_bounds(self):
        f = self.by_key["intervals.pr_check_minutes"]
        self.assertEqual(pm.apply(self.cfg, f, 0), "At least 1")
        self.assertEqual(pm.apply(self.cfg, f, 999), "At most 240")
        self.assertIsNone(pm.apply(self.cfg, f, 10))
        self.assertEqual(self.cfg.intervals.pr_check_minutes, 10)

    def test_time_is_validated_and_normalized(self):
        f = self.by_key["quiet_hours.start"]
        self.assertEqual(pm.apply(self.cfg, f, "25:00"), "Use HH:MM, e.g. 08:30")
        self.assertIsNone(pm.apply(self.cfg, f, " 7:05 "))
        self.assertEqual(self.cfg.quiet_hours.start, "07:05")

    def test_color_and_choice(self):
        color = self.by_key["colors.draft"]
        self.assertIsNotNone(pm.apply(self.cfg, color, "red"))
        self.assertIsNone(pm.apply(self.cfg, color, "#abcdef"))
        self.assertEqual(self.cfg.colors.draft, "#ABCDEF")
        self.assertIsNone(pm.apply(self.cfg, color, ""))  # off
        choice = self.by_key["error_policy.on_error"]
        self.assertIsNotNone(pm.apply(self.cfg, choice, "explode"))
        self.assertIsNone(pm.apply(self.cfg, choice, "retry"))

    def test_invalid_values_are_not_written(self):
        f = self.by_key["intervals.fetch_minutes"]
        before = self.cfg.intervals.fetch_minutes
        pm.apply(self.cfg, f, -5)
        self.assertEqual(self.cfg.intervals.fetch_minutes, before)

    def test_editing_a_working_copy_keeps_fields_the_window_doesnt_show(self):
        cfg = cfg_mod.Config(repos=[cfg_mod.RepoConfig("/r", auto_update=True, excluded=["/r/x"])])
        working = copy.deepcopy(cfg)
        pm.apply(working, self.by_key["intervals.fetch_minutes"], 30)
        self.assertTrue(working.repos[0].auto_update)
        self.assertEqual(working.repos[0].excluded, ["/r/x"])
        self.assertEqual(cfg.intervals.fetch_minutes, cfg_mod.Intervals().fetch_minutes)

    def test_dependent_fields(self):
        f = self.by_key["quiet_hours.start"]
        self.assertFalse(pm.is_active(self.cfg, f))
        self.cfg.quiet_hours.enabled = True
        self.assertTrue(pm.is_active(self.cfg, f))

    def test_exclusions_keep_entries_for_missing_subfolders(self):
        self.assertEqual(
            pm.exclusions_after_choice(["/c/gone", "/c/a"], ["/c/a", "/c/b"], ["/c/b"]),
            ["/c/gone", "/c/b"],
        )

    def test_parse_lines(self):
        self.assertEqual(pm.parse_lines(" a \n\n b\n"), ["a", "b"])


class EditSessionTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        self.timers = []
        self.committed = []
        patcher = mock.patch.object(cfg_mod, "save")
        self.save = patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = cfg_mod.Config()
        self.session = pm.EditSession(
            self.cfg,
            self.committed.append,
            schedule=lambda ms, fn: self.timers.append(fn) or len(self.timers),
            cancel=lambda handle: self.timers.__setitem__(handle - 1, None),
        )
        self.field = next(f for f in pm.all_fields() if f.key == "intervals.fetch_minutes")

    def _fire_pending(self):
        live = [fn for fn in self.timers if fn]
        self.timers.clear()
        for fn in live:
            fn()

    def test_edits_are_debounced_into_one_commit(self):
        self.session.edit(self.field, 20)
        self.session.edit(self.field, 25)
        self.assertEqual(self.committed, [])
        self._fire_pending()
        self.assertEqual(len(self.committed), 1)
        self.assertEqual(self.committed[0].intervals.fetch_minutes, 25)
        self.save.assert_called_once()

    def test_commit_hands_over_a_copy_and_never_touches_the_original(self):
        self.session.edit(self.field, 20)
        self.session.commit()
        self.assertIsNot(self.committed[0], self.session.working)
        self.assertEqual(self.cfg.intervals.fetch_minutes, cfg_mod.Intervals().fetch_minutes)
        self.assertFalse(self.session.pending)

    def test_reset_drops_pending_edits(self):
        self.session.edit(self.field, 20)
        fresh = cfg_mod.Config(intervals=cfg_mod.Intervals(fetch_minutes=99))
        self.session.reset(fresh)
        self._fire_pending()
        self.assertEqual(self.committed, [])
        self.assertEqual(self.session.working.intervals.fetch_minutes, 99)
        self.assertIsNot(self.session.working, fresh)

    def test_commit_keeps_snoozes_and_pause_made_from_the_menu_meanwhile(self):
        live = cfg_mod.Config()
        session = pm.EditSession(
            live,
            self.committed.append,
            schedule=lambda ms, fn: 1,
            cancel=lambda h: None,
            live=lambda: live,
        )
        live.snoozed["k"] = cfg_mod.SnoozeEntry(head_oid="x")  # snoozed from the menu
        live.paused = True
        session.edit(self.field, 30)
        session.commit()
        self.assertIn("k", self.committed[-1].snoozed)
        self.assertTrue(self.committed[-1].paused)
        self.assertEqual(self.committed[-1].intervals.fetch_minutes, 30)

    def test_invalid_edit_does_not_schedule(self):
        self.assertIsNotNone(self.session.edit(self.field, 0))
        self.assertEqual([fn for fn in self.timers if fn], [])


if __name__ == "__main__":
    unittest.main()
