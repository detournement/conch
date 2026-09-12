"""Validation, lifecycle, and client tests for secure remote SSH support."""

import io
import os
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conch.commands import handle_slash_command
from conch.remote import REMOTE_EXCLUDED_TOOLS
from conch.secure_terminal import TerminalRunResult
from conch.ssh_control import (
    SSHControlManager,
    SSHTarget,
    SSHValidationError,
    parse_ssh_target,
    validate_remote_command,
    validate_ssh_host,
    validate_ssh_port,
    validate_ssh_user,
)
from conch.tooling import (
    DelegateTaskClient,
    LocalShellPolicy,
    SSHRemoteClient,
    set_agent_mode,
)


class TestSSHValidation(unittest.TestCase):
    def test_valid_target(self):
        target = parse_ssh_target("milgauss@192.168.1.152", "22")
        self.assertEqual(target.user, "milgauss")
        self.assertEqual(target.host, "192.168.1.152")
        self.assertEqual(target.port, 22)

    def test_ipv6_and_config_alias_are_supported(self):
        self.assertEqual(validate_ssh_host("2001:db8::1"), "2001:db8::1")
        self.assertEqual(validate_ssh_host("lab_gpu-1"), "lab_gpu-1")

    def test_option_injection_is_rejected(self):
        for host in (
            "-oProxyCommand=touch /tmp/pwned",
            "host -o ProxyCommand=x",
            "host;touch",
            "user@host",
        ):
            with self.assertRaises(SSHValidationError, msg=host):
                validate_ssh_host(host)
        for user in ("-oProxyCommand=x", "user name", "u;id"):
            with self.assertRaises(SSHValidationError, msg=user):
                validate_ssh_user(user)
        for port in (0, 65536, "22 -oProxyCommand=x", True):
            with self.assertRaises(SSHValidationError, msg=str(port)):
                validate_ssh_port(port)

    def test_password_forwarding_commands_are_rejected(self):
        for command in (
            "sshpass -p x ssh host",
            "sudo -S id",
            "sudo -A id",
            "sudo --stdin id",
            "sudo --askpass id",
            "SUDO_ASKPASS=/tmp/a sudo -A id",
            "client --password=secret",
            "client --passphrase secret",
        ):
            with self.assertRaises(SSHValidationError, msg=command):
                validate_remote_command(command)
        self.assertEqual(validate_remote_command("sudo -s"), "sudo -s")


class TestSSHCommandConstruction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = SSHControlManager(
            runtime_dir=Path(self.tmp.name) / "ssh",
            persist_seconds=900,
        )
        self.addCleanup(self.manager.close)
        self.target = SSHTarget(
            host="192.168.1.152", user="milgauss", port=2222
        )

    def test_runtime_directory_is_private(self):
        self.manager.runtime_dir.mkdir(mode=0o777)
        os.chmod(self.manager.runtime_dir, 0o777)
        path = self.manager.control_path(self.target)
        mode = stat.S_IMODE(self.manager.runtime_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)
        self.assertEqual(path.parent, self.manager.runtime_dir)

    def test_control_paths_are_unique_per_manager(self):
        other = SSHControlManager(runtime_dir=self.manager.runtime_dir)
        self.addCleanup(other.close)
        self.assertNotEqual(
            self.manager.control_path(self.target),
            other.control_path(self.target),
        )

    def test_connect_argv_keeps_host_key_verification_default(self):
        argv = self.manager.connect_argv(self.target)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("ControlMaster=yes", argv)
        self.assertIn("ControlPersist=900", argv)
        self.assertIn("-M", argv)
        self.assertIn("-N", argv)
        self.assertIn("-f", argv)
        self.assertEqual(argv[-2:], ["--", "192.168.1.152"])
        rendered = " ".join(argv)
        self.assertNotIn("StrictHostKeyChecking=no", rendered)
        self.assertNotIn("UserKnownHostsFile", rendered)
        self.assertNotIn("password", rendered.lower())

    def test_captured_exec_is_batchmode_and_command_is_one_argument(self):
        command = "printf '%s\\n' safe; uname -a"
        argv = self.manager.exec_argv(self.target, command)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("-T", argv)
        self.assertEqual(argv[-1], command)
        host_index = argv.index("--") + 1
        self.assertEqual(argv[host_index], self.target.host)

    def test_interactive_exec_allocates_tty(self):
        argv = self.manager.exec_argv(
            self.target, "sudo systemctl status llama", tty=True
        )
        self.assertIn("-tt", argv)
        self.assertNotIn("-T", argv)
        self.assertEqual(argv[-1], "sudo systemctl status llama")


class TestSSHControlLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = SSHControlManager(
            runtime_dir=Path(self.tmp.name) / "ssh"
        )
        self.addCleanup(self.manager.close)
        self.target = SSHTarget("host.example", "alice", 22)

    def _bind_control_socket(self):
        self.manager.connect_argv(self.target)
        path = self.manager.control_path(self.target)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        self.addCleanup(sock.close)
        return path

    def test_status_remembers_live_socket_and_disconnect_removes_it(self):
        path = self._bind_control_socket()
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        with patch("conch.ssh_control.subprocess.run", side_effect=fake_run):
            self.assertTrue(self.manager.is_connected(self.target))
            self.assertEqual(
                self.manager.resolve().identity, self.target.identity
            )
            self.assertTrue(self.manager.disconnect(self.target))
        self.assertFalse(path.exists())
        self.assertTrue(any("-O" in argv and "exit" in argv for argv, _ in calls))
        for _, kwargs in calls:
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    def test_cleanup_does_not_delete_non_socket_collision(self):
        path = self.manager.control_path(self.target)
        path.write_text("unrelated")
        self.manager.remember(self.target)
        with patch(
            "conch.ssh_control.subprocess.run",
            return_value=SimpleNamespace(returncode=255),
        ):
            self.manager.cleanup_all()
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(), "unrelated")

    def test_cleanup_does_not_adopt_or_delete_unreserved_socket(self):
        path = self.manager.control_path(self.target)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        self.addCleanup(sock.close)
        self.manager.remember(self.target)
        with patch("conch.ssh_control.subprocess.run") as run:
            self.manager.cleanup_all()
        run.assert_not_called()
        self.assertTrue(path.exists())

    def test_connect_refuses_existing_control_path(self):
        path = self.manager.control_path(self.target)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        self.addCleanup(sock.close)
        with self.assertRaises(OSError):
            self.manager.connect_argv(self.target)
        self.manager.cleanup_all()
        self.assertTrue(path.exists())


class _FakeManager:
    def __init__(self):
        self.target = SSHTarget("192.168.1.152", "milgauss", 22)
        self.connected = False
        self.remembered = []
        self.disconnected = []
        self.exec_calls = []

    def resolve(self, host="", user="", port=None):
        if host:
            return SSHTarget(host, user, port)
        if not self.remembered:
            raise SSHValidationError("no active SSH connection")
        return self.target

    def is_connected(self, target):
        return self.connected

    def connected_targets(self):
        return [self.target] if self.connected else []

    def remember(self, target):
        self.target = target
        self.remembered.append(target)

    def connect_argv(self, target):
        return ["ssh", "CONNECT", target.identity]

    def exec_argv(self, target, command, tty=False):
        self.exec_calls.append((target, command, tty))
        return ["ssh", "TTY" if tty else "EXEC", target.identity, command]

    def disconnect(self, target, timeout=5):
        self.connected = False
        self.disconnected.append(target)
        return True

    def close(self):
        pass


class _FakeRunner:
    def __init__(self, manager, result=None):
        self.manager = manager
        self.result = result or TerminalRunResult(
            approved=True, returncode=0
        )
        self.calls = []
        self.policy = None

    def set_policy(self, policy):
        self.policy = policy

    def available(self):
        return bool(self.policy and self.policy.local_session)

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if not self.policy or not self.policy.local_session:
            self.calls.pop()
            return TerminalRunResult(
                approved=False, error="outside the local foreground session"
            )
        if "CONNECT" in argv and self.result.returncode == 0:
            self.manager.connected = True
        return self.result


