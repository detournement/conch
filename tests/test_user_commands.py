"""Tests for user-defined slash commands (plan 1.7): markdown files in
~/.config/conch/commands/ become /name commands with $ARGUMENTS templates."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from conch.commands import (
    handle_slash_command,
    load_user_commands,
    render_user_command,
)


class UserCommandsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commands_dir = Path(self._tmp.name) / "conch" / "commands"
        self.commands_dir.mkdir(parents=True)

    def _write(self, name, body):
        (self.commands_dir / f"{name}.md").write_text(body)


class TestLoadUserCommands(UserCommandsTestCase):
    def test_loads_markdown_files(self):
        self._write("review", "Review the following code:\n\n$ARGUMENTS")
        commands = load_user_commands()
        self.assertIn("review", commands)
        self.assertIn("$ARGUMENTS", commands["review"])

    def test_missing_dir_returns_empty(self):
        self.commands_dir.rmdir()
        self.assertEqual(load_user_commands(), {})

    def test_empty_and_invalid_names_skipped(self):
        self._write("empty", "   ")
        (self.commands_dir / "bad name!.md").write_text("body")
        (self.commands_dir / "notes.txt").write_text("not a command")
        commands = load_user_commands()
        self.assertEqual(commands, {})

    def test_names_lowercased(self):
        self._write("standup", "Summarize today's work")
        self.assertIn("standup", load_user_commands())


class TestRenderUserCommand(unittest.TestCase):
    def test_arguments_interpolated(self):
        self.assertEqual(
            render_user_command("Review: $ARGUMENTS", "main.py"),
            "Review: main.py",
        )

    def test_no_placeholder_appends_args(self):
        self.assertEqual(
            render_user_command("Do the thing", "with feeling"),
            "Do the thing\n\nwith feeling",
        )

    def test_no_placeholder_no_args(self):
        self.assertEqual(render_user_command("Do the thing", ""), "Do the thing")


class TestDispatch(UserCommandsTestCase):
    def _run(self, cmd):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handle_slash_command(
                cmd, {"provider": "ollama"}, "ollama", "qwen3", lambda v: None
            )
        return result

    def test_custom_command_returns_user_prompt(self):
        self._write("review", "Review the following:\n$ARGUMENTS")
        result = self._run("/review conch/app.py")
        self.assertEqual(result[0], "user_prompt")
        self.assertIn("conch/app.py", result[1])

    def test_unknown_command_still_none(self):
        self.assertIsNone(self._run("/definitely-not-a-command"))

    def test_builtin_takes_precedence(self):
        # A user file named clear.md must not shadow the builtin /clear
        self._write("clear", "custom clear prompt")
        result = self._run("/clear")
        self.assertEqual(result, "clear_conversation")


class TestUnknownCommandFeedback(UserCommandsTestCase):
    """An unrecognised slash command is reported, never silently dropped."""

    def _run(self, cmd):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handle_slash_command(
                cmd, {"provider": "ollama"}, "ollama", "qwen3", lambda v: None
            )
        return result, out.getvalue()

    def test_unknown_command_prints_message_with_help_hint(self):
        result, out = self._run("/frobnicate")
        self.assertIsNone(result)
        self.assertIn("Unknown command /frobnicate", out)
        self.assertIn("/help", out)

    def test_typo_gets_nearest_match_suggestion(self):
        _, out = self._run("/statsu")
        self.assertIn("did you mean", out)
        self.assertIn("/status", out)

    def test_user_command_typo_suggests_user_command(self):
        self._write("review", "Review:\n$ARGUMENTS")
        _, out = self._run("/reveiw")
        self.assertIn("/review", out)

    def test_compile_without_works_points_at_install(self):
        from conch import plugins

        with mock.patch.object(plugins, "slash_handler", return_value=None), \
                mock.patch.object(plugins, "load_builtin_plugins"):
            result, out = self._run("/compile make a thing")
        self.assertIsNone(result)
        self.assertIn("Works", out)
        self.assertIn("/install works", out)
        self.assertNotIn("Unknown command", out)

    def test_message_helper_shapes(self):
        from conch.commands import unknown_slash_command_message

        self.assertEqual(
            unknown_slash_command_message("/zzzz", ["/help", "/status"]),
            "Unknown command /zzzz — try /help",
        )
        text = unknown_slash_command_message("/hepl", ["/help", "/status"])
        self.assertIn("did you mean /help", text)
        self.assertIn("/install works", unknown_slash_command_message("/compile"))

    def test_known_commands_include_builtins_loop_and_user(self):
        from conch.commands import BUILTIN_SLASH_COMMANDS, known_slash_commands

        self._write("mine", "x")
        known = known_slash_commands()
        for name in BUILTIN_SLASH_COMMANDS:
            self.assertIn(name, known)
        self.assertIn("/q", known)
        self.assertIn("/mine", known)

    def test_builtin_list_matches_dispatcher_literals(self):
        """Drift guard: every literal handle_slash_command compares
        `command` against must be in BUILTIN_SLASH_COMMANDS and vice
        versa, so suggestions can never name a command that does not
        exist or miss one that does."""
        import ast
        import inspect

        from conch import commands as commands_mod
        from conch.commands import BUILTIN_SLASH_COMMANDS

        tree = ast.parse(inspect.getsource(commands_mod))
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "handle_slash_command"
        )
        literals = set()
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Name)
                    and node.left.id == "command"):
                continue
            for comp in node.comparators:
                elts = (
                    comp.elts if isinstance(comp, (ast.Tuple, ast.List, ast.Set))
                    else [comp]
                )
                for elt in elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str) \
                            and elt.value.startswith("/"):
                        literals.add(elt.value)
        self.assertEqual(literals, set(BUILTIN_SLASH_COMMANDS))


if __name__ == "__main__":
    unittest.main()
