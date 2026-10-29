"""githubstatus.com incidents in the menu, and a notice for major ones."""

import io
import json
import unittest
from unittest import mock

from awaitray import github_status as gs
from awaitray.config import AccountInfoConfig, Config, HostConfig
from awaitray.controller import Controller

OK = {
    "status": {"indicator": "none"},
    "components": [{"name": n, "status": "operational"} for n in ("Actions", "Pages")],
    "incidents": [],
}


def _incident(iid="i1", name="Actions runs delayed", impact="major", status="investigating"):
    return {
        "id": iid,
        "name": name,
        "impact": impact,
        "status": status,
        "shortlink": f"https://stspg.io/{iid}",
    }


class ParseTests(unittest.TestCase):
    def test_all_fine(self):
        status = gs.parse(OK)
        self.assertTrue(status.ok)
        self.assertEqual(status.label(), "")

    def test_unresolved_incidents_first(self):
        payload = dict(
            OK,
            incidents=[
                _incident(),
                _incident("i2", "Copilot slow", "minor"),
                _incident("i3", "Old one", status="resolved"),
            ],
        )
        status = gs.parse(payload)
        self.assertEqual(status.label(), "⚠ GitHub: Actions runs delayed (+1 more)")
        self.assertEqual(status.url, "https://stspg.io/i1")

    def test_degraded_watched_components_only(self):
        payload = dict(
            OK,
            components=[
                {"name": "Actions", "status": "partial_outage"},
                {"name": "Pages", "status": "major_outage"},  # not one we watch
                {"name": "Copilot", "status": "degraded_performance"},
            ],
        )
        status = gs.parse(payload)
        self.assertEqual(status.label(), "⚠ GitHub: Actions, Copilot degraded")
        self.assertEqual(status.url, gs.PAGE_URL)

    def test_unknown_shapes(self):
        self.assertIsNone(gs.parse(None))
        self.assertIsNone(gs.parse([]))
        self.assertTrue(gs.parse({"incidents": [None, {"name": ""}], "components": "?"}).ok)


class FetchTests(unittest.TestCase):
    def test_reads_the_summary(self):
        body = io.BytesIO(json.dumps(dict(OK, incidents=[_incident()])).encode())
        with mock.patch.object(gs.urllib.request, "urlopen", return_value=body) as urlopen:
            status = gs.fetch()
        self.assertEqual(urlopen.call_args.args[0].full_url, gs.SUMMARY_URL)
        self.assertEqual(status.incidents[0].name, "Actions runs delayed")

    def test_an_oversized_answer_is_none(self):
        body = io.BytesIO(b" " * (gs.MAX_BYTES + 1))
        with mock.patch.object(gs.urllib.request, "urlopen", return_value=body):
            self.assertIsNone(gs.fetch())

    def test_incident_names_are_one_short_line(self):
        status = gs.parse(dict(OK, incidents=[_incident(name="Down\u202e\n" + "x" * 300)]))
        self.assertEqual(len(status.incidents[0].name), 120)
        self.assertTrue(status.incidents[0].name.startswith("Down x"))

    def test_unreachable_is_none(self):
        with mock.patch.object(gs.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(gs.fetch())

    def test_only_with_a_github_com_account_and_turned_on(self):
        self.assertTrue(gs.wanted(Config(hosts=[HostConfig("github.com")])))
        self.assertFalse(gs.wanted(Config(hosts=[HostConfig("ghe.corp")])))
        self.assertFalse(gs.wanted(Config(hosts=[HostConfig("github.com", enabled=False)])))
        off = AccountInfoConfig(github_status=False)
        self.assertFalse(gs.wanted(Config(hosts=[HostConfig("github.com")], account_info=off)))


class _UI:
    def __init__(self):
        self.notifications = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append(body)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class ControllerTests(unittest.TestCase):
    def _ctl(self, **cfg):
        ui = _UI()
        cfg.setdefault("hosts", [HostConfig("github.com")])
        return Controller(ui=ui, host=ui, cfg=Config(**cfg)), ui

    def _labels(self, ctl):
        return [i.label for i in ctl.menu_model().items]

    def test_menu_line_opens_the_incident(self):
        ctl, _ = self._ctl()
        ctl.usage.apply_github_status(gs.parse(dict(OK, incidents=[_incident()])))
        items = {i.label: i for i in ctl.menu_model().items}
        line = items["⚠ GitHub: Actions runs delayed"]
        self.assertEqual(line.action.args, ("https://stspg.io/i1",))
        self.assertEqual(self._labels(ctl).index(line.label), 1)  # right under the status

    def test_all_fine_or_turned_off_shows_nothing(self):
        ctl, _ = self._ctl()
        before = self._labels(ctl)
        ctl.usage.apply_github_status(gs.parse(OK))
        self.assertEqual(self._labels(ctl), before)
        off, _ = self._ctl(account_info=AccountInfoConfig(github_status=False))
        off.usage.apply_github_status(gs.parse(dict(OK, incidents=[_incident()])))
        self.assertNotIn("⚠ GitHub: Actions runs delayed", self._labels(off))

    def test_major_incidents_notify_once_minor_ones_never(self):
        ctl, ui = self._ctl()
        minor = _incident("m", "Slow pages", "minor")
        for _ in range(3):
            ctl.usage.apply_github_status(gs.parse(dict(OK, incidents=[_incident(), minor])))
        self.assertEqual(ui.notifications, ["Actions runs delayed"])


if __name__ == "__main__":
    unittest.main()
