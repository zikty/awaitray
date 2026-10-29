"""The install scripts' shared helpers (lib/xdg.sh, lib/linux-autostart.sh)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINUX = unittest.skipUnless(sys.platform.startswith("linux"), "Linux scripts")


def _bash(script, *args, env=None, cwd=None):
    lib = f'source "{ROOT}/lib/xdg.sh"; source "{ROOT}/lib/linux-autostart.sh"; '
    done = subprocess.run(
        ["bash", "-c", lib + script, "bash", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=True,
    )
    return done.stdout


class ConfigHomeTests(unittest.TestCase):
    def test_relative_or_empty_means_home_config(self):
        home = os.environ.get("HOME", "")
        for value, want in (
            ("/x/cfg", "/x/cfg"),
            ("", f"{home}/.config"),
            ("rel", f"{home}/.config"),
        ):
            env = dict(os.environ, XDG_CONFIG_HOME=value)
            self.assertEqual(_bash("config_home", env=env).strip(), want, value)


@LINUX
class FilesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A folder name with every character that broke the old sed/heredocs.
        self.here = Path(tmp.name) / "a&b|c%d\\e f"
        self.here.mkdir()
        shutil.copy(ROOT / "awaitray.service.in", self.here)

    def test_unit_keeps_the_path_and_doubles_percent(self):
        out = self.here / "unit"
        _bash('write_unit "$1" "$2"', str(self.here), str(out))
        want = str(self.here).replace("%", "%%")
        self.assertIn(f"WorkingDirectory={want}\n", out.read_text())

    def test_desktop_escapes_backslashes(self):
        out = self.here / "desktop"
        _bash('write_desktop "$1" "$2"', str(self.here), str(out))
        self.assertIn("Path=" + str(self.here).replace("\\", "\\\\") + "\n", out.read_text())

    def test_a_path_with_a_line_break_is_refused(self):
        with self.assertRaises(subprocess.CalledProcessError):
            _bash('check_install_dir "$1"', "/tmp/a\nb")


@LINUX
class StopAppTests(unittest.TestCase):
    def test_pattern_matches_the_app_only(self):
        pattern = _bash('_app_pattern "$1"', "awaitray")
        for cmd, hit in (
            ("python3 -m awaitray", True),
            ("/usr/bin/python3 -m awaitray", True),
            ("/Users/me/awaitray/.venv/bin/python -m awaitray", True),
            ("vim python3 -m awaitray", False),
            ("python3 -m awaitray config import x.json", False),
            ("python3 -m awaitray_other", False),
        ):
            got = subprocess.run(["grep", "-Eq", pattern], input=cmd, text=True).returncode == 0
            self.assertEqual(got, hit, cmd)


if __name__ == "__main__":
    unittest.main()
