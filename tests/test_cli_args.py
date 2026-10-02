"""Argument parsing for the `conch` and `conch-ask` entrypoints (review
finding F11): --help/--version, --new, --non-interactive, unknown leading
flags rejected with a usage error, `--` for prompts that start with a
dash, and prompt words never mistaken for options."""

import contextlib
import io
import unittest
from unittest import mock

import conch
from conch import cli
from conch.app import _USAGE, main, parse_argv, split_leading_options


class _FakeStdin(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def _model_check_off(config, **_kwargs):
    """Stand-in for the startup model check (tested in test_modelcheck)."""
    from conch.modelcheck import ModelCheckOutcome

    return ModelCheckOutcome(
        config.get("provider", ""), config.get("chat_model", ""), None,
        checked=False,
    )


class TestSplitLeadingOptions(unittest.TestCase):
    def test_only_leading_dash_tokens_are_options(self):
        self.assertEqual(
            split_leading_options(["-n", "--non-interactive", "hello", "-x"]),
            (["-n", "--non-interactive"], ["hello", "-x"]),
        )

    def test_prompt_words_that_look_like_flags_are_kept(self):
        self.assertEqual(
            split_leading_options(["what", "does", "-n", "mean"]),
            ([], ["what", "does", "-n", "mean"]),
        )

    def test_double_dash_ends_options_and_is_dropped(self):
        self.assertEqual(
            split_leading_options(["--", "-rf", "is", "dangerous"]),
            ([], ["-rf", "is", "dangerous"]),
        )
        self.assertEqual(
            split_leading_options(["--non-interactive", "--", "--new"]),
            (["--non-interactive"], ["--new"]),
        )

    def test_lone_dash_is_a_prompt_word(self):
        self.assertEqual(split_leading_options(["-"]), ([], ["-"]))

    def test_only_options(self):
        self.assertEqual(split_leading_options(["--new"]), (["--new"], []))
        self.assertEqual(split_leading_options([]), ([], []))


class TestParseArgv(unittest.TestCase):
    def test_known_flags(self):
        options, prompt = parse_argv(["-n", "--non-interactive", "fix", "it"])
        self.assertTrue(options.new)
        self.assertTrue(options.non_interactive)
        self.assertEqual(prompt, ["fix", "it"])

    def test_unknown_leading_flag_is_a_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                parse_argv(["--bogus", "hello"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments: --bogus", stderr.getvalue())
        self.assertIn("usage: conch", stderr.getvalue())
        self.assertIn("--", stderr.getvalue())

    def test_dash_prompt_without_separator_explains_the_fix(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                parse_argv(["-rf", "is", "dangerous"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("put -- before a prompt", stderr.getvalue())

    def test_no_abbreviations(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_argv(["--non-inter"])

    def test_usage_text_is_the_parser_help(self):
        self.assertTrue(_USAGE.startswith("usage: conch "))
        for flag in ("-h, --help", "-V, --version", "-n, --new",
                     "--non-interactive"):
            self.assertIn(flag, _USAGE)
        self.assertIn("Put -- before a prompt", _USAGE)


class TestMainArgumentHandling(unittest.TestCase):
    def test_help_prints_usage_and_returns(self):
        stdout = io.StringIO()
        with mock.patch("sys.argv", ["conch", "--help"]), \
                contextlib.redirect_stdout(stdout):
            main()
        self.assertEqual(stdout.getvalue(), _USAGE)
        stdout = io.StringIO()
        with mock.patch("sys.argv", ["conch", "-h"]), \
                contextlib.redirect_stdout(stdout):
            main()
        self.assertIn("usage: conch", stdout.getvalue())

    def test_version_prints_and_returns(self):
        for flag in ("--version", "-V"):
            stdout = io.StringIO()
            with mock.patch("sys.argv", ["conch", flag]), \
                    contextlib.redirect_stdout(stdout):
                main()
            self.assertEqual(stdout.getvalue().strip(),
                             f"conch {conch.__version__}")

    def test_unknown_flag_exits_2_before_anything_loads(self):
        stderr = io.StringIO()
        with mock.patch("sys.argv", ["conch", "--yolo"]), \
                mock.patch("conch.app.chat_loop") as loop, \
                mock.patch("conch.app.load_config") as load, \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--yolo", stderr.getvalue())
        loop.assert_not_called()
        load.assert_not_called()

    def _run_one_shot(self, argv):
        """One-shot path with the model wiring stubbed; returns the user
        message text that would have been sent."""
        sent = {}

        class FakeSession:
            def run_turn(self, messages, **kwargs):
                sent["user"] = messages[-1]["content"]
                return "ok", {}

            def close(self):
                pass

        with mock.patch("conch.app.build_agent_session",
                        lambda config, **kw: FakeSession()), \
                mock.patch("conch.app.load_config",
                           return_value={"provider": "openai"}), \
                mock.patch("conch.app.apply_agent_mode_from_config"), \
                mock.patch("conch.app.resolve_startup_provider",
                           return_value=("openai", lambda *a, **k: None)), \
                mock.patch("conch.app.ensure_working_model", _model_check_off), \
                mock.patch("conch.app.get_chat_prompt", return_value="sys"), \
                mock.patch("conch.app._build_system_prompt", return_value="sys"), \
                mock.patch("conch.app._augment_user_message",
                           lambda text, ctx: text), \
                mock.patch("conch.app.MemoryStore") as mem, \
                mock.patch("conch.onboarding.maybe_run_first_run_wizard",
                           return_value=False), \
                mock.patch("sys.stdin", _FakeStdin(True)), \
                mock.patch("sys.argv", ["conch"] + argv), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            mem.return_value.build_context.return_value = ""
            main()
        return sent["user"]

    def test_positional_prompt_is_preserved_verbatim(self):
        self.assertEqual(
            self._run_one_shot(["what", "does", "-n", "mean", "in", "ls", "-la"]),
            "what does -n mean in ls -la",
        )

    def test_double_dash_allows_a_dash_prompt(self):
        self.assertEqual(
            self._run_one_shot(["--", "-rf", "is", "the", "dangerous", "part"]),
            "-rf is the dangerous part",
        )

    def test_flags_before_prompt_are_consumed(self):
        self.assertEqual(
            self._run_one_shot(["--non-interactive", "--", "hello"]), "hello"
        )


class TestConchAsk(unittest.TestCase):
    def test_help(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli.main(["--help"])
        text = stdout.getvalue()
        self.assertTrue(text.startswith("usage: conch-ask"))
        self.assertIn("printed, never run", text)
        self.assertIn("-V, --version", text)

    def test_version(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli.main(["-V"])
        self.assertEqual(stdout.getvalue().strip(),
                         f"conch-ask {conch.__version__}")

    def test_unknown_flag_is_a_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                mock.patch("conch.cli.ask") as ask:
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--bogus", "list files"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments: --bogus", stderr.getvalue())
        ask.assert_not_called()

    def test_missing_request_exits_1_with_hint(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                cli.main([])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--help", stderr.getvalue())

    def test_request_words_and_double_dash(self):
        stdout = io.StringIO()
        with mock.patch("conch.cli.ask", return_value="ls -la") as ask, \
                contextlib.redirect_stdout(stdout):
            cli.main(["list", "files", "with", "-la"])
            cli.main(["--", "-la", "listing", "please"])
        self.assertEqual(
            [call.args[0] for call in ask.call_args_list],
            ["list files with -la", "-la listing please"],
        )
        self.assertEqual(stdout.getvalue().splitlines(), ["ls -la", "ls -la"])


if __name__ == "__main__":
    unittest.main()
