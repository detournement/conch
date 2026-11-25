"""Coding-agent file tools: read-before-edit / staleness guard, the
grep and glob search tools (ripgrep and the pure-Python fallback), and
the edit -> diagnostics loop (post_edit_check and post_tool_use hook
output reaching the model)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from conch import filetools
from conch.filetools import (
    FILE_TOOL_NAMES,
    GLOB_TOOL,
    GREP_TOOL,
    FileToolsClient,
    _glob_source,
    post_edit_check_command,
    run_post_edit_check,
)
from conch.runtime import _after_tool_use, chat_turn
from conch.seats import _strict_parameters
from conch.tooling import (
    PINNED_TOOL_NAMES,
    LocalShellPolicy,
    PermissionState,
    inject_builtin_tools,
)


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self._tmp.name)
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": os.path.join(self.dir, "state"),
            "XDG_CONFIG_HOME": os.path.join(self.dir, "config"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def client(self, config=None, cwd=None):
        client = FileToolsClient(
            permissions=PermissionState(permission_mode="yolo"),
            config={} if config is None else config,
        )
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        client.set_cwd(cwd or self.dir)
        return client

    def path(self, name):
        return os.path.join(self.dir, name)

    def put(self, name, data):
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode())
        return path

    def get(self, name):
        with open(self.path(name), "rb") as handle:
            return handle.read()

    def call(self, client, tool, **arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            result = client.call_tool(tool, arguments)
        return result["content"][0]["text"], bool(result.get("isError"))


class TestReadBeforeEdit(_Case):
    def test_edit_of_unread_file_is_refused(self):
        self.put("a.py", "x = 1\n")
        text, error = self.call(self.client(), "edit_file", path="a.py",
                                old_string="1", new_string="2")
        self.assertTrue(error)
        self.assertIn("read_file it first", text)
        self.assertEqual(self.get("a.py"), b"x = 1\n")

    def test_read_then_edit_and_follow_up_edits(self):
        self.put("a.py", "x = 1\ny = 2\n")
        client = self.client()
        self.call(client, "read_file", path="a.py")
        text, error = self.call(client, "edit_file", path="a.py",
                                old_string="x = 1", new_string="x = 10")
        self.assertFalse(error, text)
        # Its own write is remembered: a second edit needs no re-read.
        text, error = self.call(client, "edit_file", path="a.py",
                                old_string="y = 2", new_string="y = 20")
        self.assertFalse(error, text)
        self.assertEqual(self.get("a.py"), b"x = 10\ny = 20\n")

    def test_partial_read_counts(self):
        self.put("a.txt", "".join(f"line {i}\n" for i in range(50)))
        client = self.client()
        self.call(client, "read_file", path="a.txt", offset=40, limit=2)
        _, error = self.call(client, "edit_file", path="a.txt",
                             old_string="line 3\n", new_string="line three\n")
        self.assertFalse(error)

    def test_external_change_is_stale_until_read_again(self):
        self.put("a.txt", "one\n")
        client = self.client()
        self.call(client, "read_file", path="a.txt")
        self.put("a.txt", "one\ntwo\n")
        text, error = self.call(client, "edit_file", path="a.txt",
                                old_string="one", new_string="1")
        self.assertTrue(error)
        self.assertIn("changed on disk since you last read it", text)
        self.assertEqual(self.get("a.txt"), b"one\ntwo\n")
        self.call(client, "read_file", path="a.txt")
        _, error = self.call(client, "edit_file", path="a.txt",
                             old_string="one", new_string="1")
        self.assertFalse(error)

    def test_touch_without_content_change_is_not_stale(self):
        path = self.put("a.txt", "one\n")
        client = self.client()
        self.call(client, "read_file", path="a.txt")
        later = time.time() + 5
        os.utime(path, (later, later))
        _, error = self.call(client, "edit_file", path="a.txt",
                             old_string="one", new_string="1")
        self.assertFalse(error)

    def test_write_file_creates_freely_but_overwrite_needs_read(self):
        client = self.client()
        text, error = self.call(client, "write_file", path="new/b.txt", content="hi\n")
        self.assertFalse(error, text)
        # Created by this session: replacing it needs no read.
        _, error = self.call(client, "write_file", path="new/b.txt", content="ho\n")
        self.assertFalse(error)
        self.put("c.txt", "old\n")
        text, error = self.call(client, "write_file", path="c.txt", content="new\n")
        self.assertTrue(error)
        self.assertIn("read_file it first", text)
        self.assertEqual(self.get("c.txt"), b"old\n")
        self.call(client, "read_file", path="c.txt")
        _, error = self.call(client, "write_file", path="c.txt", content="new\n")
        self.assertFalse(error)

    def test_binary_file_can_be_replaced_after_a_read_attempt(self):
        self.put("blob.bin", b"\x00\x01\x02")
        client = self.client()
        _, error = self.call(client, "write_file", path="blob.bin", content="text\n")
        self.assertTrue(error)
        _, error = self.call(client, "read_file", path="blob.bin")
        self.assertTrue(error)  # binary is refused, but now known
        _, error = self.call(client, "write_file", path="blob.bin", content="text\n")
        self.assertFalse(error)

    def test_symlinked_path_shares_the_record(self):
        self.put("real.txt", "v1\n")
        os.symlink(self.path("real.txt"), self.path("link.txt"))
        client = self.client()
        self.call(client, "read_file", path="link.txt")
        _, error = self.call(client, "edit_file", path="real.txt",
                             old_string="v1", new_string="v2")
        self.assertFalse(error)

    def test_huge_files_are_tracked_by_mtime_and_size(self):
        path = self.put("big.log", "start\n" + "x" * 100 + "\n")
        client = self.client()
        with mock.patch.object(filetools, "_HASH_MAX_BYTES", 10):
            self.call(client, "read_file", path="big.log", limit=1)
            self.assertEqual(client._seen[path][2], "")
            _, error = self.call(client, "edit_file", path="big.log",
                                 old_string="start", new_string="begin")
            self.assertFalse(error)
            self.call(client, "read_file", path="big.log", limit=1)
            self.put("big.log", "begin\n" + "y" * 200 + "\n")
            _, error = self.call(client, "edit_file", path="big.log",
                                 old_string="begin", new_string="start")
            self.assertTrue(error)

    def test_opt_out(self):
        self.put("a.txt", "one\n")
        client = self.client(config={"require_read_before_edit": "false"})
        _, error = self.call(client, "edit_file", path="a.txt",
                             old_string="one", new_string="1")
        self.assertFalse(error)
        # The config is read at call time.
        config = {}
        client = self.client(config=config)
        _, error = self.call(client, "edit_file", path="a.txt",
                             old_string="1", new_string="2")
        self.assertTrue(error)
        config["require_read_before_edit"] = "off"
        _, error = self.call(client, "edit_file", path="a.txt",
                             old_string="1", new_string="2")
        self.assertFalse(error)

    def test_fork_has_its_own_record_and_parent_sees_its_changes(self):
        self.put("a.txt", "one\n")
        parent = self.client()
        self.call(parent, "read_file", path="a.txt")
        child = parent.fork()
        self.assertIs(child.policy, parent.policy)
        self.assertEqual(child.cwd, parent.cwd)
        _, error = self.call(child, "edit_file", path="a.txt",
                             old_string="one", new_string="1")
        self.assertTrue(error, "a subagent must read before editing")
        self.call(child, "read_file", path="a.txt")
        _, error = self.call(child, "edit_file", path="a.txt",
                             old_string="one", new_string="1")
        self.assertFalse(error)
        text, error = self.call(parent, "edit_file", path="a.txt",
                                old_string="1", new_string="2")
        self.assertTrue(error)
        self.assertIn("changed on disk", text)

    def test_delegate_task_forks_file_tools_once(self):
        from conch.tooling import DelegateTaskClient

        parent = self.client()
        clients = {name: parent for name in FILE_TOOL_NAMES}
        delegate = DelegateTaskClient()
        delegate.bind({"provider": "openai", "model": "m"}, mock.Mock(tools=[]), clients)
        captured = {}

        def fake_attach(session_self, builtin_clients, *args, **kwargs):
            captured.update(builtin_clients)
            raise RuntimeError("stop here")

        with mock.patch("conch.session.AgentSession.attach_clients", fake_attach), \
                mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(RuntimeError):
                delegate._run("task", "")
        forks = {id(captured[name]) for name in FILE_TOOL_NAMES}
        self.assertEqual(len(forks), 1)
        self.assertIsNot(captured["read_file"], parent)

    def test_successful_change_reports_changed_path(self):
        client = self.client()
        with contextlib.redirect_stdout(io.StringIO()):
            result = client.call_tool("write_file", {"path": "x.txt", "content": "a\n"})
            self.assertEqual(result["changed_path"], self.path("x.txt"))
            result = client.call_tool("edit_file", {"path": "x.txt",
                                                    "old_string": "a", "new_string": "b"})
            self.assertEqual(result["changed_path"], self.path("x.txt"))
            result = client.call_tool("edit_file", {"path": "x.txt",
                                                    "old_string": "zzz", "new_string": "b"})
            self.assertNotIn("changed_path", result)


class _SearchCase(_Case):
    use_rg = False

    def setUp(self):
        super().setUp()
        if not self.use_rg:
            which = mock.patch.object(filetools.shutil, "which", return_value=None)
            which.start()
            self.addCleanup(which.stop)

    def git_repo(self):
        try:
            subprocess.run(["git", "init", "-q", self.dir], check=True,
                           capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git unavailable")

    def grep(self, client=None, **arguments):
        return self.call(client or self.client(), "grep", **arguments)


class TestGrepFallback(_SearchCase):
    def setUp(self):
        super().setUp()
        self.put("src/app.py", "import os\n\ndef main():\n    return os.getcwd()\n")
        self.put("src/util.py", "def helper():\n    return 'Main'\n")
        self.put("README.md", "main entry point\n")

    def test_files_with_matches_is_default_and_sorted(self):
        text, error = self.grep(pattern="main")
        self.assertFalse(error, text)
        self.assertEqual(text.splitlines(), ["README.md", "src/app.py"])

    def test_case_insensitive_and_glob(self):
        text, _ = self.grep(pattern="main", case_insensitive=True, glob="*.py")
        self.assertEqual(text.splitlines(), ["src/app.py", "src/util.py"])
        text, _ = self.grep(pattern="main", glob="src/**/*.{py,md}")
        self.assertEqual(text.splitlines(), ["src/app.py"])
        text, _ = self.grep(pattern="main", glob="!*.py")
        self.assertEqual(text.splitlines(), ["README.md"])

    def test_content_with_context_and_count(self):
        text, _ = self.grep(pattern="def main", output_mode="content", context=1,
                            path="src")
        self.assertEqual(text.splitlines(), [
            "src/app.py-2-", "src/app.py:3:def main():", "src/app.py-4-    return os.getcwd()",
        ])
        text, _ = self.grep(pattern="os", output_mode="count")
        self.assertEqual(text.splitlines(), ["src/app.py:2"])

    def test_single_file_path(self):
        text, _ = self.grep(pattern="helper", path="src/util.py", output_mode="content")
        self.assertEqual(text, "src/util.py:1:def helper():")

    def test_no_match_and_errors(self):
        text, error = self.grep(pattern="nothing-here")
        self.assertFalse(error)
        self.assertIn("No matches", text)
        _, error = self.grep(pattern="(")
        self.assertTrue(error)
        _, error = self.grep(pattern="x", path="missing")
        self.assertTrue(error)
        _, error = self.grep(pattern="x", output_mode="lines")
        self.assertTrue(error)

    def test_head_limit_reports_truncation(self):
        self.put("many.txt", "".join("hit\n" for _ in range(30)))
        text, _ = self.grep(pattern="hit", output_mode="content", head_limit=5)
        lines = text.splitlines()
        self.assertEqual(len([line for line in lines if ":hit" in line]), 5)
        self.assertIn("showing the first 5 lines", text)

    def test_binary_skipped_and_long_lines_cut(self):
        self.put("blob.bin", b"main\x00\x01")
        self.put("wide.txt", "main" + "x" * 5000 + "\n")
        text, _ = self.grep(pattern="main", output_mode="content")
        self.assertNotIn("blob.bin", text)
        wide = [line for line in text.splitlines() if line.startswith("wide.txt")][0]
        self.assertLess(len(wide), 700)
        self.assertIn("chars]", wide)

    def test_credentials_redacted_and_credential_files_withheld(self):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        self.put("config.py", f"TOKEN = '{token}'\n")
        self.put(".env", "TOKEN=main-secret\n")
        text, _ = self.grep(pattern="TOKEN", output_mode="content")
        self.assertNotIn(token, text)
        self.assertIn("redacted", text)
        self.assertNotIn("main-secret", text)
        self.assertIn("withheld", text)
        self.assertIn(".env", text)

    def test_gitignore_respected_in_repo(self):
        self.git_repo()
        self.put(".gitignore", "build/\n")
        self.put("build/out.py", "def main(): pass\n")
        text, _ = self.grep(pattern="main")
        self.assertNotIn("build/out.py", text)
        self.assertIn("src/app.py", text)
        # An explicitly named ignored directory is still searched.
        text, _ = self.grep(pattern="main", path="build")
        self.assertIn("build/out.py", text)

    def test_walk_skips_vcs_and_dependency_dirs_outside_git(self):
        if self.use_rg:
            self.skipTest("fallback walker only (ripgrep has its own rules)")
        self.put("node_modules/lib/index.js", "main\n")
        with mock.patch.object(filetools, "_git_files", return_value=None):
            text, _ = self.grep(pattern="main")
        self.assertNotIn("node_modules", text)


@unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
class TestGrepRipgrep(TestGrepFallback):
    use_rg = True


class TestGlob(_SearchCase):
    def test_recursive_and_top_level_patterns_newest_first(self):
        old = self.put("a/old.py", "")
        new = self.put("a/b/new.py", "")
        self.put("top.py", "")
        self.put("notes.md", "")
        now = time.time()
        os.utime(old, (now - 100, now - 100))
        os.utime(new, (now + 100, now + 100))
        text, error = self.call(self.client(), "glob", pattern="**/*.py")
        self.assertFalse(error, text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "a/b/new.py")
        self.assertEqual(lines[-1], "a/old.py")
        self.assertEqual(set(lines), {"a/old.py", "a/b/new.py", "top.py"})
        text, _ = self.call(self.client(), "glob", pattern="*.py")
        self.assertEqual(text.splitlines(), ["top.py"])
        text, _ = self.call(self.client(), "glob", pattern="*.{md,py}")
        self.assertEqual(set(text.splitlines()), {"top.py", "notes.md"})
        text, _ = self.call(self.client(), "glob", pattern="*.py", path="a")
        self.assertEqual(text.splitlines(), ["a/old.py"])
        text, _ = self.call(self.client(), "glob", pattern=self.path("a") + "/**/*.py")
        self.assertEqual(set(text.splitlines()), {"a/old.py", "a/b/new.py"})

    def test_head_limit_and_no_match(self):
        for i in range(5):
            self.put(f"f{i}.txt", "")
        text, _ = self.call(self.client(), "glob", pattern="*.txt", head_limit=2)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("5 files matched", text)
        text, error = self.call(self.client(), "glob", pattern="*.nope")
        self.assertFalse(error)
        self.assertIn("No files match", text)
        _, error = self.call(self.client(), "glob", pattern="*.py", path="missing")
        self.assertTrue(error)

    def test_gitignore_respected(self):
        self.git_repo()
        self.put(".gitignore", "*.log\n")
        self.put("keep.txt", "")
        self.put("drop.log", "")
        text, _ = self.call(self.client(), "glob", pattern="*")
        self.assertIn("keep.txt", text)
        self.assertNotIn("drop.log", text)

    def test_glob_source(self):
        import re

        def match(pattern, path):
            return bool(re.fullmatch(_glob_source(pattern), path))

        self.assertTrue(match("**/*.py", "a.py"))
        self.assertTrue(match("**/*.py", "x/y/a.py"))
        self.assertFalse(match("*.py", "x/a.py"))
        self.assertTrue(match("src/**", "src/a/b"))
        self.assertTrue(match("test_?.[ch]", "test_1.c"))
        self.assertFalse(match("[!a]*", "abc"))
        self.assertTrue(match("*.{ts,tsx}", "a.tsx"))


class TestToolRegistration(unittest.TestCase):
    def test_search_tools_registered_and_pinned(self):
        self.assertIn("grep", FILE_TOOL_NAMES)
        self.assertIn("glob", FILE_TOOL_NAMES)
        self.assertTrue({"grep", "glob"} <= PINNED_TOOL_NAMES)
        client = FileToolsClient()
        tools, tool_map = [], {}
        inject_builtin_tools(tools, tool_map, {name: client for name in FILE_TOOL_NAMES})
        names = {tool["function"]["name"] for tool in tools}
        self.assertTrue({"grep", "glob", "read_file"} <= names)
        for tool in (GREP_TOOL, GLOB_TOOL):
            self.assertIsNotNone(_strict_parameters(tool["function"]["parameters"]))

    def test_bootstrap_shares_one_client_with_config(self):
        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore

        config = {"provider": "openai", "require_read_before_edit": "false"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp, "XDG_CONFIG_HOME": tmp}):
            clients = make_builtin_clients(MemoryStore(), config, interactive=False)
        self.assertIs(clients["grep"], clients["read_file"])
        self.assertIs(clients["glob"], clients["edit_file"])
        self.assertFalse(clients["edit_file"]._require_read())

    def test_remote_turns_get_one_noninteractive_variant(self):
        from conch.remote import REMOTE_EXCLUDED_TOOLS, RemoteLoop

        client = FileToolsClient()
        pool = {name: client for name in FILE_TOOL_NAMES}
        loop = RemoteLoop.__new__(RemoteLoop)
        loop.approvals = mock.Mock()
        clients = RemoteLoop._remote_clients(loop, pool, "chan", "t", "s")
        self.assertNotIn("edit_file", clients)
        self.assertIn("edit_file", REMOTE_EXCLUDED_TOOLS)
        self.assertIs(clients["grep"], clients["read_file"])
        self.assertIs(clients["glob"], clients["read_file"])
        self.assertIsNot(clients["grep"], client)
        self.assertFalse(clients["grep"].policy.interactive)


class TestPostEditCheck(_Case):
    def test_command_selection(self):
        config = {
            "post_edit_check": "echo any {path}",
            "post_edit_check.py": "ruff check {path}",
            "post_edit_check.md": "",
            "post_edit_check.txt": "off",
        }
        self.assertEqual(post_edit_check_command(config, "/x/a.py"), "ruff check {path}")
        self.assertEqual(post_edit_check_command(config, "/x/a.PY"), "ruff check {path}")
        self.assertEqual(post_edit_check_command(config, "/x/a.md"), "")
        self.assertEqual(post_edit_check_command(config, "/x/a.txt"), "")
        self.assertEqual(post_edit_check_command(config, "/x/a.go"), "echo any {path}")
        self.assertEqual(post_edit_check_command(config, "/x/Makefile"), "echo any {path}")
        self.assertEqual(post_edit_check_command({}, "/x/a.py"), "")
        self.assertEqual(post_edit_check_command(None, "/x/a.py"), "")

    def test_runs_in_cwd_with_quoted_relative_path(self):
        path = self.put("dir with space/a b.py", "x\n")
        out = run_post_edit_check(
            {"post_edit_check": "pwd; printf '<%s>\\n' {path}; exit 3"}, path, self.dir,
        )
        self.assertIn("exited 3", out)
        self.assertIn(f"\n{self.dir}\n", out)
        self.assertIn("<dir with space/a b.py>", out)

    def test_injection_in_path_is_quoted(self):
        path = self.put("x;touch pwned.py", "x\n")
        run_post_edit_check({"post_edit_check": "cat {path} > /dev/null"}, path, self.dir)
        self.assertFalse(os.path.exists(self.path("pwned.py")))

    def test_pass_timeout_and_cap(self):
        path = self.put("a.py", "x\n")
        out = run_post_edit_check({"post_edit_check": "true"}, path, self.dir)
        self.assertTrue(out.endswith("passed (exit 0)]"), out)
        start = time.monotonic()
        out = run_post_edit_check(
            {"post_edit_check": "sleep 30", "post_edit_check_timeout": "1"}, path, self.dir,
        )
        self.assertLess(time.monotonic() - start, 15)
        self.assertIn("timed out after 1s", out)
        out = run_post_edit_check(
            {"post_edit_check": "yes error | head -c 20000"}, path, self.dir,
        )
        self.assertLess(len(out), 4300)
        self.assertIn("output truncated", out)
        self.assertEqual(run_post_edit_check({}, path, self.dir), "")


class TestAfterToolUse(_Case):
    def test_hook_output_reaches_the_model_labelled_and_capped(self):
        text = _after_tool_use("read_file", {"path": "a"}, "ok",
                               {"hook_post_tool_use": "echo lint: 2 problems"})
        self.assertEqual(text, "ok\n\n[post_tool_use hook output]\nlint: 2 problems")
        text = _after_tool_use("read_file", {}, "ok",
                               {"hook_post_tool_use": "echo bad >&2; exit 1"})
        self.assertIn("[post_tool_use hook output (hook exited non-zero)]\nbad", text)
        text = _after_tool_use("read_file", {}, "ok",
                               {"hook_post_tool_use": "head -c 9000 /dev/zero | tr '\\0' x"})
        self.assertLess(len(text), 4300)
        self.assertEqual(_after_tool_use("read_file", {}, "ok", {}), "ok")
        self.assertEqual(_after_tool_use("read_file", {}, "ok", None), "ok")

    def test_check_runs_only_for_changed_path_and_precedes_hook(self):
        path = self.put("a.py", "x\n")
        capture = self.path("hook.json")
        config = {"post_edit_check": "echo checked {path}",
                  "hook_post_tool_use": f"cat > '{capture}'"}
        text = _after_tool_use("edit_file", {}, "Edited a.py", config,
                               changed_path=path, cwd=self.dir)
        self.assertIn("[post_edit_check `echo checked a.py` passed (exit 0)]\nchecked a.py", text)
        with open(capture) as handle:
            self.assertIn("checked a.py", json.load(handle)["result"])
        text = _after_tool_use("edit_file", {}, "Error: x", config,
                               changed_path=None, cwd=self.dir)
        self.assertNotIn("post_edit_check", text)

    def test_chat_turn_appends_diagnostics_to_edit_result(self):
        self.put("a.py", "x = 1\n")
        client = self.client(config={"require_read_before_edit": "false"})
        client.policy = LocalShellPolicy(interactive=False, allow_auto_execute=True)
        config = {"post_edit_check.py": "echo 'a.py:1: E999 boom'; exit 1"}
        tools, tool_map = [], {}
        clients = {name: client for name in FILE_TOOL_NAMES}
        inject_builtin_tools(tools, tool_map, clients)
        responses = [
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "edit_file", "arguments": json.dumps(
                    {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})},
            }], "_usage": {"input_tokens": 1, "output_tokens": 1}, "_model": "t"},
            {"content": "done", "tool_calls": None,
             "_usage": {"input_tokens": 1, "output_tokens": 1}, "_model": "t"},
        ]
        messages = [{"role": "user", "content": "go"}]
        with mock.patch("sys.stderr", io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch("conch.tooling.discover_user_tools",
                           return_value=([], mock.Mock())):
            chat_turn(config=config, provider="openai",
                      raw_fn=lambda cfg, msgs, tl: responses.pop(0),
                      messages=messages, tools=tools, tool_map={},
                      builtin_clients=clients, max_tool_rounds=3)
        result = [m for m in messages if m.get("role") == "tool"][0]["content"]
        self.assertIn("Edited a.py", result)
        self.assertIn("exited 1", result)
        self.assertIn("E999 boom", result)


if __name__ == "__main__":
    unittest.main()
