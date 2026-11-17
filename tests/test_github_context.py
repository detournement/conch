"""Repository awareness: remote detection, the GitHub snapshot, and what the
model and the status bar are shown."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import github_context as gc


def _snapshot(**overrides):
    data = {
        "nameWithOwner": "acme/rocket", "description": "Rockets, mostly", "visibility": "PRIVATE",
        "url": "https://github.com/acme/rocket", "isFork": False, "isArchived": False, "parent": None,
        "defaultBranchRef": {"name": "main"},
        "latestRelease": {"tagName": "v2.1.0", "publishedAt": "2026-11-01T10:00:00Z"},
        "pullRequests": {"totalCount": 7, "nodes": [
            {"number": 42, "title": "Add thrust vectoring", "isDraft": False,
             "headRefName": "feature/thrust", "author": {"login": "ada"}}]},
        "issues": {"totalCount": 12, "nodes": [
            {"number": 9, "title": "Engine stalls on cold start", "labels": {"nodes": [{"name": "bug"}]}}]},
        "ref": {
            "associatedPullRequests": {"nodes": [
                {"number": 42, "title": "Add thrust vectoring", "state": "OPEN", "isDraft": False,
                 "reviewDecision": "CHANGES_REQUESTED", "baseRefName": "main",
                 "url": "https://github.com/acme/rocket/pull/42"}]},
            "target": {"abbreviatedOid": "abc1234", "statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
                {"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "name": "test (3.9)", "conclusion": "FAILURE"},
                {"__typename": "CheckRun", "name": "test (3.9)", "conclusion": "FAILURE"},
                {"__typename": "StatusContext", "context": "ci/deploy", "state": "ERROR"}]}}},
        },
    }
    data.update(overrides)
    return data


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "rocket"
        (self.root / ".git").mkdir(parents=True)
        self.checkout("feature/thrust")

    def tearDown(self):
        self._tmp.cleanup()

    def checkout(self, branch):
        (self.root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")

    def context(self, data=None, error=""):
        context = gc.RepoContext(gc.Repo(self.root, "github.com", "acme", "rocket"))
        context.data, context.error = data, error
        context.done.set()
        return context


class RemoteParsingTests(unittest.TestCase):
    def test_remote_forms_resolve_to_host_owner_and_name(self):
        cases = {
            "git@github.com:acme/rocket.git": ("github.com", "acme", "rocket"),
            "https://github.com/acme/rocket": ("github.com", "acme", "rocket"),
            "https://x-access-token:ghp_secret@github.com/acme/rocket.git": ("github.com", "acme", "rocket"),
            "ssh://git@ssh.github.com:443/acme/rocket.git": ("ssh.github.com", "acme", "rocket"),
            "git@github-work:acme/rocket.git": ("github-work", "acme", "rocket"),
            "https://github.example.com/acme/rocket.git": ("github.example.com", "acme", "rocket"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(gc.parse_remote_url(url), expected)
        for url in ("/srv/git/rocket.git", "file:///srv/git/rocket.git", ""):
            with self.subTest(url=url):
                self.assertIsNone(gc.parse_remote_url(url))

    def test_github_hosts_and_others(self):
        self.assertEqual(gc.github_host("github.com"), "github.com")
        self.assertEqual(gc.github_host("ssh.github.com"), "github.com")
        self.assertEqual(gc.github_host("github.com-personal"), "github.com")
        self.assertEqual(gc.github_host("github-work"), "github.com")  # SSH alias
        self.assertEqual(gc.github_host("github.example.com"), "github.example.com")
        for host in ("gitlab.com", "bitbucket.org", "git.example.com", "mygithub.example.net"):
            with self.subTest(host=host):
                self.assertEqual(gc.github_host(host), "")


class DetectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_github_remote_wins_over_other_hosts(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "git@gitlab.com:acme/mirror.git")
        self.git("remote", "add", "upstream", "https://github.com/acme/rocket.git")
        repo = gc.detect(str(self.root))
        self.assertEqual((repo.host, repo.slug), ("github.com", "acme/rocket"))

    def test_origin_wins_among_github_remotes(self):
        self.git("init", "-q")
        self.git("remote", "add", "upstream", "https://github.com/acme/rocket.git")
        self.git("remote", "add", "origin", "git@github.com:me/rocket.git")
        self.assertEqual(gc.detect(str(self.root)).slug, "me/rocket")

    def test_plain_git_checkout_has_no_github_host(self):
        self.git("init", "-q")
        repo = gc.detect(str(self.root))
        self.assertEqual((repo.host, repo.slug), ("", self.root.name))


class BranchTests(_RepoCase):
    def test_branch_detached_and_worktree(self):
        self.assertEqual(gc.current_branch(self.root), "feature/thrust")
        (self.root / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
        self.assertEqual(gc.current_branch(self.root), "")
        worktree = Path(self._tmp.name) / "wt"
        gitdir = Path(self._tmp.name) / "gitdirs" / "wt"
        gitdir.mkdir(parents=True)
        worktree.mkdir()
        (gitdir / "HEAD").write_text("ref: refs/heads/hotfix\n")
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
        self.assertEqual(gc.current_branch(worktree), "hotfix")


class FetchTests(unittest.TestCase):
    repo = gc.Repo(Path("/tmp/rocket"), "github.com", "acme", "rocket")

    def run_gh(self, returncode=0, stdout="", stderr=""):
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        with patch("conch.github_context.shutil.which", return_value="/usr/bin/gh"), \
                patch("conch.github_context.subprocess.run", return_value=done) as run:
            return gc.fetch(self.repo, "main"), run

    def test_snapshot_comes_back(self):
        (data, error), run = self.run_gh(stdout=json.dumps({"data": {"repository": _snapshot()}}))
        self.assertEqual((data["nameWithOwner"], error), ("acme/rocket", ""))
        self.assertIn("branch=refs/heads/main", run.call_args.args[0])

    def test_failures_say_why(self):
        with patch("conch.github_context.shutil.which", return_value=None):
            self.assertEqual(gc.fetch(self.repo, "main"), (None, "the gh CLI is not installed"))
        for stderr in ("gh: Bad credentials (HTTP 401)",
                       "To get started with GitHub CLI, please run:  gh auth login",
                       "gh: To use GitHub CLI in automation, set the GH_TOKEN environment variable."):
            with self.subTest(stderr=stderr):
                self.assertEqual(self.run_gh(1, stderr=stderr)[0],
                                 (None, "gh is not signed in (gh auth login)"))
        self.assertEqual(self.run_gh(stdout='{"data": {"repository": null}}')[0],
                         (None, "repository not found on GitHub"))
        with patch("conch.github_context.shutil.which", return_value="/usr/bin/gh"), \
                patch("conch.github_context.subprocess.run",
                      side_effect=subprocess.TimeoutExpired("gh", 10)):
            self.assertEqual(gc.fetch(self.repo, "main"), (None, "GitHub did not answer in time"))


class ViewTests(_RepoCase):
    def test_prompt_section_tells_the_model_what_github_knows(self):
        section = self.context(_snapshot()).prompt_section()
        for expected in ("acme/rocket (private)", "default branch main", "latest release v2.1.0",
                         "Current branch: feature/thrust (GitHub head abc1234, checks failing)",
                         "Failing checks: test (3.9), ci/deploy",
                         '#42 "Add thrust vectoring" (open, review changes requested) into main',
                         "Open pull requests (7)", "Open issues (12", "#9 Engine stalls on cold start [bug]"):
            self.assertIn(expected, section)
        self.assertEqual(section.count("test (3.9)"), 1)

    def test_prompt_section_stays_bounded(self):
        title = "x" * 500
        data = _snapshot(description="d" * 1000,
                         pullRequests={"totalCount": 99, "nodes": [
                             {"number": n, "title": title, "headRefName": "b" * 200,
                              "author": {"login": "a"}} for n in range(gc.LIST_LIMIT)]},
                         issues={"totalCount": 99, "nodes": [
                             {"number": n, "title": title} for n in range(gc.LIST_LIMIT)]})
        self.assertLessEqual(len(self.context(data).prompt_section()), gc.PROMPT_BUDGET_CHARS)

    def test_without_a_snapshot_the_model_learns_why(self):
        section = self.context(error="gh is not signed in (gh auth login)").prompt_section()
        self.assertIn("GitHub repository: acme/rocket; current branch feature/thrust.", section)
        self.assertIn("gh is not signed in", section)

    def test_indicator_follows_the_checked_out_branch(self):
        context = self.context(_snapshot())
        self.assertEqual(context.indicator(), "🐙 acme/rocket ⎇ feature/thrust #42 ✗")
        self.checkout("main")  # the snapshot's PR and checks belong to feature/thrust
        self.assertEqual(context.indicator(), "🐙 acme/rocket ⎇ main")

    def test_banner_summarizes_or_explains(self):
        banner = self.context(_snapshot()).banner_line()
        self.assertIn("PR #42 open", banner)
        self.assertIn("checks failing: test (3.9), ci/deploy", banner)
        self.assertIn("7 open PRs · 12 open issues", banner)
        self.assertIn("no GitHub details: the gh CLI is not installed",
                      self.context(error="the gh CLI is not installed").banner_line())

    def test_plain_git_checkout_shows_name_and_branch_only(self):
        context = gc.RepoContext(gc.Repo(self.root))
        self.assertTrue(context.done.is_set())
        self.assertEqual(context.indicator(), "rocket ⎇ feature/thrust")
        self.assertEqual(context.prompt_section(), "")


class StartTests(_RepoCase):
    def tearDown(self):
        gc._contexts.clear()
        super().tearDown()

    def test_github_context_off_never_fetches(self):
        repo = gc.Repo(self.root, "github.com", "acme", "rocket")
        with patch("conch.github_context.detect", return_value=repo), \
                patch("conch.github_context.fetch", side_effect=AssertionError("fetched")):
            context = gc.start({"github_context": "false"}, str(self.root))
        self.assertTrue(context.done.is_set())
        self.assertIn("github_context is off", context.banner_line())

    def test_snapshot_listeners_run_once_it_lands(self):
        repo = gc.Repo(self.root, "github.com", "acme", "rocket")
        seen = []
        with patch("conch.github_context.detect", return_value=repo), \
                patch("conch.github_context.fetch", return_value=(_snapshot(), "")):
            context = gc.start({}, str(self.root))
            context.done.wait(5)
        context.on_ready(lambda: seen.append(context.indicator()))
        self.assertEqual(seen, ["🐙 acme/rocket ⎇ feature/thrust #42 ✗"])
        self.assertIs(gc.get(str(self.root)), context)


if __name__ == "__main__":
    unittest.main()
