"""Tests for the skill system (plan 4.1) and skill-scoped subagents (plan 4.2)."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.skills import (
    build_skills_context,
    delete_skill,
    get_skill,
    load_skills,
    parse_skill,
    render_skill,
    save_skill,
)
from conch.tooling import (
    DelegateTaskClient,
    SkillManageClient,
    ToolRuntimeState,
)


SKILL_MD = """---
name: deploy-check
description: Verify a deployment is healthy
tools: local_shell, public_api
model: gpt-4o-mini
rounds: 6
---
1. Check the pods: `kubectl get pods`
2. Curl the health endpoint.
3. Report anything not Running.
"""


class SkillsDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(self._tmp.name) / "conch" / "skills"
        self.dir.mkdir(parents=True)

    def _write(self, name, text):
        (self.dir / f"{name}.md").write_text(text)


class TestParseSkill(unittest.TestCase):
    def test_full_frontmatter(self):
        skill = parse_skill(SKILL_MD, "fallback")
        self.assertEqual(skill["name"], "deploy-check")
        self.assertEqual(skill["description"], "Verify a deployment is healthy")
        self.assertEqual(skill["tools"], ["local_shell", "public_api"])
        self.assertEqual(skill["model"], "gpt-4o-mini")
        self.assertEqual(skill["rounds"], 6)
        self.assertIn("kubectl get pods", skill["body"])

    def test_body_only_file(self):
        skill = parse_skill("Just do the thing carefully.", "simple")
        self.assertEqual(skill["name"], "simple")
        self.assertIsNone(skill["tools"], "no tools line = all tools")
        self.assertEqual(skill["body"], "Just do the thing carefully.")

    def test_tools_all_means_none(self):
        skill = parse_skill("---\nname: x\ntools: all\n---\nbody", "x")
        self.assertIsNone(skill["tools"])

    def test_empty_body_rejected(self):
        self.assertIsNone(parse_skill("---\nname: x\n---\n", "x"))
        self.assertIsNone(parse_skill("", "x"))

    def test_invalid_name_rejected(self):
        self.assertIsNone(parse_skill("---\nname: Bad Name!\n---\nbody", "ok"))


class TestSkillStore(SkillsDirTestCase):
    def test_load_skills(self):
        self._write("deploy-check", SKILL_MD)
        skills = load_skills()
        self.assertIn("deploy-check", skills)

    def test_missing_dir_still_serves_builtin_skills(self):
        # Shipped skills (conch/skills_data/) load without any user dir;
        # every one carries the builtin marker and its directory.
        self.dir.rmdir()
        skills = load_skills()
        self.assertIn("capitol", skills)
        self.assertIn("pack-author", skills)
        self.assertTrue(all(
            skill.get("builtin") and skill.get("dir")
            for skill in skills.values()
        ))

    def test_save_and_get_roundtrip(self):
        path = save_skill("release", "Cut a release", "1. tag\n2. push",
                          tools=["local_shell"], model="qwen3-8b")
        self.assertTrue(path.is_file())
        skill = get_skill("release")
        self.assertEqual(skill["tools"], ["local_shell"])
        self.assertEqual(skill["model"], "qwen3-8b")
        self.assertIn("1. tag", skill["body"])

    def test_save_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            save_skill("Bad Name", "", "body")
        with self.assertRaises(ValueError):
            save_skill("okname", "", "   ")

    def test_delete(self):
        save_skill("temp", "", "body")
        self.assertTrue(delete_skill("temp"))
        self.assertIsNone(get_skill("temp"))
        self.assertFalse(delete_skill("temp"))

    def test_render_bounded(self):
        save_skill("big", "", "x" * 50000)
        rendered = render_skill(get_skill("big"))
        self.assertLess(len(rendered), 9000)
        self.assertIn("[skill truncated]", rendered)

    def test_skills_context_block(self):
        # Shipped skills are always advertised; user skills join them.
        self.assertIn("capitol", build_skills_context())
        self._write("deploy-check", SKILL_MD)
        ctx = build_skills_context()
        self.assertIn("deploy-check", ctx)
        self.assertIn("skill_manage", ctx)

    def test_context_injected_into_system_prompt(self):
        self._write("deploy-check", SKILL_MD)
        from conch.app import _build_system_prompt
        prompt = _build_system_prompt("base", config={"repo_map": "false"})
        self.assertIn("deploy-check", prompt)


class TestSkillManageTool(SkillsDirTestCase):
    def _client(self, answers=(), interactive=True):
        answers = list(answers)

        def scripted_input(prompt):
            if not answers:
                raise EOFError
            return answers.pop(0)

        client = SkillManageClient()
        client.configure(interactive=interactive, input_fn=scripted_input)
        return client

    def _call(self, client, args):
        return client.call_tool("skill_manage", args)["content"][0]["text"]

    def test_list_shows_builtin_skills_without_user_dir(self):
        text = self._call(self._client(), {"action": "list"})
        self.assertIn("capitol", text)
        self.assertIn("pack-author", text)

    def test_list_shows_scope(self):
        self._write("deploy-check", SKILL_MD)
        text = self._call(self._client(), {"action": "list"})
        self.assertIn("deploy-check", text)
        self.assertIn("local_shell, public_api", text)
        self.assertIn("model=gpt-4o-mini", text)

    def test_use_injects_body(self):
        self._write("deploy-check", SKILL_MD)
        text = self._call(self._client(), {"action": "use", "name": "deploy-check"})
        self.assertIn("[Skill: deploy-check]", text)
        self.assertIn("kubectl get pods", text)
        self.assertIn("Follow this skill's procedure", text)

    def test_use_unknown(self):
        self.assertIn("Unknown skill", self._call(self._client(), {"action": "use", "name": "zzz"}))

    def test_save_requires_confirmation(self):
        client = self._client(answers=["y"])
        with patch("sys.stdout", io.StringIO()):
            text = self._call(client, {
                "action": "save", "name": "new-skill",
                "description": "d", "body": "1. do it",
                "tools": ["local_shell"],
            })
        self.assertIn("Saved skill 'new-skill'", text)
        self.assertIsNotNone(get_skill("new-skill"))

    def test_save_declined_writes_nothing(self):
        client = self._client(answers=["n"])
        with patch("sys.stdout", io.StringIO()):
            text = self._call(client, {
                "action": "save", "name": "new-skill", "body": "1. do it",
            })
        self.assertIn("declined", text.lower())
        self.assertIsNone(get_skill("new-skill"))

    def test_save_refused_non_interactive(self):
        client = self._client(interactive=False)
        text = self._call(client, {"action": "save", "name": "x", "body": "b"})
        self.assertIn("interactive", text)
        self.assertIsNone(get_skill("x"))

    def test_save_validates_input(self):
        client = self._client(answers=["y"])
        self.assertIn("Error", self._call(client, {"action": "save", "name": "Bad Name", "body": "b"}))
        self.assertIn("Error", self._call(client, {"action": "save", "name": "ok", "body": ""}))

    def test_delete_confirmed(self):
        self._write("deploy-check", SKILL_MD)
        client = self._client(answers=["y"])
        text = self._call(client, {"action": "delete", "name": "deploy-check"})
        self.assertIn("Deleted", text)
        self.assertIsNone(get_skill("deploy-check"))

    def test_use_requests_the_skills_round_budget(self):
        self._write("deploy-check", SKILL_MD)  # rounds: 6
        state = ToolRuntimeState(all_tools=[], tool_map={}, tools=[])
        client = self._client()
        client.bind_state(state)
        text = self._call(client, {"action": "use", "name": "deploy-check"})
        self.assertEqual(state.requested_tool_rounds, 6)
        self.assertIn("budget of 6 tool rounds", text)
        # a smaller skill never lowers an earlier request
        self._write("tiny", "---\nname: tiny\nrounds: 2\n---\nbody\n")
        self._call(client, {"action": "use", "name": "tiny"})
        self.assertEqual(state.requested_tool_rounds, 6)

    def test_use_without_rounds_requests_nothing(self):
        self._write("plain", "---\nname: plain\n---\nbody\n")
        state = ToolRuntimeState(all_tools=[], tool_map={}, tools=[])
        client = self._client()
        client.bind_state(state)
        self._call(client, {"action": "use", "name": "plain"})
        self.assertEqual(state.requested_tool_rounds, 0)


class TestRoundsBudgetRaisedMidTurn(SkillsDirTestCase):
    """NL-routed ``skill_manage use`` honours the skill's ``rounds:`` the
    way /skill does: the running turn's round budget rises to it (never
    falls), and the session keeps the raised budget afterwards."""

    def _turn(self, skill_rounds, max_tool_rounds, *, session=None):
        from conch.runtime import chat_turn

        self._write(
            "long-arc",
            f"---\nname: long-arc\nrounds: {skill_rounds}\n---\n"
            "1. scaffold\n2. verify\n3. report\n",
        )
        state = ToolRuntimeState(
            all_tools=[], tool_map={}, tools=[_tool("skill_manage"), _tool("noop")],
        )
        skills = SkillManageClient()
        skills.configure(interactive=False)
        skills.bind_state(state)
        clients = {"skill_manage": skills, "noop": _FakeShell()}
        seen = {"tool_rounds": 0}

        def raw_fn(config, messages, tools):
            if tools is None:  # exhaustion summary
                return {"content": "ran out", "tool_calls": None,
                        "_usage": {"input_tokens": 1, "output_tokens": 1},
                        "_model": "t"}
            seen["tool_rounds"] += 1
            n = seen["tool_rounds"]
            if n == 1:
                call = {"name": "skill_manage",
                        "arguments": '{"action": "use", "name": "long-arc"}'}
            elif n < 5:
                call = {"name": "noop", "arguments": "{}"}
            else:
                return {"content": "done after five rounds", "tool_calls": None,
                        "_usage": {"input_tokens": 1, "output_tokens": 1},
                        "_model": "t"}
            return {"content": "", "_model": "t",
                    "_usage": {"input_tokens": 1, "output_tokens": 1},
                    "tool_calls": [{"id": f"c{n}", "type": "function",
                                    "function": call}]}

        messages = [{"role": "user", "content": "do the long arc"}]
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), patch("sys.stdout", io.StringIO()):
            if session is not None:
                session.attach_clients(clients, chat_state=state, bind=False)
                session.config.update({"provider": "openai"})
                with patch.object(session, "raw_fn", return_value=raw_fn):
                    reply, _ = session.run_turn(messages)
            else:
                reply, _ = chat_turn(
                    config={}, provider="openai", raw_fn=raw_fn,
                    messages=messages, tools=state.tools, tool_map={},
                    builtin_clients=clients, max_tool_rounds=max_tool_rounds,
                    chat_state=state,
                )
        return reply, seen["tool_rounds"], stderr.getvalue()

    def test_turn_budget_rises_to_the_skills_rounds(self):
        # Without the raise, max_tool_rounds=2 would exhaust after two rounds.
        reply, rounds, err = self._turn(skill_rounds=6, max_tool_rounds=2)
        self.assertEqual(reply, "done after five rounds")
        self.assertEqual(rounds, 5)
        self.assertIn("tool round budget raised to 6 for this turn", err)

    def test_turn_budget_is_never_lowered(self):
        reply, rounds, err = self._turn(skill_rounds=1, max_tool_rounds=3)
        self.assertTrue(reply.startswith("ran out"), reply)  # exhausted at 3, not cut to 1
        self.assertEqual(rounds, 3)
        self.assertNotIn("raised", err)
        self.assertIn("Tool round budget (3) exhausted", err)

    def test_session_keeps_the_raised_budget(self):
        from conch.session import AgentSession, SessionBudgets

        session = AgentSession(
            {"provider": "openai"}, budgets=SessionBudgets(max_tool_rounds=2),
        )
        reply, rounds, _ = self._turn(skill_rounds=6, max_tool_rounds=2, session=session)
        self.assertEqual(reply, "done after five rounds")
        self.assertEqual(session.budgets.max_tool_rounds, 6)


class _FakeShell:
    name = "local_shell"

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}


def _tool(name):
    return {"function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


class TestSkillScopedDelegate(SkillsDirTestCase):
    def _delegate(self, config=None):
        client = DelegateTaskClient()
        all_tools = [_tool("local_shell"), _tool("public_api"),
                     _tool("gh_search"), _tool("delegate_task"),
                     _tool("skill_manage")]
        state = ToolRuntimeState(all_tools=all_tools, tool_map={},
                                 tools=[_tool("local_shell"), _tool("delegate_task")])
        builtins = {"local_shell": _FakeShell(), "delegate_task": client,
                    "skill_manage": SkillManageClient()}
        client.bind(config or {"provider": "openai", "chat_model": "gpt-4o",
                               "model": "gpt-4o"}, state, builtins)
        return client

    def test_skill_scopes_prompt_tools_model_and_rounds(self):
        self._write("deploy-check", SKILL_MD)
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, max_tool_rounds=25, **kw):
            seen.update(config=config, messages=messages, tools=tools,
                        clients=builtin_clients, rounds=max_tool_rounds)
            return "checked; all pods Running", {}

        client = self._delegate()
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            result = client.call_tool("delegate_task", {
                "task": "verify staging", "skill": "deploy-check",
            })
        text = result["content"][0]["text"]
        self.assertIn("checked; all pods Running", text)
        # scoped system prompt contains the skill body
        self.assertIn("kubectl get pods", seen["messages"][0]["content"])
        # only the skill's allowed tools, drawn from all_tools
        names = {t["function"]["name"] for t in seen["tools"]}
        self.assertEqual(names, {"local_shell", "public_api"})
        self.assertNotIn("skill_manage", seen["clients"])
        # model preference and round budget from the skill
        self.assertEqual(seen["config"]["chat_model"], "gpt-4o-mini")
        self.assertEqual(seen["rounds"], 6)

    def test_skill_tools_never_include_excluded(self):
        self._write("sneaky", "---\nname: sneaky\ntools: local_shell, delegate_task, conch_config\n---\nbody")
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools, *a, **kw):
            seen["tools"] = tools
            return "ok", {}

        client = self._delegate()
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t", "skill": "sneaky"})
        names = {t["function"]["name"] for t in seen["tools"]}
        self.assertEqual(names, {"local_shell"},
                         "recursion/self-management tools stay excluded")

    def test_unknown_skill_errors_without_running(self):
        client = self._delegate()
        with patch("conch.runtime.chat_turn",
                   side_effect=AssertionError("must not run")):
            result = client.call_tool("delegate_task", {"task": "t", "skill": "nope"})
        self.assertIn("unknown skill", result["content"][0]["text"].lower())

    def test_ollama_skill_model_capability_gated(self):
        self._write("gated", "---\nname: gated\nmodel: not-installed\n---\nbody")
        seen = {}

        def fake_chat_turn(config, *a, **kw):
            seen["config"] = config
            return "ok", {}

        client = self._delegate(config={
            "provider": "ollama", "chat_model": "qwen3.6:27b", "model": "qwen3.6:27b",
        })
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("conch.providers.validate_ollama_model",
                   return_value=(False, "not installed")), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t", "skill": "gated"})
        self.assertEqual(seen["config"]["chat_model"], "qwen3.6:27b",
                         "ungated skill model must fall back to parent")

    def test_no_skill_behaves_as_before(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools, *a, **kw):
            seen["tools"] = tools
            seen["messages"] = messages
            return "ok", {}

        client = self._delegate()
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "plain task"})
        names = {t["function"]["name"] for t in seen["tools"]}
        self.assertEqual(names, {"local_shell"})
        self.assertNotIn("Skill:", seen["messages"][0]["content"])


class TestSkillSlashCommands(SkillsDirTestCase):
    def _run(self, cmd):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from conch.commands import handle_slash_command
            result = handle_slash_command(cmd, {}, "ollama", "m", lambda v: None)
        return result, out.getvalue()

    def test_skills_lists(self):
        self._write("deploy-check", SKILL_MD)
        result, output = self._run("/skills")
        self.assertIsNone(result)
        self.assertIn("deploy-check", output)

    def test_skill_returns_user_prompt(self):
        self._write("deploy-check", SKILL_MD)
        result, _ = self._run("/skill deploy-check the staging cluster")
        self.assertEqual(result[0], "user_prompt")
        self.assertIn("kubectl get pods", result[1])
        self.assertIn("Task: the staging cluster", result[1])

    def test_skill_unknown(self):
        result, output = self._run("/skill nope")
        self.assertIsNone(result)
        self.assertIn("Unknown skill", output)


if __name__ == "__main__":
    unittest.main()
