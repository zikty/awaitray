import unittest

from awaitray.config import Colors, Config, Priorities
from awaitray.controller import Controller, PollResult
from awaitray.github import PRDetails, PullRequest


def _pr(repo="acme/web", is_draft=False, number=1):
    return PullRequest(
        host="github.com",
        repo=repo,
        number=number,
        title="t",
        url=f"https://github.com/{repo}/pull/{number}",
        author="alice",
        is_draft=is_draft,
    )


def _details(url, *, review_decision=None, ci_state=None):
    return PRDetails(url=url, head_oid=None, ci_state=ci_state, review_decision=review_decision)


class _Stub:
    """Stand-in for the UI/host ports; pr_color never calls into them."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _poll(ctl, pr, details):
    ctl.apply_poll_result(PollResult(review_prs=[pr], details={pr.url: details}))


def _controller(cfg):
    return Controller(ui=_Stub(), host=_Stub(), cfg=cfg)


class PrColorTests(unittest.TestCase):
    def test_changes_requested_wins_over_draft(self):
        cfg = Config(colors=Colors(changes_requested="#E74C3C", draft="#7F8C8D"))
        ctl = _controller(cfg)
        pr = _pr(is_draft=True)
        _poll(ctl, pr, _details(pr.url, review_decision="CHANGES_REQUESTED"))
        self.assertEqual(ctl.pr_color(pr, None), "#E74C3C")

    def test_approved_used_when_set(self):
        cfg = Config(colors=Colors(approved="#3DB371"))
        ctl = _controller(cfg)
        pr = _pr()
        _poll(ctl, pr, _details(pr.url, review_decision="APPROVED"))
        self.assertEqual(ctl.pr_color(pr, None), "#3DB371")

    def test_ci_failure_used_when_set(self):
        cfg = Config(colors=Colors(ci_failure="#E67E22"))
        ctl = _controller(cfg)
        pr = _pr()
        _poll(ctl, pr, _details(pr.url, ci_state="FAILURE"))
        self.assertEqual(ctl.pr_color(pr, None), "#E67E22")

    def test_review_decision_over_ci_state(self):
        cfg = Config(colors=Colors(approved="#3DB371", ci_failure="#E67E22"))
        ctl = _controller(cfg)
        pr = _pr()
        _poll(ctl, pr, _details(pr.url, review_decision="APPROVED", ci_state="FAILURE"))
        self.assertEqual(ctl.pr_color(pr, None), "#3DB371")

    def test_state_color_skipped_when_field_empty(self):
        # APPROVED but no approved color → falls through to section default.
        cfg = Config(colors=Colors(approved="", draft=""))
        ctl = _controller(cfg)
        pr = _pr()
        _poll(ctl, pr, _details(pr.url, review_decision="APPROVED"))
        self.assertEqual(ctl.pr_color(pr, "#000000"), "#000000")

    def test_no_details_skips_state_colors(self):
        # Details not fetched yet → only draft / high-priority / section apply.
        cfg = Config(colors=Colors(changes_requested="#E74C3C", draft="#7F8C8D"))
        ctl = _controller(cfg)
        pr = _pr(is_draft=True)
        self.assertEqual(ctl.pr_color(pr, None), "#7F8C8D")

    def test_draft_color(self):
        cfg = Config(colors=Colors(draft="#7F8C8D"))
        ctl = _controller(cfg)
        self.assertEqual(ctl.pr_color(_pr(is_draft=True), None), "#7F8C8D")

    def test_high_priority_color(self):
        cfg = Config(
            colors=Colors(high_priority="#111111", draft=""),
            priorities=Priorities(high_priority_repos=["acme/*"]),
        )
        ctl = _controller(cfg)
        self.assertEqual(ctl.pr_color(_pr(repo="acme/web"), None), "#111111")

    def test_section_default_fallback(self):
        cfg = Config(colors=Colors(draft="", high_priority=""))
        ctl = _controller(cfg)
        self.assertEqual(ctl.pr_color(_pr(), "#3DB371"), "#3DB371")
        self.assertIsNone(ctl.pr_color(_pr(), None))


if __name__ == "__main__":
    unittest.main()
