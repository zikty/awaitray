import sys
import unittest
from unittest import mock


@unittest.skipUnless(sys.platform == "darwin", "macOS backend")
class MacHostTimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from awaitray.backends.macos import app
        except Exception as e:  # pragma: no cover - no PyObjC
            raise unittest.SkipTest(f"macOS backend unavailable: {e}") from e
        cls.app = app

    def test_timers_run_in_common_modes(self):
        # Default-mode timers stop while a menu is open.
        run_loop = mock.Mock()
        fake_cls = mock.Mock()
        fake_cls.mainRunLoop.return_value = run_loop
        with mock.patch.object(self.app, "NSRunLoop", fake_cls):
            host = self.app._MacHost()
            timer = host.timeout_add_seconds(60, lambda: True)
        try:
            run_loop.addTimer_forMode_.assert_called_once_with(timer, self.app.NSRunLoopCommonModes)
        finally:
            host.source_remove(timer)

    def test_the_app_is_named_awaitray_not_python(self):
        info = {"CFBundleName": "Python"}
        bundle = mock.Mock()
        bundle.localizedInfoDictionary.return_value = None
        bundle.infoDictionary.return_value = info
        self.assertTrue(self.app.name_the_app(bundle))
        self.assertEqual(info["CFBundleName"], "awaitray")

    def test_a_bundle_that_cant_be_renamed_is_left_alone(self):
        bundle = mock.Mock()
        bundle.localizedInfoDictionary.return_value = None
        bundle.infoDictionary.return_value = None
        self.assertFalse(self.app.name_the_app(bundle))
        self.app.name_the_app()  # the real one never raises


if __name__ == "__main__":
    unittest.main()
