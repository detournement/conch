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
        target = parse_ssh_target("user@192.0.2.152", "22")
        self.assertEqual(target.user, "user")
        self.assertEqual(target.host, "192.0.2.152")
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
            host="192.0.2.152", user="user", port=2222
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
        self.assertEqual(argv[-2:], ["--", "192.0.2.152"])
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
        self.target = SSHTarget("192.0.2.152", "user", 22)
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

    def is_known(self, target):
        return bool(self.remembered)

    def was_lost(self, target):
        return False

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
                "host": "192.0.2.152",
                "user": "user",
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
                "host": "192.0.2.152",
                "user": "user",
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


_FAKE_SSH = r'''#!/usr/bin/env python3
"""Fake OpenSSH client: a real Unix-socket master daemon per ControlPath."""
import os
import socket
import sys

args = sys.argv[1:]


def opt(flag):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None


sock_path = opt("-S")
op = opt("-O")
user = opt("-l") or ""


def ping(payload):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(sock_path)
        s.sendall(payload)
        return s.recv(1) == b"k"
    except OSError:
        return False
    finally:
        s.close()


if op == "check":
    sys.exit(0 if ping(b"chck") else 255)
if op == "exit":
    sys.exit(0 if ping(b"exit") else 255)
if "-M" in args:
    # Emulate `ssh -M -N -f`: daemonize a master that owns the socket.
    ready_r, ready_w = os.pipe()
    pid = os.fork()
    if pid:
        os.close(ready_w)
        os.read(ready_r, 1)
        os._exit(0)
    os.close(ready_r)
    os.setsid()
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock_path)
    server.listen(8)
    with open(sock_path + ".pid", "w") as fh:
        fh.write(str(os.getpid()))
    os.write(ready_w, b"r")
    os.close(ready_w)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    while True:
        conn, _ = server.accept()
        data = conn.recv(4)
        conn.sendall(b"k")
        conn.close()
        if data == b"exit":
            try:
                os.unlink(sock_path)
            except OSError:
                pass
            os._exit(0)

# Multiplexed exec: fails exactly like OpenSSH when the master is gone.
if not ping(b"ping"):
    sys.stderr.write("Control socket connect failed\n")
    sys.exit(255)
sep = args.index("--")
host = args[sep + 1]
command = args[sep + 2] if len(args) > sep + 2 else ""
ident = (user + "@" if user else "") + host
sys.stdout.write("REMOTE(%s):%s\n" % (ident, command))
sys.exit(0)
'''


class _ExecutingRunner:
    """Terminal-handoff stand-in that really executes the connect argv."""

    def __init__(self):
        self.policy = None
        self.calls = []

    def set_policy(self, policy):
        self.policy = policy

    def available(self):
        return True

    def run(self, argv, **_kwargs):
        self.calls.append(list(argv))
        proc = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return TerminalRunResult(approved=True, returncode=proc.returncode)


