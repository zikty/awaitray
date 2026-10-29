"""Text from GitHub made safe to show, and safe to paste as Markdown."""

import unittest

from awaitray import menu_model as mm
from awaitray import text
from awaitray.github import PullRequest


class CleanTests(unittest.TestCase):
    def test_hidden_and_control_characters_go(self):
        self.assertEqual(text.clean("a\u202eb\u2066c\u200bd\x1b[31me\x00"), "abcd[31me")
        self.assertEqual(text.clean(" one\ntwo\tthree\u2028 "), "one two three")
        self.assertEqual(text.clean("héllo 👋 日本"), "héllo 👋 日本")

    def test_limit_ends_in_an_ellipsis(self):
        self.assertEqual(text.clean("abcdef", 4), "abc…")
        self.assertEqual(text.clean("abcd", 4), "abcd")


class MarkdownLinkTests(unittest.TestCase):
    def test_brackets_in_the_title_stay_in_the_link_text(self):
        pr = PullRequest(
            "github.com",
            "acme/web",
            1,
            "[x](https://evil) \\o/",
            "https://github.com/acme/web/pull/1",
            "a",
            False,
        )
        self.assertEqual(
            mm.markdown_link(pr),
            "[acme/web#1: \\[x\\](https://evil) \\\\o/](https://github.com/acme/web/pull/1)",
        )


if __name__ == "__main__":
    unittest.main()
