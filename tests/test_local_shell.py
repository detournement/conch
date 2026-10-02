"""Tests for LocalShellClient — approval flow, streaming output, always-allow."""

import contextlib
import io
import os
import sys
import tempfile
import unittest

from conch.tooling import LocalShellClient, LocalShellPolicy


class _ScriptedInput:
    """Callable that returns answers in order from a queue.

    Answers may be exception instances (``EOFError()``,
    ``KeyboardInterrupt()``); those are raised instead of returned, which
    is what a closed stdin or Ctrl-C at the prompt looks like to the
    caller. An exhausted queue raises EOFError.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _CaptureStderr:
    def __enter__(self):
        self._old = sys.stderr
        sys.stderr = self.buf = io.StringIO()
        return self.buf

    def __exit__(self, *args):
        sys.stderr = self._old



class _QuietStdout:
    """Swallow the approval banners LocalShellClient prints so the suite's
    output stays readable; tests that assert on stdout redirect it again
    inside the test, which takes precedence."""

    def setUp(self):
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        super().setUp()

class TestLocalShellApproval(_QuietStdout, unittest.TestCase):
    def test_yes_runs_command(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False,
            input_fn=_ScriptedInput(["y"]),
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo hi"})
        text = result["content"][0]["text"]
        self.assertIn("hi", text)

    def test_enter_runs_command(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False,
            input_fn=_ScriptedInput([""]),
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo enter-default"})
        self.assertIn("enter-default", result["content"][0]["text"])

    def test_no_declines(self):
        scripted = _ScriptedInput(["n", ""])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo nope"})
        self.assertIn("declined", result["content"][0]["text"].lower())

    def test_no_with_feedback(self):
        scripted = _ScriptedInput(["n", "don't run echo"])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo nope"})
        text = result["content"][0]["text"]
        self.assertIn("declined", text.lower())
        self.assertIn("don't run echo", text)

    def test_edit_runs_modified_command(self):
        scripted = _ScriptedInput(["e", "echo edited"])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo original"})
        text = result["content"][0]["text"]
        self.assertIn("edited", text)
        self.assertNotIn("original", text)

    def test_edit_empty_falls_back_to_original(self):
        scripted = _ScriptedInput(["e", ""])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo keep"})
        self.assertIn("keep", result["content"][0]["text"])

    def test_always_allow_skips_prompt_on_repeat(self):
        scripted = _ScriptedInput(["a"])  # only one prompt expected
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            result_1 = client.call_tool("local_shell", {"command": "echo cached"})
            result_2 = client.call_tool("local_shell", {"command": "echo cached"})
        self.assertIn("cached", result_1["content"][0]["text"])
        self.assertIn("cached", result_2["content"][0]["text"])
        self.assertEqual(len(scripted.prompts), 1)

    def test_always_allow_persists_across_set_policy(self):
        scripted = _ScriptedInput(["a", ""])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            client.call_tool("local_shell", {"command": "echo persistent"})
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo persistent"})
        self.assertIn("persistent", result["content"][0]["text"])

    def test_always_allow_covers_same_prefix(self):
        # 'a' allows the command *prefix* (plan 2.1), so a different echo
        # command runs without another prompt...
        scripted = _ScriptedInput(["a"])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            client.call_tool("local_shell", {"command": "echo first"})
            client.call_tool("local_shell", {"command": "echo second"})
        self.assertEqual(len(scripted.prompts), 1)

    def test_always_allow_does_not_cover_other_prefixes(self):
        # ...but a command with a different prefix still prompts.
        scripted = _ScriptedInput(["a", "y"])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False, input_fn=scripted,
        ))
        with _CaptureStderr():
            client.call_tool("local_shell", {"command": "echo first"})
            client.call_tool("local_shell", {"command": "printf second"})
        self.assertEqual(len(scripted.prompts), 2)

    def test_agent_mode_skips_prompt(self):
        scripted = _ScriptedInput([])
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=True, input_fn=scripted,
        ))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo agent"})
        self.assertIn("agent", result["content"][0]["text"])
        self.assertEqual(len(scripted.prompts), 0)

    def test_non_interactive_without_auto_execute_returns_message(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=False))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo background"})
        self.assertIn("background tasks", result["content"][0]["text"].lower())


class TestBeforeRunHook(_QuietStdout, unittest.TestCase):
    """The pre-execution hook app.py uses for the pre-turn git checkpoint
    (/undo): fires with the command right before an approved command
    runs, not when the command is declined, and can never block it."""

    def test_hook_fires_before_an_approved_command(self):
        events = []
        marker = os.path.join(tempfile.mkdtemp(prefix="conch-hook-"), "m")
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False,
            input_fn=_ScriptedInput(["y"]),
        ))
        client.set_before_run(
            lambda cmd: events.append((cmd, os.path.exists(marker)))
        )
        with _CaptureStderr():
            client.call_tool("local_shell", {"command": f"touch {marker}"})
        self.assertEqual(events, [(f"touch {marker}", False)],
                         "hook must see the command before it has run")
        self.assertTrue(os.path.exists(marker))

    def test_hook_does_not_fire_for_a_declined_command(self):
        events = []
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False,
            input_fn=_ScriptedInput(["n", ""]),
        ))
        client.set_before_run(events.append)
        with _CaptureStderr():
            client.call_tool("local_shell", {"command": "echo nope"})
        self.assertEqual(events, [])

    def test_hook_does_not_fire_when_non_interactive_refuses(self):
        events = []
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=False, allow_auto_execute=False,
            input_fn=_ScriptedInput([]),
        ))
        client.set_before_run(events.append)
        with _CaptureStderr():
            client.call_tool("local_shell", {"command": "echo nope"})
        self.assertEqual(events, [])

    def test_hook_failure_never_blocks_the_command(self):
        def boom(_cmd):
            raise RuntimeError("checkpoint exploded")

        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=True, allow_auto_execute=False,
            input_fn=_ScriptedInput(["y"]),
        ))
        client.set_before_run(boom)
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo survived"})
        self.assertIn("survived", result["content"][0]["text"])

    def test_interactive_terminal_hook_fires_before_handoff(self):
        from conch.tooling import InteractiveTerminalClient

        order = []

        class _Result:
            approved = True
            error = ""
            timed_out = False
            interrupted = False
            returncode = 0

        class _Runner:
            def set_policy(self, policy):
                pass

            def available(self):
                return True

            def run(self, argv, *, description, timeout):
                order.append(("run", list(argv)))
                return _Result()

        client = InteractiveTerminalClient(runner=_Runner())
        client.set_before_run(lambda what: order.append(("hook", what)))
        result = client.call_tool("interactive_terminal", {"command": "vim notes.md"})
        self.assertEqual(order[0], ("hook", "/bin/sh -c vim notes.md"))
        self.assertEqual(order[1][0], "run")
        self.assertIn("exit code 0", result["content"][0]["text"])


class TestNoAnswerDeclines(_QuietStdout, unittest.TestCase):
    """No answer at the approval prompt is a "no" — never consent.

    A closed stdin (``conch 'prompt' < /dev/null``, an exhausted pipe) or
    Ctrl-C at the prompt used to be treated as the Enter default, which
    approved — destructive gate included. Every branch must decline and
    leave the filesystem untouched.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.victim = os.path.join(self._tmp.name, "victim.txt")
        with open(self.victim, "w") as fh:
            fh.write("precious\n")
        self.marker = os.path.join(self._tmp.name, "ran.marker")

    def _client(self, answers, *, interactive=True, agent_mode=False):
        scripted = _ScriptedInput(answers)
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=interactive,
            allow_auto_execute=agent_mode,
            input_fn=scripted,
        ))
        return client, scripted

    def _run(self, client, command):
        out = io.StringIO()
        with _CaptureStderr(), contextlib.redirect_stdout(out):
            result = client.call_tool("local_shell", {"command": command})
        return result["content"][0]["text"], out.getvalue()

    def _assert_declined(self, text, printed):
        self.assertIn("not run", text)
        self.assertFalse(os.path.exists(self.marker), "command executed")
        self.assertTrue(os.path.exists(self.victim), "victim deleted")
        self.assertIn("Command not run", printed)

    # -- normal command ----------------------------------------------------

    def test_eof_at_prompt_declines_normal_command(self):
        client, scripted = self._client([EOFError()])
        text, printed = self._run(client, f"touch {self.marker}")
        self._assert_declined(text, printed)
        self.assertIn("EOF", text)
        self.assertEqual(len(scripted.prompts), 1)

    def test_ctrl_c_at_prompt_declines_normal_command(self):
        client, scripted = self._client([KeyboardInterrupt()])
        text, printed = self._run(client, f"touch {self.marker}")
        self._assert_declined(text, printed)
        self.assertIn("interrupted", text)
        self.assertEqual(len(scripted.prompts), 1)

    def test_exhausted_scripted_input_declines(self):
        # The shape of a pipe that ran out of lines: input() raises EOFError.
        client, _ = self._client([])
        text, printed = self._run(client, f"touch {self.marker}")
        self._assert_declined(text, printed)

    # -- destructive command (prompts even in agent mode) ------------------

    def test_eof_at_prompt_declines_destructive_command(self):
        client, _ = self._client([EOFError()])
        text, printed = self._run(client, f"rm -rf {self.victim}")
        self._assert_declined(text, printed)

    def test_ctrl_c_at_prompt_declines_destructive_command(self):
        client, _ = self._client([KeyboardInterrupt()])
        text, printed = self._run(client, f"rm -rf {self.victim}")
        self._assert_declined(text, printed)

    def test_eof_declines_destructive_command_in_agent_mode(self):
        client, scripted = self._client([EOFError()], agent_mode=True)
        text, printed = self._run(client, f"rm -rf {self.victim}")
        self._assert_declined(text, printed)
        self.assertEqual(len(scripted.prompts), 1, "destructive must prompt")

    def test_ctrl_c_declines_destructive_command_in_agent_mode(self):
        client, _ = self._client([KeyboardInterrupt()], agent_mode=True)
        text, printed = self._run(client, f"rm -rf {self.victim}")
        self._assert_declined(text, printed)

    # -- sub-prompts -------------------------------------------------------

    def test_eof_at_edit_prompt_does_not_run_original(self):
        client, _ = self._client(["e", EOFError()])
        text, printed = self._run(client, f"touch {self.marker}")
        self._assert_declined(text, printed)

    def test_ctrl_c_at_edit_prompt_does_not_run_original(self):
        client, _ = self._client(["e", KeyboardInterrupt()])
        text, printed = self._run(client, f"rm -rf {self.victim}")
        self._assert_declined(text, printed)

    def test_eof_at_reason_prompt_still_declines(self):
        client, _ = self._client(["n", EOFError()])
        text, _ = self._run(client, f"touch {self.marker}")
        self.assertIn("declined", text.lower())
        self.assertFalse(os.path.exists(self.marker))

    def test_ctrl_c_at_reason_prompt_still_declines(self):
        client, _ = self._client(["n", KeyboardInterrupt()])
        text, _ = self._run(client, f"touch {self.marker}")
        self.assertIn("declined", text.lower())
        self.assertFalse(os.path.exists(self.marker))

    # -- one-shot / non-interactive: never prompts, never runs --------------

    def test_non_interactive_never_prompts_and_never_runs_normal(self):
        client, scripted = self._client(["y"], interactive=False)
        text, _ = self._run(client, f"touch {self.marker}")
        self.assertIn("cannot prompt", text.lower())
        self.assertEqual(scripted.prompts, [])
        self.assertFalse(os.path.exists(self.marker))

    def test_non_interactive_never_prompts_and_never_runs_destructive(self):
        client, scripted = self._client(["y"], interactive=False)
        text, _ = self._run(client, f"rm -rf {self.victim}")
        self.assertIn("refused", text.lower())
        self.assertEqual(scripted.prompts, [])
        self.assertTrue(os.path.exists(self.victim))

    def test_non_interactive_agent_mode_still_refuses_destructive(self):
        client, scripted = self._client(
            ["y"], interactive=False, agent_mode=True
        )
        text, _ = self._run(client, f"rm -rf {self.victim}")
        self.assertIn("refused", text.lower())
        self.assertEqual(scripted.prompts, [])
        self.assertTrue(os.path.exists(self.victim))

    def test_explicit_yes_still_runs(self):
        # Guard against over-correcting: a real answer still approves.
        client, _ = self._client(["y"])
        self._run(client, f"touch {self.marker}")
        self.assertTrue(os.path.exists(self.marker))