class TestSSHUserSequenceRegression(unittest.TestCase):
    """Reproduce the reported transcript against a fake ssh with real
    sockets: connect(user@host) then exec(host-only) must succeed, and every
    liveness claim must be backed by a real -O check."""

    HOST = "192.0.2.152"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        fake_ssh = base / "bin" / "ssh"
        fake_ssh.parent.mkdir()
        fake_ssh.write_text(_FAKE_SSH)
        fake_ssh.chmod(0o755)
        patcher = patch.dict(
            os.environ,
            {"PATH": f"{fake_ssh.parent}{os.pathsep}{os.environ['PATH']}"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = SSHControlManager(runtime_dir=base / "run")
        self.addCleanup(self.manager.close)
        self.runner = _ExecutingRunner()
        self.client = SSHRemoteClient(manager=self.manager, runner=self.runner)
        self.answers = []
        self.client.set_policy(
            LocalShellPolicy(
                interactive=True,
                allow_auto_execute=True,
                input_fn=lambda _prompt: (
                    self.answers.pop(0) if self.answers else ""
                ),
                tty_check=lambda: True,
            )
        )
        self.addCleanup(self._kill_masters)

    def _kill_masters(self):
        for pid_file in Path(self.tmp.name).rglob("*.pid"):
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (OSError, ValueError):
                pass

    def _call(self, arguments):
        with patch("sys.stdout", io.StringIO()), patch(
            "sys.stderr", io.StringIO()
        ):
            result = self.client.call_tool("ssh_remote", dict(arguments))
        return result["content"][0]["text"]

    def _connect(self, user="conchremote", host=None):
        arguments = {"action": "connect", "host": host or self.HOST}
        if user:
            arguments["user"] = user
        return self._call(arguments)

    def _kill_master(self, identity_target):
        path = self.manager.control_path(identity_target)
        pid_file = Path(str(path) + ".pid")
        os.kill(int(pid_file.read_text()), 9)

    def test_user_transcript_connect_userhost_then_exec_hostonly(self):
        text = self._connect()
        self.assertIn(f"conchremote@{self.HOST} is active", text)
        text = self._call(
            {"action": "exec", "host": self.HOST, "command": "uname -a"}
        )
        self.assertNotIn("Error", text)
        self.assertIn(f"REMOTE(conchremote@{self.HOST}):uname -a", text)
        text = self._call({"action": "status", "host": self.HOST})
        self.assertEqual(text, f"SSH conchremote@{self.HOST}: connected.")

    def test_connect_accepts_user_at_host_in_host_field(self):
        text = self._connect(user="", host=f"conchremote@{self.HOST}")
        self.assertIn(f"conchremote@{self.HOST} is active", text)
        text = self._call(
            {"action": "exec", "host": self.HOST, "command": "id"}
        )
        self.assertIn(f"REMOTE(conchremote@{self.HOST}):id", text)

    def test_second_connect_already_active_only_when_check_passes(self):
        self._connect()
        self.assertEqual(len(self.runner.calls), 1)
        text = self._connect()
        self.assertIn("already active", text)
        self.assertEqual(len(self.runner.calls), 1)
        target = SSHTarget(self.HOST, "conchremote")
        self._kill_master(target)
        text = self._connect()
        self.assertNotIn("already active", text)
        self.assertIn("is active", text)
        self.assertEqual(len(self.runner.calls), 2)

    def test_killed_master_reports_lost_and_prompts_reconnect(self):
        self._connect()
        target = SSHTarget(self.HOST, "conchremote")
        socket_path = self.manager.control_path(target)
        self._kill_master(target)
        self.assertTrue(socket_path.exists())
        text = self._call(
            {"action": "exec", "host": self.HOST, "command": "id"}
        )
        self.assertIn("lost", text)
        self.assertIn("reconnect", text)
        self.assertFalse(socket_path.exists())
        text = self._call({"action": "status", "host": self.HOST})
        self.assertIn("lost", text)
        self.assertIn("reconnect", text)

    def test_two_users_on_one_host_is_ambiguous(self):
        self._connect(user="alice")
        self._connect(user="bob")
        text = self._call(
            {"action": "exec", "host": self.HOST, "command": "id"}
        )
        self.assertIn("multiple active SSH connections", text)
        self.assertIn(f"alice@{self.HOST}", text)
        self.assertIn(f"bob@{self.HOST}", text)
        text = self._call(
            {
                "action": "exec",
                "host": self.HOST,
                "user": "alice",
                "command": "id",
            }
        )
        self.assertIn(f"REMOTE(alice@{self.HOST}):id", text)

    def test_master_survives_handoff_pty_close(self):
        """The persisted master must outlive the interactive handoff PTY.

        OpenSSH's ``-f`` post-auth fork daemonizes (setsid), detaching the
        master from the handoff terminal session; the fake ssh mirrors that.
        Closing the PTY that hosted the bootstrap must not kill the master.
        """
        import pty as pty_module

        target = SSHTarget(self.HOST, "conchremote")
        argv = self.manager.connect_argv(target)
        pid, pty_fd = pty_module.fork()
        if pid == 0:
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        os.close(pty_fd)
        self.assertTrue(self.manager.is_connected(target))
        self.assertEqual(
            self._call({"action": "status", "host": self.HOST}),
            f"SSH conchremote@{self.HOST}: connected.",
        )

    def test_disconnect_cleans_socket_and_registry(self):
        self._connect()
        target = SSHTarget(self.HOST, "conchremote")
        socket_path = self.manager.control_path(target)
        self.assertTrue(socket_path.exists())
        self.answers.append("y")
        text = self._call({"action": "disconnect", "host": self.HOST})
        self.assertIn("closed", text)
        self.assertFalse(socket_path.exists())
        self.assertFalse(self.manager.is_known(target))
        text = self._call({"action": "status"})
        self.assertEqual(text, "No active SSH control connection.")


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
            "/ssh connect user@192.0.2.152 22"
        )
        self.assertEqual(result[0:2], ("run_builtin_tool", "ssh_remote"))
        self.assertEqual(
            result[2],
            {
                "action": "connect",
                "host": "192.0.2.152",
                "user": "user",
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
