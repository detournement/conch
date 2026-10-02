"""`conch --non-interactive` and non-TTY auto-detection.

A one-shot or piped run must never reach an approval prompt: the session
policy is non-interactive, so approval-gated tools refuse ("cannot
prompt") instead of asking a stdin that cannot answer. These tests cover
the decision helper, the argv wiring in main() for both the one-shot and
shell paths, and an end-to-end one-shot turn whose model asks for a
destructive command.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from conch.app import _USAGE, main, session_is_interactive
from conch.tooling import LocalShellClient, LocalShellPolicy


class _FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class TestSessionIsInteractive(unittest.TestCase):
    def test_tty_without_flag_is_interactive(self):
        self.assertTrue(session_is_interactive(False, stdin=_FakeStdin(True)))

    def test_flag_wins_over_tty(self):
        self.assertFalse(session_is_interactive(True, stdin=_FakeStdin(True)))

    def test_non_tty_stdin_is_non_interactive(self):
        self.assertFalse(session_is_interactive(False, stdin=_FakeStdin(False)))

    def test_flag_and_non_tty_agree(self):
        self.assertFalse(session_is_interactive(True, stdin=_FakeStdin(False)))

    def test_closed_or_odd_stdin_fails_closed(self):
        class Broken:
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        self.assertFalse(session_is_interactive(False, stdin=Broken()))
        self.assertFalse(session_is_interactive(False, stdin=object()))

    def test_help_documents_flag_and_autodetection(self):
        self.assertIn("--non-interactive", _USAGE)
        self.assertIn("stdin is not a terminal", _USAGE)
        self.assertIn("OR'd", _USAGE)


class TestMainWiring(unittest.TestCase):
    """main() hands the interactive decision to chat_loop / the one-shot
    session, skips the wizard when non-interactive, and keeps --new."""

    def _run_shell(self, argv, tty):
        calls = []

        def fake_chat_loop(new_conversation=False, interactive=True):
            calls.append((new_conversation, interactive))

        wizard = mock.Mock(return_value=False)
        with mock.patch("conch.app.chat_loop", fake_chat_loop), \
                mock.patch("conch.onboarding.maybe_run_first_run_wizard", wizard), \
                mock.patch("sys.stdin", _FakeStdin(tty)), \
                mock.patch("sys.argv", ["conch"] + argv), \
                contextlib.redirect_stderr(io.StringIO()):
            main()
        return calls, wizard

    def test_tty_shell_is_interactive_and_offers_wizard(self):
        calls, wizard = self._run_shell([], tty=True)
        self.assertEqual(calls, [(False, True)])
        self.assertEqual(wizard.call_count, 1)

    def test_flag_makes_shell_non_interactive_and_skips_wizard(self):
        calls, wizard = self._run_shell(["--non-interactive"], tty=True)
        self.assertEqual(calls, [(False, False)])
        self.assertEqual(wizard.call_count, 0)

    def test_piped_stdin_makes_shell_non_interactive(self):
        calls, wizard = self._run_shell([], tty=False)
        self.assertEqual(calls, [(False, False)])
        self.assertEqual(wizard.call_count, 0)

    def test_flag_combines_with_new_in_either_order(self):
        calls, _ = self._run_shell(["--non-interactive", "--new"], tty=True)
        self.assertEqual(calls, [(True, False)])
        calls, _ = self._run_shell(["-n", "--non-interactive"], tty=True)
        self.assertEqual(calls, [(True, False)])

    def test_new_with_prompt_still_rejected_after_flag(self):
        stderr = io.StringIO()
        with mock.patch("sys.stdin", _FakeStdin(True)), \
                mock.patch("sys.argv", ["conch", "--non-interactive", "--new", "hi"]), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--new", stderr.getvalue())

    def _run_one_shot(self, argv, tty):
        """Run the one-shot path with the model and session wiring stubbed;
        returns the kwargs build_agent_session was called with."""
        captured = {}

        class FakeSession:
            def run_turn(self, messages, **kwargs):
                return "ok", {}

            def close(self):
                pass

        def fake_build(config, **kwargs):
            captured.update(kwargs)
            return FakeSession()

        with mock.patch("conch.app.build_agent_session", fake_build), \
                mock.patch("conch.app.load_config", return_value={"provider": "openai"}), \
                mock.patch("conch.app.apply_agent_mode_from_config"), \
                mock.patch("conch.app.resolve_startup_provider",
                           return_value=("openai", lambda *a, **k: None)), \
                mock.patch("conch.app.warn_unknown_cloud_model", return_value=""), \
                mock.patch("conch.app.get_chat_prompt", return_value="sys"), \
                mock.patch("conch.app._build_system_prompt", return_value="sys"), \
                mock.patch("conch.app.MemoryStore") as mem, \
                mock.patch("conch.onboarding.maybe_run_first_run_wizard",
                           return_value=False), \
                mock.patch("sys.stdin", _FakeStdin(tty)), \
                mock.patch("sys.argv", ["conch"] + argv), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            mem.return_value.build_context.return_value = ""
            main()
        return captured, err.getvalue()

    def test_one_shot_on_tty_stays_interactive(self):
        captured, err = self._run_one_shot(["hello"], tty=True)
        self.assertTrue(captured["interactive"])
        self.assertNotIn("non-interactive", err)

    def test_one_shot_flag_is_non_interactive(self):
        captured, err = self._run_one_shot(["--non-interactive", "hello"], tty=True)
        self.assertFalse(captured["interactive"])
        self.assertIn("non-interactive", err)

    def test_one_shot_piped_stdin_is_non_interactive(self):
        captured, err = self._run_one_shot(["hello"], tty=False)
        self.assertFalse(captured["interactive"])
        self.assertIn("non-interactive", err)


class TestOneShotTurnNeverPromptsOrExecutes(unittest.TestCase):
    """The policy a non-interactive main() installs, exercised through the
    real tool client: the destructive gate refuses without ever calling
    input(), and nothing touches the filesystem."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.victim = os.path.join(self._tmp.name, "victim.txt")
        with open(self.victim, "w") as fh:
            fh.write("precious\n")
        self.marker = os.path.join(self._tmp.name, "ran.marker")

    def _client(self, agent_mode):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=False, allow_auto_execute=agent_mode,
        ))
        return client

    def test_destructive_refused_without_prompt(self):
        client = self._client(agent_mode=False)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = client.call_tool(
                "local_shell", {"command": f"rm -rf {self.victim}"}
            )
        self.assertIn("Refused", result["content"][0]["text"])
        self.assertTrue(os.path.exists(self.victim))

    def test_destructive_refused_even_in_agent_mode(self):
        client = self._client(agent_mode=True)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = client.call_tool(
                "local_shell", {"command": f"rm -rf {self.victim}"}
            )
        self.assertIn("Refused", result["content"][0]["text"])
        self.assertTrue(os.path.exists(self.victim))

    def test_unapproved_normal_command_not_run(self):
        client = self._client(agent_mode=False)
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = client.call_tool(
                "local_shell", {"command": f"touch {self.marker}"}
            )
        self.assertIn("cannot prompt", result["content"][0]["text"])
        self.assertFalse(os.path.exists(self.marker))


if __name__ == "__main__":
    unittest.main()
