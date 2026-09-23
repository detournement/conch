"""Gates for turn-level git checkpoints (conch/gitcheckpoint.py).

Covered: snapshot on mutation and skip on no-change, fingerprint
stability, restore round-trip (user branch/index untouched), gitignore
and secret-path exclusion (a restore can never materialize a skipped
secret), non-repo/config-off no-ops, ref pruning, refusal to restore
arbitrary (non-checkpoint) commits, and the unborn-HEAD repo case.
"""

import os
import subprocess
import tempfile
import unittest

from conch.gitcheckpoint import REF_PREFIX, GitCheckpoints


def _git(cwd, *args):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    )
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env,
    )
    return proc


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="conch-ckpt-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        _git(self.root, "init", "-q", "-b", "main")
        _write(self.root, "app.py", "print('v1')\n")
        _git(self.root, "add", "app.py")
        _git(self.root, "commit", "-q", "-m", "base")
        self.ckpt = GitCheckpoints({}, cwd=self.root)

    def _snapshot_files(self, sha):
        out = _git(self.root, "ls-tree", "-r", "--name-only", sha)
        return set(out.stdout.split())


class SnapshotTests(_RepoCase):
    def test_mutation_creates_checkpoint_ref(self):
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("edit app")
        self.assertIsNotNone(snap)
        self.assertTrue(snap["ref"].startswith(REF_PREFIX))
        entries = self.ckpt.list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "edit app")
        show = _git(self.root, "show", f"{snap['sha']}:app.py")
        self.assertIn("v2", show.stdout)

    def test_untracked_file_is_captured(self):
        _write(self.root, "newmod.py", "x = 1\n")
        snap = self.ckpt.snapshot("add module")
        self.assertIsNotNone(snap)
        self.assertIn("newmod.py", self._snapshot_files(snap["sha"]))

    def test_no_change_means_no_checkpoint(self):
        self.assertIsNone(self.ckpt.snapshot("nothing happened"))
        self.assertEqual(self.ckpt.list(), [])

    def test_same_state_twice_yields_one_checkpoint(self):
        _write(self.root, "app.py", "print('v2')\n")
        self.assertIsNotNone(self.ckpt.snapshot("first"))
        self.assertIsNone(self.ckpt.snapshot("dup"))
        self.assertEqual(len(self.ckpt.list()), 1)

    def test_fingerprint_tracks_worktree_state(self):
        before = self.ckpt.fingerprint()
        self.assertEqual(before, self.ckpt.fingerprint())
        _write(self.root, "app.py", "print('v2')\n")
        self.assertNotEqual(before, self.ckpt.fingerprint())

    def test_user_branch_and_index_untouched(self):
        head_before = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _write(self.root, "app.py", "print('v2')\n")
        self.ckpt.snapshot("edit")
        self.assertEqual(
            _git(self.root, "rev-parse", "HEAD").stdout.strip(), head_before
        )
        # The user's index has nothing staged.
        staged = _git(self.root, "diff", "--cached", "--name-only").stdout
        self.assertEqual(staged.strip(), "")

    def test_gitignored_file_excluded(self):
        _write(self.root, ".gitignore", "build/\n")
        _write(self.root, "build/artifact.bin", "binary junk")
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("with ignored")
        files = self._snapshot_files(snap["sha"])
        self.assertNotIn("build/artifact.bin", files)
        self.assertIn(".gitignore", files)

    def test_secret_paths_excluded_whole(self):
        canary = "AKIA-canary-9x7f-secret"
        _write(self.root, ".env", f"AWS_KEY={canary}\n")
        _write(self.root, "deploy/id_rsa", canary)
        _write(self.root, "certs/server.pem", canary)
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("with secrets present")
        files = self._snapshot_files(snap["sha"])
        self.assertIn("app.py", files)
        for secret in (".env", "deploy/id_rsa", "certs/server.pem"):
            self.assertNotIn(secret, files)
        # And the canary bytes are nowhere in the snapshot commit.
        show = _git(self.root, "show", "--stat", snap["sha"]).stdout
        self.assertNotIn(canary, show)

    def test_restore_never_materializes_skipped_secret(self):
        _write(self.root, ".env", "TOKEN=abc\n")
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("edit")
        os.remove(os.path.join(self.root, ".env"))
        ok, _ = self.ckpt.restore(snap["sha"])
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".env")))


class RestoreTests(_RepoCase):
    def test_restore_round_trip(self):
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("good state")
        _write(self.root, "app.py", "print('broken')\n")
        ok, msg = self.ckpt.restore(snap["sha"])
        self.assertTrue(ok, msg)
        with open(os.path.join(self.root, "app.py")) as fh:
            self.assertIn("v2", fh.read())

    def test_files_created_after_snapshot_survive_restore(self):
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("good state")
        _write(self.root, "later.py", "keep me\n")
        ok, _ = self.ckpt.restore(snap["sha"])
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(os.path.join(self.root, "later.py")))

    def test_restore_refuses_non_checkpoint_commits(self):
        head = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        ok, msg = self.ckpt.restore(head)
        self.assertFalse(ok)
        self.assertIn("not a conch checkpoint", msg)

    def test_diff_stat_names_changed_file(self):
        _write(self.root, "app.py", "print('v2')\n")
        snap = self.ckpt.snapshot("edit")
        self.assertIn("app.py", self.ckpt.diff_stat(snap["sha"]))


class GatingAndPruneTests(unittest.TestCase):
    def test_non_repo_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            ckpt = GitCheckpoints({}, cwd=tmp)
            self.assertFalse(ckpt.enabled())
            self.assertIsNone(ckpt.snapshot("anything"))

    def test_config_off_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            _git(tmp, "init", "-q")
            ckpt = GitCheckpoints({"git_checkpoints": "false"}, cwd=tmp)
            self.assertFalse(ckpt.enabled())
            self.assertIsNone(ckpt.snapshot("anything"))

    def test_prune_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            _git(root, "init", "-q", "-b", "main")
            _write(root, "f.txt", "0")
            _git(root, "add", "f.txt")
            _git(root, "commit", "-q", "-m", "base")
            ckpt = GitCheckpoints({"git_checkpoint_limit": "3"}, cwd=root)
            import time as _time
            for i in range(1, 6):
                _write(root, "f.txt", str(i))
                self.assertIsNotNone(ckpt.snapshot(f"edit {i}"))
                _time.sleep(0.002)  # distinct millisecond ref names
            entries = ckpt.list()
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[-1]["label"], "edit 5")

    def test_unborn_head_repo_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            _git(root, "init", "-q", "-b", "main")
            _write(root, "first.py", "hello\n")
            ckpt = GitCheckpoints({}, cwd=root)
            snap = ckpt.snapshot("very first")
            self.assertIsNotNone(snap)
            out = _git(root, "ls-tree", "-r", "--name-only", snap["sha"])
            self.assertIn("first.py", out.stdout)


if __name__ == "__main__":
    unittest.main()
