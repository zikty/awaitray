"""lib/linux-deps.sh, driven with stub commands on an isolated PATH."""

import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib" / "linux-deps.sh"


@unittest.skipUnless(shutil.which("bash") and shutil.which("sed"), "bash required")
class LinuxDepsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(tmp.name) / "sudo.log"
        self.installed = Path(tmp.name) / "installed"

    def _stub(self, name, body="exit 0"):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _machine(self, package_manager, *, already_ok=False):
        # python3 "has the bindings" once the package manager ran (or from the start).
        marker = self.installed
        if already_ok:
            marker.touch()
        self._stub("python3", f'[ -e "{marker}" ]')
        # sudo records the command and "installs" everything.
        tools = " ".join(f'"{self.bin}/{t}"' for t in ("git", "gh", "notify-send"))
        touch, chmod = shutil.which("touch"), shutil.which("chmod")  # PATH is isolated
        self._stub(
            "sudo",
            (
                f'echo "$@" >> "{self.log}"\n'
                f'{touch} "{marker}"\n'
                f'for t in {tools}; do printf "#!/bin/sh\\nexit 0\\n" > "$t"; {chmod} +x "$t"; done'
            ),
        )
        if already_ok:
            for t in ("git", "gh", "notify-send"):
                self._stub(t)
        if package_manager:
            self._stub(package_manager)

    def _run(self, snippet):
        env = {"PATH": str(self.bin), "HOME": str(self.bin.parent)}
        bash = shutil.which("bash")
        return subprocess.run(
            [bash, "-c", f'set -euo pipefail; source "{LIB}"; {snippet}'],
            capture_output=True,
            text=True,
            env=env,
        )

    def _sudo_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_detects_package_manager(self):
        for pm, cmd in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman")):
            for f in self.bin.iterdir():
                f.unlink()
            self._stub(cmd)
            res = self._run("detect_package_manager")
            self.assertEqual(res.stdout.strip(), pm)
        for f in self.bin.iterdir():
            f.unlink()
        self.assertEqual(self._run("detect_package_manager").stdout.strip(), "unknown")

    def test_nothing_installed_when_everything_is_present(self):
        self._machine("apt-get", already_ok=True)
        res = self._run("ensure_linux_deps")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("nothing to install", res.stdout)
        self.assertEqual(self._sudo_calls(), [])

    def test_installs_with_the_detected_manager(self):
        cases = {
            "apt-get": "apt-get install -y --no-install-recommends python3-gi",
            "dnf": "dnf install -y python3-gobject gtk3 libayatana-appindicator-gtk3",
            "pacman": "pacman -S --needed --noconfirm python-gobject gtk3 libayatana-appindicator",
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd):
                for f in self.bin.iterdir():
                    f.unlink()
                self.log.unlink(missing_ok=True)
                self.installed.unlink(missing_ok=True)
                self._machine(cmd)
                res = self._run("ensure_linux_deps")
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertTrue(any(expected in c for c in self._sudo_calls()), self._sudo_calls())

    def test_unknown_distro_explains_instead_of_failing_silently(self):
        self._machine(None)
        res = self._run("ensure_linux_deps || echo FAILED")
        self.assertIn("FAILED", res.stdout)
        self.assertIn("Unknown package manager", res.stderr)
        self.assertEqual(self._sudo_calls(), [])


if __name__ == "__main__":
    unittest.main()
