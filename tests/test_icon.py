import unittest

from awaitray import icon as icon_mod
from awaitray.config import DEFAULT_ICON_BUCKETS, Bucket, _parse_buckets


class ParseBucketsTests(unittest.TestCase):
    def test_parses_legacy_strings_and_sorts_with_catch_all_last(self):
        out = _parse_buckets(["15: ⚠️", "0: ✅", "*: 🚨", "5: 🔄"])
        self.assertEqual(
            [(b.up_to, b.emoji) for b in icon_mod._ordered(out)[0]],
            [(0, "✅"), (5, "🔄"), (15, "⚠️")],
        )
        self.assertEqual(icon_mod.pick_glyph(99, out), "🚨")

    def test_parses_structured_dicts_with_sound(self):
        out = _parse_buckets(
            [{"up_to": 5, "emoji": "🔄", "sound": "Glass"}, {"up_to": "*", "emoji": "🚨"}]
        )
        self.assertEqual(out[0], Bucket(up_to=5, emoji="🔄", sound="Glass"))
        self.assertEqual(icon_mod.pick_sound(3, out), "Glass")
        self.assertIsNone(icon_mod.pick_sound(99, out))  # catch-all has no sound

    def test_ignores_malformed_legacy_lines(self):
        out = _parse_buckets(["nope", "5", "x: 🔄", "3: ", "7: 👍"])
        self.assertEqual([(b.up_to, b.emoji) for b in out], [(7, "👍")])


class PickGlyphTests(unittest.TestCase):
    def setUp(self):
        self.buckets = DEFAULT_ICON_BUCKETS

    def test_zero(self):
        self.assertEqual(icon_mod.pick_glyph(0, self.buckets), "✅")

    def test_low_and_boundary(self):
        self.assertEqual(icon_mod.pick_glyph(1, self.buckets), "🔄")
        self.assertEqual(icon_mod.pick_glyph(5, self.buckets), "🔄")

    def test_mid_and_boundary(self):
        self.assertEqual(icon_mod.pick_glyph(6, self.buckets), "⚠️")
        self.assertEqual(icon_mod.pick_glyph(15, self.buckets), "⚠️")

    def test_high_catch_all(self):
        self.assertEqual(icon_mod.pick_glyph(16, self.buckets), "🚨")
        self.assertEqual(icon_mod.pick_glyph(999, self.buckets), "🚨")

    def test_no_match_without_catch_all_returns_none(self):
        buckets = [Bucket(up_to=5, emoji="🔄")]
        self.assertIsNone(icon_mod.pick_glyph(10, buckets))


class MenubarPresentationTests(unittest.TestCase):
    """What the macOS status item shows (pure; runs on every platform)."""

    def setUp(self):
        from awaitray.config import TrayIcon
        from awaitray.controller import TrayState

        self.TrayIcon, self.TrayState = TrayIcon, TrayState

    def _p(self, kind, count=0, alert=False, **cfg):
        return icon_mod.menubar_presentation(
            self.TrayState(kind, count, alert), self.TrayIcon(**cfg)
        )

    def test_states_use_their_symbol_without_text(self):
        self.assertEqual(self._p("paused"), icon_mod.MenubarPresentation("pause.circle", ""))
        self.assertEqual(self._p("offline").symbol, "wifi.slash")
        err = self._p("error")
        self.assertEqual((err.symbol, err.title, err.red), ("exclamationmark.triangle", "", True))

    def test_count_next_to_the_pr_symbol(self):
        p = self._p("count", 7, enabled=False)
        self.assertEqual((p.symbol, p.title, p.red), ("arrow.triangle.pull", "7", False))
        self.assertEqual(self._p("count", 150, enabled=False).title, "99+")
        self.assertTrue(self._p("count", 2, alert=True, enabled=False).red)

    def test_unseen_dot_follows_the_count(self):
        from awaitray.controller import TrayState

        state = TrayState("count", 3, False, True)
        pres = icon_mod.menubar_presentation(state, self.TrayIcon(enabled=False))
        self.assertEqual(pres.title, "3•")
        self.assertIn("new since you last looked", icon_mod.describe(state))

    def test_idle_has_no_number(self):
        self.assertEqual(self._p("idle", enabled=False).title, "")

    def test_bucket_emoji_prefixes_the_count(self):
        self.assertEqual(self._p("count", 3, enabled=True, show_count=True).title, "🔄 3")
        self.assertEqual(self._p("count", 3, enabled=True, show_count=False).title, "🔄")
        self.assertEqual(self._p("idle", enabled=True, show_count=True).title, "✅")


class DescribeTests(unittest.TestCase):
    def test_descriptions(self):
        from awaitray.controller import TrayState

        self.assertEqual(icon_mod.describe(TrayState("count", 1)), "awaitray: 1 PR to review")
        self.assertEqual(
            icon_mod.describe(TrayState("count", 3, True)),
            "awaitray: 3 PRs to review, high priority",
        )
        self.assertEqual(icon_mod.describe(TrayState("offline")), "awaitray: offline")


class StatusItemApplyTests(unittest.TestCase):
    """Applies a presentation to a status-item button (macOS only)."""

    @classmethod
    def setUpClass(cls):
        try:
            from awaitray.backends.macos import status_icon
        except Exception as e:  # pragma: no cover - non-macOS / no pyobjc
            raise unittest.SkipTest(f"status_icon unavailable: {e}") from e
        cls.status_icon = status_icon

    def test_template_symbol_and_plain_title(self):
        from unittest import mock

        button = mock.Mock()
        self.status_icon.apply(
            button, icon_mod.MenubarPresentation("arrow.triangle.pull", "3"), "d"
        )
        self.assertTrue(button.setImage_.call_args.args[0].isTemplate())
        button.setTitle_.assert_called_with("3")
        # Setting it at all (even to nil) draws black on macOS 26's dark bar.
        button.setContentTintColor_.assert_not_called()

    def test_red_goes_into_symbol_and_title(self):
        from unittest import mock

        button = mock.Mock()
        self.status_icon.apply(
            button, icon_mod.MenubarPresentation("arrow.triangle.pull", "3", red=True), "d"
        )
        self.assertIsNotNone(button.setImage_.call_args.args[0])
        title = button.setAttributedTitle_.call_args.args[0]
        self.assertEqual(title.string(), "3")
        self.assertIsNotNone(title.attribute_atIndex_effectiveRange_("NSColor", 0, None)[0])
        font = title.attribute_atIndex_effectiveRange_("NSFont", 0, None)[0]
        # Without it the count draws in Helvetica 12, not the menu bar font.
        self.assertEqual(font, self.status_icon.AppKit.NSFont.menuBarFontOfSize_(0))
        button.setContentTintColor_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
