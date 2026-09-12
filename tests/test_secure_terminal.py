"""Security tests for direct, non-recording terminal handoff."""

import io
import unittest
from unittest.mock import patch

from conch.app import TypeaheadBuffer
from conch.secure_terminal import (
    DirectTerminalRunner,
    TerminalHandoffPolicy,
    TerminalRunResult,
)
from conch.tooling import (
    InteractiveTerminalClient,
    LocalShellClient,
    LocalShellPolicy,
    set_agent_mode,
)


class _TTYBuffer(io.StringIO):
    def __init__(self, fd):
        super().__init__()
        self._fd = fd

    def fileno(self):
        return self._fd

    def isatty(self):
        return True


class _FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        # A sentinel that would be a leak if the runner ever inspected output.
        self.stdout = "PASSWORD-SENTINEL"

    def wait(self, timeout=None):
        return self.returncode

    def send_signal(self, _signal):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass


class _StuckReaderThread:
    def __init__(self):
        self.joined = False

    def join(self, timeout=None):
        self.joined = True

    def is_alive(self):
        return True


class TestTypeaheadHandoffBoundary(unittest.TestCase):
    def test_handoff_fails_closed_if_reader_thread_does_not_stop(self):
        reader = TypeaheadBuffer()
        thread = _StuckReaderThread()
        reader._thread = thread
        with self.assertRaisesRegex(RuntimeError, "handoff refused"):
            reader.stop_for_handoff()
        self.assertTrue(thread.joined)
        self.assertTrue(reader.is_running())


class TestDirectTerminalRunner(unittest.TestCase):
    def test_explicit_confirmation_required_even_in_agent_mode(self):
        set_agent_mode(True)
        self.addCleanup(set_agent_mode, False)
        runner = DirectTerminalRunner(
            TerminalHandoffPolicy(
                local_session=True,
                input_fn=lambda _prompt: "",
                tty_check=lambda: True,
            )
        )
        with patch("conch.secure_terminal.subprocess.Popen") as popen:
            result = runner.run(
                ["/bin/sh", "-c", "true"], description="Run?"
            )
        self.assertFalse(result.approved)
        popen.assert_not_called()

    def test_remote_or_background_session_cannot_handoff(self):
        runner = DirectTerminalRunner(
            TerminalHandoffPolicy(
                local_session=False,
                input_fn=lambda _prompt: "y",
                tty_check=lambda: True,
            )
        )
        with patch("conch.secure_terminal.subprocess.Popen") as popen:
            result = runner.run(["true"], description="Run?")
        self.assertFalse(result.approved)
        self.assertIn("outside the local", result.error)
        popen.assert_not_called()

    def test_child_inherits_terminal_without_capture_or_env_injection(self):
        runner = DirectTerminalRunner(
            TerminalHandoffPolicy(
                local_session=True,
                input_fn=lambda _prompt: "yes",
                tty_check=lambda: True,
            )
        )
        process = _FakeProcess()
        with patch(
            "conch.secure_terminal.subprocess.Popen", return_value=process
        ) as popen, patch(
            "conch.secure_terminal.discard_pending_terminal_input"
        ) as discard_input, patch("sys.stdout", io.StringIO()):
            result = runner.run(["secret-reader"], description="Run?")
        self.assertTrue(result.approved)
        self.assertEqual(result.returncode, 0)
        kwargs = popen.call_args.kwargs
        for forbidden in (
            "stdin",
            "stdout",
            "stderr",
            "env",
            "input",
            "capture_output",
        ):
            self.assertNotIn(forbidden, kwargs)
        self.assertFalse(hasattr(result, "stdout"))
        discard_input.assert_called_once_with()

    def test_terminal_flags_restored_when_spawn_fails(self):
        stdin = _TTYBuffer(0)
        stdout = _TTYBuffer(1)
        stderr = _TTYBuffer(2)
        original = {0: ["stdin"], 1: ["stdout"], 2: ["stderr"]}

        with patch("conch.secure_terminal.sys.stdin", stdin), patch(
            "conch.secure_terminal.sys.stdout", stdout
        ), patch("conch.secure_terminal.sys.stderr", stderr), patch(
            "conch.secure_terminal.os.isatty", return_value=True
        ), patch(
            "conch.secure_terminal.termios.tcgetattr",
            side_effect=lambda fd: original[fd],
        ), patch(
            "conch.secure_terminal.termios.tcsetattr"
        ) as restore, patch(
            "conch.secure_terminal.subprocess.Popen",
            side_effect=OSError("spawn failed"),
        ):
            runner = DirectTerminalRunner(
                TerminalHandoffPolicy(
                    local_session=True,
                    input_fn=lambda _prompt: "y",
                    tty_check=lambda: True,
                )
            )
            result = runner.run(["missing"], description="Run?")

        self.assertTrue(result.approved)
        self.assertIn("spawn failed", result.error)
        restored = {
            (call.args[0], tuple(call.args[2]))
            for call in restore.call_args_list
        }
        self.assertEqual(
            restored,
            {
                (0, ("stdin",)),
                (1, ("stdout",)),
                (2, ("stderr",)),
            },
        )

    def test_terminal_flags_restored_while_unwinding_signal(self):
        stdin = _TTYBuffer(0)
        stdout = _TTYBuffer(1)
        stderr = _TTYBuffer(2)
        process = _FakeProcess()
        process.wait = lambda timeout=None: (_ for _ in ()).throw(
            SystemExit(143)
        )
        runner = DirectTerminalRunner(
            TerminalHandoffPolicy(
                local_session=True,
                input_fn=lambda _prompt: "y",
                tty_check=lambda: True,
            )
        )
        with patch("conch.secure_terminal.sys.stdin", stdin), patch(
            "conch.secure_terminal.sys.stdout", stdout
        ), patch("conch.secure_terminal.sys.stderr", stderr), patch(
            "conch.secure_terminal.os.isatty", return_value=True
        ), patch(
            "conch.secure_terminal.termios.tcgetattr",
            side_effect=lambda fd: ["saved", fd],
        ), patch(
            "conch.secure_terminal.termios.tcsetattr"
        ) as restore, patch(
            "conch.secure_terminal.subprocess.Popen", return_value=process
        ), patch.object(
            runner, "_stop_child"
        ) as stop_child, self.assertRaises(SystemExit):
            runner.run(["command"], description="Run?")
        self.assertEqual(restore.call_count, 3)
        stop_child.assert_called_once_with(process)


