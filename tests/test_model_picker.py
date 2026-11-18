"""The model picker and its catalog: added models become switchable,
provider-exact switching, roles saved to config and cleared with the model
they name, and unusable providers refused before anything changes."""

import os
import tempfile
import unittest
from unittest import mock

from conch import model_catalog
from conch.model_catalog import ModelEntry, ProviderStatus
from conch.model_picker import ModelPicker


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name,
                                           "ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "x"})
        env.start()
        self.addCleanup(env.stop)
        self.config = {"provider": "anthropic", "chat_model": "claude-sonnet-5",
                       "model": "claude-sonnet-5"}

    def saved(self):
        from conch.config import load_config

        return load_config()


class CatalogTests(_ConfigCase):
    def test_added_model_persists_and_passes_catalog_validation(self):
        from conch.providers import validate_model_for_provider

        self.assertFalse(validate_model_for_provider("openai", "gpt-7-preview", self.config)[0])
        self.assertTrue(model_catalog.add_model(self.config, "openai", "gpt-7-preview"))
        self.assertFalse(model_catalog.add_model(self.config, "openai", "gpt-7-preview"))
        self.assertEqual(validate_model_for_provider("openai", "gpt-7-preview", self.saved()),
                         (True, ""))
        self.assertEqual(model_catalog.provider_for("gpt-7-preview", self.saved()), "openai")
        self.assertTrue(model_catalog.remove_model(self.config, "openai", "gpt-7-preview"))
        self.assertFalse(validate_model_for_provider("openai", "gpt-7-preview", self.saved())[0])

    def test_new_model_text_names_the_provider_first(self):
        self.assertEqual(model_catalog.parse_new_model("openrouter/vendor/model-x"),
                         ("openrouter", "vendor/model-x"))
        for bad in ("gpt-7", "ollama/qwen3", "nosuch/model", "openai/", "openai/has space"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model_catalog.parse_new_model(bad)

    def test_switch_command_names_the_provider_exactly(self):
        self.assertEqual(ModelEntry("bedrock", "zai.glm-5").switch_command(),
                         "/model zai.glm-5 --provider bedrock")
        self.assertEqual(ModelEntry("llamaidx", "llamaidx/box/m").switch_command(),
                         "/model llamaidx/box/m")


class PickerTests(_ConfigCase):
    def picker(self):
        picker = ModelPicker(self.config, "anthropic", "claude-sonnet-5")
        picker.apply_live([], {"claude-code": ProviderStatus(False, "CLI not installed"),
                               "codex": ProviderStatus(True, "signed in")})
        return picker

    def select(self, picker, provider, model):
        picker.selected_key = (provider, model)
        self.assertIsNotNone(picker.selected())

    def test_unusable_provider_is_refused_with_its_reason(self):
        picker = self.picker()
        self.select(picker, "claude-code", "opus")
        picker.choose()
        self.assertIsNone(picker.result)
        self.assertIn("CLI not installed", picker.notice)
        self.select(picker, "openai", "gpt-6-sol")
        picker.choose()
        self.assertEqual(picker.result.key, ("openai", "gpt-6-sol"))

    def test_typing_filters_and_selects_the_first_match(self):
        picker = self.picker()
        picker.query.text = "kimi"
        labels = [entry.label for kind, entry in picker.rows if kind == "model"]
        self.assertTrue(labels)
        self.assertTrue(all("kimi" in label for label in labels))
        self.assertEqual(picker.selected().label, labels[0])

    def test_roles_save_to_config_and_clear_with_a_removed_model(self):
        picker = self.picker()
        picker.start_add()
        picker.add_input.text = "openai/gpt-7-preview"
        picker.finish_add(save=True)
        self.assertEqual(picker.selected().key, ("openai", "gpt-7-preview"))
        picker.toggle_fanout()
        picker.toggle_side()
        saved = self.saved()
        self.assertEqual(saved.get("fanout_model"), "openai/gpt-7-preview")
        self.assertEqual((saved.get("weak_provider"), saved.get("weak_model")),
                         ("openai", "gpt-7-preview"))
        picker.remove_selected()
        saved = self.saved()
        self.assertFalse(saved.get("fanout_model"))
        self.assertFalse(saved.get("weak_model"))
        self.assertEqual(model_catalog.added_models(saved), [])

    def test_only_added_models_can_be_removed(self):
        picker = self.picker()
        self.select(picker, "openai", "gpt-6-sol")
        picker.remove_selected()
        self.assertIn("Only models you added", picker.notice)
        self.assertIn(("openai", "gpt-6-sol"),
                      [entry.key for kind, entry in picker.rows if kind == "model"])

    def test_native_refresh_removes_stale_models_but_keeps_user_additions(self):
        self.config["extra_models"] = "codex/user-model"
        picker = ModelPicker(self.config, "codex", "default")
        source = model_catalog._seat_source("codex")
        with mock.patch("conch.auth.cli_installed", return_value=True), \
             mock.patch("conch.seats.status", return_value=(True, "signed in")), \
             mock.patch("conch.seat_codex.list_models",
                        side_effect=[["native-old"], ["native-new"]]):
            picker.apply_source(*source(self.config))
            self.select(picker, "codex", "native-old")
            picker.apply_source(*source(self.config))
        keys = {entry.key for entry in picker.entries}
        self.assertNotIn(("codex", "native-old"), keys)
        self.assertNotEqual(picker.selected_key, ("codex", "native-old"))
        self.assertIn(("codex", "native-new"), keys)
        self.assertIn(("codex", "default"), keys)
        self.select(picker, "codex", "user-model")
        picker.remove_selected()
        self.assertNotIn(("codex", "user-model"), model_catalog.added_models(self.config))

    def test_native_discovery_failure_does_not_revoke_signed_in_seat(self):
        from conch.seat_common import SeatError

        picker = ModelPicker(self.config, "codex", "default")
        source = model_catalog._seat_source("codex")
        with mock.patch("conch.auth.cli_installed", return_value=True), \
             mock.patch("conch.seats.status", return_value=(True, "signed in")), \
             mock.patch("conch.seat_codex.list_models",
                        side_effect=[["native-model"], SeatError("catalog unavailable")]):
            picker.apply_source(*source(self.config))
            self.select(picker, "codex", "native-model")
            picker.apply_source(*source(self.config))
        self.assertTrue(picker.statuses["codex"].available)
        self.assertIn("model list unavailable", picker.statuses["codex"].note)
        picker.choose()
        self.assertEqual(picker.result.key, ("codex", "native-model"))


class ModelCommandTests(_ConfigCase):
    def test_explicit_provider_wins_over_catalog_inference(self):
        """The same id can exist under several providers; the picker's
        --provider must decide, not catalog order."""
        from conch.commands import handle_slash_command

        self.config["model_check"] = "off"
        messages = [{"role": "system", "content": "s"}]
        with mock.patch("conch.model_catalog.provider_for", return_value="anthropic"):
            result = handle_slash_command(
                "/model gpt-6-sol --provider openai", self.config, "anthropic",
                "claude-sonnet-5", lambda _on: None, messages=messages)
        self.assertEqual(result[:2], ("openai", "gpt-6-sol"))
        self.assertEqual(self.config["provider"], "openai")


if __name__ == "__main__":
    unittest.main()
