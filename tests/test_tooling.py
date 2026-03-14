import unittest

from conch.tooling import PINNED_TOOL_NAMES, cap_tools


class ToolingTests(unittest.TestCase):
    def test_cap_tools_preserves_pinned_builtins(self):
        tools = [
            {"function": {"name": f"tool_{i}"}} for i in range(10)
        ] + [
            {"function": {"name": name}} for name in sorted(PINNED_TOOL_NAMES)
        ]
        capped = cap_tools(tools, max_tools=3)
        names = {tool["function"]["name"] for tool in capped}
        self.assertTrue(PINNED_TOOL_NAMES.issubset(names))


if __name__ == "__main__":
    unittest.main()
