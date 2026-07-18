"""Tests for LocalShellClient — approval flow, streaming output, always-allow."""

import io
import sys
import unittest

from conch.tooling import LocalShellClient, LocalShellPolicy


class _ScriptedInput:
    """Callable that returns answers in order from a queue."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class _CaptureStderr:
    def __enter__(self):
        self._old = sys.stderr
        sys.stderr = self.buf = io.StringIO()
        return self.buf

    def __exit__(self, *args):
        sys.stderr = self._old


class TestLocalShellApproval(unittest.TestCase):
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


class TestLocalShellExecution(unittest.TestCase):
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
        self.assertIn("line1", text)
        self.assertIn("line2", text)
        # should also have streamed live to stderr
        self.assertIn("line1", buf.getvalue())

    def test_timeout_zero_means_no_timeout(self):
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        with _CaptureStderr():
            result = client.call_tool(
                "local_shell",
                {"command": "echo done", "timeout": 0},
            )
        self.assertIn("done", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