class TestLocalShellExecution(_QuietStdout, unittest.TestCase):
    def test_empty_command_errors(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        result = client.call_tool("local_shell", {"command": ""})
        self.assertIn("empty", result["content"][0]["text"].lower())

    def test_nonzero_exit_appended(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "false"})
        text = result["content"][0]["text"]
        self.assertIn("exit code 1", text)

    def test_streaming_output_captured(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        with _CaptureStderr() as buf:
            result = client.call_tool(
                "local_shell",
                {"command": "printf 'line1\\nline2\\n'"},
            )
        text = result["content"][0]["text"]
        # Exact: the PTY's CRLF endings must not become blank lines.
        self.assertEqual(text, "line1\nline2")
        # should also have streamed live to stderr
        self.assertIn("line1", buf.getvalue())

    def test_multiline_output_has_no_doubled_newlines(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        with _CaptureStderr():
            result = client.call_tool(
                "local_shell",
                {"command": "printf 'a\\nb\\nc\\n\\nd\\n'"},
            )
        text = result["content"][0]["text"]
        self.assertEqual(text, "a\nb\nc\n\nd")
        self.assertNotIn("\r", text)

    def test_bare_carriage_return_still_breaks_line(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        with _CaptureStderr():
            result = client.call_tool(
                "local_shell",
                {"command": "printf '10%%\\r20%%\\r100%%\\n'"},
            )
        self.assertEqual(result["content"][0]["text"], "10%\n20%\n100%")

    def test_timeout_zero_means_no_timeout(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        with _CaptureStderr():
            result = client.call_tool(
                "local_shell",
                {"command": "echo done", "timeout": 0},
            )
        self.assertIn("done", result["content"][0]["text"])

    def test_large_output_is_bounded_while_streaming(self):
        client = LocalShellClient()
        client.set_policy(
            LocalShellPolicy(interactive=False, allow_auto_execute=True)
        )
        client.set_result_budget(1000)
        with _CaptureStderr():
            result = client.call_tool(
                "local_shell",
                {
                    "command": (
                        "python3 -c \"print('HEAD' + 'x' * 50000 + 'TAIL')\""
                    )
                },
            )
        text = result["content"][0]["text"]
        self.assertLessEqual(len(text), 1000)
        self.assertTrue(text.startswith("HEAD"))
        self.assertIn("truncated", text)
        self.assertTrue(text.endswith("TAIL"))


if __name__ == "__main__":
    unittest.main()
