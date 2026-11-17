"""A skill's ``tools:`` line is honoured when the skill is invoked in the
main session (``/skill`` and ``skill_manage use``), not only inside
``delegate_task``.

Observed before this plumbing existed: with a local model (minimal tool
profile) ``/skill capitol-frontend`` ran with ``capitol_control`` filtered
out; in a shell-only install the model spent 25 rounds grepping
site-packages and reading credential files to reach the gateway by hand.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.skills import (
    build_skills_context,
    describe_missing_skill_tools,
    get_skill,
    missing_skill_tools,
    render_skill,
    skill_context_line,
    skill_one_liner,
)
from conch.tooling import (
    PINNED_TOOL_NAMES,
    SkillManageClient,
    ToolRuntimeState,
    ensure_tools_active,
    select_relevant_tools,
)

SKILL_MD = """---
name: gateway-work
description: Drive the gateway from a form. Use when the user asks to wire a console to the gateway, start runs, or watch events. Covers discovery and verification.
tools: local_shell, capitol_control
---
1. Call capitol_control op=discover.
2. Report the card.
"""


def _tool(name):
    return {"function": {"name": name, "description": f"{name} tool",
                         "parameters": {"type": "object", "properties": {}}}}


class SkillsDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(self._tmp.name) / "conch" / "skills"
        self.dir.mkdir(parents=True)
        (self.dir / "gateway-work.md").write_text(SKILL_MD)


class TestMissingSkillTools(SkillsDirCase):
    def test_unknown_availability_reports_nothing(self):
        skill = get_skill("gateway-work")
        self.assertEqual(missing_skill_tools(skill, None), [])

    def test_tool_map_and_name_sets_both_work(self):
        skill = get_skill("gateway-work")
        self.assertEqual(missing_skill_tools(skill, {"local_shell": object()}),
                         ["capitol_control"])
        self.assertEqual(missing_skill_tools(skill, {"local_shell", "capitol_control"}), [])

    def test_description_names_the_fix(self):
        text = describe_missing_skill_tools("gateway-work", ["capitol_control"])
        self.assertIn("capitol_control", text)
        self.assertIn("/install works", text)
        self.assertIn("capitol_base_url", text)


class TestSlashSkillFailsClosed(SkillsDirCase):
    def _run(self, cmd, tool_map):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from conch.commands import handle_slash_command
            result = handle_slash_command(cmd, {}, "custom", "m", lambda v: None,
                                          tool_map=tool_map)
        return result, out.getvalue()

    def test_missing_tool_sends_nothing_to_the_model(self):
        result, output = self._run("/skill gateway-work build it",
                                   {"local_shell": object()})
        self.assertIsNone(result)
        self.assertIn("capitol_control", output)
        self.assertIn("/install works", output)
        self.assertIn("Nothing was sent to the model", output)

    def test_available_tools_are_handed_to_the_session(self):
        result, _ = self._run("/skill gateway-work build it",
                              {"local_shell": object(), "capitol_control": object()})
        self.assertEqual(result[0], "user_prompt")
        self.assertIn("op=discover", result[1])
        self.assertEqual(result[2]["skill"], "gateway-work")
        self.assertEqual(result[2]["tools"], ["local_shell", "capitol_control"])
        self.assertEqual(result[2]["rounds"], 0)  # no rounds: line → keep the session default

    def test_declared_rounds_ride_along_for_the_session_budget(self):
        (self.dir / "long-arc.md").write_text(
            "---\nname: long-arc\ntools: local_shell\nrounds: 40\n---\nbody"
        )
        result, _ = self._run("/skill long-arc go", {"local_shell": object()})
        self.assertEqual(result[2]["rounds"], 40)

    def test_unknown_availability_keeps_the_old_behaviour(self):
        result, _ = self._run("/skill gateway-work build it", None)
        self.assertEqual(result[0], "user_prompt")

    def test_skills_listing_flags_what_the_session_lacks(self):
        _, output = self._run("/skills", {"local_shell": object()})
        self.assertIn("needs: capitol_control (not in this session)", output)
        # one-liner, not the whole trigger-rich description
        self.assertIn("Drive the gateway from a form.", output)
        self.assertNotIn("Covers discovery and verification", output)


class TestEnsureToolsActive(unittest.TestCase):
    def _state(self):
        all_tools = [_tool("local_shell"), _tool("capitol_control"), _tool("gh_search")]
        return ToolRuntimeState(
            all_tools=all_tools,
            tool_map={t["function"]["name"]: object() for t in all_tools},
            tools=[_tool("local_shell")],  # minimal profile dropped the rest
        )

    def test_profile_hidden_tool_is_activated_and_pinned(self):
        state = self._state()
        added = ensure_tools_active(state, ["local_shell", "capitol_control"])
        self.assertEqual(added, ["capitol_control"])
        names = [t["function"]["name"] for t in state.tools]
        self.assertEqual(names, ["local_shell", "capitol_control"])
        self.assertEqual(state.pinned_tools, {"local_shell", "capitol_control"})
        self.assertTrue(state.needs_tool_refresh)

    def test_unloaded_tool_is_not_invented(self):
        state = self._state()
        self.assertEqual(ensure_tools_active(state, ["fleet_delegate"]), [])
        self.assertNotIn("fleet_delegate", state.pinned_tools)

    def test_none_state_is_a_noop(self):
        self.assertEqual(ensure_tools_active(None, ["x"]), [])

    def test_pinned_tools_survive_the_per_request_cap(self):
        filler = [_tool(f"tool_{i}") for i in range(30)]
        tools = filler + [_tool("capitol_control")]
        without = select_relevant_tools(tools, "continue please", 5)
        self.assertNotIn("capitol_control", {t["function"]["name"] for t in without})
        kept = select_relevant_tools(tools, "continue please", 5,
                                     pinned_names={"capitol_control"})
        self.assertIn("capitol_control", {t["function"]["name"] for t in kept})
        self.assertLessEqual(len(kept), 5 + len(PINNED_TOOL_NAMES))


class TestSkillManageUseActivates(SkillsDirCase):
    def test_use_enables_declared_tools_and_reports_missing(self):
        all_tools = [_tool("local_shell"), _tool("capitol_control")]
        state = ToolRuntimeState(
            all_tools=all_tools,
            tool_map={"local_shell": object(), "capitol_control": object()},
            tools=[_tool("local_shell")],
        )
        client = SkillManageClient()
        client.bind_state(state)
        result = client.call_tool("skill_manage", {"action": "use", "name": "gateway-work"})
        text = result["content"][0]["text"]
        self.assertIn("enabled for this session: capitol_control", text)
        self.assertIn("capitol_control", {t["function"]["name"] for t in state.tools})

        bare = ToolRuntimeState(all_tools=[_tool("local_shell")],
                                tool_map={"local_shell": object()},
                                tools=[_tool("local_shell")])
        client.bind_state(bare)
        text = client.call_tool("skill_manage", {"action": "use", "name": "gateway-work"})["content"][0]["text"]
        self.assertIn("needs tools this session does not have: capitol_control", text)
        self.assertIn("Tell the user plainly", text)

    def test_unbound_client_behaves_as_before(self):
        client = SkillManageClient()
        text = client.call_tool("skill_manage", {"action": "use", "name": "gateway-work"})["content"][0]["text"]
        self.assertIn("op=discover", text)
        self.assertNotIn("enabled for this session", text)


class TestSkillSummaries(SkillsDirCase):
    def test_one_liner_is_the_first_sentence(self):
        skill = get_skill("gateway-work")
        self.assertEqual(skill_one_liner(skill), "Drive the gateway from a form.")

    def test_context_line_keeps_the_use_when_triggers(self):
        skill = get_skill("gateway-work")
        line = skill_context_line(skill)
        self.assertIn("Use when the user asks to wire a console", line)
        self.assertIn("Use when", build_skills_context())

    def test_render_announces_asset_trees(self):
        # a directory-form (package-style) skill with a templates tree
        # beside its SKILL.md
        root = Path(self._tmp.name) / "builtin"
        skill_dir = root / "with-assets"
        (skill_dir / "templates" / "console").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: with-assets\n---\nbody")
        (skill_dir / "templates" / "console" / "index.html").write_text("<html></html>")
        (skill_dir / "templates" / "console" / "app.js").write_text("export {}")
        with patch("conch.skills.builtin_skills_dir", return_value=root):
            rendered = render_skill(get_skill("with-assets"))
        self.assertIn("[Skill assets:", rendered)
        self.assertIn("templates/ (2 files)", rendered)
        self.assertIn("do not retype them", rendered)


if __name__ == "__main__":
    unittest.main()
