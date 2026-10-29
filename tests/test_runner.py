"""Background work: off the main thread, the result back on it."""

import threading
import unittest
from unittest import mock

from awaitray import log as log_mod
from awaitray.runner import ImmediateRunner, ThreadRunner


class _Host:
    """Collects what would run on the main thread."""

    def __init__(self):
        self.posted = []
        self.done = threading.Event()

    def run_on_main(self, callback, *args):
        self.posted.append((callback, args))
        self.done.set()


class ThreadRunnerTests(unittest.TestCase):
    def test_the_job_runs_elsewhere_and_its_result_comes_back_on_main(self):
        host, seen = _Host(), []
        ThreadRunner(host).background(
            lambda: threading.current_thread() is threading.main_thread(), seen.append
        )
        self.assertTrue(host.done.wait(5))
        callback, args = host.posted[0]
        self.assertFalse(callback(*args))  # once: GLib.idle_add must not repeat it
        self.assertEqual(seen, [False])  # the job wasn't on the main thread

    def test_a_job_that_raises_is_logged_and_never_delivered(self):
        host = _Host()
        with mock.patch.object(log_mod, "warn") as warn:
            ThreadRunner(host).background(_boom, host.posted.append)
            self.assertFalse(host.done.wait(0.5))
        self.assertEqual(host.posted, [])
        self.assertIn("_boom failed", warn.call_args.args[0])


class ImmediateRunnerTests(unittest.TestCase):
    def test_runs_both_right_away(self):
        seen = []
        ImmediateRunner().background(lambda: 42, seen.append)
        self.assertEqual(seen, [42])
        with mock.patch.object(log_mod, "warn"):
            ImmediateRunner().background(_boom, seen.append)
        self.assertEqual(seen, [42])


def _boom():
    raise RuntimeError("bug")


if __name__ == "__main__":
    unittest.main()
