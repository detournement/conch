"""Tests for project-level context and config (plan 1.8): CONCH.md/AGENTS.md
injection and per-project .conchrc overrides."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.config import (
    PROJECT_CONTEXT_MAX_CHARS,
    find_project_rc,
    load_config,
    load_project_context,
)


class ProjectTreeTestCase(unittest.TestCase):
    """Builds tmp/repo/.git + tmp/repo/sub as a fake project tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        self.sub = self.root / "sub"
        self.sub.mkdir(parents=True)
        (self.root / ".git").mkdir()


class TestLoadProjectContext(ProjectTreeTestCase):
    def test_conch_md_found_from_subdir(self):
        (self.root / "CONCH.md").write_text("Always run tests with make test.")
        ctx = load_project_context(str(self.sub))
        self.assertIn("Always run tests with make test.", ctx)
        self.assertIn("Project instructions", ctx)

    def test_agents_md_fallback(self):
        (self.root / "AGENTS.md").write_text("Use ruff for linting.")
        ctx = load_project_context(str(self.sub))
        self.assertIn("Use ruff for linting.", ctx)

    def test_conch_md_preferred_over_agents_md(self):
        (self.sub / "CONCH.md").write_text("from conch.md")
        (self.sub / "AGENTS.md").write_text("from agents.md")
        ctx = load_project_context(str(self.sub))
        self.assertIn("from conch.md", ctx)
        self.assertNotIn("from agents.md", ctx)

    def test_nearest_file_wins(self):
        (self.root / "CONCH.md").write_text("root instructions")
        (self.sub / "CONCH.md").write_text("sub instructions")
        ctx = load_project_context(str(self.sub))
        self.assertIn("sub instructions", ctx)

    def test_walk_stops_at_git_root(self):
        (Path(self._tmp.name) / "CONCH.md").write_text("outside the repo")
        ctx = load_project_context(str(self.sub))
        self.assertEqual(ctx, "")

    def test_no_file_empty(self):
        self.assertEqual(load_project_context(str(self.sub)), "")

    def test_oversized_content_capped(self):
        (self.root / "CONCH.md").write_text("x" * (PROJECT_CONTEXT_MAX_CHARS * 3))
        ctx = load_project_context(str(self.sub))
        self.assertLess(len(ctx), PROJECT_CONTEXT_MAX_CHARS + 200)
        self.assertIn("truncated", ctx)


class TestProjectRc(ProjectTreeTestCase):
    def test_found_from_subdir(self):
        (self.root / ".conchrc").write_text("model = qwen3.6:27b\n")
        rc = find_project_rc(str(self.sub))
        self.assertEqual(rc, (self.root / ".conchrc").resolve())

    def test_none_when_absent(self):
        self.assertIsNone(find_project_rc(str(self.sub)))

    def test_overrides_global_config(self):
        (self.root / ".conchrc").write_text(
            "provider = ollama\nmodel = qwen3.6:27b\nollama_num_ctx = 16384\n"
        )
        fake_home = Path(self._tmp.name) / "home"
        (fake_home / ".config" / "conch").mkdir(parents=True)
        (fake_home / ".config" / "conch" / "config").write_text(
            "provider = anthropic\nmodel = claude-sonnet-4-6\n"
        )
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(fake_home / ".config")}), \
             patch("conch.config.Path.home", return_value=fake_home), \
             patch("os.getcwd", return_value=str(self.sub)):
            config = load_config()
        self.assertEqual(config["provider"], "ollama")
        self.assertEqual(config["model"], "qwen3.6:27b")
        self.assertEqual(config["ollama_num_ctx"], "16384")


if __name__ == "__main__":
    unittest.main()
