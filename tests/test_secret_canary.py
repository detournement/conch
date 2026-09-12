"""Secret-canary gate (Swarm Phase 0, non-negotiable invariant).

A distinctive canary value stands in for every credential Conch handles:
API keys in environment variables, secret-like config values, and input
typed during terminal handoffs. These tests drive representative flows and
assert the canary never appears in model-visible prompts, transcripts,
terminal logs, state files on disk, or tool results.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.secure_terminal import TerminalRunResult
from conch.session import AgentSession
from conch.tooling import (
    ConchConfigClient,
    ConchIntrospectClient,
    InteractiveTerminalClient,
    LocalShellClient,
    LocalShellPolicy,
    PermissionState,
    ToolRuntimeState,
)

CANARY = "CANARY-9f2c1e-hunter2-do-not-log"


class SecretCanaryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_home = Path(self._tmp.name) / "state"
        self.config_home = Path(self._tmp.name) / "config"
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_CONFIG_HOME": str(self.config_home),
            # The canary poses as a real provider credential in the
            # environment, exactly where conch reads API keys from.
            "OPENAI_API_KEY": CANARY,
            "CONCH_CANARY_EXTRA": CANARY,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {
            "provider": "openai",
            "chat_model": "gpt-4o",
            "model": "gpt-4o",
            "api_key_env": "OPENAI_API_KEY",
            # A literal secret-like value in the config file itself.
            "API_LAYER_KEY": CANARY,
        }

    def _assert_clean(self, text: str, where: str):
        self.assertNotIn(
            CANARY, text, f"secret canary leaked into {where}"
        )

    def _walk_state_files(self):
        for root in (self.state_home, self.config_home):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    yield path


class TestPromptsNeverContainCanary(SecretCanaryCase):
    def test_system_prompt_construction(self):
        from conch.app import _build_system_prompt
        from conch.prompts import get_chat_prompt

        base = get_chat_prompt("openai", "gpt-4o", self.config)
        full = _build_system_prompt(
            base, provider="openai", model="gpt-4o", config=self.config
        )
        self._assert_clean(base, "the base chat prompt")
        self._assert_clean(full, "the composed system prompt")

    def test_self_description_and_user_augmentation(self):
        from conch.app import _augment_user_message
        from conch.prompts import build_self_description

        self._assert_clean(
            build_self_description("openai", "gpt-4o", self.config),
            "the model self-description",
        )
        self._assert_clean(
            _augment_user_message("what is my api key situation?", ""),
            "the augmented user message",
        )

    def test_remote_system_prompt(self):
        from conch.remote import REMOTE_SYSTEM_PROMPT

        self._assert_clean(
            REMOTE_SYSTEM_PROMPT.format(channel="slack"),
            "the remote system prompt",
        )


class TestTurnTranscriptLogsAndState(SecretCanaryCase):
    """One full agent turn with a real shell tool: nothing the session
    persists or prints may contain the canary credential."""

    def _run_full_turn(self):
        responses = [
            {
                "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {
                        "name": "local_shell",
                        "arguments": json.dumps(
                            {"command": "echo tool-ran-fine"}
                        ),
                    },
                }],
                "_usage": {"input_tokens": 1, "output_tokens": 1},
                "_model": "test",
            },
            {
                "content": "All done.",
                "tool_calls": None,
                "_usage": {"input_tokens": 1, "output_tokens": 1},
                "_model": "test",
            },
        ]

        def raw_fn(cfg, messages, tools):
            return responses.pop(0)

        shell = LocalShellClient(
            permissions=PermissionState(agent_mode=True)
        )
        shell.set_policy(LocalShellPolicy(interactive=False))
        session = AgentSession(self.config, permissions=PermissionState())
        session.attach_clients(
            {"local_shell": shell},
            chat_state=ToolRuntimeState(all_tools=[], tool_map={}, tools=[]),
            bind=False,
        )
        messages = [
            {"role": "system", "content": "You are Conch."},
            {"role": "user", "content": "run a command"},
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": "local_shell",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                patch("conch.providers.RAW_FNS", {"openai": raw_fn}):
            reply, usage = session.run_turn(
                messages, tools=tools, tool_map={}
            )
        return reply, messages, stdout.getvalue(), stderr.getvalue()

    def test_transcript_logs_and_state_files_stay_clean(self):
        reply, messages, out, err = self._run_full_turn()
        self.assertEqual(reply, "All done.")
        transcript = json.dumps(messages)
        self.assertIn("tool-ran-fine", transcript,
                      "the tool really ran and reported output")
        self._assert_clean(transcript, "the conversation transcript")
        self._assert_clean(out, "terminal stdout")
        self._assert_clean(err, "terminal stderr")

        # Persist everything a session persists, then sweep the state tree.
        from conch.conversations import ConversationManager
        from conch.memory import MemoryStore

        conv_mgr = ConversationManager()
        conv = conv_mgr.create(model="gpt-4o", provider="openai")
        conv.messages = messages
        conv_mgr.save(conv)
        conv_mgr.close()
        memory = MemoryStore()
        memory.add("session ran a command and finished")

        swept = 0
        for path in self._walk_state_files():
            swept += 1
            content = path.read_bytes().decode("utf-8", errors="replace")
            self._assert_clean(content, f"state file {path.name}")
        self.assertGreater(swept, 0, "the sweep must inspect real files")


class TestToolResultsNeverContainCanary(SecretCanaryCase):
    def test_interactive_terminal_result_is_status_only(self):
        """Even if the handed-off program's (uncaptured) output was the
        canary, the tool result carries status only."""

        class _Runner:
            def __init__(self):
                self.policy = None
                # What the child printed to the real terminal; a leak would
                # require the runner to have captured it, which it cannot.
                self.uncaptured_terminal_output = CANARY

            def set_policy(self, policy):
                self.policy = policy

            def run(self, argv, **kwargs):
                return TerminalRunResult(approved=True, returncode=0)

        client = InteractiveTerminalClient(runner=_Runner())
        client.set_policy(
            LocalShellPolicy(interactive=True, tty_check=lambda: True)
        )
        result = client.call_tool(
            "interactive_terminal", {"command": "sudo id"}
        )
        text = result["content"][0]["text"]
        self.assertIn("exit code 0", text)
        self._assert_clean(text, "the interactive_terminal tool result")

    def test_credential_forward_rejection_does_not_echo_secret(self):
        from conch.ssh_control import SSHValidationError, validate_remote_command

        for command in (
            f"sshpass -p {CANARY} ssh host",
            f"tool --password={CANARY}",
            f"SSHPASS={CANARY} sshpass -e ssh host",
        ):
            with self.subTest(command=command.split()[0]):
                with self.assertRaises(SSHValidationError) as ctx:
                    validate_remote_command(command)
                self._assert_clean(
                    str(ctx.exception), "the validation error message"
                )

    def test_conch_config_get_hides_key_values(self):
        client = ConchConfigClient()
        client.bind("openai", "gpt-4o", {"turns": 1}, self.config)
        result = client.call_tool("conch_config", {"action": "get"})
        self._assert_clean(
            result["content"][0]["text"], "conch_config get output"
        )

    def test_introspect_config_report_hides_secret_like_settings(self):
        client = ConchIntrospectClient()
        client.bind("openai", "gpt-4o", self.config)
        result = client.call_tool("conch_introspect", {"action": "config"})
        text = result["content"][0]["text"]
        self._assert_clean(text, "conch_introspect config report")
        self.assertIn("secret-like setting(s) hidden", text)

    def test_ssh_argv_construction_carries_no_environment_secrets(self):
        from conch.ssh_control import SSHControlManager, SSHTarget

        manager = SSHControlManager(
            runtime_dir=Path(self._tmp.name) / "ssh-runtime"
        )
        target = SSHTarget(host="example.com", user="deploy", port=2222)
        for argv in (
            manager.connect_argv(target),
            manager.check_argv(target),
            manager.exec_argv(target, "uptime"),
            manager.disconnect_argv(target),
        ):
            self._assert_clean(" ".join(argv), "an ssh argv")
        manager.close()


class TestKernelNeverContainsCanary(SecretCanaryCase):
    """Swarm Phase 1 extension: secret bytes never reach kernel rows,
    events, the daemon log, or control-socket responses — even when the
    environment and config carry live credentials the whole time."""

    def test_kernel_daemon_log_and_socket_sweep(self):
        from conch.kernel import control
        from conch.kernel.daemon import EdgeDaemon

        root = Path(self._tmp.name) / "edge"
        socket_path = root / "run" / "edge.sock"

        def factory(mission, messages, control_client, caps):
            # the model summarizes without ever seeing credentials; assert
            # its inputs were clean too
            for message in messages:
                self._assert_clean(
                    json.dumps(message), "mission session messages"
                )
            return "session complete", {"total_tokens": 11}

        daemon = EdgeDaemon(
            self.config, kernel_dir=root / "kernel", state_dir=root,
            socket_path=socket_path, session_factory=factory,
        )
        daemon.start()
        try:
            mission_id = control.request("mission.new", {"spec": {
                "goal": "sweep probe", "budgets": {"sessions": 3},
                "cadence_seconds": 3600,
            }}, socket_path=socket_path)["mission_id"]
            grant = daemon.store.request_approval(
                mission_id, "publish", {"item": "x"},
                notify_payload={"text": "approval needed"},
            )
            daemon.tick()  # runs the session, delivers outbox to the log
            control.request("approval.decide", {
                "approval_id": grant["approval_id"], "verb": "deny",
                "nonce": grant["nonce"],
            }, socket_path=socket_path)
            for op in ("status", "missions.list", "schedule.list",
                       "approvals.list"):
                self._assert_clean(
                    json.dumps(control.request(
                        op, socket_path=socket_path
                    )),
                    f"control response for {op}",
                )
            self._assert_clean(
                json.dumps(control.request(
                    "mission.get", {"mission_id": mission_id},
                    socket_path=socket_path,
                )),
                "control response for mission.get",
            )
        finally:
            daemon.shutdown()
        # sweep every kernel byte on disk: database, WAL, log, lock
        swept = 0
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            swept += 1
            content = path.read_bytes().decode("utf-8", errors="replace")
            self._assert_clean(content, f"kernel file {path.name}")
        self.assertGreaterEqual(swept, 2)


if __name__ == "__main__":
    unittest.main()
