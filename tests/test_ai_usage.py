"""AI assistants' quota left: Copilot (gh), Codex (app-server), Antigravity (agy)."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from awaitray import ai_usage as au
from awaitray import config as cfg_mod
from awaitray import github as gh
from awaitray import menu_model as mm
from awaitray import prefs_model as pm
from awaitray.config import AiUsageConfig, Config, HostConfig
from awaitray.controller import Controller

PRO = {
    "copilot_plan": "individual",
    "access_type_sku": "copilot_pro",
    "quota_reset_date_utc": "2026-11-01T00:00:00Z",
    "quota_snapshots": {
        "chat": {"unlimited": True},
        "completions": {"unlimited": True},
        "premium_interactions": {
            "entitlement": 1500,
            "remaining": 570,
            "percent_remaining": 38.0,
            "unlimited": False,
            "quota_reset_at": "2026-11-01T00:00:00Z",
        },
    },
}
PRO_LINE = "Copilot Pro · 38% left (570 of 1,500) · resets Nov 1"

# account/rateLimits/read's result, as codex app-server answers it.
CODEX = {
    "rateLimits": {
        "planType": "plus",
        "primary": {"usedPercent": 16, "windowDurationMins": 300, "resetsAt": 1791262800},
        "secondary": {"usedPercent": 39, "windowDurationMins": 10080, "resetsAt": 1791700000},
    }
}

# agy -p /usage --output-format json, as CodexBar's tests record it.
AGY = {
    "status": "SUCCESS",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "w",
                            "window": "weekly",
                            "remaining_fraction": 0.86,
                            "reset_time": "2026-09-17T18:40:27Z",
                        },
                        {
                            "id": "5h",
                            "window": "5h",
                            "remaining_fraction": 0.95,
                            "reset_time": "2026-09-13T03:48:04Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "x",
                            "remaining_fraction": 0.89,
                            "reset_time": "2026-09-17T02:38:46Z",
                        },
                        {"id": "off", "remaining_fraction": 0.1, "disabled": True},
                    ],
                },
            ]
        },
    },
}

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class CopilotTests(unittest.TestCase):
    def test_a_personal_plan_with_quota(self):
        usage = au.parse_copilot(PRO, "github.com")
        self.assertEqual(usage.describe(NOW), PRO_LINE)
        self.assertEqual(usage.url, "https://github.com/settings/copilot")
        self.assertIsNone(usage.low)

    def test_an_unlimited_seat(self):
        payload = {
            "copilot_plan": "business",
            "access_type_sku": "copilot_for_business",
            "quota_snapshots": {"premium_interactions": {"unlimited": True}},
        }
        self.assertEqual(au.parse_copilot(payload).describe(NOW), "Copilot Business · unlimited")

    def test_free_falls_back_to_chat_and_top_level_reset(self):
        payload = {
            "access_type_sku": "free_limited_copilot",
            "quota_reset_date": "2026-10-15",
            "quota_snapshots": {"chat": {"percent_remaining": 12.5, "quota_reset_at": 0}},
        }
        usage = au.parse_copilot(payload)
        self.assertEqual(usage.describe(NOW), "Copilot Free · 12% left · resets Oct 15")
        self.assertEqual(usage.low.percent_left, 12.5)

    def test_unknown_shapes_show_nothing_or_only_the_plan(self):
        for payload in (None, [], {}, {"message": "Not Found"}):
            self.assertIsNone(au.parse_copilot(payload), payload)
        odd = {"copilot_plan": "enterprise", "quota_snapshots": "?"}
        self.assertEqual(au.parse_copilot(odd).describe(NOW), "Copilot Enterprise")

    def test_github_com_accounts_only_and_404_is_no_copilot(self):
        cfg = Config(
            hosts=[
                HostConfig("github.com"),
                HostConfig("github.com", user="me-corp"),
                HostConfig("ghe.corp"),
            ]
        )

        def answer(host, path, user=""):
            if user == "me-corp":
                raise gh.NotFoundError("HTTP 404")
            return PRO

        with (
            mock.patch.object(gh, "gh_available", return_value=True),
            mock.patch.object(gh, "rest_get", side_effect=answer) as rest,
        ):
            usages = au._copilot(cfg)
        self.assertEqual([u.account for u in usages], ["github.com"])
        self.assertNotIn("ghe.corp", [c.args[0] for c in rest.call_args_list])


class CodexTests(unittest.TestCase):
    def test_both_windows_and_the_plan(self):
        usage = au.parse_codex(CODEX)
        self.assertEqual(usage.title, "Codex Plus")
        self.assertEqual(
            [(w.name, w.percent_left) for w in usage.windows], [("5h", 84), ("week", 61)]
        )
        reset = datetime.fromtimestamp(1791700000, timezone.utc)
        self.assertEqual(usage.tightest().reset, reset)  # the week: least left
        self.assertTrue(usage.describe(NOW).startswith("Codex Plus · 5h 84% left · week 61% left"))

    def test_nothing_known_is_no_line(self):
        for result in (None, {}, {"rateLimits": {}}, {"rateLimits": {"primary": {"x": 1}}}):
            self.assertIsNone(au.parse_codex(result), result)

    def test_talks_json_rpc_to_the_cli(self):
        """A fake `codex` that answers like app-server: checks the handshake
        and the read-only flags, answers in order, logs noise in between."""
        script = (
            "import json,sys\n"
            "assert sys.argv[1:] == ['-s','read-only','-a','never','app-server'], sys.argv\n"
            "print('starting up', flush=True)\n"
            "m = json.loads(sys.stdin.readline())\n"
            "assert m['method'] == 'initialize' and m['params']['clientInfo']['name']\n"
            "print(json.dumps({'id': m['id'], 'result': {}}), flush=True)\n"
            "n = json.loads(sys.stdin.readline())\n"
            "assert n['method'] == 'initialized' and 'id' not in n\n"
            "r = json.loads(sys.stdin.readline())\n"
            "assert r['method'] == 'account/rateLimits/read'\n"
            "print(json.dumps({'method': 'some/notification', 'params': {}}), flush=True)\n"
            f"print(json.dumps({{'id': r['id'], 'result': {json.dumps(CODEX)}}}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            exe = _executable(Path(tmp) / "codex", script)
            with mock.patch.object(au, "find_cli", return_value=exe):
                usages = au._codex(Config())
        self.assertEqual([u.title for u in usages], ["Codex Plus"])

    def test_a_cli_that_fails_or_hangs_shows_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dead = _executable(Path(tmp) / "dead", "import sys; sys.exit(1)\n")
            hung = _executable(Path(tmp) / "hung", "import time; time.sleep(30)\n")
            for exe in (dead, hung):
                with (
                    mock.patch.object(au, "find_cli", return_value=exe),
                    mock.patch.object(au, "CLI_TIMEOUT", 1),
                ):
                    self.assertEqual(au._codex(Config()), [], exe)

    def test_a_cli_that_floods_is_cut_off(self):
        script = "import sys,time\nwhile True: sys.stdout.write('x' * 65536)\n"
        with tempfile.TemporaryDirectory() as tmp:
            exe = _executable(Path(tmp) / "codex", script)
            with (
                mock.patch.object(au, "find_cli", return_value=exe),
                mock.patch.object(au, "MAX_OUTPUT", 200_000),
            ):
                self.assertEqual(au._codex(Config()), [])

    def test_not_installed_runs_nothing(self):
        with (
            mock.patch.object(au, "find_cli", return_value=None),
            mock.patch.object(au.subprocess, "Popen") as popen,
        ):
            self.assertEqual(au._codex(Config()), [])
        popen.assert_not_called()


class AntigravityTests(unittest.TestCase):
    def test_the_tightest_bucket_per_group(self):
        usage = au.parse_antigravity(AGY)
        got = [(w.name, round(w.percent_left)) for w in usage.windows]
        self.assertEqual(got, [("Gemini", 86), ("Claude and GPT models", 89)])
        self.assertTrue(usage.describe(NOW).startswith("Antigravity · Gemini 86% left"))
        self.assertEqual(usage.url, "")

    def test_an_unsuccessful_or_unrelated_report_is_no_line(self):
        for payload in (None, {"status": "ERROR"}, dict(AGY, command={"name": "help"})):
            self.assertIsNone(au.parse_antigravity(payload))

    def test_old_versions_never_get_the_prompt(self):
        """Before 1.1.11 `-p /usage` could reach a model as a prompt."""
        self.assertEqual(au.parse_agy_version("agy 1.1.11 (abc)"), (1, 1, 11))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls"
            script = (
                "import sys\n"
                f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                "print('agy 1.1.10')\n"
            )
            exe = _executable(Path(tmp) / "agy", script)
            with mock.patch.object(au, "find_cli", return_value=exe):
                self.assertEqual(au._antigravity(Config()), [])
            self.assertEqual(log.read_text().split("\n")[:-1], ["--version"])

    def test_runs_the_usage_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import json,sys\n"
                "if sys.argv[1:] == ['--version']: print('1.2.0'); sys.exit()\n"
                "assert sys.argv[1:4] == ['-p','/usage','--output-format'], sys.argv\n"
                f"print(json.dumps({AGY!r}))\n"
            )
            exe = _executable(Path(tmp) / "agy", script)
            with mock.patch.object(au, "find_cli", return_value=exe):
                self.assertEqual([u.title for u in au._antigravity(Config())], ["Antigravity"])


class FormatTests(unittest.TestCase):
    def test_soon_is_a_time_later_a_date_monthly_always_a_date(self):
        soon = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(au.format_reset(soon, NOW), soon.astimezone().strftime("%H:%M"))
        later = datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc)
        self.assertRegex(au.format_reset(later, NOW), r"^Oct \d+$")
        monthly = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(au.format_reset(monthly, NOW), "Oct 6")

    def test_dates_out_of_range_are_unknown(self):
        for value in (0, -5, 1e20, 253402300799, "9999-12-31T23:59:59Z", "0001-01-01", True):
            self.assertIsNone(au._when(value), value)
        self.assertEqual(au._when("2026-11-01").year, 2026)
        self.assertEqual(au._when(1_790_000_000).year, 2026)

    def test_never_raises_on_a_date_the_local_zone_cant_hold(self):
        far = datetime(9999, 12, 31, 23, 0, tzinfo=timezone.utc)
        self.addCleanup(time.tzset)  # after the environment is restored
        with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
            time.tzset()
            self.assertEqual(au.format_reset(far, NOW), "Dec 31")


class FetchTests(unittest.TestCase):
    def test_enabled_providers_only_and_one_failing_never_hides_the_rest(self):
        boom = mock.Mock(side_effect=RuntimeError("bug"))
        codex = mock.Mock(return_value=[au.Usage("codex", "Codex Plus")])
        providers = (
            au.Provider("copilot", "C", "", boom),
            au.Provider("codex", "X", "", codex),
            au.Provider("antigravity", "A", "", mock.Mock()),
        )
        cfg = Config(ai_usage=AiUsageConfig(antigravity=False))
        with mock.patch.object(au, "PROVIDERS", providers):
            self.assertEqual([u.title for u in au.fetch(cfg)], ["Codex Plus"])
        providers[2].fetch.assert_not_called()

    def test_finds_clis_outside_a_short_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _executable(Path(tmp) / "codex", "")
            with (
                mock.patch.dict(os.environ, {"PATH": "/nonexistent"}),
                mock.patch.object(au, "_EXTRA_DIRS", (tmp,)),
            ):
                self.assertEqual(au.find_cli("codex"), exe)


class _UI:
    def __init__(self):
        self.notifications = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append(title)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def _ctl(self, **cfg):
        ui = _UI()
        return Controller(ui=ui, host=ui, cfg=Config(**cfg)), ui

    def _items(self, ctl):
        return {i.label: i for i in ctl.menu_model().items}

    def test_lines_right_under_the_status_with_their_links(self):
        ctl, _ = self._ctl()
        ctl.usage.apply_ai_usage([au.parse_copilot(PRO, "github.com"), au.parse_antigravity(AGY)])
        items = ctl.menu_model().items
        self.assertEqual(items[0].kind, mm.STATUS)
        self.assertEqual(items[1].label, PRO_LINE)
        # In the account's browser.
        self.assertEqual(
            items[1].action.args, ("https://github.com/settings/copilot", "", "github.com")
        )
        self.assertTrue(items[2].label.startswith("Antigravity"))
        self.assertEqual(items[2].kind, mm.EMPTY)  # no page to open
        self.assertEqual(items[3], mm.SEP)

    def test_several_copilot_accounts_are_named(self):
        ctl, _ = self._ctl()
        ctl.usage.apply_ai_usage(
            [au.parse_copilot(PRO, "github.com"), au.parse_copilot(PRO, "me-corp@github.com")]
        )
        self.assertIn(f"me-corp@github.com: {PRO_LINE}", self._items(ctl))

    def test_a_provider_turned_off_disappears_at_once(self):
        ctl, _ = self._ctl(ai_usage=AiUsageConfig(copilot=False))
        ctl.usage.apply_ai_usage([au.parse_copilot(PRO, "github.com")])
        self.assertNotIn(PRO_LINE, self._items(ctl))

    def test_low_quota_warns_once_per_window_and_period(self):
        def codex(used, reset):
            limits = {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset}
            return au.parse_codex({"rateLimits": {"planType": "pro", "secondary": limits}})

        ctl, ui = self._ctl()
        for _ in range(3):
            ctl.usage.apply_ai_usage([codex(90, 1791700000)])
        self.assertEqual(ui.notifications, ["Codex Pro: 10% left"])
        ctl.usage.apply_ai_usage([codex(95, 1792300000)])  # the next week
        self.assertEqual(len(ui.notifications), 2)


class PrefsTests(unittest.TestCase):
    def test_a_switch_per_provider(self):
        keys = {f.key for f in pm.all_fields() if f.key.startswith("ai_usage.")}
        self.assertEqual(keys, {f"ai_usage.{p.id}" for p in au.PROVIDERS})
        for p in au.PROVIDERS:
            self.assertTrue(hasattr(AiUsageConfig(), p.id), p.id)

    def test_copilot_turned_off_before_stays_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"account_info": {"copilot": False, "inbox": True}}))
            with mock.patch.object(cfg_mod, "CONFIG_PATH", path):
                self.assertFalse(cfg_mod.load().ai_usage.copilot)
            path.write_text(json.dumps({"account_info": {"inbox": True}}))
            with mock.patch.object(cfg_mod, "CONFIG_PATH", path):
                self.assertTrue(cfg_mod.load().ai_usage.copilot)


def _executable(path, body):
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


if __name__ == "__main__":
    unittest.main()
