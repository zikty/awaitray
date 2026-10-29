import unittest
from datetime import datetime, timezone

from awaitray import pr_state as ps
from awaitray.github import PRDetails, PullRequest


def _pr(draft=False, created="2026-09-20T10:00:00Z"):
    return PullRequest(
        "github.com", "acme/web", 1, "t", "u", "me", is_draft=draft, created_at=created
    )


def _det(ci=None, review=None, mergeable=None):
    return PRDetails("u", "oid", ci, review, mergeable)


class ClassifyMineTests(unittest.TestCase):
    def test_sections(self):
        cases = [
            (_pr(draft=True), _det("FAILURE", "CHANGES_REQUESTED"), ps.DRAFTS),
            (_pr(), _det(review="CHANGES_REQUESTED"), ps.NEEDS_ACTION),
            (_pr(), _det(ci="FAILURE", review="APPROVED"), ps.NEEDS_ACTION),
            (
                _pr(),
                _det(ci="SUCCESS", review="APPROVED", mergeable="CONFLICTING"),
                ps.NEEDS_ACTION,
            ),
            (_pr(), _det(ci="SUCCESS", review="APPROVED"), ps.READY_TO_MERGE),
            (_pr(), _det(ci=None, review=None), ps.READY_TO_MERGE),  # no CI, no review required
            (_pr(), _det(ci="SUCCESS", review="APPROVED", mergeable="UNKNOWN"), ps.READY_TO_MERGE),
            (_pr(), _det(ci="PENDING", review="APPROVED"), ps.WAITING),
            (_pr(), _det(ci="SUCCESS", review="REVIEW_REQUIRED"), ps.WAITING),
            (_pr(), None, ps.READY_TO_MERGE),  # nothing known yet
        ]
        for pr, det, expected in cases:
            with self.subTest(det=det, draft=pr.is_draft):
                self.assertEqual(ps.classify_mine(pr, det), expected)


class GlyphAndLabelTests(unittest.TestCase):
    def test_glyphs(self):
        self.assertEqual(
            ps.glyph(_pr(), _det(review="CHANGES_REQUESTED"), mine=True), ps.GLYPH_ACTION
        )
        self.assertEqual(ps.glyph(_pr(), _det(ci="FAILURE")), ps.GLYPH_FAILING)
        self.assertEqual(ps.glyph(_pr(), _det(ci="PENDING")), ps.GLYPH_RUNNING)
        self.assertEqual(ps.glyph(_pr(), _det(ci="SUCCESS")), ps.GLYPH_GREEN)
        self.assertEqual(ps.glyph(_pr(), None), ps.GLYPH_NONE)
        # Review rows never show ⚠: that's about *your* PRs.
        self.assertEqual(ps.glyph(_pr(), _det(review="CHANGES_REQUESTED")), ps.GLYPH_NONE)

    def test_labels(self):
        self.assertEqual(
            ps.state_label(_pr(), _det(review="CHANGES_REQUESTED", ci="FAILURE")),
            "changes requested",
        )
        self.assertEqual(ps.state_label(_pr(), _det(ci="FAILURE")), "checks failing")
        self.assertEqual(ps.state_label(_pr(), _det(mergeable="CONFLICTING")), "conflict")
        self.assertEqual(ps.state_label(_pr(), _det(review="APPROVED")), "approved")
        self.assertEqual(ps.state_label(_pr(), _det()), "ready")
        self.assertEqual(
            ps.state_label(_pr(), _det(ci="PENDING", review="APPROVED")), "checks running"
        )
        self.assertEqual(ps.state_label(_pr(), _det(review="REVIEW_REQUIRED")), "review required")
        self.assertEqual(ps.state_label(_pr(draft=True), None), "draft")


class AgeTests(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    def test_compact_ages(self):
        cases = {
            "2026-09-25T11:59:50Z": "1m",
            "2026-09-25T11:15:00Z": "45m",
            "2026-09-25T09:00:00Z": "3h",
            "2026-09-23T12:00:00Z": "2d",
            "2026-08-21T12:00:00Z": "5w",
        }
        for created, expected in cases.items():
            self.assertEqual(ps.age(created, self.NOW), expected, created)

    def test_unknown_or_invalid(self):
        self.assertEqual(ps.age(None, self.NOW), "")
        self.assertEqual(ps.age("yesterday", self.NOW), "")


if __name__ == "__main__":
    unittest.main()
