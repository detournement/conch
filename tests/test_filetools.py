"""read_file / edit_file / write_file: matching semantics, atomic
preservation of mode and line endings, the approval gate (decline/EOF
leave files untouched, yolo applies, guarded paths always ask,
non-interactive refuses), and bounded, redacted reads."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from unittest import mock

from conch.filetools import (
    EDIT_FILE_TOOL,
    FILE_TOOL_DEFS,
    FileToolsClient,
    READ_FILE_TOOL,
    sensitive_path_reason,
    system_path_reason,
)
from conch.runtime import parse_and_validate_tool_arguments
from conch.seats import _strict_parameters
from conch.tooling import LocalShellPolicy, PermissionState, inject_builtin_tools


def _never_called(prompt):
    raise AssertionError(f"unexpected prompt: {prompt!r}")


class _Answers:
    """input_fn feeding scripted answers; an exception instance is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        answer = self.answers.pop(0) if self.answers else ""
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FileToolsCase(unittest.TestCase):
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

    def client(self, mode="prompt_all", interactive=True, input_fn=None,
               auto=False):
        client = FileToolsClient(permissions=PermissionState(permission_mode=mode))
        client.set_policy(LocalShellPolicy(
            interactive=interactive, allow_auto_execute=auto,
            input_fn=input_fn or _never_called,
        ))
        client.set_cwd(self.dir)
        return client

    def path(self, name):
        return os.path.join(self.dir, name)

    def put(self, name, data, mode=None):
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode())
        if mode is not None:
            os.chmod(path, mode)
        return path

    def get(self, name):
        with open(self.path(name), "rb") as handle:
            return handle.read()

    def call(self, client, tool, **arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            result = client.call_tool(tool, arguments)
        return result["content"][0]["text"], bool(result.get("isError"))


class TestEditSemantics(FileToolsCase):
    def test_unique_match_is_replaced(self):
        self.put("a.py", "x = 1\ny = 2\n")
        text, error = self.call(self.client(mode="yolo"), "edit_file",
                                path="a.py", old_string="y = 2", new_string="y = 3\nz = 4")
        self.assertFalse(error, text)
        self.assertEqual(self.get("a.py"), b"x = 1\ny = 3\nz = 4\n")
        self.assertIn("a.py", text)
        self.assertIn("+2", text)
        self.assertIn("1", text)

    def test_zero_matches_names_file_and_read_file(self):
        self.put("a.py", "x = 1\n")
        text, error = self.call(self.client(mode="yolo"), "edit_file",
                                path="a.py", old_string="nope", new_string="y")
        self.assertTrue(error)
        self.assertIn("a.py", text)
        self.assertIn("read_file", text)
        self.assertEqual(self.get("a.py"), b"x = 1\n")

    def test_multiple_matches_need_replace_all(self):
        self.put("a.txt", "foo bar foo baz foo\n")
        text, error = self.call(self.client(mode="yolo"), "edit_file",
                                path="a.txt", old_string="foo", new_string="qux")
        self.assertTrue(error)
        self.assertIn("3", text)
        self.assertEqual(self.get("a.txt"), b"foo bar foo baz foo\n")
        text, error = self.call(self.client(mode="yolo"), "edit_file",
                                path="a.txt", old_string="foo", new_string="qux",
                                replace_all=True)
        self.assertFalse(error, text)
        self.assertEqual(self.get("a.txt"), b"qux bar qux baz qux\n")

    def test_identical_strings_rejected(self):
        self.put("a.txt", "same\n")
        _, error = self.call(self.client(mode="yolo"), "edit_file",
                             path="a.txt", old_string="same", new_string="same")
        self.assertTrue(error)

    def test_crlf_and_mode_preserved_atomically(self):
        self.put("run.sh", b"#!/bin/sh\r\necho one\r\necho two\r\n", mode=0o750)
        text, error = self.call(self.client(mode="yolo"), "edit_file",
                                path="run.sh", old_string="echo one\n",
                                new_string="echo uno\necho eins\n")
        self.assertFalse(error, text)
        self.assertEqual(
            self.get("run.sh"),
            b"#!/bin/sh\r\necho uno\r\necho eins\r\necho two\r\n",
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.path("run.sh")).st_mode), 0o750)
        self.assertEqual(sorted(os.listdir(self.dir)), ["run.sh"])

    def test_non_utf8_file_refused(self):
        self.put("latin.txt", "caf\xe9\n".encode("latin-1"))
        _, error = self.call(self.client(mode="yolo"), "edit_file",
                             path="latin.txt", old_string="caf", new_string="bar")
        self.assertTrue(error)
        self.assertEqual(self.get("latin.txt"), "caf\xe9\n".encode("latin-1"))


