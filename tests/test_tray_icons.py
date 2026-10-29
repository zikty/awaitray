import unittest
import xml.etree.ElementTree as ET

from awaitray import github as gh
from awaitray.backends.linux import icon_renderer as ir
from awaitray.config import Config, HostConfig, Priorities
from awaitray.controller import (
    TRAY_COUNT,
    TRAY_ERROR,
    TRAY_IDLE,
    TRAY_OFFLINE,
    TRAY_PAUSED,
    Controller,
    PollResult,
    TrayState,
)
from awaitray.github import PullRequest


class _UI:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _pr(n, repo="acme/web"):
    return PullRequest("github.com", repo, n, "t", f"u{n}", "a")


class TrayStateTests(unittest.TestCase):
    def _ctl(self, **cfg):
        return Controller(ui=_UI(), host=_UI(), cfg=Config(**cfg))

    def test_states_in_priority_order(self):
        ctl = self._ctl()
        ctl.apply_poll_result(PollResult())
        self.assertEqual(ctl.tray_state(), TrayState(TRAY_IDLE))
        ctl.apply_poll_result(PollResult(review_prs=[_pr(1), _pr(2)]))
        self.assertEqual(ctl.tray_state(), TrayState(TRAY_COUNT, 2, False, unseen=True))
        ctl.cfg.paused = True
        self.assertEqual(ctl.tray_state().kind, TRAY_PAUSED)

    def test_offline_vs_error(self):
        ctl = self._ctl(hosts=[HostConfig("github.com")])
        ctl.apply_poll_result(
            PollResult(
                errors=["x"],
                total_failure=True,
                failed_hosts={"github.com"},
                host_errors={"github.com": gh.ERR_NETWORK},
            )
        )
        self.assertEqual(ctl.tray_state().kind, TRAY_OFFLINE)
        ctl.apply_poll_result(
            PollResult(
                errors=["x"],
                total_failure=True,
                failed_hosts={"github.com"},
                host_errors={"github.com": gh.ERR_AUTH},
            )
        )
        self.assertEqual(ctl.tray_state().kind, TRAY_ERROR)

    def test_priority_repo_sets_alert(self):
        ctl = self._ctl(priorities=Priorities(high_priority_repos=["acme/*"]))
        ctl.apply_poll_result(PollResult(review_prs=[_pr(1)]))
        self.assertTrue(ctl.tray_state().alert)


class UnseenDotTests(unittest.TestCase):
    """A dot for review requests not seen yet; opening the menu clears it."""

    def setUp(self):
        self.ui = _UI()
        self.ctl = Controller(
            ui=self.ui, host=self.ui, cfg=Config(hosts=[HostConfig("github.com")])
        )

    def _poll(self, *prs, failed=frozenset()):
        self.ctl.apply_poll_result(PollResult(review_prs=list(prs), failed_hosts=set(failed)))

    def test_first_poll_is_all_seen_then_new_prs_show_the_dot(self):
        self._poll(_pr(1))
        self.assertFalse(self.ctl.tray_state().unseen)
        self._poll(_pr(1), _pr(2))
        self.assertTrue(self.ctl.tray_state().unseen)
        self.ctl.menu_opened()
        self.assertFalse(self.ctl.tray_state().unseen)

    def test_a_re_requested_pr_is_new_again(self):
        self._poll(_pr(1))
        self._poll()
        self._poll(_pr(1))
        self.assertTrue(self.ctl.tray_state().unseen)

    def test_a_host_blip_does_not_bring_the_dot_back(self):
        self._poll(_pr(1))
        self._poll(failed={"github.com"})
        self._poll(_pr(1))
        self.assertFalse(self.ctl.tray_state().unseen)

    def test_setting_off_and_snoozed_prs(self):
        from awaitray.config import SnoozeEntry

        self._poll()
        self._poll(_pr(1))
        self.ctl.cfg.snoozed[_pr(1).key] = SnoozeEntry(ignored=True)
        self.assertFalse(self.ctl.tray_state().unseen)  # not counted, not "new"
        del self.ctl.cfg.snoozed[_pr(1).key]
        self.ctl.cfg.tray_icon.unseen_dot = False
        self.assertFalse(self.ctl.tray_state().unseen)


class LinuxSvgTests(unittest.TestCase):
    STATES = (
        TrayState(TRAY_IDLE),
        TrayState(TRAY_COUNT, 3),
        TrayState(TRAY_COUNT, 150),
        TrayState(TRAY_COUNT, 4, True),
        TrayState(TRAY_COUNT, 4, False, True),
        TrayState(TRAY_COUNT, 4, True, True),
        TrayState(TRAY_PAUSED),
        TrayState(TRAY_ERROR),
        TrayState(TRAY_OFFLINE),
    )

    def test_every_state_is_valid_svg_with_a_distinct_name(self):
        names = set()
        for st in self.STATES:
            name, svg = ir.icon_for(st)
            root = ET.fromstring(svg.split("\n", 1)[1])  # skip the XML declaration
            self.assertTrue(root.tag.endswith("svg"), st)
            self.assertFalse(name.endswith("-symbolic"))  # GTK would fill our strokes
            names.add(name)
        self.assertEqual(len(names), len(self.STATES))

    def test_monochrome_icons_follow_the_panel_color_on_kde(self):
        _, svg = ir.icon_for(TrayState(TRAY_IDLE))
        self.assertIn("ColorScheme-Text", svg)
        self.assertIn("currentColor", svg)
        self.assertIn(ir.FALLBACK_GRAY, svg)

    def test_alert_is_red_with_white_text(self):
        name, svg = ir.icon_for(TrayState(TRAY_COUNT, 4, True))
        self.assertEqual(name, "awaitray-4-alert")
        self.assertIn(ir.ALERT_RED, svg)
        self.assertIn('fill="#ffffff"', svg)

    def test_counts_are_capped(self):
        name, svg = ir.icon_for(TrayState(TRAY_COUNT, 150))
        self.assertEqual(name, "awaitray-99plus")
        self.assertIn(">99+<", svg)


if __name__ == "__main__":
    unittest.main()