class _ResultRunner:
    def __init__(self, result):
        self.result = result
        self.policy = None
        self.calls = []

    def set_policy(self, policy):
        self.policy = policy

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.result


class TestInteractiveTerminalClient(unittest.TestCase):
    def test_captured_local_shell_has_no_interactive_stdin(self):
        client = LocalShellClient()
        client.set_policy(
            LocalShellPolicy(interactive=False, allow_auto_execute=True)
        )
        with patch("sys.stderr", io.StringIO()), patch(
            "sys.stdout", io.StringIO()
        ):
            result = client.call_tool(
                "local_shell",
                {
                    "command": (
                        "python3 -c 'import sys; print(sys.stdin.isatty())'"
                    )
                },
            )
        self.assertIn("False", result["content"][0]["text"])

    def test_captured_local_shell_has_no_controlling_terminal(self):
        client = LocalShellClient()
        client.set_policy(
            LocalShellPolicy(interactive=False, allow_auto_execute=True)
        )
        command = (
            'python3 -c "import os\n'
            "try:\n"
            " os.open('/dev/tty', os.O_RDONLY)\n"
            " print('CONTROLLING_TTY')\n"
            "except OSError:\n"
            " print('NO_CONTROLLING_TTY')\""
        )
        with patch("sys.stderr", io.StringIO()), patch(
            "sys.stdout", io.StringIO()
        ):
            result = client.call_tool(
                "local_shell", {"command": command, "timeout": 5}
            )
        text = result["content"][0]["text"]
        self.assertIn("NO_CONTROLLING_TTY", text)
        self.assertNotIn("CONTROLLING_TTY", text.splitlines())

    def test_returns_only_status_not_program_output_or_password(self):
        runner = _ResultRunner(
            TerminalRunResult(approved=True, returncode=0)
        )
        client = InteractiveTerminalClient(runner=runner)
        client.set_policy(
            LocalShellPolicy(interactive=True, tty_check=lambda: True)
        )
        result = client.call_tool(
            "interactive_terminal",
            {"command": "python3 -m password_prompt"},
        )
        text = result["content"][0]["text"]
        self.assertIn("exit code 0", text)
        self.assertIn("No terminal input or output was captured", text)
        self.assertNotIn("PASSWORD-SENTINEL", text)
        self.assertEqual(
            runner.calls[0][0],
            ["/bin/sh", "-c", "python3 -m password_prompt"],
        )

    def test_confirmation_shows_full_command_with_controls_escaped(self):
        runner = _ResultRunner(
            TerminalRunResult(approved=True, returncode=0)
        )
        client = InteractiveTerminalClient(runner=runner)
        client.set_policy(
            LocalShellPolicy(interactive=True, tty_check=lambda: True)
        )
        command = "printf 'first\nsecond'; " + ("x" * 300)
        client.call_tool("interactive_terminal", {"command": command})
        description = runner.calls[0][1]["description"]
        self.assertIn("\\x0a", description)
        self.assertNotIn("\n", description)
        self.assertIn("x" * 300, description)

    def test_insecure_password_forwarding_is_rejected(self):
        for command in (
            "sshpass -p hunter2 ssh host",
            "printf secret | sudo -S id",
            "sudo -A id",
            "SUDO_ASKPASS=/tmp/helper sudo -A id",
            "tool --password=secret",
        ):
            runner = _ResultRunner(
                TerminalRunResult(approved=True, returncode=0)
            )
            client = InteractiveTerminalClient(runner=runner)
            result = client.call_tool(
                "interactive_terminal", {"command": command}
            )
            self.assertIn("Refused", result["content"][0]["text"], command)
            self.assertEqual(runner.calls, [], command)

    def test_nonlocal_policy_reaches_hard_runner_boundary(self):
        runner = DirectTerminalRunner(
            TerminalHandoffPolicy(
                local_session=True,
                input_fn=lambda _prompt: "y",
                tty_check=lambda: True,
            )
        )
        client = InteractiveTerminalClient(runner=runner)
        client.set_policy(
            LocalShellPolicy(interactive=False, tty_check=lambda: True)
        )
        with patch("conch.secure_terminal.subprocess.Popen") as popen:
            result = client.call_tool(
                "interactive_terminal", {"command": "sudo id"}
            )
        self.assertIn("Refused", result["content"][0]["text"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
