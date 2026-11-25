"""Coding guidance in the chat system prompt: present inside a git
repository, absent outside one, and stable across rebuilds."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.config import reset_project_context_cache
from conch.prompts import CODING_GUIDANCE, build_coding_guidance

_CONFIG = {"repo_map": "false"}


class TestBuildCodingGuidance(unittest.TestCase):
    def test_mentions_grep_glob_only_when_both_present(self):
        with_tools = build_coding_guidance({"grep", "glob", "read_file"})
        self.assertIn("grep tool", with_tools)
        self.assertIn("glob tool", with_tools)
        self.assertNotIn("rg (ripgrep)", with_tools)
        for names in (None, {"read_file"}, {"grep"}):
            text = build_coding_guidance(names)
            self.assertIn("rg (ripgrep) through local_shell", text)
            self.assertNotIn("grep tool", text)

    def test_covers_core_practices_concisely(self):
        text = build_coding_guidance()
        for phrase in ("Read a file before editing", "minimal",
                       "package.json", "Makefile", "Never claim tests pass",
                       "gh pr create", "never force-push"):
            self.assertIn(phrase, text)
        self.assertLess(len(text.split()), 250)
        self.assertNotIn("{search}", text)
        self.assertIn("{search}", CODING_GUIDANCE)


class TestCodingGuidanceInPrompt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        reset_project_context_cache()
        self.addCleanup(reset_project_context_cache)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.plain = Path(self._tmp.name) / "plain"
        self.plain.mkdir()

    def _prompt(self, cwd):
        from conch.app import _build_system_prompt
        with patch("os.getcwd", return_value=str(cwd)):
            return _build_system_prompt("base", config=_CONFIG)

    def test_present_in_git_repo(self):
        prompt = self._prompt(self.repo)
        self.assertIn("Working in a code repository:", prompt)
        self.assertEqual(prompt, self._prompt(self.repo))

    def test_absent_outside_git_repo(self):
        self.assertNotIn("Working in a code repository:", self._prompt(self.plain))

    def test_project_instructions_injected_and_stable(self):
        (self.repo / "CLAUDE.md").write_text("Run make check before finishing.")
        first = self._prompt(self.repo)
        (self.repo / "CLAUDE.md").write_text("changed mid-session")
        second = self._prompt(self.repo)
        self.assertIn("--- CLAUDE.md ---\nRun make check before finishing.", first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