class TestApproval(FileToolsCase):
    def test_decline_leaves_file_untouched_and_returns_feedback(self):
        self.put("a.txt", "old\n")
        answers = _Answers("n", "use a constant")
        text, _ = self.call(self.client(input_fn=answers), "edit_file",
                            path="a.txt", old_string="old", new_string="new")
        self.assertEqual(self.get("a.txt"), b"old\n")
        self.assertIn("declined", text)
        self.assertIn("use a constant", text)

    def test_eof_and_ctrl_c_decline(self):
        self.put("a.txt", "old\n")
        for exc in (EOFError(), KeyboardInterrupt()):
            text, _ = self.call(self.client(input_fn=_Answers(exc)), "edit_file",
                                path="a.txt", old_string="old", new_string="new")
            self.assertEqual(self.get("a.txt"), b"old\n")
            self.assertIn("not", text)
        text, _ = self.call(self.client(input_fn=_Answers(EOFError())), "write_file",
                            path="new.txt", content="x\n")
        self.assertFalse(os.path.exists(self.path("new.txt")))

    def test_enter_applies(self):
        self.put("a.txt", "old\n")
        self.call(self.client(input_fn=_Answers("")), "edit_file",
                  path="a.txt", old_string="old", new_string="new")
        self.assertEqual(self.get("a.txt"), b"new\n")

    def test_safe_auto_still_prompts(self):
        self.put("a.txt", "old\n")
        answers = _Answers("n", "")
        self.call(self.client(mode="safe_auto", input_fn=answers), "edit_file",
                  path="a.txt", old_string="old", new_string="new")
        self.assertEqual(len(answers.prompts), 2)
        self.assertEqual(self.get("a.txt"), b"old\n")

    def test_session_always_skips_later_prompts(self):
        self.put("a.txt", "one two\n")
        client = self.client(input_fn=_Answers("a"))
        self.call(client, "edit_file", path="a.txt", old_string="one", new_string="1")
        client.policy.input_fn = _never_called
        self.call(client, "edit_file", path="a.txt", old_string="two", new_string="2")
        self.assertEqual(self.get("a.txt"), b"1 2\n")

    def test_capital_a_turns_on_agent_mode(self):
        self.put("a.txt", "old\n")
        client = self.client(input_fn=_Answers("A"))
        self.call(client, "edit_file", path="a.txt", old_string="old", new_string="new")
        self.assertTrue(client.permissions().get_agent_mode())
        self.assertEqual(self.get("a.txt"), b"new\n")

    def test_yolo_applies_without_prompt_but_sensitive_path_prompts(self):
        self.put("a.txt", "old\n")
        self.call(self.client(mode="yolo"), "edit_file",
                  path="a.txt", old_string="old", new_string="new")
        self.assertEqual(self.get("a.txt"), b"new\n")

        self.put(".env", "DEBUG=0\n")
        answers = _Answers("n", "")
        self.call(self.client(mode="yolo", auto=True, input_fn=answers), "edit_file",
                  path=".env", old_string="DEBUG=0", new_string="DEBUG=1")
        self.assertTrue(answers.prompts)
        self.assertEqual(self.get(".env"), b"DEBUG=0\n")

    def test_session_always_does_not_cover_guarded_paths(self):
        self.put(".env.local", "A=1\n")
        client = self.client(input_fn=_Answers("a"))
        self.call(client, "edit_file", path=".env.local", old_string="A=1", new_string="A=2")
        answers = _Answers("n", "")
        client.policy.input_fn = answers
        self.call(client, "edit_file", path=".env.local", old_string="A=2", new_string="A=3")
        self.assertTrue(answers.prompts)
        self.assertEqual(self.get(".env.local"), b"A=2\n")

    def test_non_interactive_refuses_writes(self):
        self.put("a.txt", "old\n")
        client = self.client(interactive=False)
        text, _ = self.call(client, "edit_file", path="a.txt",
                            old_string="old", new_string="new")
        self.assertEqual(self.get("a.txt"), b"old\n")
        self.assertIn("not changed", text)
        self.call(client, "write_file", path="b.txt", content="hi\n")
        self.assertFalse(os.path.exists(self.path("b.txt")))

    def test_non_interactive_yolo_still_refuses_guarded_path(self):
        self.put("server.pem", "cert\n")
        text, _ = self.call(self.client(mode="yolo", interactive=False), "write_file",
                            path="server.pem", content="other\n")
        self.assertIn("Refused", text)
        self.assertEqual(self.get("server.pem"), b"cert\n")

    def test_before_run_hook_fires_only_for_applied_changes(self):
        self.put("a.txt", "old\n")
        seen = []
        client = self.client(input_fn=_Answers("n", ""))
        client.set_before_run(seen.append)
        self.call(client, "edit_file", path="a.txt", old_string="old", new_string="new")
        self.assertEqual(seen, [])
        client.policy.input_fn = _Answers("y")
        self.call(client, "edit_file", path="a.txt", old_string="old", new_string="new")
        self.assertEqual(len(seen), 1)


