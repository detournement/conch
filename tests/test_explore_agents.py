"""Explore subagents: a read-only profile for delegate_task and fan_out.

The model is scripted to ask for writes and mutating commands; the explore
profile must refuse them through tool filtering and wrapper clients, never
by trusting the prompt."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conch import fanout
from conch.filetools import FileToolsClient
from conch.session import AgentSession
from conch.tooling import (
    DELEGATE_TASK_TOOL,
    EXPLORE_TOOLS,
    DelegateTaskClient,
    ExploreFileClient,
    LocalShellClient,
    LocalShellPolicy,
    PermissionState,
    ToolRuntimeState,
    explore_shell_refusal,
    explore_toolset,
)


def _call(name, arguments, call_id="c"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


class _ScriptedModel:
    """raw_fn that issues the scripted tool calls one round at a time, then
    replies with a summary that quotes every tool result it saw."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.offered = []
        self.results = []

    def __call__(self, config, messages, tools):
        self.offered.append(sorted(t["function"]["name"] for t in tools or []))
        last = messages[-1]
        if last.get("role") == "tool":
            self.results.extend(
                str(m.get("content")) for m in messages if m.get("role") == "tool")
        if self.rounds:
            calls = self.rounds.pop(0)
            return {"content": "", "tool_calls": calls,
                    "_usage": {"input_tokens": 1, "output_tokens": 1}, "_model": "t"}
        return {"content": "FINDINGS: " + " || ".join(self.results), "tool_calls": None,
                "_usage": {"input_tokens": 1, "output_tokens": 1}, "_model": "t"}


