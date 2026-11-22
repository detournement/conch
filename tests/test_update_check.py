"""Read-only update detection against the actual installation's source."""

import contextlib
import io
import json
import os
from email.message import Message
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

from conch import update


@unittest.skipUnless(shutil.which("git"), "Git is required")
class CheckoutCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conch-check-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        home = self.root / "home"
        home.mkdir()
        config = home / "gitconfig"
        config.write_text(
            "[init]\n\tdefaultBranch = release\n"
            "[commit]\n\tgpgSign = false\n"
            "[core]\n\thooksPath = /dev/null\n\tautocrlf = false\n"
            "[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n",
            encoding="utf-8",
        )
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR")
                       if key in os.environ}
        environment.update(
            HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(config), GIT_ATTR_NOSYSTEM="1", GIT_ALLOW_PROTOCOL="file",
            GIT_AUTHOR_NAME="Check Test", GIT_AUTHOR_EMAIL="check@example.invalid",
            GIT_COMMITTER_NAME="Check Test", GIT_COMMITTER_EMAIL="check@example.invalid",
            GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
        )
        env_patch = patch.dict(os.environ, environment, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.consumer = self.root / "consumer"
        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.seed))
        (self.seed / "conch").mkdir()
        (self.seed / "conch" / "__init__.py").write_text('__version__ = "1.0"\n')
        self.commit(self.seed, "tracked", "base")
        self.git(self.seed, "push", "-u", "origin", "release")
        self.git(self.root, "clone", str(self.remote), str(self.consumer))
        package_patch = patch.object(update.identity, "package_dir", return_value=self.consumer / "conch")
        package_patch.start()
        self.addCleanup(package_patch.stop)

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, check=True, timeout=15)
        return result.stdout.strip()

    def commit(self, root, name, content):
        (root / name).write_text(content)
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", content)

    def snapshot(self):
        return {str(path.relative_to(self.consumer)): path.read_bytes()
                for path in self.consumer.rglob("*") if path.is_file()}

    def test_equal_ahead_and_incoming_preserve_dirty_checkout(self):
        self.assertIsNone(update.check_for_update())
        self.commit(self.consumer, "local", "local ahead")
        self.assertIsNone(update.check_for_update())
        (self.consumer / "tracked").write_text("staged")
        self.git(self.consumer, "add", "tracked")
        (self.consumer / "tracked").write_text("unstaged")
        (self.consumer / "untracked").write_text("keep me")
        before = self.snapshot()
        self.assertIsNone(update.check_for_update())
        self.assertEqual(self.snapshot(), before)
        self.commit(self.seed, "incoming", "upstream incoming")
        self.git(self.seed, "push")
        tip = self.git(self.seed, "rev-parse", "HEAD")
        # A caller's repository environment must not redirect the check.
        with patch.dict(os.environ, {"GIT_DIR": str(self.seed / ".git")}):
            self.assertEqual(update.check_for_update(), "upstream " + tip[:12])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.consumer / ".git" / "FETCH_HEAD").exists())

    def test_installed_git_tracks_branch_not_tags_or_commit_pins(self):
        installed = self.git(self.consumer, "rev-parse", "HEAD")
        self.commit(self.seed, "incoming", "next source commit")
        self.git(self.seed, "push")
        tip = self.git(self.seed, "rev-parse", "HEAD")
        direct = {"url": self.remote.as_uri(), "vcs_info": {
            "vcs": "git", "commit_id": installed, "requested_revision": "release",
        }}
        self.assertEqual(update._installed_git_update(self.consumer, direct), "source " + tip[:12])
        direct["vcs_info"]["requested_revision"] = installed
        self.assertIsNone(update._installed_git_update(self.consumer, direct))
        self.git(self.seed, "tag", "release", installed)
        self.git(self.seed, "push", "origin", "refs/tags/release")
        direct["vcs_info"]["requested_revision"] = "release"
        self.assertIsNone(update._installed_git_update(self.consumer, direct))
        direct["vcs_info"]["requested_revision"] = "refs/heads/release"
        self.assertEqual(update._installed_git_update(self.consumer, direct), "source " + tip[:12])

    def test_git_timeout_is_short_noninteractive_and_quiet(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(update.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 3)) as run:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertIsNone(update.check_for_update())
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertEqual(run.call_args.kwargs["timeout"], update._CHECK_TIMEOUT)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")
        self.assertIn("StrictHostKeyChecking=yes", env["GIT_SSH_COMMAND"])


class ArchiveCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conch-archive-test-")
        self.addCleanup(temporary.cleanup)
        self.pkg = Path(temporary.name).resolve() / "site-packages" / "conch"
        self.pkg.mkdir(parents=True)
        self.direct = {"url": update._OFFICIAL_SOURCE, "archive_info": {}}
        meta = Message()
        meta["Project-URL"] = "Repository, " + update._OFFICIAL_REPOSITORY
        self.dist = SimpleNamespace(
            metadata=meta,
            read_text=lambda name: json.dumps(self.direct) if self.direct is not None else None,
            locate_file=lambda name: self.pkg,
        )
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(update.identity, "package_dir", return_value=self.pkg))
        self.record = self.stack.enter_context(patch.object(update.identity, "install_source_record", return_value={}))
        self.stack.enter_context(patch.object(update.metadata, "distribution", return_value=self.dist))
        self.stack.enter_context(patch.object(update, "__version__", "1.9.0"))
        self.opener = MagicMock()
        self.open_mock = self.stack.enter_context(patch.object(update, "build_opener", return_value=self.opener))
        self.response = self.opener.open.return_value.__enter__.return_value = Mock()
        self.response.read.return_value = b'__version__ = "1.10.0"\n'

    def test_numeric_version_ordering_and_no_downgrade(self):
        for available, expected in (("1.10.0", "1.10.0"), ("1.9", None), ("1.8.99", None),
                                    ("1.9.0rc1", None), ("1.9.0.dev1", None),
                                    ("1.9.0.post1", "1.9.0.post1"), ("garbage", None)):
            with self.subTest(available=available):
                self.response.read.return_value = ('__version__ = ' + repr(available)).encode()
                self.assertEqual(update.check_for_update(), expected)
        self.assertEqual(self.response.read.call_args.args, (update._CHECK_MAX_BYTES + 1,))
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], update._CHECK_TIMEOUT)
        self.assertIn("/edge/conch/__init__.py", self.opener.open.call_args.args[0].full_url)

    def test_source_precedence_pins_forks_and_distribution_mismatch(self):
        self.record.return_value = {"source": update._OFFICIAL_SOURCE, "commit": "a" * 40}
        for source in (update._OFFICIAL_REPOSITORY + "/archive/refs/tags/v1.0.tar.gz",
                       "https://github.com/other/conch/archive/refs/heads/edge.tar.gz"):
            self.direct["url"] = source
            self.assertIsNone(update.check_for_update())
        self.open_mock.assert_not_called()
        self.direct["url"] = update._OFFICIAL_SOURCE
        self.dist.locate_file = lambda name: self.pkg.parent / "different-conch"
        self.assertIsNone(update.check_for_update())
        self.open_mock.assert_not_called()

    def test_installer_record_and_official_fallback_use_their_branch(self):
        self.direct = {"url": (self.pkg.parent / "removed-installer-source").as_uri(), "dir_info": {}}
        self.record.return_value = {"source": update._OFFICIAL_SOURCE.replace("edge", "main"),
                                    "commit": "a" * 40}
        self.assertEqual(update.check_for_update(), "1.10.0")
        self.assertIn("/main/conch/__init__.py", self.opener.open.call_args.args[0].full_url)
        self.direct = None
        self.record.return_value = {}
        self.assertEqual(update.check_for_update(), "1.10.0")
        self.assertIn("/edge/conch/__init__.py", self.opener.open.call_args.args[0].full_url)
        # The global website record is not trustworthy commit provenance.
        self.record.return_value = {"source": update._OFFICIAL_SOURCE, "commit": "b" * 40}
        self.response.read.return_value = b'__version__ = "1.9.0"\n'
        self.assertIsNone(update.check_for_update())

    def test_offline_timeout_oversize_and_nonliteral_are_quiet(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        for error in (URLError("offline"), TimeoutError("timed out")):
            self.opener.open.side_effect = error
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertIsNone(update.check_for_update())
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.opener.open.side_effect = None
        for content in (b"x" * (update._CHECK_MAX_BYTES + 1),
                        b'__version__ = __import__("os").environ["VERSION"]',
                        b'__version__ = "1.10"\n__version__ = "1.8"', b"not python !"):
            self.response.read.return_value = content
            self.assertIsNone(update.check_for_update())


if __name__ == "__main__":
    unittest.main()
