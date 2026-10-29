import unittest

from awaitray.config import Filters
from awaitray.filters import filter_prs, keep
from awaitray.github import PullRequest


def _pr(repo="acme/web", author="alice", is_draft=False, number=1):
    return PullRequest(
        host="github.com",
        repo=repo,
        number=number,
        title="t",
        url=f"https://github.com/{repo}/pull/{number}",
        author=author,
        is_draft=is_draft,
    )


class KeepTests(unittest.TestCase):
    def test_keeps_default(self):
        self.assertTrue(keep(_pr(), Filters()))

    def test_drops_ignored_author_glob(self):
        f = Filters(ignored_authors=["*-bot"])
        self.assertFalse(keep(_pr(author="release-bot"), f))
        self.assertTrue(keep(_pr(author="alice"), f))

    def test_drops_ignored_repo_glob(self):
        f = Filters(ignored_repos=["*/legacy-*"])
        self.assertFalse(keep(_pr(repo="acme/legacy-api"), f))
        self.assertTrue(keep(_pr(repo="acme/web"), f))

    def test_literal_match_handles_brackets(self):
        # fnmatch treats `[bot]` as a character set, so the default ignored
        # authors only match via the literal-equality fallback.
        f = Filters(ignored_authors=["dependabot[bot]"])
        self.assertFalse(keep(_pr(author="dependabot[bot]"), f))

    def test_author_match_is_case_insensitive(self):
        f = Filters(ignored_authors=["RENOVATE[bot]"])
        self.assertFalse(keep(_pr(author="renovate[bot]"), f))

    def test_drafts_excluded_when_disabled(self):
        f = Filters(include_drafts=False)
        self.assertFalse(keep(_pr(is_draft=True), f))
        self.assertTrue(keep(_pr(is_draft=False), f))

    def test_empty_pattern_does_not_match_everything(self):
        f = Filters(ignored_authors=[""])
        self.assertTrue(keep(_pr(author="alice"), f))


class FilterPrsTests(unittest.TestCase):
    def test_preserves_order_and_drops_matches(self):
        prs = [_pr(number=1, author="alice"), _pr(number=2, author="release-bot"), _pr(number=3)]
        f = Filters(ignored_authors=["*-bot"])
        out = filter_prs(prs, f)
        self.assertEqual([p.number for p in out], [1, 3])


if __name__ == "__main__":
    unittest.main()
