"""Tests for conch.tooling — profiles, tool groups, filtering."""

import unittest

from conch.tooling import (
    BUILTIN_PROFILES,
    PINNED_TOOL_NAMES,
    cap_tools,
    group_tools,
    list_profiles,
    tool_group,
)


def _make_tool(name):
    return {"type": "function", "function": {"name": name, "description": f"Tool {name}"}}


class TestToolGroup(unittest.TestCase):
    def test_builtin_tools(self):
        self.assertEqual(tool_group("local_shell", {}), "local_shell")
        self.assertEqual(tool_group("manage_tools", {}), "manage_tools")
        self.assertEqual(tool_group("save_memory", {}), "save_memory")

    def test_unknown_tool(self):
        result = tool_group("some_tool", {})
        self.assertEqual(result, "unknown")


class TestGroupTools(unittest.TestCase):
    def test_groups_by_name(self):
        tools = [_make_tool("local_shell"), _make_tool("save_memory")]
        groups = group_tools(tools, {})
        self.assertIn("local_shell", groups)
        self.assertIn("save_memory", groups)


class TestCapTools(unittest.TestCase):
    def test_no_cap_when_under_limit(self):
        tools = [_make_tool(f"t{i}") for i in range(5)]
        result = cap_tools(tools, max_tools=10)
        self.assertEqual(len(result), 5)

    def test_caps_at_limit(self):
        tools = [_make_tool(f"t{i}") for i in range(20)]
        result = cap_tools(tools, max_tools=10)
        self.assertEqual(len(result), 10)

    def test_pinned_tools_preserved(self):
        tools = [_make_tool(f"t{i}") for i in range(20)]
        tools.append(_make_tool("local_shell"))
        result = cap_tools(tools, max_tools=10)
        names = {t["function"]["name"] for t in result}
        self.assertIn("local_shell", names)


class TestProfiles(unittest.TestCase):
    def test_builtin_profiles_exist(self):
        self.assertIn("minimal", BUILTIN_PROFILES)
        self.assertIn("dev", BUILTIN_PROFILES)
        self.assertIn("comms", BUILTIN_PROFILES)
        self.assertIn("full", BUILTIN_PROFILES)

    def test_list_profiles_includes_builtins(self):
        profiles = list_profiles()
        self.assertIn("minimal", profiles)
        self.assertIn("full", profiles)

    def test_full_profile_enables_all(self):
        self.assertEqual(BUILTIN_PROFILES["full"]["groups"], "__all__")


if __name__ == "__main__":
    unittest.main()
