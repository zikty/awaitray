import unittest
from unittest import mock

from awaitray.config import Config, Filters, HostConfig, MyPRsConfig
from awaitray.controller import Controller, PollResult
from awaitray.github import PullRequest


def _pr(number, host="github.com", repo="acme/web"):
    return PullRequest(
        host=host,
        repo=repo,
        number=number,
        title=f"PR {number}",
        url=f"https://{host}/{repo}/pull/{number}",
        author="alice",
        is_draft=False,
    )


class _RecordingUI:
    """UI/host port stub that records notifications and ignores the rest."""

    def __init__(self):
        self.notifications: list[str] = []

    def notify(self, title, body, urgency, icon):
        self.notifications.append(title)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Base(unittest.TestCase):
    def _controller(self, **cfg_kwargs):
        self.ui = _RecordingUI()
        cfg = Config(**cfg_kwargs)
        return Controller(ui=self.ui, host=self.ui, cfg=cfg)

    def _poll(self, ctl, review=(), mine=(), failed=None, my_failed=None, errors=None):
        ctl.apply_poll_result(
            PollResult(
                review_prs=list(review),
                my_prs=list(mine),
                errors=list(errors or []),
                failed_hosts=failed or set(),
                my_failed_hosts=my_failed or set(),
            )
        )

    def _review_notifications(self):
        return [n for n in self.ui.notifications if n == "New PR review request"]

    def _my_notifications(self):
        return [n for n in self.ui.notifications if n == "New PR of yours"]


class ReviewRequestNotificationTests(_Base):
    def test_first_poll_takes_a_silent_baseline(self):
        ctl = self._controller()
        self._poll(ctl, review=[_pr(1), _pr(2)])
        self.assertEqual(self._review_notifications(), [])

    def test_empty_queue_then_first_request_notifies(self):
        ctl = self._controller()
        self._poll(ctl, review=[])
        self._poll(ctl, review=[_pr(1)])
        self.assertEqual(len(self._review_notifications()), 1)

    def test_new_request_on_top_of_existing_notifies_once(self):
        ctl = self._controller()
        self._poll(ctl, review=[_pr(1)])
        self._poll(ctl, review=[_pr(1), _pr(2)])
        self._poll(ctl, review=[_pr(1), _pr(2)])
        self.assertEqual(len(self._review_notifications()), 1)

    def test_host_blip_does_not_renotify_its_prs(self):
        ctl = self._controller(hosts=[HostConfig("github.com"), HostConfig("ghe.corp:8443")])
        gh_pr, ghe_pr = _pr(1), _pr(7, host="ghe.corp:8443")
        self._poll(ctl, review=[gh_pr, ghe_pr])
        # GHE fails for one round: its PR is missing only because we couldn't see it.
        self._poll(ctl, review=[gh_pr], failed={"ghe.corp:8443"}, errors=["ghe.corp:8443: timeout"])
        self._poll(ctl, review=[gh_pr, ghe_pr])
        self.assertEqual(self._review_notifications(), [])

    def test_request_removed_while_host_healthy_notifies_if_it_returns(self):
        ctl = self._controller()
        self._poll(ctl, review=[_pr(1)])
        self._poll(ctl, review=[])  # review done / request withdrawn
        self._poll(ctl, review=[_pr(1)])  # re-requested
        self.assertEqual(len(self._review_notifications()), 1)

    def test_snoozed_pr_does_not_notify(self):
        ctl = self._controller()
        self._poll(ctl, review=[])
        pr = _pr(1)
        ctl.cfg.snoozed[pr.key] = mock.Mock()
        with mock.patch.object(ctl.snoozes, "sweep_expired", return_value=False):
            self._poll(ctl, review=[pr])
        self.assertEqual(self._review_notifications(), [])

    def test_changing_filters_rebaselines_silently(self):
        ctl = self._controller(filters=Filters(ignored_authors=["alice"]))
        self._poll(ctl, review=[])
        new_cfg = Config(filters=Filters(ignored_authors=[]))
        with (
            mock.patch.object(ctl, "tick_prs"),
            mock.patch.object(ctl, "tick_fetch"),
            mock.patch.object(ctl, "_reschedule_timers"),
        ):
            ctl.on_prefs_saved(new_cfg)
        # alice's PR becomes visible because of the filter change, not because it's new.
        self._poll(ctl, review=[_pr(1)])
        self.assertEqual(self._review_notifications(), [])
        self._poll(ctl, review=[_pr(1), _pr(2)])
        self.assertEqual(len(self._review_notifications()), 1)


class TeamRequestTests(_Base):
    def _team(self, n):
        from dataclasses import replace

        return replace(_pr(n), direct_request=False)

    def test_team_requests_are_not_counted_or_notified_by_default(self):
        ctl = self._controller()
        self._poll(ctl, review=[])
        self._poll(ctl, review=[_pr(1), self._team(2)])
        self.assertEqual(len(self._review_notifications()), 1)
        self.assertEqual(ctl.visible_count(), 1)
        self.assertEqual(ctl.icon_count(), 1)
        self.assertEqual([p.number for p in ctl.team_prs()], [2])

    def test_settings_opt_into_team_counting_and_notifications(self):
        from awaitray.config import TrayIcon

        ctl = self._controller(
            notify_team_requests=True, tray_icon=TrayIcon(count_team_requests=True)
        )
        self._poll(ctl, review=[])
        self._poll(ctl, review=[_pr(1), self._team(2)])
        self.assertEqual(len(self._review_notifications()), 2)
        self.assertEqual(ctl.icon_count(), 2)
        self.assertEqual(ctl.visible_count(), 1)  # the headline stays "yours"


class MyPRNotificationTests(_Base):
    def _mine(self, ctl, prs):
        self._poll(ctl, mine=prs)

    def test_zero_to_one_of_my_prs_notifies(self):
        ctl = self._controller(my_prs=MyPRsConfig(enabled=True), notify_my_prs=True)
        self._mine(ctl, [])
        self._mine(ctl, [_pr(3)])
        self.assertEqual(len(self._my_notifications()), 1)

    def test_enabling_my_prs_later_does_not_flood(self):
        ctl = self._controller(my_prs=MyPRsConfig(enabled=False), notify_my_prs=True)
        self._mine(ctl, [])  # section disabled: nothing queried
        ctl.cfg.my_prs.enabled = True
        self._mine(ctl, [_pr(3), _pr(4)])  # first poll with it enabled
        self.assertEqual(self._my_notifications(), [])


class SnoozeByActivityTests(_Base):
    def test_activity_snooze_wakes_on_update_and_ignore_never_does(self):
        from dataclasses import replace

        from awaitray import snooze as snooze_mod

        ctl = self._controller()
        a, b = _pr(1), _pr(2)
        a = replace(a, updated_at="2026-09-25T10:00:00Z")
        b = replace(b, updated_at="2026-09-25T10:00:00Z")
        with mock.patch("awaitray.config.save"):
            self._poll(ctl, review=[a, b])
            ctl.snoozes.snooze(a, "activity")
            ctl.snoozes.snooze(b, "ignore")
            self.assertEqual(ctl.visible_prs(), [])
            later = "2026-09-25T11:00:00Z"
            self._poll(ctl, review=[replace(a, updated_at=later), replace(b, updated_at=later)])
        self.assertEqual([p.number for p in ctl.visible_prs()], [1])
        self.assertTrue(snooze_mod.is_active(ctl.cfg.snoozed[b.key]))


if __name__ == "__main__":
    unittest.main()