class TestWriteFile(FileToolsCase):
    def test_creates_parents_and_reports_create(self):
        text, error = self.call(self.client(mode="yolo"), "write_file",
                                path="deep/er/new.txt", content="a\nb\n")
        self.assertFalse(error, text)
        self.assertEqual(self.get("deep/er/new.txt"), b"a\nb\n")
        self.assertIn("Created", text)

    def test_overwrite_keeps_mode(self):
        self.put("tool.sh", "old\n", mode=0o700)
        text, error = self.call(self.client(mode="yolo"), "write_file",
                                path="tool.sh", content="new\n")
        self.assertFalse(error, text)
        self.assertIn("Overwrote", text)
        self.assertEqual(self.get("tool.sh"), b"new\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path("tool.sh")).st_mode), 0o700)

    def test_absolute_path_and_directory_refused(self):
        os.mkdir(self.path("adir"))
        _, error = self.call(self.client(mode="yolo"), "write_file",
                             path=self.path("adir"), content="x")
        self.assertTrue(error)


class TestHostFileScope(FileToolsCase):
    def test_mcp_collision_does_not_confine_host_writes_to_working_directory(self):
        workspace = self.path("workspace")
        os.mkdir(workspace)
        self.put("original.html", "<p>original</p>\n")
        client = self.client(mode="yolo")
        client.set_cwd(workspace)

        class RestrictedMCP:
            def call_tool(self, name, arguments):
                raise AssertionError("host file tool was routed to restricted MCP")

        restricted = RestrictedMCP()
        tools = list(FILE_TOOL_DEFS)
        mapping = {tool["function"]["name"]: restricted for tool in tools}
        inject_builtin_tools(
            tools, mapping,
            {tool["function"]["name"]: client for tool in FILE_TOOL_DEFS},
        )
        with mock.patch.dict(os.environ, {"HOME": self.dir}):
            text, error = self.call(mapping["write_file"], "write_file",
                                    path="~/artifacts/revised.html", content="<p>revised</p>\n")
        self.assertFalse(error, text)
        self.assertEqual(self.get("artifacts/revised.html"), b"<p>revised</p>\n")
        text, error = self.call(mapping["edit_file"], "edit_file",
                                path=self.path("artifacts/revised.html"),
                                old_string="revised", new_string="finished")
        self.assertFalse(error, text)
        self.assertEqual(self.get("artifacts/revised.html"), b"<p>finished</p>\n")
        self.assertEqual(self.get("original.html"), b"<p>original</p>\n")
        text, error = self.call(mapping["read_file"], "read_file",
                                path="../artifacts/revised.html")
        self.assertFalse(error, text)
        self.assertIn("<p>finished</p>", text)


class TestReadFile(FileToolsCase):
    def test_offset_limit_numbering(self):
        self.put("ten.txt", "".join(f"line{n}\n" for n in range(1, 11)))
        text, error = self.call(self.client(), "read_file",
                                path="ten.txt", offset=3, limit=2)
        self.assertFalse(error, text)
        rows = [row for row in text.splitlines() if "\t" in row]
        self.assertEqual(rows, ["3\tline3", "4\tline4"])
        self.assertIn("offset=5", text)

    def test_whole_file_has_no_continuation(self):
        self.put("two.txt", "a\r\nb\r\n")
        text, _ = self.call(self.client(), "read_file", path="two.txt")
        self.assertEqual(text, "1\ta\n2\tb")

    def test_offset_past_end(self):
        self.put("one.txt", "a\n")
        text, _ = self.call(self.client(), "read_file", path="one.txt", offset=9)
        self.assertIn("past the end", text)

    def test_long_line_is_cut(self):
        self.put("wide.txt", "x" * 5000 + "\n")
        text, _ = self.call(self.client(), "read_file", path="wide.txt")
        self.assertLess(len(text), 2200)
        self.assertIn("3,000", text)

    def test_binary_refused(self):
        self.put("blob.bin", b"\x89PNG\x00\x01\x02secret")
        text, error = self.call(self.client(), "read_file", path="blob.bin")
        self.assertTrue(error)
        self.assertNotIn("secret", text)

    def test_directory_refused(self):
        os.mkdir(self.path("adir"))
        _, error = self.call(self.client(), "read_file", path="adir")
        self.assertTrue(error)

    def test_secrets_redacted(self):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        self.put("conf.txt", f"user = me\ngithub = {token}\n")
        text, _ = self.call(self.client(), "read_file", path="conf.txt")
        self.assertNotIn(token, text)
        self.assertIn("user = me", text)

    def test_output_capped_by_result_budget(self):
        self.put("big.txt", "".join(f"row {n} " + "y" * 60 + "\n" for n in range(1500)))
        client = self.client()
        client.set_result_budget(3000)
        text, _ = self.call(client, "read_file", path="big.txt")
        self.assertLessEqual(len(text), 3000)

    def test_sensitive_read_needs_confirmation(self):
        self.put(".env", "DEBUG=1\n")
        text, _ = self.call(self.client(mode="yolo", interactive=False),
                            "read_file", path=".env")
        self.assertIn("Refused", text)
        self.assertNotIn("DEBUG", text)
        answers = _Answers("n")
        text, _ = self.call(self.client(mode="yolo", input_fn=answers),
                            "read_file", path=".env")
        self.assertTrue(answers.prompts)
        self.assertNotIn("DEBUG", text)
        text, _ = self.call(self.client(input_fn=_Answers("y")),
                            "read_file", path=".env")
        self.assertIn("DEBUG=1", text)

    def test_relative_path_follows_set_cwd(self):
        sub = os.path.join(self.dir, "sub")
        os.mkdir(sub)
        self.put("sub/here.txt", "inside\n")
        client = self.client()
        client.set_cwd(sub)
        text, _ = self.call(client, "read_file", path="here.txt")
        self.assertIn("inside", text)


class TestGuardsAndSchemas(FileToolsCase):
    def test_guarded_paths(self):
        home = os.path.expanduser("~")
        self.assertTrue(sensitive_path_reason(os.path.join(home, ".ssh", "config")))
        self.assertTrue(sensitive_path_reason(self.path("id_ed25519")))
        self.assertTrue(sensitive_path_reason(self.path("server.key")))
        self.assertIsNone(sensitive_path_reason(self.path("notes.md")))
        self.assertTrue(system_path_reason("/etc/hosts"))
        self.assertIsNone(system_path_reason(self.path("notes.md")))

    def test_symlink_to_sensitive_file_is_guarded(self):
        target = self.put("secret.pem", "k\n")
        os.symlink(target, self.path("innocent.txt"))
        self.assertTrue(sensitive_path_reason(self.path("innocent.txt")))

    def test_schemas_fit_strict_seats_and_validate(self):
        for tool in FILE_TOOL_DEFS:
            self.assertIsNotNone(_strict_parameters(tool["function"]["parameters"]))
        args, error = parse_and_validate_tool_arguments(
            {"function": {"arguments": '{"path": "a", "old_string": "x", '
                                       '"new_string": "y", "replace_all": true}'}},
            EDIT_FILE_TOOL,
        )
        self.assertIsNone(error)
        _, error = parse_and_validate_tool_arguments(
            {"function": {"arguments": '{"path": "a", "offset": "3"}'}}, READ_FILE_TOOL,
        )
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
