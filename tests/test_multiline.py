"""Tests for one-message multiline input assembly (conch.multiline):
fence/backslash continuation, paste-drain stitching, /paste sentinel and
cancel paths, $EDITOR composition, history sanitation, and slash-command
safety for pasted blocks."""

import os
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

import readline

from conch import multiline
from conch.commands import slash_command_names


def make_input(items):
    """Scripted input_fn: returns strings in order, raises exception items."""
    seq = list(items)
    calls = []

    def fn(prompt):
        calls.append(prompt)
        if not seq:
            raise AssertionError("input_fn exhausted — reader asked for too many lines")
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    fn.calls = calls
    fn.remaining = seq
    return fn


def make_drain(chunks):
    """Scripted drain_fn: returns chunks in order, then ""."""
    seq = list(chunks)

    def fn():
        return seq.pop(0) if seq else ""

    return fn


class HelperTests(unittest.TestCase):
    def test_normalize_newlines(self):
        self.assertEqual(multiline.normalize_newlines("a\r\nb\rc\n"), "a\nb\nc\n")

    def test_fence_closed(self):
        self.assertFalse(multiline.fence_open_after("```py\ncode\n```"))

    def test_fence_open(self):
        self.assertTrue(multiline.fence_open_after("text\n```py\ncode"))

    def test_fence_indented_marker_counts(self):
        self.assertTrue(multiline.fence_open_after("  ```"))

    def test_fence_inline_backticks_ignored(self):
        self.assertFalse(multiline.fence_open_after("run a ``` b inline"))

    def test_fence_reopened(self):
        self.assertTrue(multiline.fence_open_after("```\na\n```\n```"))

    def test_is_slash_command_single_line(self):
        self.assertTrue(multiline.is_slash_command("/help"))
        self.assertTrue(multiline.is_slash_command("  /help"))

    def test_is_slash_command_rejects_plain_text(self):
        self.assertFalse(multiline.is_slash_command("hello"))
        self.assertFalse(multiline.is_slash_command(""))

    def test_multiline_never_a_slash_command(self):
        self.assertFalse(multiline.is_slash_command("/help\nsecond line"))

    def test_sanitize_history_entry(self):
        self.assertEqual(
            multiline.sanitize_history_entry("a\nb\r\nc"),
            "a" + multiline.HISTORY_NEWLINE_MARK + "b" + multiline.HISTORY_NEWLINE_MARK + "c",
        )

    def test_registry_lists_paste_and_edit(self):
        names = slash_command_names()
        self.assertIn("/paste", names)
        self.assertIn("/edit", names)


class ReadUserMessageTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(multiline, "_print_paste_hint", lambda n: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, inputs, drains=()):
        self.input_fn = make_input(inputs)
        return multiline.read_user_message(
            "> ",
            input_fn=self.input_fn,
            drain_fn=make_drain(drains),
            echo_fn=lambda lines: None,
            use_history=False,
        )

    def test_plain_single_line(self):
        self.assertEqual(self.read(["hello"]), "hello")

    def test_empty_line_passthrough(self):
        self.assertEqual(self.read([""]), "")

    def test_slash_command_returns_immediately(self):
        self.assertEqual(self.read(["/help"]), "/help")
        self.assertEqual(len(self.input_fn.calls), 1)

    def test_pasted_two_lines_yield_one_message(self):
        # Regression: a two-line paste used to fire as two separate inputs.
        # The drain recovers the second line; one Enter sends the block.
        message = self.read(["first line", ""], drains=["second line\n"])
        self.assertEqual(message, "first line\nsecond line")

    def test_paste_without_trailing_newline(self):
        self.assertEqual(self.read(["a", ""], drains=["b"]), "a\nb")

    def test_paste_interior_slash_lines_stay_literal(self):
        message = self.read(["a", ""], drains=["/help\n/quit\n"])
        self.assertEqual(message, "a\n/help\n/quit")
        self.assertFalse(multiline.is_slash_command(message))

    def test_paste_first_line_slash_becomes_chat(self):
        message = self.read(["/var/log/system.log:1: err", ""], drains=["next\n"])
        self.assertEqual(message, "/var/log/system.log:1: err\nnext")
        self.assertFalse(multiline.is_slash_command(message))

    def test_paste_then_append_before_send(self):
        message = self.read(["a", "c", ""], drains=["b\n"])
        self.assertEqual(message, "a\nb\nc")

    def test_chained_pastes_coalesce(self):
        message = self.read(["a", "c", ""], drains=["b\n", "d\n"])
        self.assertEqual(message, "a\nb\nc\nd")

    def test_ctrl_c_mid_block_cancels_without_partial(self):
        result = self.read(["a", KeyboardInterrupt()], drains=["b\n"])
        self.assertIsNone(result)

    def test_eof_mid_block_submits(self):
        self.assertEqual(self.read(["a", EOFError()], drains=["b\n"]), "a\nb")

    def test_trailing_blank_lines_stripped(self):
        self.assertEqual(self.read(["a", ""], drains=["b\n\n\n"]), "a\nb")

    def test_leading_blank_lines_stripped(self):
        self.assertEqual(self.read(["", ""], drains=["x\n"]), "x")

    def test_carriage_returns_normalized(self):
        self.assertEqual(self.read(["a", ""], drains=["b\r\nc\r"]), "a\nb\nc")

    def test_typed_fence_submits_on_close(self):
        message = self.read(["```py", "x = 1", "```"])
        self.assertEqual(message, "```py\nx = 1\n```")
        self.assertEqual(len(self.input_fn.calls), 3)

    def test_empty_lines_inside_fence_are_content(self):
        message = self.read(["```", "", "done", "```"])
        self.assertEqual(message, "```\n\ndone\n```")

    def test_pasted_open_fence_continues_until_closed(self):
        message = self.read(["x", "```", ""], drains=["```js\ncode\n"])
        self.assertEqual(message, "x\n```js\ncode\n```")

    def test_backslash_continuation(self):
        self.assertEqual(self.read(["one \\", "two"]), "one \ntwo")

    def test_backslash_chain(self):
        self.assertEqual(self.read(["a\\", "b\\", "c"]), "a\nb\nc")

    def test_backslash_cancel_returns_none(self):
        self.assertIsNone(self.read(["a\\", KeyboardInterrupt()]))

    def test_backslash_not_applied_to_pasted_tail(self):
        # The drained tail ends with a backslash: that's content, not a
        # continuation request. One Enter sends the block.
        message = self.read(["a", ""], drains=["b\\\n"])
        self.assertEqual(message, "a\nb\\")

    def test_bracketed_paste_result_submits_immediately(self):
        # GNU bracketed paste: input() returns embedded newlines and the
        # user already pressed Enter on the reviewed buffer.
        message = self.read(["a\nb"])
        self.assertEqual(message, "a\nb")
        self.assertEqual(len(self.input_fn.calls), 1)

    def test_bracketed_paste_trailing_backslash_not_continued(self):
        self.assertEqual(self.read(["a\nb\\"]), "a\nb\\")

    def test_bracketed_paste_open_fence_still_continues(self):
        self.assertEqual(self.read(["```\na", "```"]), "```\na\n```")

    def test_first_line_keyboard_interrupt_propagates(self):
        with self.assertRaises(KeyboardInterrupt):
            self.read([KeyboardInterrupt()])

    def test_first_line_eof_propagates(self):
        with self.assertRaises(EOFError):
            self.read([EOFError()])