class _Workspace:
    """A temp repo with a parent session whose clients are real."""

    def __init__(self, test, mode="prompt_all"):
        root = tempfile.TemporaryDirectory()
        test.addCleanup(root.cleanup)
        self.root = Path(os.path.realpath(root.name))
        (self.root / ".git").mkdir()
        (self.root / "notes.txt").write_text("alpha line\nbeta line\n")
        self.permissions = PermissionState(
            agent_mode=mode == "yolo",
            permission_mode=mode if mode != "yolo" else "prompt_all")
        self.prompts = []

        def ask(prompt):
            self.prompts.append(prompt)
            return "y"

        policy = LocalShellPolicy(interactive=True, input_fn=ask,
                                  allow_auto_execute=mode == "yolo")
        self.shell = LocalShellClient(permissions=self.permissions)
        self.shell.set_policy(policy)
        self.shell.set_cwd(str(self.root))
        self.files = FileToolsClient(permissions=self.permissions, config={})
        self.files.set_policy(policy)
        self.files.set_cwd(str(self.root))
        self.config = {"provider": "openai", "model": "m", "chat_model": "m",
                       "repo_map": "false"}
        self.session = AgentSession(self.config, interactive=True,
                                    permissions=self.permissions, cwd=str(self.root))
        test.addCleanup(self.session.close)
        self.delegate = DelegateTaskClient()
        clients = {"local_shell": self.shell, "read_file": self.files,
                   "edit_file": self.files, "write_file": self.files,
                   "grep": self.files, "glob": self.files,
                   "delegate_task": self.delegate, "conch_config": object()}
        tools = [{"type": "function", "function": {"name": n, "parameters": {}}}
                 for n in clients]
        state = ToolRuntimeState(all_tools=tools, tool_map={}, tools=tools)
        self.session.attach_clients(clients, chat_state=state, bind=False)
        self.delegate.bind_session(self.session)

    def run(self, model, **arguments):
        arguments.setdefault("task", "look around")
        with patch.dict("conch.providers.RAW_FNS", {"openai": model}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = self.delegate.call_tool("delegate_task", arguments)
        return result["content"][0]["text"]


class DelegateExploreTests(unittest.TestCase):
    def test_explore_offers_only_read_only_tools(self):
        ws = _Workspace(self)
        model = _ScriptedModel([])
        ws.run(model, mode="explore")
        self.assertEqual(model.offered[0], ["glob", "grep", "local_shell", "read_file"])

    def test_write_tools_and_mutating_commands_are_refused_without_prompting(self):
        ws = _Workspace(self, mode="prompt_all")
        model = _ScriptedModel([
            [_call("write_file", {"path": "new.txt", "content": "x"}, "w")],
            [_call("edit_file", {"path": "notes.txt", "old_string": "alpha",
                                 "new_string": "omega"}, "e")],
            [_call("local_shell", {"command": "touch made-by-explore"}, "t")],
            [_call("local_shell", {"command": "rm notes.txt"}, "r")],
            [_call("local_shell", {"command": "git branch evil"}, "b")],
            [_call("read_file", {"path": "notes.txt"}, "rd")],
            [_call("local_shell", {"command": "cat notes.txt"}, "c")],
        ])
        text = ws.run(model, mode="explore")
        self.assertFalse((ws.root / "new.txt").exists())
        self.assertFalse((ws.root / "made-by-explore").exists())
        self.assertEqual((ws.root / "notes.txt").read_text(), "alpha line\nbeta line\n")
        self.assertIn("was not offered", text)            # write_file / edit_file
        self.assertIn("explore agents are read-only", text)  # shell refusals
        self.assertIn("beta line", text)                  # reads work
        # Nothing asked the user, even in prompt_all.
        self.assertEqual(ws.prompts, [])

    def test_explore_ignores_agent_mode(self):
        ws = _Workspace(self, mode="yolo")
        model = _ScriptedModel([
            [_call("local_shell", {"command": "touch made-in-yolo"}, "t")],
        ])
        text = ws.run(model, mode="explore")
        self.assertFalse((ws.root / "made-in-yolo").exists())
        self.assertIn("explore agents are read-only", text)

    def test_skill_cannot_widen_explore_tools(self):
        ws = _Workspace(self)
        skill = {"name": "s", "tools": ["write_file", "read_file"], "body": "b",
                 "description": "d"}
        model = _ScriptedModel([])
        with patch("conch.skills.get_skill", return_value=skill), \
                patch("conch.skills.render_skill", return_value="skill body"):
            ws.run(model, mode="explore", skill="s")
        self.assertEqual(model.offered[0], ["read_file"])

    def test_general_mode_is_unchanged(self):
        ws = _Workspace(self)
        model = _ScriptedModel([])
        ws.run(model)
        self.assertIn("write_file", model.offered[0])

    def test_unknown_mode_is_an_error(self):
        ws = _Workspace(self)
        text = ws.run(_ScriptedModel([]), mode="sideways")
        self.assertTrue(text.startswith("Error"))

    def test_prompt_and_tool_schema_mention_explore(self):
        props = DELEGATE_TASK_TOOL["function"]["parameters"]["properties"]
        self.assertEqual(props["mode"]["enum"], ["general", "explore"])
        from conch.prompts import _CHAT_LOCAL, build_coding_guidance

        self.assertIn("mode='explore'", _CHAT_LOCAL)
        self.assertIn("mode='explore'", build_coding_guidance({"delegate_task"}))
        self.assertNotIn("mode='explore'", build_coding_guidance({"grep", "glob"}))

    def test_explore_tool_names_match_fanout_copy(self):
        self.assertEqual(tuple(fanout.EXPLORE_TOOLS), tuple(EXPLORE_TOOLS))


class ExploreShellPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cwd = directory.name
        (Path(self.cwd) / ".git").mkdir()

    def test_read_only_commands_pass(self):
        for cmd in ("ls -la", "cat README", "git log --oneline -5", "git status",
                    "rg -n TODO src", "grep -rn id_rsa .", "find . -name '*.py'",
                    "git branch -a", "git branch --list 'feat*'", "git remote -v"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(explore_shell_refusal(cmd, self.cwd))

    def test_mutating_and_leaky_commands_are_refused(self):
        env = Path(self.cwd) / ".env"
        env.write_text("KEY=1\n")
        for cmd in ("touch x", "rm -rf x", "echo hi > out", "ls; rm x",
                    "git branch new-branch", "git branch -D main",
                    "git branch --set-upstream-to=origin/main",
                    "git remote add o url", "git diff --output=patch.diff",
                    "find . -delete", "printenv", "cat .env", "tree -o out.txt",
                    "python -c 'print(1)'", "sed -i s/a/b/ f",
                    f"grep -r token {Path.home()}", "grep -r token /"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(explore_shell_refusal(cmd, self.cwd))

    def test_content_search_from_home_is_refused(self):
        self.assertIsNotNone(explore_shell_refusal("grep -r token .", str(Path.home())))

    def test_explore_file_client_refuses_writes(self):
        files = FileToolsClient(permissions=PermissionState(agent_mode=True), config={})
        files.set_cwd(self.cwd)
        wrapped = ExploreFileClient(files)
        for name in ("write_file", "edit_file"):
            result = wrapped.call_tool(name, {"path": "a.txt", "content": "x"})
            self.assertTrue(result.get("isError"))
        self.assertFalse((Path(self.cwd) / "a.txt").exists())

    def test_web_fetch_follows_the_session_rule_but_never_prompts(self):
        from conch.webfetch import WebFetchClient

        asked = []
        web = WebFetchClient(permissions=PermissionState(), config={})
        web.set_policy(LocalShellPolicy(interactive=True,
                                        input_fn=lambda p: asked.append(p) or "y"))
        _, out = explore_toolset({"web_fetch": web})
        with contextlib.redirect_stdout(io.StringIO()):
            refusal = out["web_fetch"]._approve("https://example.com/", "example.com")
        self.assertIn("cannot prompt", refusal)
        self.assertEqual(asked, [])
        web.bind_permissions(PermissionState(permission_mode="safe_auto"))
        _, out = explore_toolset({"web_fetch": web})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(out["web_fetch"]._approve("https://example.com/", "example.com"))

    def test_toolset_drops_everything_else(self):
        clients = {"local_shell": LocalShellClient(), "write_file": object(),
                   "conch_config": object(), "save_memory": object()}
        defs, out = explore_toolset(clients)
        self.assertEqual(set(out), {"local_shell"})
        self.assertEqual([d["function"]["name"] for d in defs], ["local_shell"])

    def test_explore_cannot_start_or_manage_background_processes(self):
        """Background processes (1.0.4) outlive a read-only explore run, so
        explore agents get no process tools and their shell never starts
        one, even if the model asks for background mode."""
        shell = LocalShellClient()
        clients = {"local_shell": shell, "process_output": object(),
                   "process_kill": object(), "process_list": object()}
        defs, out = explore_toolset(clients)
        self.assertEqual(set(out), {"local_shell"})
        shell_def = next(d for d in defs if d["function"]["name"] == "local_shell")
        self.assertNotIn("background", shell_def["function"]["parameters"]["properties"])
        with patch.object(LocalShellClient, "_run_process",
                          return_value={"content": [{"type": "text", "text": "ok"}]}) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            out["local_shell"].call_tool(
                "local_shell", {"command": "ls", "background": True})
        run.assert_called_once()  # ran in the foreground
        self.assertIsNone(shell._processes)  # no process registry was ever created


class FanOutExploreParentTests(unittest.TestCase):
    """The parent side of an explore fan-out (workers replaced)."""

    def _parent(self, mode="prompt_all", answers=()):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = {"provider": "custom", "chat_model": "m", "model": "m",
                  "custom_model": "m", "custom_base_url": "http://127.0.0.1:9/v1",
                  "fanout_concurrency": "8"}
        permissions = PermissionState(permission_mode=mode)
        asked = []
        answers = iter(answers)

        def ask(prompt):
            asked.append(prompt)
            return next(answers)

        shell = LocalShellClient(permissions=permissions)
        shell.set_policy(LocalShellPolicy(interactive=True, input_fn=ask))
        client = fanout.FanOutClient()
        envelopes = []

        def run(envelope, timeout):
            envelopes.append(envelope)
            return {"ok": True, "summary": "found in " + envelope["task"]}

        client._run_worker = run
        clients = {name: object() for name in (
            "save_memory", "public_api", "read_file", "write_file", "edit_file",
            "grep", "glob")}
        clients.update(local_shell=shell, fan_out=client)
        session = AgentSession(config, interactive=True, permissions=permissions,
                               cwd=directory.name)
        self.addCleanup(session.close)
        session.attach_clients(clients, bind=False, chat_state=SimpleNamespace(
            tools=[{"function": {"name": name}} for name in clients]))
        client.bind_session(session, usage={"input_tokens": 0, "output_tokens": 0,
                                            "cost": 0.0, "turns": 0})
        return client, envelopes, asked

    @staticmethod
    def _fan_out(client, arguments):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return client.call_tool("fan_out", arguments)["content"][0]["text"]

    def test_explore_only_fan_out_starts_without_asking_and_runs_in_parallel(self):
        client, envelopes, asked = self._parent()
        tasks = [{"task": f"area {i}"} for i in range(fanout.MAX_TASKS)]
        text = self._fan_out(client, {"tasks": tasks, "mode": "explore"})
        self.assertEqual(asked, [])
        self.assertIn(f"{fanout.MAX_TASKS} of {fanout.MAX_TASKS}", text)
        self.assertEqual({e["mode"] for e in envelopes}, {"explore"})
        for envelope in envelopes:
            self.assertEqual(envelope["tools"],
                             ["glob", "grep", "local_shell", "read_file"])

    def test_mixed_fan_out_still_asks_in_prompt_all(self):
        client, envelopes, asked = self._parent(answers=["y"])
        self._fan_out(client, {"tasks": [{"task": "a", "mode": "explore"},
                                         {"task": "b"}]})
        self.assertEqual(len(asked), 1)
        by_task = {e["task"]: e for e in envelopes}
        self.assertEqual(by_task["a"]["mode"], "explore")
        self.assertNotIn("write_file", by_task["a"]["tools"])
        self.assertEqual(by_task["b"]["mode"], "general")
        self.assertIn("write_file", by_task["b"]["tools"])

    def test_unknown_mode_starts_no_worker(self):
        client, envelopes, _ = self._parent()
        text = self._fan_out(client, {"tasks": [{"task": "a", "mode": "bogus"}]})
        self.assertTrue(text.startswith("Error"))
        self.assertEqual(envelopes, [])


class ExploreEnvelopeTests(unittest.TestCase):
    """run_envelope in explore mode (in-process, scripted model)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(os.path.realpath(directory.name))
        (self.root / "notes.txt").write_text("gamma\n")
        environment = {"HOME": str(self.root / "home"),
                       "XDG_CONFIG_HOME": str(self.root / "home" / "config"),
                       "XDG_STATE_HOME": str(self.root / "home" / "state"),
                       "XDG_DATA_HOME": str(self.root / "home" / "data"),
                       "CONCH_NO_WIZARD": "1"}
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)

    def test_worker_in_yolo_parent_still_cannot_write(self):
        model = _ScriptedModel([
            [_call("write_file", {"path": "w.txt", "content": "x"}, "w")],
            [_call("local_shell", {"command": "touch t.txt"}, "t")],
            [_call("local_shell", {"command": "cat notes.txt"}, "c")],
        ])
        envelope = {
            "task": "explore", "context": "", "cwd": str(self.root),
            "config": {"provider": "openai", "model": "m", "chat_model": "m",
                       "repo_map": "false"},
            "tools": ["local_shell", "read_file", "write_file", "grep", "glob"],
            "mode": "explore", "rounds": 6, "permission_mode": "yolo",
        }
        with patch.dict("conch.providers.RAW_FNS", {"openai": model}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = fanout.run_envelope(envelope)
        self.assertTrue(result["ok"], result)
        self.assertEqual(model.offered[0], ["glob", "grep", "local_shell", "read_file"])
        self.assertFalse((self.root / "w.txt").exists())
        self.assertFalse((self.root / "t.txt").exists())
        self.assertIn("gamma", result["summary"])
        self.assertIn("explore agents are read-only", result["summary"])


if __name__ == "__main__":
    unittest.main()
