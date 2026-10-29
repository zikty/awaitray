"""Sound packs: synthesized built-ins, your own packs, one sound per notice."""

import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from awaitray import prefs_model as pm
from awaitray import sounds, transitions
from awaitray.config import Config, SoundsConfig
from awaitray.controller import Controller, PollResult
from awaitray.github import PullRequest


def _pr(n=1):
    return PullRequest("github.com", "acme/web", n, f"PR {n}", f"https://x/{n}", "alice")


class _Dirs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.user = Path(tmp.name) / "user"
        self.cache = Path(tmp.name) / "cache"
        self.user.mkdir()


class SynthesisTests(unittest.TestCase):
    def test_every_builtin_sound_is_a_short_audible_wav(self):
        for pack in sounds.BUILTIN:
            for slot in sounds.SLOTS:
                with wave.open(io.BytesIO(sounds.synthesize(pack, slot))) as w:
                    self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2))
                    seconds = w.getnframes() / w.getframerate()
                    self.assertTrue(0.05 < seconds < 1.5, (pack, slot, seconds))
                    frames = w.readframes(w.getnframes())
                samples = [
                    int.from_bytes(frames[i : i + 2], "little", signed=True)
                    for i in range(0, len(frames), 2)
                ]
                peak = max(abs(s) for s in samples)
                # Loud enough to hear, never clipped, and it starts silent (no click).
                self.assertTrue(3000 < peak < 32767, (pack, slot, peak))
                self.assertLess(abs(samples[0]), 500, (pack, slot))

    def test_every_effect_is_audible_and_distinct(self):
        bodies = set()
        for fx in sounds.FX:
            body = sounds.synthesize("fx", fx)
            with wave.open(io.BytesIO(body)) as w:
                self.assertTrue(0.04 < w.getnframes() / w.getframerate() < 2.5, fx)
            bodies.add(body)
        self.assertEqual(len(bodies), len(sounds.FX))

    def test_the_four_slots_sound_different(self):
        for pack in sounds.BUILTIN:
            bodies = {sounds.synthesize(pack, slot) for slot in sounds.SLOTS}
            self.assertEqual(len(bodies), len(sounds.SLOTS), pack)


class PackTests(_Dirs):
    def test_builtins_then_your_packs(self):
        (self.user / "mario").mkdir()
        (self.user / "mario" / "review.wav").write_bytes(b"x")
        (self.user / "empty").mkdir()  # no sound in it: not a pack
        ids = [(p.id, p.builtin) for p in sounds.packs(self.user)]
        self.assertEqual(
            ids,
            [
                ("arcade", True),
                ("retro", True),
                ("scifi", True),
                ("chimes", True),
                ("mario", False),
            ],
        )

    def test_builtin_sounds_are_written_once_to_the_cache(self):
        path = sounds.path_for("retro", sounds.GOOD, self.user, self.cache)
        self.assertTrue(path and os.path.isfile(path))
        mtime = os.path.getmtime(path)
        self.assertEqual(sounds.path_for("retro", sounds.GOOD, self.user, self.cache), path)
        self.assertEqual(os.path.getmtime(path), mtime)

    def test_your_pack_plays_its_files_and_leaves_missing_slots_silent(self):
        mine = self.user / "mario"
        mine.mkdir()
        (mine / "review.ogg").write_bytes(b"x")
        self.assertEqual(
            sounds.path_for("mario", sounds.REVIEW, self.user, self.cache), str(mine / "review.ogg")
        )
        self.assertIsNone(sounds.path_for("mario", sounds.BAD, self.user, self.cache))

    def test_a_folder_named_like_a_builtin_replaces_it(self):
        mine = self.user / "retro"
        mine.mkdir()
        (mine / "bad.wav").write_bytes(b"x")
        self.assertEqual(
            sounds.path_for("retro", sounds.BAD, self.user, self.cache), str(mine / "bad.wav")
        )
        self.assertIsNone(sounds.path_for("retro", sounds.GOOD, self.user, self.cache))
        labels = {p.id: p.label for p in sounds.packs(self.user)}
        self.assertEqual(labels["retro"], "Retro (8-bit) (yours)")

    def test_nothing_to_play(self):
        for pack, slot in (("", sounds.REVIEW), ("gone", sounds.REVIEW), ("retro", "nope")):
            self.assertIsNone(sounds.path_for(pack, slot, self.user, self.cache))


