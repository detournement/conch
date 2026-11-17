"""Tests for per-model system-prompt template overrides (plan 1.9)."""

import tempfile
import unittest
from pathlib import Path

from conch.prompts import (
    get_ask_prompt,
    get_chat_prompt,
    resolve_prompt_override,
)


class PromptOverrideTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _template(self, name, body):
        path = Path(self._tmp.name) / name
        path.write_text(body)
        return str(path)


class TestResolvePromptOverride(PromptOverrideTestCase):
    def test_exact_model_match(self):
        path = self._template("qwen.md", "You are QwenConch.")
        config = {"chat_prompt:ollama/qwen3.6:27b": path}
        result = resolve_prompt_override("chat", "ollama", "qwen3.6:27b", config)
        self.assertEqual(result, "You are QwenConch.")

    def test_glob_pattern_match(self):
        path = self._template("qwen.md", "glob prompt")
        config = {"chat_prompt:ollama/qwen*": path}
        self.assertEqual(
            resolve_prompt_override("chat", "ollama", "qwen3.6:27b", config),
            "glob prompt",
        )
        self.assertEqual(
            resolve_prompt_override("chat", "ollama", "gpt-oss:20b", config), ""
        )

    def test_bare_provider_pattern_matches_all_models(self):
        path = self._template("local.md", "local prompt")
        config = {"chat_prompt:ollama": path}
        self.assertEqual(
            resolve_prompt_override("chat", "ollama", "anything", config),
            "local prompt",
        )

    def test_most_specific_pattern_wins(self):
        generic = self._template("generic.md", "generic")
        specific = self._template("specific.md", "specific")
        config = {
            "chat_prompt:ollama/*": generic,
            "chat_prompt:ollama/qwen*": specific,
        }
        self.assertEqual(
            resolve_prompt_override("chat", "ollama", "qwen3.6:27b", config),
            "specific",
        )

    def test_missing_file_falls_through(self):
        config = {"chat_prompt:ollama/*": "/nonexistent/prompt.md"}
        self.assertEqual(resolve_prompt_override("chat", "ollama", "qwen3", config), "")

    def test_kind_separation(self):
        path = self._template("ask.md", "ask prompt")
        config = {"ask_prompt:ollama/*": path}
        self.assertEqual(resolve_prompt_override("chat", "ollama", "m", config), "")
        self.assertEqual(resolve_prompt_override("ask", "ollama", "m", config), "ask prompt")


class TestGetPromptWithOverrides(PromptOverrideTestCase):
    def test_chat_prompt_override_used(self):
        path = self._template("chat.md", "custom chat template")
        config = {"chat_prompt:ollama/qwen*": path}
        self.assertEqual(get_chat_prompt("ollama", "qwen3.6:27b", config),
                         "custom chat template")

    def test_ask_prompt_override_used(self):
        path = self._template("ask.md", "custom ask template")
        config = {"ask_prompt:openai/gpt-4o*": path}
        self.assertEqual(get_ask_prompt("openai", "gpt-4o-mini", config),
                         "custom ask template")

    def test_builtin_used_without_override(self):
        prompt = get_chat_prompt("ollama", "qwen3.6:27b", {})
        self.assertIn("Conch", prompt)

    def test_no_config_still_works(self):
        self.assertIn("Conch", get_chat_prompt("ollama", "qwen3.6:27b"))
        self.assertTrue(get_ask_prompt("anthropic"))


if __name__ == "__main__":
    unittest.main()
