"""Tests for the graded permission model (plan 2.1): prompt_all / safe_auto /
yolo modes, safe-command detection, prefix allowlists, and the destructive
check that prompts even in agent mode."""

import io
import sys
import unittest

from conch.tooling import (
    LocalShellClient,
    LocalShellPolicy,
    command_prefix,
    get_permission_mode,
    is_destructive_command,
    is_safe_command,
    set_agent_mode,
    set_permission_mode,
)


class _ScriptedInput:
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


class PermissionStateMixin:
    def setUp(self):
        set_agent_mode(False)
        set_permission_mode("prompt_all")

    def tearDown(self):
        set_agent_mode(False)
        set_permission_mode("prompt_all")


class TestPermissionModes(PermissionStateMixin, unittest.TestCase):
    def test_default_is_prompt_all(self):
        self.assertEqual(get_permission_mode(), "prompt_all")

    def test_set_modes(self):
        for mode in ("safe_auto", "yolo", "prompt_all"):
            set_permission_mode(mode)
            self.assertEqual(get_permission_mode(), mode)

    def test_dashes_normalized(self):
        set_permission_mode("safe-auto")
        self.assertEqual(get_permission_mode(), "safe_auto")

    def test_invalid_mode_ignored(self):
        set_permission_mode("bogus")
        self.assertEqual(get_permission_mode(), "prompt_all")

    def test_agent_mode_means_yolo(self):
        set_agent_mode(True)
        self.assertEqual(get_permission_mode(), "yolo")


class TestSafeCommandDetection(unittest.TestCase):
    def test_read_only_commands_safe(self):
        for cmd in ("ls -la", "pwd", "git status", "git log --oneline",
                    "cat README.md", "df -h", "rg pattern src/"):
            self.assertTrue(is_safe_command(cmd), cmd)

    def test_mutating_commands_not_safe(self):
        for cmd in ("touch x", "git push", "pip install foo", "make install"):
            self.assertFalse(is_safe_command(cmd), cmd)

    def test_chaining_disqualifies(self):
        for cmd in ("ls; rm -rf /", "cat x | sh", "ls && curl evil",
                    "echo $(whoami)", "cat x > /etc/passwd", "ls `id`"):
            self.assertFalse(is_safe_command(cmd), cmd)

    def test_empty_not_safe(self):
        self.assertFalse(is_safe_command(""))


class TestDestructiveDetection(unittest.TestCase):
    def test_destructive_commands(self):
        for cmd in (
            "rm -rf /tmp/x", "sudo rm file", "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda", "shutdown -h now", "reboot",
            "git push --force origin main", "git push -f",
            "git reset --hard HEAD~3", "git clean -fd",
            "killall python", "DROP TABLE users;",
        ):
            self.assertTrue(is_destructive_command(cmd), cmd)

    def test_ordinary_commands_not_destructive(self):
        for cmd in ("ls -la", "git status", "echo added", "date",
                    "git push origin main", "npm run build", "python x.py"):
            self.assertFalse(is_destructive_command(cmd), cmd)


class TestCommandPrefix(unittest.TestCase):
    def test_single_word_tools(self):
        self.assertEqual(command_prefix("ls -la /tmp"), "ls")
        self.assertEqual(command_prefix("echo hi"), "echo")

    def test_subcommand_tools_take_two_words(self):
        self.assertEqual(command_prefix("git status -sb"), "git status")
        self.assertEqual(command_prefix("docker ps -a"), "docker ps")
        self.assertEqual(command_prefix("kubectl get pods"), "kubectl get")

    def test_empty(self):
        self.assertEqual(command_prefix(""), "")


class TestShellPermissionFlow(PermissionStateMixin, unittest.TestCase):
    def _client(self, answers=(), interactive=True, auto=False):
        scripted = _ScriptedInput(answers)
        client = LocalShellClient()
        client.set_policy(LocalShellPolicy(
            interactive=interactive, allow_auto_execute=auto, input_fn=scripted,
        ))
        return client, scripted

    def test_safe_auto_runs_safe_command_without_prompt(self):
        set_permission_mode("safe_auto")
        client, scripted = self._client()
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo safe-mode"})
        self.assertIn("safe-mode", result["content"][0]["text"])
        self.assertEqual(len(scripted.prompts), 0)

    def test_safe_auto_still_prompts_for_mutating_command(self):
        set_permission_mode("safe_auto")
        client, scripted = self._client(["n", ""])
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "touch /tmp/x"})
        self.assertIn("declined", result["content"][0]["text"].lower())
        self.assertEqual(len(scripted.prompts), 2)  # approval + feedback

    def test_yolo_mode_auto_executes(self):
        set_permission_mode("yolo")
        client, scripted = self._client()
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo yolo"})
        self.assertIn("yolo", result["content"][0]["text"])
        self.assertEqual(len(scripted.prompts), 0)

    def test_destructive_prompts_even_in_agent_mode(self):
        set_agent_mode(True)
        client, scripted = self._client(["n", ""])
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "rm -rf /tmp/scratch"})
        self.assertIn("declined", result["content"][0]["text"].lower())
        self.assertGreater(len(scripted.prompts), 0,
                           "destructive commands must prompt even in agent mode")

    def test_destructive_refused_non_interactive(self):
        set_agent_mode(True)
        client, _ = self._client(interactive=False, auto=True)
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "rm -rf /tmp/scratch"})
        self.assertIn("Refused", result["content"][0]["text"])

    def test_config_allow_prefixes_skip_prompt(self):
        client, scripted = self._client()
        client.allow_prefixes(["echo", "git status"])
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "echo preseeded"})
        self.assertIn("preseeded", result["content"][0]["text"])
        self.assertEqual(len(scripted.prompts), 0)

    def test_allowed_prefix_does_not_cover_destructive(self):
        client, scripted = self._client(["n", ""])
        client.allow_prefixes(["rm"])
        with _CaptureStderr():
            result = client.call_tool("local_shell", {"command": "rm -rf /tmp/scratch"})
        self.assertIn("declined", result["content"][0]["text"].lower())
        self.assertGreater(len(scripted.prompts), 0)


class TestPermissionModeFromConfig(PermissionStateMixin, unittest.TestCase):
    def test_config_sets_mode(self):
        from conch.app import apply_agent_mode_from_config
        apply_agent_mode_from_config({"permission_mode": "safe_auto"})
        self.assertEqual(get_permission_mode(), "safe_auto")

    def test_agent_mode_config_still_wins(self):
        from conch.app import apply_agent_mode_from_config
        apply_agent_mode_from_config({"permission_mode": "safe_auto",
                                      "agent_mode": "true"})
        self.assertEqual(get_permission_mode(), "yolo")


if __name__ == "__main__":
    unittest.main()