class TestSSHRemoteClient(unittest.TestCase):
    def _client(self, answers=()):
        manager = _FakeManager()
        runner = _FakeRunner(manager)
        client = SSHRemoteClient(manager=manager, runner=runner)
        replies = iter(answers)
        client.set_policy(
            LocalShellPolicy(
                interactive=True,
                input_fn=lambda _prompt: next(replies, ""),
                tty_check=lambda: True,
            )
        )
        return client, manager, runner

    def test_connect_has_no_terminal_transcript_in_result(self):
        client, manager, runner = self._client()
        result = client.call_tool(
            "ssh_remote",
            {
                "action": "connect",
                "host": "192.168.1.152",
                "user": "milgauss",
                "port": 22,
            },
        )
        text = result["content"][0]["text"]
        self.assertIn("is active", text)
        self.assertIn("not captured", text)
        self.assertNotIn("PASSWORD-SENTINEL", text)
        self.assertTrue(manager.remembered)
        self.assertEqual(len(runner.calls), 1)

    def test_noninteractive_connect_is_denied_before_spawn(self):
        manager = _FakeManager()
        runner = _FakeRunner(manager)
        client = SSHRemoteClient(manager=manager, runner=runner)
        client.set_policy(
            LocalShellPolicy(interactive=False, tty_check=lambda: True)
        )
        result = client.call_tool(
            "ssh_remote",
            {
                "action": "connect",
                "host": "192.168.1.152",
                "user": "milgauss",
            },
        )
        self.assertIn("Refused", result["content"][0]["text"])
        self.assertEqual(runner.calls, [])

    def test_remote_exec_requires_approval_outside_agent_mode(self):
        client, manager, _ = self._client(answers=[""])
        manager.connected = True
        manager.remember(manager.target)
        with patch("sys.stdout", io.StringIO()):
            result = client.call_tool(
                "ssh_remote",
                {"action": "exec", "command": "touch /tmp/x"},
            )
        self.assertIn("declined", result["content"][0]["text"].lower())
        self.assertEqual(manager.exec_calls, [])

    def test_captured_remote_exec_uses_timeout_and_result_budget(self):
        client, manager, _ = self._client()
        client.set_policy(
            LocalShellPolicy(interactive=False, allow_auto_execute=True)
        )
        client.set_result_budget(600)
        manager.connected = True
        manager.remember(manager.target)
        manager.exec_argv = lambda _target, _command, tty=False: [
            "python3",
            "-c",
            "print('HEAD' + 'x' * 10000 + 'TAIL')",
        ]
        with patch("sys.stdout", io.StringIO()), patch(
            "sys.stderr", io.StringIO()
        ):
            result = client.call_tool(
                "ssh_remote",
                {
                    "action": "exec",
                    "command": "read-only-probe",
                    "timeout": 5,
                },
            )
        text = result["content"][0]["text"]
        self.assertLessEqual(len(text), 600)
        self.assertTrue(text.startswith("HEAD"))
        self.assertIn("truncated", text)
        self.assertTrue(text.endswith("TAIL"))

    def test_interactive_remote_sudo_always_uses_direct_tty(self):
        set_agent_mode(True)
        self.addCleanup(set_agent_mode, False)
        client, manager, runner = self._client()
        client.set_policy(
            LocalShellPolicy(
                interactive=True,
                allow_auto_execute=True,
                tty_check=lambda: True,
            )
        )
        manager.connected = True
        manager.remember(manager.target)
        result = client.call_tool(
            "ssh_remote",
            {"action": "shell", "command": "sudo systemctl status llama"},
        )
        self.assertIn(
            "No terminal input or output was captured",
            result["content"][0]["text"],
        )
        self.assertEqual(manager.exec_calls[-1][2], True)
        self.assertEqual(len(runner.calls), 1)

    def test_disconnect_requires_local_confirmation(self):
        client, manager, _ = self._client(answers=[""])
        manager.connected = True
        manager.remember(manager.target)
        result = client.call_tool(
            "ssh_remote", {"action": "disconnect"}
        )
        self.assertIn("declined", result["content"][0]["text"].lower())
        self.assertEqual(manager.disconnected, [])


class TestRemoteChannelBoundary(unittest.TestCase):
    def test_interactive_and_ssh_tools_are_excluded(self):
        self.assertIn("interactive_terminal", REMOTE_EXCLUDED_TOOLS)
        self.assertIn("ssh_remote", REMOTE_EXCLUDED_TOOLS)
        self.assertIn(
            "interactive_terminal", DelegateTaskClient.EXCLUDED_TOOLS
        )
        self.assertIn("ssh_remote", DelegateTaskClient.EXCLUDED_TOOLS)


class TestSSHSlashCommands(unittest.TestCase):
    def _parse(self, command):
        with patch("sys.stdout", io.StringIO()):
            return handle_slash_command(
                command, {}, "ollama", "model", lambda _value: None
            )

    def test_connect_target_is_split_into_validated_fields(self):
        result = self._parse(
            "/ssh connect milgauss@192.168.1.152 22"
        )
        self.assertEqual(result[0:2], ("run_builtin_tool", "ssh_remote"))
        self.assertEqual(
            result[2],
            {
                "action": "connect",
                "host": "192.168.1.152",
                "user": "milgauss",
                "port": 22,
            },
        )

    def test_exec_and_terminal_commands_preserve_command_text(self):
        ssh_result = self._parse("/ssh exec printf '%s' hello")
        term_result = self._parse("/terminal sudo -k id")
        self.assertEqual(ssh_result[2]["command"], "printf '%s' hello")
        self.assertEqual(term_result[2]["command"], "sudo -k id")

    def test_option_injection_target_is_rejected(self):
        self.assertIsNone(
            self._parse("/ssh connect '-oProxyCommand=id' 22")
        )


if __name__ == "__main__":
    unittest.main()
