"""The packaged launcher (packaging/install-tree.sh)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux packages only (GNU install)")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "root"
        subprocess.run(
            ["bash", str(ROOT / "packaging/install-tree.sh"), str(self.dest)], check=True
        )
        # Point the launcher at this tree and this interpreter.
        launcher = self.dest / "usr/bin/awaitray"
        text = launcher.read_text()
        text = text.replace("/usr/lib/awaitray", str(self.dest / "usr/lib/awaitray"))
        launcher.write_text(text.replace("/usr/bin/python3", sys.executable))
        self.launcher = launcher
        self.cwd = Path(tmp.name) / "cwd"
        (self.cwd / "awaitray").mkdir(parents=True)
        (self.cwd / "awaitray/__init__.py").write_text("print('SHADOWED')\n")
        (self.cwd / "awaitray/__main__.py").write_text("print('SHADOWED')\n")

    def _run(self, *args):
        env = dict(os.environ, PYTHONPATH=str(self.cwd))
        return subprocess.run(
            [str(self.launcher), *args],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_runs_the_installed_package_not_one_in_the_current_folder(self):
        done = self._run("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("SHADOWED", done.stdout + done.stderr)
        self.assertIn("usage", done.stdout.lower())


if __name__ == "__main__":
    unittest.main()
