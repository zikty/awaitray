import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from awaitray import repos as repo_mod

# Identity via env so every repo the tests create (including clones, which
# don't inherit the origin's local config) can commit on machines without a
# global git identity, like CI runners.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=_GIT_ENV
    )


def _init_repo(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "tester")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(path, fname, content):
    (Path(path) / fname).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", f"add {fname}")


@unittest.skipUnless(shutil.which("git"), "git required")
class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        # git fetch may leave a background maintenance job writing into .git:
        # TemporaryDirectory.cleanup() then fails now and then ("Directory not
        # empty"); a best-effort rmtree doesn't.
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.origin = str(Path(root) / "origin")
        self.work = str(Path(root) / "work")
        _init_repo(self.origin)
        _commit(self.origin, "a.txt", "1")
        _git(root, "clone", "-q", self.origin, self.work)
        # Advance origin so the clone is exactly one commit behind.
        _commit(self.origin, "a.txt", "2")

    def _read(self, fname):
        return (Path(self.work) / fname).read_text()

    def test_clean_behind_fast_forwards(self):
        res = repo_mod.fetch(self.work, "origin", auto_update=True)
        self.assertTrue(res.ok)
        self.assertEqual(res.updated, 1)
        self.assertEqual(res.behind, 0)
        self.assertEqual(self._read("a.txt"), "2")

    def test_auto_update_off_does_not_update(self):
        res = repo_mod.fetch(self.work, "origin", auto_update=False)
        self.assertTrue(res.ok)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.behind, 1)
        self.assertEqual(self._read("a.txt"), "1")

    def test_dirty_tracked_file_blocks_update(self):
        (Path(self.work) / "a.txt").write_text("local edit")
        res = repo_mod.fetch(self.work, "origin", auto_update=True)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.behind, 1)
        self.assertEqual(self._read("a.txt"), "local edit")

    def test_untracked_file_blocks_update(self):
        (Path(self.work) / "scratch.txt").write_text("untracked")
        res = repo_mod.fetch(self.work, "origin", auto_update=True)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.behind, 1)
        self.assertEqual(self._read("a.txt"), "1")

    def test_diverged_branch_is_not_fast_forwarded(self):
        # Local commit makes the clone ahead as well as behind → not ff-able.
        _commit(self.work, "b.txt", "local")
        res = repo_mod.fetch(self.work, "origin", auto_update=True)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.ahead, 1)
        self.assertEqual(res.behind, 1)


@unittest.skipUnless(shutil.which("git"), "git required")
class IsCleanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self._tmp.name) / "r")
        _init_repo(self.repo)
        _commit(self.repo, "a.txt", "1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_repo(self):
        self.assertTrue(repo_mod.is_clean(self.repo))

    def test_modified_is_dirty(self):
        (Path(self.repo) / "a.txt").write_text("changed")
        self.assertFalse(repo_mod.is_clean(self.repo))

    def test_untracked_is_dirty(self):
        (Path(self.repo) / "new.txt").write_text("x")
        self.assertFalse(repo_mod.is_clean(self.repo))

    def test_non_repo_is_not_clean(self):
        self.assertFalse(repo_mod.is_clean(self._tmp.name))


class SummarizeGitErrorTests(unittest.TestCase):
    """One actionable line from a failed git call, not git's trailing boilerplate."""

    def s(self, stderr, stdout="", code=128):
        return repo_mod._summarize_git_error(stderr, stdout, code)

    def test_ssh_key_rejected(self):
        err = (
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\n"
            "and the repository exists.\n"
        )
        self.assertTrue(self.s(err).startswith("SSH auth failed"))

    def test_https_auth_failed(self):
        err = (
            "remote: Invalid username or token.\n"
            "fatal: Authentication failed for 'https://github.com/o/r.git/'\n"
        )
        self.assertEqual(self.s(err), "auth failed (check `gh auth` / git credentials)")
        err = "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
        self.assertEqual(self.s(err), "auth failed (check `gh auth` / git credentials)")

    def test_permission_denied_without_publickey(self):
        err = (
            "remote: Permission to o/r.git denied to someone.\n"
            "fatal: unable to access: The requested URL returned error: 403\n"
        )
        self.assertEqual(self.s(err), "permission denied (no access to this repo)")

    def test_http_403(self):
        err = "fatal: unable to access 'https://x/': The requested URL returned error: 403\n"
        self.assertEqual(self.s(err), "auth failed (HTTP 403)")

    def test_network(self):
        self.assertEqual(
            self.s("ssh: Could not resolve hostname github.com: nodename nor servname provided\n"),
            "network: cannot resolve host (offline?)",
        )
        self.assertEqual(
            self.s("ssh: connect to host github.com port 22: Operation timed out\n"),
            "network: connection failed",
        )

    def test_repository_not_found(self):
        err = (
            "ERROR: Repository not found.\n"
            "fatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\n"
            "and the repository exists.\n"
        )
        self.assertEqual(self.s(err), "repository not found (or no access with this account)")

    def test_fallback_skips_boilerplate(self):
        err = (
            "fatal: couldn't find remote ref refs/heads/gone\n"
            "fatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\n"
            "and the repository exists.\n"
        )
        self.assertEqual(self.s(err), "fatal: couldn't find remote ref refs/heads/gone")

    def test_empty_output_reports_exit_code(self):
        self.assertEqual(self.s("", "", 1), "exit 1")
        self.assertEqual(self.s("", "only stdout\n", 1), "only stdout")


class FetchErrorMessageTests(unittest.TestCase):
    def test_fetch_from_missing_remote_is_not_boilerplate(self):
        tmp = tempfile.mkdtemp()
        # git may leave a background maintenance job writing into .git.
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        _commit(tmp, "a", "1")
        _git(tmp, "remote", "add", "origin", os.path.join(tmp, "does-not-exist"))
        res = repo_mod.fetch(tmp)
        self.assertFalse(res.ok)
        self.assertNotIn("repository exists", res.message)
        self.assertNotIn("correct access rights", res.message)


if __name__ == "__main__":
    unittest.main()