class HistoryTests(unittest.TestCase):
    """record_history_entry against the real readline module (works on both
    GNU readline and libedit; indexing semantics verified on macOS libedit)."""

    def setUp(self):
        readline.clear_history()
        self.addCleanup(readline.clear_history)

    def _items(self):
        n = readline.get_current_history_length()
        return [readline.get_history_item(i) for i in range(1, n + 1)]

    def test_multiline_collapses_to_single_entry(self):
        readline.add_history("first")  # what input() auto-adds per line
        readline.add_history("second")
        multiline.record_history_entry(0, "first\nsecond")
        self.assertEqual(
            self._items(), ["first" + multiline.HISTORY_NEWLINE_MARK + "second"]
        )

    def test_single_line_auto_add_left_untouched(self):
        readline.add_history("hello")
        multiline.record_history_entry(0, "hello")
        self.assertEqual(self._items(), ["hello"])

    def test_block_with_no_auto_adds_is_recorded(self):
        # /paste and /edit bypass readline entirely; the block still becomes
        # one recallable entry.
        multiline.record_history_entry(0, "a\nb")
        self.assertEqual(self._items(), ["a" + multiline.HISTORY_NEWLINE_MARK + "b"])

    def test_duplicate_of_last_entry_not_added_again(self):
        multiline.record_history_entry(0, "a\nb")
        multiline.record_history_entry(1, "a\nb")
        self.assertEqual(len(self._items()), 1)

    def test_empty_message_only_scrubs(self):
        readline.add_history("keep")
        readline.add_history("fragment")
        multiline.record_history_entry(1, "")
        self.assertEqual(self._items(), ["keep"])

    def test_none_snapshot_is_a_no_op(self):
        readline.add_history("keep")
        multiline.record_history_entry(None, "x\ny")
        self.assertEqual(self._items(), ["keep"])


class ReadPasteBlockTests(unittest.TestCase):
    def block(self, items):
        return multiline.read_paste_block(make_input(items), use_history=False)

    def test_sentinel_ends_block(self):
        self.assertEqual(self.block(["a", "b", "."]), "a\nb")

    def test_sentinel_must_be_exact(self):
        # An indented "." is content (code often has lone dots); only a line
        # that is exactly "." ends the block.
        self.assertEqual(self.block(["  .", "."]), "  .")

    def test_eof_submits(self):
        self.assertEqual(self.block(["a", EOFError()]), "a")

    def test_ctrl_c_cancels(self):
        self.assertIsNone(self.block(["a", KeyboardInterrupt()]))

    def test_interior_commands_stay_literal(self):
        self.assertEqual(self.block(["/help", "exit", "."]), "/help\nexit")

    def test_empty_block(self):
        self.assertEqual(self.block(["."]), "")


class EditInEditorTests(unittest.TestCase):
    def _editor(self, body):
        """Create a tiny python 'editor' whose argv[1] is the temp file."""
        fd, path = tempfile.mkstemp(prefix="conch-test-editor-", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(os.unlink, path)
        return "%s %s" % (shlex.quote(sys.executable), shlex.quote(path))

    def test_saved_content_returned(self):
        editor = self._editor(
            "import sys\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write('edited\\ncontent\\n')\n"
        )
        result = multiline.edit_in_editor({"editor": editor}, use_history=False)
        self.assertEqual(result, "edited\ncontent")

    def test_empty_file_aborts(self):
        editor = self._editor("pass\n")
        self.assertIsNone(multiline.edit_in_editor({"editor": editor}, use_history=False))

    def test_nonzero_exit_aborts(self):
        editor = self._editor(
            "import sys\n"
            "with open(sys.argv[1], 'w') as fh:\n"
            "    fh.write('should not be sent')\n"
            "sys.exit(1)\n"
        )
        self.assertIsNone(multiline.edit_in_editor({"editor": editor}, use_history=False))

    def test_missing_editor_aborts(self):
        with patch("sys.stdout"):
            result = multiline.edit_in_editor(
                {"editor": "/nonexistent/conch-editor-xyz"}, use_history=False
            )
        self.assertIsNone(result)

    def test_editor_precedence(self):
        env = {"VISUAL": "visual-editor", "EDITOR": "editor-editor"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(multiline.resolve_editor({"editor": "cfg"}), "cfg")
            self.assertEqual(multiline.resolve_editor({}), "visual-editor")
        with patch.dict(os.environ, {"EDITOR": "editor-editor"}, clear=False):
            os.environ.pop("VISUAL", None)
            self.assertEqual(multiline.resolve_editor(None), "editor-editor")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VISUAL", None)
            os.environ.pop("EDITOR", None)
            self.assertEqual(multiline.resolve_editor(None), "vi")


if __name__ == "__main__":
    unittest.main()
