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
    format_skill_file,
    get_skill,
    load_skills,
    parse_skill,
    render_skill,
    save_skill,
    skills_dir,
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
model: qwen3-8b
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
        self.assertEqual(skill["model"], "qwen3-8b")
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

    def test_missing_dir_empty(self):
        self.dir.rmdir()
        self.assertEqual(load_skills(), {})

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
        self.assertEqual(build_skills_context(), "")
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

    def test_list_empty(self):
        self.assertIn("No skills", self._call(self._client(), {"action": "list"}))

    def test_list_shows_scope(self):
        self._write("deploy-check", SKILL_MD)
        text = self._call(self._client(), {"action": "list"})
        self.assertIn("deploy-check", text)
        self.assertIn("local_shell, public_api", text)
        self.assertIn("model=qwen3-8b", text)

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
        self.assertEqual(seen["config"]["chat_model"], "qwen3-8b")
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
