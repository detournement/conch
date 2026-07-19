"""Tests for cloud-provider model validation on switches (bug: `/model
ogooaboog` on anthropic used to succeed) and startup scrutiny of config-file
model values."""

import contextlib
import io
import unittest
from unittest.mock import patch

from conch.app import warn_unknown_cloud_model
from conch.providers import (
    KNOWN_MODELS,
    suggest_models,
    validate_model_for_provider,
)
from conch.tooling import ConchConfigClient


class TestValidateModelForProvider(unittest.TestCase):
    def test_valid_cloud_model_accepted(self):
        ok, reason = validate_model_for_provider("anthropic", "claude-sonnet-4-6")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_gibberish_rejected(self):
        ok, reason = validate_model_for_provider("anthropic", "ogooaboog")
        self.assertFalse(ok)
        self.assertIn("unknown anthropic model 'ogooaboog'", reason)
        self.assertIn("Known anthropic models:", reason)

    def test_typo_gets_suggestions(self):
        ok, reason = validate_model_for_provider("anthropic", "claude-sonet-4-6")
        self.assertFalse(ok)
        self.assertIn("Did you mean:", reason)
        self.assertIn("claude-sonnet-4-6", reason)

    def test_openai_typo_suggestion(self):
        ok, reason = validate_model_for_provider("openai", "gpt4o-mini")
        self.assertFalse(ok)
        self.assertIn("gpt-4o-mini", reason)

    def test_ollama_delegates_to_live_validation(self):
        with patch("conch.providers.validate_ollama_model",
                   return_value=(False, "not installed")) as mock_validate:
            ok, reason = validate_model_for_provider("ollama", "qwen9", {"provider": "ollama"})
        self.assertFalse(ok)
        mock_validate.assert_called_once()

    def test_custom_unverifiable(self):
        ok, reason = validate_model_for_provider("custom", "whatever-model")
        self.assertIsNone(ok)
        self.assertIn("taken as-is", reason)

    def test_empty_model_rejected(self):
        ok, _ = validate_model_for_provider("anthropic", "  ")
        self.assertFalse(ok)

    def test_suggest_models_helper(self):
        self.assertIn("claude-sonnet-4-6",
                      suggest_models("claude-sonet-46", KNOWN_MODELS["anthropic"]))
        self.assertEqual(suggest_models("zzzzz", KNOWN_MODELS["anthropic"]), [])


class TestModelSwitchValidation(unittest.TestCase):
    def _run(self, cmd, provider="anthropic", model="claude-sonnet-4-6"):
        from conch.commands import handle_slash_command
        config = {"provider": provider}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             patch("conch.commands.list_ollama_models", return_value=None):
            result = handle_slash_command(cmd, config, provider, model, lambda v: None)
        return result, out.getvalue(), config

    def test_gibberish_rejected_on_anthropic(self):
        result, output, config = self._run("/model ogooaboog")
        self.assertIsNone(result, "switch must be rejected")
        self.assertIn("unknown anthropic model 'ogooaboog'", output)
        self.assertIn("/models", output)
        self.assertNotIn("model", config, "config must be untouched")

    def test_typo_suggests_close_match(self):
        result, output, _ = self._run("/model claude-sonnet-46")
        self.assertIsNone(result)
        self.assertIn("Did you mean:", output)
        self.assertIn("claude-sonnet-4-6", output)

    def test_valid_cloud_switch_still_works(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            result, output, config = self._run("/model claude-haiku-4-5")
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], ("anthropic", "claude-haiku-4-5"))
        self.assertEqual(config["model"], "claude-haiku-4-5")

    def test_cross_provider_switch_still_works(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result, output, config = self._run("/model gpt-4o-mini")
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], ("openai", "gpt-4o-mini"))

    def test_force_bypasses_validation_with_warning(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            result, output, config = self._run("/model claude-brand-new-9 --force")
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], ("anthropic", "claude-brand-new-9"))
        self.assertIn("Skipping model validation", output)

    def test_force_alone_shows_usage(self):
        result, output, _ = self._run("/model --force")
        self.assertIsNone(result)
        self.assertIn("Usage", output)


class TestConchConfigSuggestions(unittest.TestCase):
    def test_set_model_unknown_gets_suggestions(self):
        client = ConchConfigClient()
        client.bind("anthropic", "claude-sonnet-4-6", {}, {"provider": "anthropic"})
        with patch("conch.providers.validate_ollama_model",
                   return_value=(None, "Ollama server unreachable at x")):
            result = client.call_tool("conch_config", {
                "action": "set_model", "value": "claude-sonet-4-6",
            })
        text = result["content"][0]["text"]
        self.assertIn("Unknown model", text)
        self.assertIn("Did you mean:", text)
        self.assertIn("claude-sonnet-4-6", text)
        self.assertEqual(client.pending_actions, [])

    def test_set_model_gibberish_rejected(self):
        client = ConchConfigClient()
        client.bind("anthropic", "claude-sonnet-4-6", {}, {"provider": "anthropic"})
        with patch("conch.providers.validate_ollama_model",
                   return_value=(None, "unreachable")):
            result = client.call_tool("conch_config", {
                "action": "set_model", "value": "ogooaboog",
            })
        self.assertIn("Unknown model", result["content"][0]["text"])
        self.assertEqual(client.pending_actions, [])


class TestStartupModelScrutiny(unittest.TestCase):
    def test_known_model_no_warning(self):
        self.assertEqual(warn_unknown_cloud_model("anthropic", "claude-sonnet-4-6"), "")

    def test_unknown_model_warns_but_keeps(self):
        warning = warn_unknown_cloud_model("anthropic", "claude-sonet-4-6")
        self.assertIn("isn't in conch's anthropic catalog", warning)
        self.assertIn("did you mean claude-sonnet-4-6", warning)
        self.assertIn("keeping it", warning)

    def test_gibberish_warns_without_suggestions(self):
        warning = warn_unknown_cloud_model("openai", "ogooaboog")
        self.assertIn("catalog", warning)
        self.assertNotIn("did you mean", warning)

    def test_ollama_and_custom_skipped(self):
        self.assertEqual(warn_unknown_cloud_model("ollama", "anything"), "")
        self.assertEqual(warn_unknown_cloud_model("custom", "anything"), "")


if __name__ == "__main__":
    unittest.main()
