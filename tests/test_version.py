"""Tests for the version string: single-sourced in conch.__version__,
shown by --version and in /status."""

import contextlib
import io
import re
import sys
import unittest
from unittest.mock import patch

import conch


class TestVersion(unittest.TestCase):
    def test_version_is_pep440_ish(self):
        self.assertRegex(conch.__version__, r"^\d+\.\d+\.\d+$")

    def test_version_at_least_published(self):
        # PyPI has conch-shell 0.4.0; the scheme must never go backwards.
        major, minor, patch = (int(p) for p in conch.__version__.split("."))
        self.assertGreaterEqual((major, minor, patch), (0, 4, 0))

    def test_pyproject_single_sources_version(self):
        text = open("pyproject.toml").read()
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = {attr = "conch.__version__"}', text)
        self.assertNotRegex(text, r'^version = "\d', "no hardcoded version left")

    def test_chat_entrypoint_version_flag(self):
        from conch.app import main
        out = io.StringIO()
        with patch.object(sys, "argv", ["conch", "--version"]), \
             contextlib.redirect_stdout(out):
            main()
        self.assertEqual(out.getvalue().strip(), f"conch {conch.__version__}")

    def test_ask_entrypoint_version_flag(self):
        from conch.cli import main
        out = io.StringIO()
        with patch.object(sys, "argv", ["conch-ask", "-V"]), \
             contextlib.redirect_stdout(out):
            main()
        self.assertEqual(out.getvalue().strip(), f"conch-ask {conch.__version__}")

    def test_status_shows_version(self):
        from conch.commands import handle_slash_command
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handle_slash_command("/status", {"provider": "anthropic"},
                                 "anthropic", "claude-sonnet-4-6", lambda v: None)
        self.assertIn(conch.__version__, out.getvalue())

    def test_chat_prompt_carries_current_version(self):
        from conch.prompts import get_chat_prompt
        prompt = get_chat_prompt("openai", "gpt-4o")
        self.assertIn(f"Conch v{conch.__version__}", prompt)
        self.assertNotIn("v0.4 ", prompt)


if __name__ == "__main__":
    unittest.main()
