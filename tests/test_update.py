"""Loss-prevention gates for updates against isolated, real Git repositories."""

import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from conch.update import UpdateError, update_checkout


GIT = shutil.which("git")


@unittest.skipUnless(GIT, "Git is required for checkout update tests")
class UpdateCheckoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conch-update-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        home = self.root / "home"
        home.mkdir()
        hooks = self.root / "empty-hooks"
        hooks.mkdir()
        config = self.root / "gitconfig"
        config.write_text(
            "[init]\n\tdefaultBranch = main\n"
            "[commit]\n\tgpgSign = false\n"
            "[tag]\n\tgpgSign = false\n"
            "[core]\n\tautocrlf = false\n"
            "\tattributesFile = /dev/null\n"
            f"\thooksPath = {hooks.as_posix()}\n"
            "[gc]\n\tauto = 0\n"
            "[maintenance]\n\tauto = false\n",
            encoding="utf-8",
        )
        # Do not inherit Git routing, signing, helpers, attributes, or user config.
        isolated = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR")
            if key in os.environ
        }
        isolated.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "TMPDIR": str(self.root),
            "LC_ALL": "C",
            "TZ": "UTC",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "GIT_MERGE_AUTOEDIT": "no",
            "GIT_AUTHOR_NAME": "Update Test",
            "GIT_AUTHOR_EMAIL": "update@example.invalid",
            "GIT_COMMITTER_NAME": "Update Test",
            "GIT_COMMITTER_EMAIL": "update@example.invalid",
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000",
        })
        environment = patch.dict(os.environ, isolated, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.consumer = self.root / "consumer"
        self.git("init", "--bare", str(self.remote), root=self.root)
        self.git("clone", str(self.remote), str(self.seed), root=self.root)
        for name, content in {
            ".gitignore": "cache/\n",
            "shared.txt": "base shared\n",
            "scratch.txt": "base scratch\n",
            "remote.txt": "base remote\n",
        }.items():
            self.write(name, content, root=self.seed)
        self.git("add", ".", root=self.seed)
        self.git("commit", "-m", "base", root=self.seed)
        self.git("push", "-u", "origin", "main", root=self.seed)
        self.git("clone", str(self.remote), str(self.consumer), root=self.root)
        self.base = self.out("rev-parse", "HEAD")

    def git(self, *args, root=None, check=True):
        result = subprocess.run(
            [GIT, *args], cwd=root or self.consumer, capture_output=True,
            text=True, encoding="utf-8", timeout=15,
        )
        if check:
            self.assertEqual(
                result.returncode, 0,
                f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}",
            )
        return result

    def out(self, *args, root=None):
        return self.git(*args, root=root).stdout.rstrip("\n")

    def write(self, name, content, *, root=None):
        path = (root or self.consumer) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, name, content, message, *, root=None):
        self.write(name, content, root=root)
        self.git("add", "--", name, root=root)
        self.git("commit", "-m", message, root=root)
        return self.out("rev-parse", "HEAD", root=root)

    def publish(self, name="remote.txt", content="upstream remote\n"):
        sha = self.commit(name, content, "upstream", root=self.seed)
        self.git("push", "origin", "main", root=self.seed)
        return sha

    def update(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return update_checkout(self.consumer, **kwargs)

    def stashes(self):
        return self.out("stash", "list", "--format=%H%x00%gs")

    def existing_stash(self, label="older user work"):
        self.write("scratch.txt", label + "\n")
        self.git("stash", "push", "-m", label)
        return self.out("rev-parse", "refs/stash")

    def dirty_work(self):
        self.write("scratch.txt", "staged scratch\n")
        self.git("add", "scratch.txt")
        self.write("scratch.txt", "unstaged scratch\n")
        self.write("local notes/draft.txt", "untracked draft\n")
        self.write("cache/private.txt", "ignored private data\n")
        return (
            self.out("diff", "--cached", "--binary"),
            self.out("diff", "--binary"),
        )

    def assert_dirty_work(self, before, *, root=None):
        self.assertEqual(self.out("diff", "--cached", "--binary", root=root), before[0])
        self.assertEqual(self.out("diff", "--binary", root=root), before[1])
        self.assertEqual(self.out("show", ":scratch.txt", root=root), "staged scratch")
        directory = root or self.consumer
        self.assertEqual((directory / "scratch.txt").read_text(), "unstaged scratch\n")
        self.assertEqual(
            (directory / "local notes/draft.txt").read_text(), "untracked draft\n",
        )
        self.assertEqual(
            self.out("ls-files", "--", "local notes/draft.txt", root=root), "",
        )

    def state(self):
        files = {}
        for directory, directories, names in os.walk(self.consumer):
            if Path(directory) == self.consumer:
                directories.remove(".git")
            for name in names:
                path = Path(directory) / name
                files[str(path.relative_to(self.consumer))] = path.read_bytes()
        return {
            "head": self.out("rev-parse", "HEAD"),
            "branch": self.git("symbolic-ref", "-q", "HEAD", check=False).stdout,
            "index": self.out("ls-files", "--stage", "-z"),
            "staged": self.out("diff", "--cached", "--binary"),
            "unstaged": self.out("diff", "--binary"),
            "status": self.out("status", "--porcelain=v1", "-z", "-uall"),
            "files": files,
            "stashes": self.stashes(),
        }

    def assert_no_operation(self):
        for name in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
            "rebase-apply", "sequencer",
        ):
            self.assertFalse((self.consumer / ".git" / name).exists(), name)

    def divergence(self):
        first = self.commit("local-a.txt", "local a\n", "local first")
        second = self.commit("local-b.txt", "local b\n", "local second")
        return first, second, self.publish()

    def assert_integrated_files(self):
        for name, content in (
            ("local-a.txt", "local a\n"), ("local-b.txt", "local b\n"),
            ("remote.txt", "upstream remote\n"),
        ):
            self.assertEqual((self.consumer / name).read_text(), content)
        self.assertEqual(self.out("symbolic-ref", "--short", "HEAD"), "main")

    def test_clean_fast_forward_uses_published_commit(self):
        upstream = self.publish()
        self.assertIs(self.update(), True)
        self.assertEqual(self.out("rev-parse", "HEAD"), upstream)
        self.assertEqual(self.out("rev-parse", "HEAD^"), self.base)
        self.assertEqual((self.consumer / "remote.txt").read_text(), "upstream remote\n")
        self.assertEqual(self.out("symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.out("status", "--porcelain"), "")
        self.assertEqual(self.stashes(), "")

    def nested_worktree(self):
        nested = self.consumer / ".claude" / "worktrees" / "agent"
        self.git("worktree", "add", "-q", "-b", "agent", str(nested))
        return nested

    def test_fast_forward_skips_untracked_nested_worktree(self):
        nested = self.nested_worktree()
        upstream = self.publish()
        self.assertIs(self.update(), True)
        self.assertEqual(self.out("rev-parse", "HEAD"), upstream)
        self.assertTrue((nested / ".git").exists())
        self.assertEqual(self.out("rev-parse", "HEAD", root=nested), self.base)
        self.assertEqual(self.stashes(), "")

    def test_nested_worktree_with_dirty_work_still_updates_and_restores(self):
        nested = self.nested_worktree()
        before = self.dirty_work()
        upstream = self.publish()
        self.assertIs(self.update(), True)
        self.assertEqual(self.out("rev-parse", "HEAD"), upstream)
        self.assert_dirty_work(before)
        self.assertTrue((nested / ".git").exists())
        self.assertEqual(self.stashes(), "")

    def test_up_to_date_checkout_is_unchanged(self):
        before = self.state()
        self.assertIs(self.update(), False)
        self.assertEqual(self.state(), before)

    def test_local_only_commits_and_dirty_work_are_retained_without_push(self):
        local = self.commit("local.txt", "local commit\n", "local only")
        self.existing_stash()
        self.dirty_work()
        before = self.state()
        self.assertIs(self.update(), False)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.out("rev-parse", "HEAD"), local)
        self.assertEqual(self.out("rev-parse", "main", root=self.remote), self.base)

    def test_divergence_merges_with_both_original_histories_as_parents(self):
        first, local, upstream = self.divergence()
        self.assertIs(self.update(), True)
        parents = self.out("rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        self.assertEqual(parents, [local, upstream])
        self.assertEqual(self.out("rev-parse", f"{local}^"), first)
        self.assert_integrated_files()
        self.assertEqual(self.out("status", "--porcelain"), "")
        self.assertEqual(self.out("rev-parse", "main", root=self.remote), upstream)

    def test_explicit_rebase_replays_all_local_commits_linearly(self):
        first, local, upstream = self.divergence()
        self.assertIs(self.update(strategy="rebase"), True)
        self.assertEqual(self.out("rev-parse", "HEAD~2"), upstream)
        self.assertEqual(self.out("rev-list", "--count", f"{upstream}..HEAD"), "2")
        self.assertEqual(self.out("rev-list", "--merges", f"{upstream}..HEAD"), "")
        self.assertEqual(
            self.out("log", "--reverse", "--format=%s", f"{upstream}..HEAD").splitlines(),
            ["local first", "local second"],
        )
        for original in (first, local):
            self.assertEqual(
                self.git("merge-base", "--is-ancestor", original, "HEAD", check=False).returncode,
                1,
            )
        self.assert_integrated_files()
        self.assertEqual(self.out("status", "--porcelain"), "")
        self.assertEqual(self.out("rev-parse", "main", root=self.remote), upstream)

    def assert_dirty_update(self, *, strategy="merge", divergent=False):
        if divergent:
            self.divergence()
        else:
            self.publish()
        self.existing_stash("first user stash")
        self.existing_stash("second user stash")
        previous_stashes = self.stashes()
        dirty = self.dirty_work()
        self.assertIs(self.update(strategy=strategy), True)
        self.assert_dirty_work(dirty)
        self.assertEqual(self.stashes(), previous_stashes)
        self.assertEqual(
            (self.consumer / "cache/private.txt").read_text(), "ignored private data\n",
        )
        self.assertEqual((self.consumer / "remote.txt").read_text(), "upstream remote\n")
        if divergent:
            self.assert_integrated_files()
        self.assert_no_operation()

    def test_fast_forward_restores_index_unstaged_untracked_and_older_stashes(self):
        self.assert_dirty_update()

    def test_merge_preserves_dirty_work_and_older_stashes(self):
        self.assert_dirty_update(divergent=True)

    def test_rebase_preserves_dirty_work_and_older_stashes(self):
        self.assert_dirty_update(strategy="rebase", divergent=True)

    def assert_integration_conflict_restored(self, strategy):
        self.commit("shared.txt", "local committed change\n", "conflicting local")
        self.publish("shared.txt", "upstream conflicting change\n")
        self.existing_stash()
        self.dirty_work()
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update(strategy=strategy)
        self.assertEqual(self.state(), before)
        self.assert_no_operation()

    def test_failed_merge_aborts_and_restores_original_branch_index_and_work(self):
        self.assert_integration_conflict_restored("merge")

    def test_failed_rebase_aborts_and_restores_original_branch_index_and_work(self):
        self.assert_integration_conflict_restored("rebase")

    def test_stash_conflict_keeps_all_work_in_a_recoverable_stash(self):
        self.existing_stash()
        previous_stashes = self.stashes().splitlines()
        dirty = self.dirty_work()
        self.write("shared.txt", "uncommitted local change\n")
        dirty = (dirty[0], self.out("diff", "--binary"))
        upstream = self.publish("shared.txt", "upstream conflicting change\n")
        with self.assertRaises(UpdateError) as error:
            self.update()
        self.assertEqual(self.out("rev-parse", "HEAD"), upstream)
        stashes = self.stashes().splitlines()
        self.assertEqual(stashes[1:], previous_stashes)
        self.assertEqual(len(stashes), len(previous_stashes) + 1)
        saved = stashes[0].split("\0", 1)[0]
        # A recovery reference must identify this stash, not merely mention failure.
        self.assertTrue(saved in str(error.exception) or "stash@{0}" in str(error.exception))
        self.assertEqual(self.out("show", f"{saved}^2:scratch.txt"), "staged scratch")
        self.assertEqual(self.out("show", f"{saved}:scratch.txt"), "unstaged scratch")
        self.assertEqual(self.out("show", f"{saved}:shared.txt"), "uncommitted local change")
        self.assertEqual(self.out("show", f"{saved}^3:local notes/draft.txt"), "untracked draft")
        self.assertEqual(
            (self.consumer / "cache/private.txt").read_text(), "ignored private data\n",
        )
        recovery = self.root / "recovery"
        self.git("worktree", "add", "--detach", str(recovery), self.base)
        self.git("stash", "apply", "--index", saved, root=recovery)
        self.assert_dirty_work(dirty, root=recovery)
        self.assertEqual((recovery / "shared.txt").read_text(), "uncommitted local change\n")
        self.assertEqual(self.stashes().splitlines(), stashes)

    def test_check_fetches_without_mutating_head_index_files_or_stashes(self):
        _, _, upstream = self.divergence()
        self.existing_stash()
        self.dirty_work()
        before = self.state()
        index_before = (self.consumer / ".git/index").read_bytes()
        self.assertEqual(self.out("rev-parse", "origin/main"), self.base)
        self.assertIs(self.update(check=True), False)
        self.assertEqual(self.state(), before)
        self.assertEqual((self.consumer / ".git/index").read_bytes(), index_before)
        self.assertEqual(self.out("rev-parse", "origin/main"), upstream)

    def test_detached_head_is_refused_without_touching_local_work(self):
        self.git("checkout", "--detach")
        self.existing_stash()
        self.dirty_work()
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update()
        self.assertEqual(self.state(), before)

    def test_missing_upstream_is_refused_without_touching_local_work(self):
        self.git("branch", "--unset-upstream")
        self.existing_stash()
        self.dirty_work()
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update()
        self.assertEqual(self.state(), before)

    def test_unfinished_nonconflicting_merge_is_neither_finished_nor_aborted(self):
        self.divergence()
        self.git("fetch", "origin")
        self.git("merge", "--no-commit", "--no-ff", "origin/main")
        self.assertEqual(self.out("ls-files", "--unmerged"), "")
        marker = self.consumer / ".git/MERGE_HEAD"
        merge_head = marker.read_bytes()
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update()
        self.assertEqual(self.state(), before)
        self.assertEqual(marker.read_bytes(), merge_head)

    def assert_preexisting_conflict_untouched(self, operation):
        self.existing_stash()
        self.commit("shared.txt", "local conflict\n", "local conflict")
        self.publish("shared.txt", "upstream conflict\n")
        self.git("fetch", "origin")
        result = self.git(operation, "origin/main", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(self.out("ls-files", "--unmerged"), "")
        self.dirty_work()
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update()
        self.assertEqual(self.state(), before)
        if operation == "merge":
            self.assertTrue((self.consumer / ".git/MERGE_HEAD").exists())
        else:
            self.assertTrue(any(
                (self.consumer / ".git" / name).exists()
                for name in ("rebase-merge", "rebase-apply")
            ))

    def test_preexisting_merge_conflicts_are_untouched(self):
        self.assert_preexisting_conflict_untouched("merge")

    def test_preexisting_rebase_conflicts_are_untouched(self):
        self.assert_preexisting_conflict_untouched("rebase")

    def test_fetch_failure_leaves_original_branch_index_work_and_stashes(self):
        self.commit("local.txt", "local commit\n", "local only")
        self.existing_stash()
        self.dirty_work()
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update()
        self.assertEqual(self.state(), before)
        self.assert_no_operation()

    def assert_ignored_collision_refused(self, upstream_path):
        self.write(upstream_path, "published replacement\n", root=self.seed)
        self.git("add", "--force", "--", upstream_path, root=self.seed)
        self.git("commit", "-m", "track formerly ignored path", root=self.seed)
        self.git("push", "origin", "main", root=self.seed)
        self.existing_stash()
        self.dirty_work()
        before = self.state()
        with self.assertRaises(UpdateError):
            self.update()
        self.assertEqual(self.state(), before)
        self.assert_no_operation()

    def test_ignored_file_collision_is_refused_without_overwriting_data(self):
        self.assert_ignored_collision_refused("cache/private.txt")

    def test_ignored_directory_collision_is_refused_without_removing_data(self):
        self.assert_ignored_collision_refused("cache")

    def test_checkout_subdirectory_is_not_accepted_as_the_root(self):
        directory = self.consumer / "nested"
        directory.mkdir()
        before = self.state()
        with self.assertRaises(UpdateError):
            update_checkout(directory)
        self.assertEqual(self.state(), before)

    def test_noncheckout_directory_is_refused_without_mutation(self):
        directory = self.root / "not-a-checkout"
        directory.mkdir()
        valuable = directory / "notes.txt"
        valuable.write_text("keep this\n", encoding="utf-8")
        with self.assertRaises(UpdateError):
            update_checkout(directory)
        self.assertEqual(valuable.read_text(), "keep this\n")
        self.assertEqual(set(directory.iterdir()), {valuable})

    def test_bare_repository_is_not_accepted_as_an_installation(self):
        before = self.out("show-ref", root=self.remote)
        with self.assertRaises(UpdateError):
            update_checkout(self.remote)
        self.assertEqual(self.out("show-ref", root=self.remote), before)
        self.assertEqual(self.out("rev-parse", "--is-bare-repository", root=self.remote), "true")


if __name__ == "__main__":
    unittest.main()
