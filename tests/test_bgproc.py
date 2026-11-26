"""local_shell background processes: start, poll, kill, caps, cleanup."""

import contextlib
import io
import os
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from conch import bgproc
from conch.tooling import LocalShellClient, LocalShellPolicy


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _alive(pid: int) -> bool:
    """A live, non-zombie process (orphans may linger as zombies in
    containers whose PID 1 does not reap)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    proc_stat = f"/proc/{pid}/stat"
    if os.path.exists(proc_stat):
        # Linux: the state letter follows the parenthesised command name
        # (no dependency on a `ps` binary, absent from slim images).
        try:
            with open(proc_stat) as handle:
                state = handle.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state != "Z"
    stat = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Base(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(bgproc, "STARTUP_WAIT", 0.3)
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.mkdtemp()
        self.shell = self.make_shell(auto=True)

    def make_shell(self, auto=True, input_fn=None, interactive=False):
        shell = LocalShellClient()
        shell.set_policy(LocalShellPolicy(
            interactive=interactive, allow_auto_execute=auto, input_fn=input_fn,
        ))
        shell.set_cwd(self.tmp)
        shell.processes().kill_grace = 1.0
        self.addCleanup(shell.stop_background_processes, 0.2)
        return shell

    def call(self, shell, name, **arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            return _text(shell.call_tool(name, arguments))

    def start(self, command, shell=None):
        return self.call(shell or self.shell, "local_shell",
                         command=command, background=True)


class StartAndPollTests(_Base):
    def test_start_returns_id_and_first_output_without_waiting(self):
        began = time.monotonic()
        out = self.start("echo ready; sleep 20")
        self.assertLess(time.monotonic() - began, 5)
        self.assertIn("Started background process p1", out)
        self.assertIn("ready", out)
        self.assertIn("running", out)
        self.assertIn("process_kill", out)

    def test_incremental_output_and_exit_status(self):
        flag = os.path.join(self.tmp, "go")
        out = self.start(
            f"echo one; while [ ! -f '{flag}' ]; do sleep 0.05; done; echo two; exit 7"
        )
        body = "\n".join(out.splitlines()[1:])  # the header echoes the command
        self.assertIn("one", body)
        self.assertNotIn("two", body)
        self.assertIn("running", self.call(self.shell, "process_output", id="p1"))
        open(flag, "w").close()
        out = self.call(self.shell, "process_output", id="p1", wait=10)
        body = "\n".join(out.splitlines()[1:])  # the header echoes the command
        self.assertIn("two", body)
        self.assertNotIn("one", body)
        self.assertIn("exited with code 7", out)
        # A further read has nothing new; since=0 replays everything.
        self.assertIn("(no new output)", self.call(self.shell, "process_output", id="p1"))
        replay = self.call(self.shell, "process_output", id="p1", since=0)
        self.assertIn("one", replay)
        self.assertIn("two", replay)

    def test_sanitizes_terminal_controls_and_redacts_credentials(self):
        token = "ghp_" + "a1B2c3D4e5" * 4
        self.start(f"printf '\\033[31mred\\033[0m\\r\\n'; echo 'GITHUB_TOKEN={token}'")
        out = self.call(self.shell, "process_output", id="p1", since=0, wait=10)
        self.assertIn("red", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        self.assertNotIn(token, out)
        self.assertIn("[credential redacted]", out)

    def test_ring_buffer_drops_oldest_output(self):
        self.shell.processes().ring_chars = 2048
        self.start("head -c 20000 /dev/zero | tr '\\000' x; echo; echo END")
        out = self.call(self.shell, "process_output", id="p1", since=0, wait=10)
        self.assertIn("END", out)
        match = re.search(r"\[([\d,]+) earlier chars were dropped", out)
        self.assertIsNotNone(match, out)
        self.assertGreater(int(match.group(1).replace(",", "")), 15000)

    def test_output_is_bounded_by_result_budget(self):
        self.shell.set_result_budget(1500)
        self.start("i=0; while [ $i -lt 400 ]; do echo line-$i-padding-padding; i=$((i+1)); done")
        out = self.call(self.shell, "process_output", id="p1", since=0, wait=10)
        self.assertLess(len(out), 2200)
        self.assertIn("truncated", out)
        self.assertIn("line-399", out)

    def test_unknown_id_and_list(self):
        self.assertIn("none started", self.call(self.shell, "process_output", id="p9"))
        self.assertIn("No background processes", self.call(self.shell, "process_list"))
        self.start("sleep 20")
        listing = self.call(self.shell, "process_list")
        self.assertIn("p1", listing)
        self.assertIn("1 running", listing)


class KillTests(_Base):
    def test_kill_stops_shell_children(self):
        out = self.start("sleep 30 & echo kid=$!; sleep 30 & echo kid=$!; wait")
        pids = [int(p) for p in re.findall(r"kid=(\d+)", out)]
        self.assertEqual(len(pids), 2, out)
        self.assertTrue(all(_alive(pid) for pid in pids))
        began = time.monotonic()
        result = self.call(self.shell, "process_kill", id="p1")
        self.assertLess(time.monotonic() - began, 3)
        self.assertIn("Stopped process p1", result)
        self.assertTrue(_wait_until(lambda: not any(_alive(pid) for pid in pids)))
        self.assertIn("killed by SIGTERM", self.call(self.shell, "process_list"))
        self.assertIn("already stopped", self.call(self.shell, "process_kill", id="p1"))

    def test_sigkill_after_grace_when_term_is_ignored(self):
        self.shell.processes().kill_grace = 0.3
        flag = os.path.join(self.tmp, "trapped")
        self.start(f"trap '' TERM; touch '{flag}'; sleep 30")
        self.assertTrue(_wait_until(lambda: os.path.exists(flag)))
        self.call(self.shell, "process_kill", id="p1")
        self.assertIn("killed by SIGKILL", self.call(self.shell, "process_list"))


class LifecycleTests(_Base):
    def test_cap_refuses_before_prompting(self):
        prompts = []
        shell = self.make_shell(auto=False, interactive=True,
                                input_fn=lambda p: prompts.append(p) or "y")
        shell.processes().max_running = 2
        self.start("sleep 20", shell)
        self.start("sleep 20", shell)
        self.assertEqual(len(prompts), 2)
        out = self.start("sleep 20", shell)
        self.assertIn("Refused", out)
        self.assertIn("2 background processes", out)
        self.assertEqual(len(prompts), 2)
        self.call(shell, "process_kill", id="p1")
        self.assertIn("Started background process p3", self.start("sleep 20", shell))

    def test_session_close_kills_owned_processes_only(self):
        from conch.session import AgentSession

        out = self.start("sleep 30 & echo kid=$!; wait")
        pid = int(re.search(r"kid=(\d+)", out).group(1))
        child = AgentSession({}, permissions=None)
        child.attach_clients({"local_shell": self.shell}, bind=False)
        child.close()
        self.assertTrue(_alive(pid))
        owner = AgentSession({}, cwd=self.tmp)
        owner.attach_clients({"local_shell": self.shell})
        owner.close()
        self.assertTrue(_wait_until(lambda: not _alive(pid)))
        self.assertFalse(self.shell.processes().running())

    def test_atexit_hook_covers_live_registries(self):
        self.start("sleep 30")
        bgproc._kill_all_registries()
        self.assertFalse(self.shell.processes().running())


class ApprovalTests(_Base):
    def test_decline_starts_nothing(self):
        shell = self.make_shell(auto=False, interactive=True, input_fn=lambda p: "n")
        out = self.start("sleep 20", shell)
        self.assertIn("declined", out)
        self.assertEqual(shell.processes().list(), [])

    def test_destructive_background_command_still_prompts_in_yolo(self):
        prompts = []
        shell = self.make_shell(auto=True, interactive=True,
                                input_fn=lambda p: prompts.append(p) or "n")
        out = self.start("rm -rf ./build", shell)
        self.assertIn("Execute?", prompts[0])
        self.assertIn("declined", out)
        noninteractive = self.make_shell(auto=True)
        self.assertIn("Refused", self.start("rm -rf ./build", noninteractive))
        self.assertEqual(noninteractive.processes().list(), [])

    def test_prompt_all_noninteractive_refuses(self):
        shell = self.make_shell(auto=False)
        out = self.start("sleep 20", shell)
        self.assertIn("cannot prompt", out)
        self.assertEqual(shell.processes().list(), [])

    def test_sandbox_backend_refuses_clearly(self):
        prompts = []
        shell = self.make_shell(auto=False, interactive=True,
                                input_fn=lambda p: prompts.append(p) or "y")
        shell._exec_backend = mock.Mock(kind="docker")
        out = self.start("sleep 20", shell)
        self.assertIn("not supported with the docker sandbox", out)
        self.assertEqual(prompts, [])

    def test_disallowed_session_refuses(self):
        self.shell.set_background_allowed(False, "fan-out workers cannot start background processes")
        out = self.start("sleep 20")
        self.assertIn("fan-out workers cannot", out)
        self.assertEqual(self.shell.processes().list(), [])

    def test_remote_shell_refuses_background(self):
        from conch.remote import RemoteShellClient

        notes = []
        remote = RemoteShellClient(mock.Mock(), lambda text, tid: notes.append(text),
                                   "sms", "t1")
        out = _text(remote.call_tool("local_shell", {"command": "ls", "background": True}))
        self.assertIn("not available in remote", out)
        self.assertEqual(notes, [])


class WiringTests(unittest.TestCase):
    def test_tools_registered_and_grouped_with_local_shell(self):
        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore
        from conch.tooling import inject_builtin_tools, tool_group

        clients = make_builtin_clients(MemoryStore(), {}, interactive=False)
        for name in bgproc.PROCESS_TOOL_NAMES:
            self.assertIs(clients[name], clients["local_shell"])
        tools, tool_map = [], {}
        inject_builtin_tools(tools, tool_map, clients)
        names = {t["function"]["name"] for t in tools}
        self.assertTrue(set(bgproc.PROCESS_TOOL_NAMES) <= names)
        for name in bgproc.PROCESS_TOOL_NAMES:
            self.assertEqual(tool_group(name, tool_map), "local_shell")
        shell_def = next(t for t in tools if t["function"]["name"] == "local_shell")
        self.assertIn("background", shell_def["function"]["parameters"]["properties"])

    def test_remote_and_workers_exclude_process_tools(self):
        from conch.fanout import WORKER_EXTRA_EXCLUDED_TOOLS
        from conch.remote import REMOTE_EXCLUDED_TOOLS

        for name in bgproc.PROCESS_TOOL_NAMES:
            self.assertIn(name, REMOTE_EXCLUDED_TOOLS)
            self.assertIn(name, WORKER_EXTRA_EXCLUDED_TOOLS)

    def test_start_pins_process_tools_in_owner_session(self):
        from conch.session import AgentSession
        from conch.tooling import PROCESS_TOOLS, ToolRuntimeState

        state = ToolRuntimeState(all_tools=list(PROCESS_TOOLS), tool_map={}, tools=[])
        session = AgentSession({})
        shell = LocalShellClient()
        shell.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        session.attach_clients({"local_shell": shell}, chat_state=state)
        self.addCleanup(session.close)
        with mock.patch.object(bgproc, "STARTUP_WAIT", 0.1), \
                contextlib.redirect_stdout(io.StringIO()):
            shell.call_tool("local_shell", {"command": "true", "background": True})
        self.assertEqual(state.pinned_tools, set(bgproc.PROCESS_TOOL_NAMES))
        self.assertTrue(state.needs_tool_refresh)

    def test_ps_command_lists_and_kills(self):
        from conch.commands import handle_slash_command

        shell = LocalShellClient()
        shell.set_policy(LocalShellPolicy(interactive=False, allow_auto_execute=True))
        self.addCleanup(shell.stop_background_processes, 0.2)
        with mock.patch.object(bgproc, "STARTUP_WAIT", 0.1), \
                contextlib.redirect_stdout(io.StringIO()):
            shell.call_tool("local_shell", {"command": "sleep 20", "background": True})

        def run(cmd):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                handle_slash_command(cmd, {}, "openai", "m", lambda on: None,
                                     tool_map={"local_shell": shell})
            return buf.getvalue()

        self.assertIn("p1", run("/ps"))
        self.assertIn("Stopped", run("/ps kill p1"))
        self.assertFalse(shell.processes().running())


if __name__ == "__main__":
    unittest.main()
