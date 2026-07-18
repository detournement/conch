"""Tests for repo-map orientation context (plan 3.2)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.repomap import (
    REPO_MAP_BUDGET_CHARS,
    build_repo_map,
    find_git_root,
    get_repo_map,
    _map_cache,
)


PY_FILE = '''\
"""Module docstring."""

class OrderProcessor:
    def _private_helper(self):
        pass

def process_orders(batch):
    pass

def _internal():
    pass
'''

JS_FILE = '''\
export function renderDashboard() {}
export class ApiClient {}
export const API_BASE = "/v1";
function helper() {}
'''


class RepoTreeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / "README.md").write_text("# proj")
        (self.root / "src" / "orders.py").write_text(PY_FILE)
        (self.root / "src" / "dashboard.js").write_text(JS_FILE)
        (self.root / "notes.bin").write_bytes(b"\x00\x01")
        _map_cache.clear()
        self.addCleanup(_map_cache.clear)


class TestFindGitRoot(RepoTreeTestCase):
    def test_found_from_subdir(self):
        self.assertEqual(find_git_root(str(self.root / "src")), self.root.resolve())

    def test_none_outside_repo(self):
        self.assertIsNone(find_git_root(self._tmp.name))


class TestBuildRepoMap(RepoTreeTestCase):
    def test_map_lists_ranked_files_and_symbols(self):
        result = build_repo_map(str(self.root))
        self.assertIn("Repository map", result)
        self.assertIn("src/orders.py", result)
        self.assertIn("OrderProcessor", result)
        self.assertIn("process_orders", result)
        self.assertIn("renderDashboard", result)
        self.assertIn("ApiClient", result)

    def test_private_symbols_skipped(self):
        result = build_repo_map(str(self.root))
        self.assertNotIn("_private_helper", result)
        self.assertNotIn("_internal", result)

    def test_outside_repo_empty(self):
        self.assertEqual(build_repo_map(self._tmp.name), "")

    def test_budget_respected(self):
        for i in range(300):
            (self.root / "src" / f"module_{i:03d}.py").write_text(
                f"def function_{i}():\n    pass\n"
            )
        result = build_repo_map(str(self.root))
        self.assertLessEqual(len(result), REPO_MAP_BUDGET_CHARS + 200)
        self.assertIn("more files", result)

    def test_cache_reused(self):
        first = get_repo_map(str(self.root))
        (self.root / "src" / "later.py").write_text("def added_later(): pass\n")
        second = get_repo_map(str(self.root))
        self.assertEqual(first, second, "map is built once per session")


class TestRepoMapInPrompt(RepoTreeTestCase):
    def test_injected_when_enabled(self):
        from conch.app import _build_system_prompt
        with patch("os.getcwd", return_value=str(self.root)):
            prompt = _build_system_prompt("base", config={})
        self.assertIn("Repository map", prompt)

    def test_disabled_by_config(self):
        from conch.app import _build_system_prompt
        with patch("os.getcwd", return_value=str(self.root)):
            prompt = _build_system_prompt("base", config={"repo_map": "false"})
        self.assertNotIn("Repository map", prompt)


if __name__ == "__main__":
    unittest.main()
