"""Tests for the executable tools directory (plan 2.3): executables in
~/.config/conch/tools/ become tools via --schema, invoked with JSON on stdin."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.tooling import (
    UserToolClient,
    discover_user_tools,
    inject_builtin_tools,
    tool_group,
)


GOOD_TOOL = """#!/bin/sh
if [ "$1" = "--schema" ]; then
  cat <<'EOF'
{"name": "word_count", "description": "Count words in text",
 "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"]}}
EOF
  exit 0
fi
# Normal invocation: JSON args on stdin
input=$(cat)
printf 'got: %s' "$input"
"""

BROKEN_TOOL = """#!/bin/sh
if [ "$1" = "--schema" ]; then
  echo "this is not json"
  exit 0
fi
"""

FAILING_TOOL = """#!/bin/sh
if [ "$1" = "--schema" ]; then
  echo '{"name": "always_fails", "description": "d", "parameters": {"type": "object", "properties": {}}}'
  exit 0
fi
echo "boom" >&2
exit 3
"""


class UserToolsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools_dir = Path(self._tmp.name) / "conch" / "tools"
        self.tools_dir.mkdir(parents=True)

    def _write_tool(self, filename, body, executable=True):
        path = self.tools_dir / filename
        path.write_text(body)
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path


class TestDiscoverUserTools(UserToolsTestCase):
    def test_valid_tool_discovered(self):
        self._write_tool("word_count", GOOD_TOOL)
        tools, client = discover_user_tools()
        self.assertEqual(len(tools), 1)
        fn = tools[0]["function"]
        self.assertEqual(fn["name"], "word_count")
        self.assertEqual(fn["description"], "Count words in text")
        self.assertIn("text", fn["parameters"]["properties"])

    def test_non_executable_skipped(self):
        self._write_tool("not_executable", GOOD_TOOL, executable=False)
        tools, _ = discover_user_tools()
        self.assertEqual(tools, [])

    def test_invalid_schema_skipped(self):
        import io, sys
        self._write_tool("broken", BROKEN_TOOL)
        with patch("sys.stderr", io.StringIO()):
            tools, _ = discover_user_tools()
        self.assertEqual(tools, [])

    def test_missing_dir_empty(self):
        self.tools_dir.rmdir()
        tools, _ = discover_user_tools()
        self.assertEqual(tools, [])

    def test_invocation_passes_json_on_stdin(self):
        self._write_tool("word_count", GOOD_TOOL)
        tools, client = discover_user_tools()
        result = client.call_tool("word_count", {"text": "hello world"})
        text = result["content"][0]["text"]
        self.assertIn("got:", text)
        self.assertIn("hello world", text)

    def test_nonzero_exit_reported(self):
        self._write_tool("always_fails", FAILING_TOOL)
        tools, client = discover_user_tools()
        result = client.call_tool("always_fails", {})
        text = result["content"][0]["text"]
        self.assertIn("boom", text)
        self.assertIn("exit code 3", text)

    def test_unknown_tool_error(self):
        client = UserToolClient()
        result = client.call_tool("nope", {})
        self.assertIn("unknown user tool", result["content"][0]["text"])


class TestUserToolsInjection(UserToolsTestCase):
    def test_registered_in_tool_map_with_user_group(self):
        self._write_tool("word_count", GOOD_TOOL)
        all_tools, tool_map = [], {}
        inject_builtin_tools(all_tools, tool_map, {})
        names = [t["function"]["name"] for t in all_tools]
        self.assertIn("word_count", names)
        self.assertIn("word_count", tool_map)
        self.assertEqual(tool_group("word_count", tool_map), "user")


if __name__ == "__main__":
    unittest.main()