class EventSoundTests(_Dirs):
    """Your PRs' events: the pack's sound, its own file, or your choice."""

    def resolve(self, pack, event, choice=None, slot=sounds.GOOD):
        events = {} if choice is None else {event: choice}
        cfg = SoundsConfig(enabled=True, pack=pack, events=events)
        return sounds.resolve(cfg, slot, event, self.user, self.cache)

    def test_by_default_the_pack_plays_its_slot(self):
        self.assertTrue(self.resolve("retro", "approved").endswith("retro/good-v1.wav"))

    def test_arcade_wins_and_loses(self):
        self.assertTrue(self.resolve("arcade", "approved").endswith("fx/victory-v1.wav"))
        failed = self.resolve("arcade", "ci_failed", slot=sounds.BAD)
        self.assertTrue(failed.endswith("fx/game_over-v1.wav"))

    def test_your_pack_can_have_a_file_per_event(self):
        mine = self.user / "mario"
        mine.mkdir()
        (mine / "good.wav").write_bytes(b"x")
        (mine / "approved.wav").write_bytes(b"x")
        self.assertEqual(self.resolve("mario", "approved"), str(mine / "approved.wav"))
        self.assertEqual(self.resolve("mario", "ci_passed"), str(mine / "good.wav"))

    def test_your_choice_wins(self):
        self.assertTrue(self.resolve("retro", "approved", "fx:victory").endswith("victory-v1.wav"))
        self.assertTrue(self.resolve("retro", "approved", sounds.BAD).endswith("retro/bad-v1.wav"))
        self.assertIsNone(self.resolve("retro", "approved", sounds.SILENT))
        own = self.user / "win.wav"
        own.write_bytes(b"x")
        self.assertEqual(self.resolve("retro", "approved", str(own)), str(own))

    def test_a_chosen_file_that_is_gone_falls_back_to_the_pack(self):
        gone = str(self.user / "gone.wav")
        self.assertTrue(self.resolve("retro", "approved", gone).endswith("retro/good-v1.wav"))

    def test_hand_edited_nonsense_is_ignored(self):
        cfg = SoundsConfig(enabled=True, pack="retro", events={"approved": 3})  # type: ignore[dict-item]
        path = sounds.resolve(cfg, sounds.GOOD, "approved", self.user, self.cache)
        self.assertTrue(path.endswith("retro/good-v1.wav"))


class SlotTests(unittest.TestCase):
    def test_each_kind_of_news_has_its_sound(self):
        slot = {e: transitions.notice(_pr(), e).sound for e in transitions.EVENTS}
        self.assertEqual(slot[transitions.APPROVED], sounds.GOOD)
        self.assertEqual(slot[transitions.READY_TO_MERGE], sounds.GOOD)
        self.assertEqual(slot[transitions.CI_PASSED], sounds.GOOD)
        self.assertEqual(slot[transitions.CI_FAILED], sounds.BAD)
        self.assertEqual(slot[transitions.CHANGES_REQUESTED], sounds.BAD)
        self.assertEqual(slot[transitions.CONFLICT], sounds.BAD)
        self.assertEqual(slot[transitions.NEW_COMMENTS], sounds.INFO)


