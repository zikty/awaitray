"""Installed browsers and their profiles, offered in Preferences."""

import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awaitray import browser, browsers
from awaitray.browsers import SYSTEM, Choice


def _app(root: Path, name: str, schemes=("http", "https")) -> None:
    contents = root / f"{name}.app" / "Contents"
    contents.mkdir(parents=True)
    info = {"CFBundleName": name, "CFBundleURLTypes": [{"CFBundleURLSchemes": list(schemes)}]}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))


def _chrome_profiles(folder: Path, profiles: dict) -> None:
    folder.mkdir(parents=True)
    cache = {d: {"name": n} for d, n in profiles.items()}
    (folder / "Local State").write_text(json.dumps({"profile": {"info_cache": cache}}))


def _desktop(root: Path, file: str, name: str, exec_: str, mime="x-scheme-handler/https;") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / file).write_text(
        f"[Desktop Entry]\nType=Application\nName={name}\nName[es]=Otro\n"
        f"Exec={exec_}\nMimeType=text/html;{mime}\n"
    )


class _Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.system = Path(tmp.name) / "system"
        self.home.mkdir()


class MacTests(_Home):
    def setUp(self):
        super().setUp()
        apps = self.system / "Applications"
        for name in ("Google Chrome", "Firefox", "Safari"):
            _app(apps, name)
        _app(apps, "Slack", schemes=("slack",))  # not a browser
        dirs = (str(apps), "~/Applications")
        patcher = mock.patch.object(browsers, "_MAC_APP_DIRS", dirs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_apps_that_open_https_with_profiles(self):
        _chrome_profiles(
            self.home / "Library/Application Support/Google/Chrome",
            {"Default": "Personal", "Profile 2": "Trabajo"},
        )
        ff = self.home / "Library/Application Support/Firefox"
        ff.mkdir(parents=True)
        (ff / "profiles.ini").write_text(
            "[General]\nVersion=2\n[Profile0]\nName=default\n[Profile1]\nName=work vpn\n"
        )
        choices = browsers.installed("darwin", self.home)
        self.assertEqual(
            choices,
            [
                SYSTEM,
                Choice("Firefox", "Firefox"),
                Choice("Firefox — default", "Firefox -P default"),
                Choice("Firefox — work vpn", "Firefox -P 'work vpn'"),
                Choice("Google Chrome", "Google Chrome"),
                Choice("Google Chrome — Personal", "Google Chrome --profile-directory=Default"),
                Choice("Google Chrome — Trabajo", "Google Chrome --profile-directory='Profile 2'"),
                Choice("Safari", "Safari"),
            ],
        )

    def test_every_choice_launches_the_right_app(self):
        _chrome_profiles(
            self.home / "Library/Application Support/Google/Chrome",
            {"Default": "Personal", "Profile 2": "Trabajo"},
        )
        url = "https://x/1"
        by_label = {c.label: c.command for c in browsers.installed("darwin", self.home)}
        self.assertEqual(
            browser.argv(by_label["Google Chrome — Trabajo"], url, "darwin"),
            ["open", "-na", "Google Chrome", "--args", "--profile-directory=Profile 2", url],
        )
        self.assertEqual(
            browser.argv(by_label["Safari"], url, "darwin"), ["open", "-a", "Safari", url]
        )

    def test_a_single_profile_is_not_listed(self):
        _chrome_profiles(
            self.home / "Library/Application Support/Google/Chrome", {"Default": "Person 1"}
        )
        labels = [c.label for c in browsers.installed("darwin", self.home)]
        self.assertNotIn("Google Chrome — Person 1", labels)

    def test_broken_files_are_skipped(self):
        bad = self.system / "Applications" / "Broken.app" / "Contents"
        bad.mkdir(parents=True)
        (bad / "Info.plist").write_text("not a plist")
        state = self.home / "Library/Application Support/Google/Chrome"
        state.mkdir(parents=True)
        (state / "Local State").write_text("{")
        labels = [c.label for c in browsers.installed("darwin", self.home)]
        self.assertEqual(labels, ["System default", "Firefox", "Google Chrome", "Safari"])


class LinuxTests(_Home):
    def setUp(self):
        super().setUp()
        system = self.system / "applications"
        _desktop(system, "firefox.desktop", "Firefox", "firefox %u")
        _desktop(
            system, "google-chrome.desktop", "Google Chrome", "/usr/bin/google-chrome-stable %U"
        )
        _desktop(system, "gimp.desktop", "GIMP", "gimp %U", mime="image/png;")
        flatpak = self.system / "flatpak"
        _desktop(
            flatpak,
            "org.chromium.Chromium.desktop",
            "Chromium",
            "/usr/bin/flatpak run --branch=stable --command=chromium --file-forwarding "
            "org.chromium.Chromium @@u %U @@",
        )
        dirs = (str(system), str(flatpak), "~/.local/share/applications")
        patcher = mock.patch.object(browsers, "_LINUX_DESKTOP_DIRS", dirs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_https_handlers_with_profiles(self):
        _chrome_profiles(
            self.home / ".config/google-chrome", {"Default": "Personal", "Profile 3": "Trabajo"}
        )
        choices = browsers.installed("linux", self.home)
        self.assertEqual(
            choices,
            [
                SYSTEM,
                Choice(
                    "Chromium",
                    "/usr/bin/flatpak run --branch=stable --command=chromium org.chromium.Chromium",
                ),
                Choice("Firefox", "firefox"),
                Choice("Google Chrome", "/usr/bin/google-chrome-stable"),
                Choice(
                    "Google Chrome — Personal",
                    "/usr/bin/google-chrome-stable --profile-directory=Default",
                ),
                Choice(
                    "Google Chrome — Trabajo",
                    "/usr/bin/google-chrome-stable --profile-directory='Profile 3'",
                ),
            ],
        )

    def test_a_user_entry_overrides_and_can_hide_one(self):
        user = self.home / ".local/share/applications"
        user.mkdir(parents=True)
        (user / "firefox.desktop").write_text("[Desktop Entry]\nName=Firefox\nHidden=true\n")
        labels = [c.label for c in browsers.installed("linux", self.home)]
        self.assertNotIn("Firefox", labels)


class LabelTests(unittest.TestCase):
    CHOICES = (SYSTEM, Choice("Google Chrome — Trabajo", "Google Chrome --profile-directory=P2"))

    def test_stored_command_shows_as_its_label(self):
        self.assertEqual(browsers.label_for("", self.CHOICES), "System default")
        self.assertEqual(
            browsers.label_for("Google Chrome --profile-directory=P2", self.CHOICES),
            "Google Chrome — Trabajo",
        )
        self.assertEqual(browsers.label_for("firefox -P x", self.CHOICES), "firefox -P x")

    def test_picked_label_or_typed_command_is_stored_as_a_command(self):
        self.assertEqual(browsers.command_for("System default", self.CHOICES), "")
        self.assertEqual(
            browsers.command_for("Google Chrome — Trabajo", self.CHOICES),
            "Google Chrome --profile-directory=P2",
        )
        self.assertEqual(browsers.command_for(" firefox -P x ", self.CHOICES), "firefox -P x")


if __name__ == "__main__":
    unittest.main()
