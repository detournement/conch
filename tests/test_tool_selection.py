"""Tests for relevance-based tool selection and config-defined profiles
(plan 1.6)."""

import unittest
from unittest.mock import patch

from conch.providers import PROVIDER_TOOL_LIMITS
from conch.tooling import (
    PINNED_TOOL_NAMES,
    config_profiles,
    list_profiles,
    profile_tool_filter,
    select_relevant_tools,
)


def _tool(name, description=""):
    return {"function": {"name": name, "description": description,
                         "parameters": {"type": "object", "properties": {}}}}


class TestSelectRelevantTools(unittest.TestCase):
    def test_under_limit_unchanged(self):
        tools = [_tool("a"), _tool("b")]
        self.assertEqual(select_relevant_tools(tools, "anything", 10), tools)

    def test_pinned_always_survive(self):
        tools = [_tool(f"mcp_{i}") for i in range(20)] + [_tool("local_shell")]
        result = select_relevant_tools(tools, "", 5)
        names = [t["function"]["name"] for t in result]
        self.assertIn("local_shell", names)
        self.assertEqual(len(result), 5)

    def test_relevant_tool_beats_list_order(self):
        tools = [_tool(f"filler_{i}", "does nothing interesting") for i in range(15)]
        tools.append(_tool("jira_create_issue", "Create a Jira issue in a project"))
        result = select_relevant_tools(tools, "please create a jira issue for the bug", 5)
        names = [t["function"]["name"] for t in result]
        self.assertIn("jira_create_issue", names,
                      "relevance must beat list order")

    def test_empty_query_keeps_original_order(self):
        tools = [_tool(f"t{i}") for i in range(10)]
        result = select_relevant_tools(tools, "", 4)
        self.assertEqual([t["function"]["name"] for t in result],
                         ["t0", "t1", "t2", "t3"])

    def test_ollama_limit_is_small(self):
        self.assertEqual(PROVIDER_TOOL_LIMITS["ollama"], 12)


class TestConfigProfiles(unittest.TestCase):
    def test_parses_profile_keys(self):
        config = {"provider": "ollama", "profile_research": "github, jira"}
        profiles = config_profiles(config)
        self.assertIn("research", profiles)
        self.assertEqual(profiles["research"]["groups"], {"github", "jira"})

    def test_ignores_other_keys(self):
        self.assertEqual(config_profiles({"provider": "ollama", "model": "x"}), {})
        self.assertEqual(config_profiles(None), {})

    def test_merged_into_list_profiles(self):
        config = {"profile_research": "github"}
        with patch("conch.tooling.load_tool_prefs", return_value={}):
            profiles = list_profiles(config)
        self.assertIn("research", profiles)
        self.assertIn("minimal", profiles)  # builtins still present


class TestProfileToolFilter(unittest.TestCase):
    def _tools(self):
        tools = [_tool("local_shell"), _tool("save_memory"),
                 _tool("gh_1"), _tool("gh_2"), _tool("slack_1")]
        tool_map = {
            "gh_1": type("C", (), {"name": "github"})(),
            "gh_2": type("C", (), {"name": "github"})(),
            "slack_1": type("C", (), {"name": "slack"})(),
        }
        return tools, tool_map

    def test_minimal_keeps_only_pinned_groups(self):
        tools, tool_map = self._tools()
        with patch("conch.tooling.load_tool_prefs", return_value={}):
            filtered, desc = profile_tool_filter("minimal", tools, tool_map)
        names = {t["function"]["name"] for t in filtered}
        self.assertEqual(names, {"local_shell", "save_memory"})

    def test_config_profile_selects_groups(self):
        tools, tool_map = self._tools()
        config = {"profile_gh": "github"}
        with patch("conch.tooling.load_tool_prefs", return_value={}):
            filtered, desc = profile_tool_filter("gh", tools, tool_map, config)
        names = {t["function"]["name"] for t in filtered}
        self.assertIn("gh_1", names)
        self.assertNotIn("slack_1", names)
        self.assertIn("local_shell", names, "pinned tools always included")

    def test_unknown_profile(self):
        tools, tool_map = self._tools()
        with patch("conch.tooling.load_tool_prefs", return_value={}):
            filtered, desc = profile_tool_filter("nope", tools, tool_map)
        self.assertIsNone(filtered)
        self.assertIn("Unknown profile", desc)

    def test_never_persists_prefs(self):
        tools, tool_map = self._tools()
        with patch("conch.tooling.load_tool_prefs", return_value={}), \
             patch("conch.tooling.save_tool_prefs") as mock_save:
            profile_tool_filter("minimal", tools, tool_map)
        mock_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