class _UI:
    def __init__(self):
        self.played = []

    def play_sound(self, sound):
        self.played.append(sound)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    """Through the controller's seam: a poll that brings new review requests."""

    def _ctl(self, **cfg):
        ui = _UI()
        ctl = Controller(ui=ui, host=ui, cfg=Config(**cfg))
        patcher = mock.patch.object(
            sounds, "path_for", side_effect=lambda p, s, *_: f"/{p}/{s}.wav"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ctl.apply_poll_result(PollResult(review_prs=[]))  # baseline: nothing yet
        return ctl, ui

    def _arrive(self, ctl, *numbers):
        ctl.apply_poll_result(PollResult(review_prs=[_pr(n) for n in numbers]))

    def test_a_new_review_request_plays_the_pack_sound(self):
        ctl, ui = self._ctl(sounds=SoundsConfig(enabled=True, pack="scifi"))
        self._arrive(ctl, 1)
        self.assertEqual(ui.played, ["/scifi/review.wav"])

    def test_off_or_quiet_plays_nothing(self):
        ctl, ui = self._ctl(sounds=SoundsConfig(enabled=False))
        self._arrive(ctl, 1)
        ctl2, ui2 = self._ctl(sounds=SoundsConfig(enabled=True))
        with mock.patch("awaitray.controller.qh_mod.is_quiet", return_value=True):
            self._arrive(ctl2, 1)
        self.assertEqual((ui.played, ui2.played), ([], []))

    def test_a_burst_of_notices_plays_one_sound(self):
        ctl, ui = self._ctl(sounds=SoundsConfig(enabled=True))
        self._arrive(ctl, 1, 2, 3)  # three notices, one poll
        self.assertEqual(ui.played, ["/retro/review.wav"])


class PrefsTests(unittest.TestCase):
    FIELD = next(f for f in pm.all_fields() if f.key == "sounds.pack")

    def test_packs_are_offered_and_accepted(self):
        with mock.patch.object(sounds, "packs", return_value=[sounds.Pack("mine", "mine", False)]):
            self.assertEqual(pm.choices_for(self.FIELD), (("mine", "mine"),))
            self.assertIsNone(pm.validate(self.FIELD, "mine"))
            self.assertIsNotNone(pm.validate(self.FIELD, "retro"))

    def test_picking_a_pack_previews_its_review_sound(self):
        with mock.patch.object(sounds, "path_for", return_value="/p.wav") as path_for:
            self.assertEqual(pm.preview_sound(self.FIELD, "chimes", Config()), "/p.wav")
        path_for.assert_called_once_with("chimes", sounds.REVIEW)
        other = next(f for f in pm.all_fields() if f.key == "sounds.enabled")
        self.assertIsNone(pm.preview_sound(other, True, Config()))


class SoundFieldTests(unittest.TestCase):
    FIELD = next(f for f in pm.all_fields() if f.key == "sounds.events.approved")

    def test_every_event_has_a_field(self):
        keys = {f.key for f in pm.all_fields() if f.kind == pm.SOUND}
        self.assertEqual(keys, {f"sounds.events.{e}" for e in sounds.EVENTS})

    def test_choices_and_validation(self):
        values = [v for v, _ in pm.sound_choices()]
        self.assertIn("fx:victory", values)
        self.assertIn("fx:game_over", values)
        for ok in ("", "good", "none", "fx:coin", "/home/me/win.wav"):
            self.assertIsNone(pm.validate(self.FIELD, ok), ok)
        for bad in ("fx:nope", "win.wav", "/home/me/notes.txt"):
            self.assertIsNotNone(pm.validate(self.FIELD, bad), bad)

    def test_stored_per_event_and_default_not_stored(self):
        cfg = Config()
        pm.set_value(cfg, "sounds.events.approved", "fx:victory")
        self.assertEqual(cfg.sounds.events, {"approved": "fx:victory"})
        self.assertEqual(pm.get_value(cfg, "sounds.events.approved"), "fx:victory")
        self.assertEqual(pm.get_value(cfg, "sounds.events.conflict"), "")
        pm.set_value(cfg, "sounds.events.approved", "")
        self.assertEqual(cfg.sounds.events, {})

    def test_picking_plays_that_sound(self):
        with mock.patch.object(sounds, "fx_path", return_value="/v.wav") as fx:
            self.assertEqual(pm.preview_sound(self.FIELD, "fx:victory", Config()), "/v.wav")
        fx.assert_called_once()


if __name__ == "__main__":
    unittest.main()
