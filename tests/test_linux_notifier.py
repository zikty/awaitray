"""notify-send gets PR titles from anyone who requests your review: they must
stay text (security audit)."""

import shutil
import subprocess
import unittest

try:
    from awaitray.backends.linux import notifier
except ImportError:  # pragma: no cover - a platform without the Linux backend
    notifier = None


@unittest.skipIf(notifier is None, "Linux backend not importable here")
class NotifySendTests(unittest.TestCase):
    def test_an_option_looking_title_or_body_stays_text(self):
        argv = notifier.command("--wait", "--icon=/etc/passwd", "normal", "dialog-information")
        self.assertEqual(argv[-3:], ["--", "--wait", "--icon=/etc/passwd"])

    def test_markup_in_the_body_is_escaped(self):
        argv = notifier.command("t", '<a href="file:///etc">x</a> & <img src=y>', "low", "i")
        self.assertEqual(
            argv[-1], '&lt;a href="file:///etc"&gt;x&lt;/a&gt; &amp; &lt;img src=y&gt;'
        )

    @unittest.skipIf(shutil.which("notify-send") is None, "notify-send not installed")
    def test_the_real_notify_send_takes_them_as_text(self):
        """Without `--`, notify-send fails with "Unknown option" and the
        notice is lost; with it, the option parser never sees the title."""
        argv = notifier.command("--zzz", "-u bogus", "normal", "i")
        res = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertNotIn("Unknown option", res.stderr)
        self.assertNotIn("Missing argument", res.stderr)


if __name__ == "__main__":
    unittest.main()
