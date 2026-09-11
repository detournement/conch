import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.commands import handle_slash_command
from conch.config import load_config, local_only_enabled
from conch.providers import (
    get_fallback_chain,
    is_local_inference_url,
    raw_custom,
    validate_ollama_model,
)
from conch.runtime import weak_model_config


class TestEnvironmentConfiguration(unittest.TestCase):
    def test_environment_overrides_file_and_provider_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "conch"
            config_dir.mkdir()
            (config_dir / "config").write_text(
                "provider = anthropic\nmodel = claude-sonnet-4-6\n"
            )
            env = {
                "XDG_CONFIG_HOME": tmp,
                "CONCH_PROVIDER": "ollama",
                "CONCH_MODEL": "llama3.2:3b",
                "CONCH_OLLAMA_BASE_URL": "http://ollama:11434",
                "CONCH_LOCAL_ONLY": "true",
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                Path, "home", return_value=Path(tmp) / "home"
            ):
                config = load_config()
        self.assertEqual(config["provider"], "ollama")
        self.assertEqual(config["model"], "llama3.2:3b")
        self.assertEqual(config["chat_model"], "llama3.2:3b")
        self.assertEqual(
            config["ollama_base_url"], "http://ollama:11434"
        )

    def test_provider_default_does_not_leak_anthropic_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": tmp, "CONCH_PROVIDER": "ollama"},
            clear=True,
        ), patch.object(Path, "home", return_value=Path(tmp) / "home"):
            config = load_config()
        self.assertEqual(config["model"], "llama3.3")
        self.assertEqual(config["api_key_env"], "")


class TestLocalOnlyPolicy(unittest.TestCase):
    def test_auto_is_on_for_local_providers_only(self):
        self.assertTrue(local_only_enabled({}, "ollama"))
        self.assertTrue(local_only_enabled({}, "custom"))
        self.assertFalse(local_only_enabled({}, "anthropic"))

    def test_local_endpoint_classification(self):
        for url in (
            "http://127.0.0.1:11434",
            "http://192.168.1.247:11434",
            "http://host.docker.internal:11434",
            "http://ollama:11434",
            "http://inference.local:8080",
        ):
            self.assertTrue(is_local_inference_url(url), url)
        self.assertFalse(
            is_local_inference_url("https://inference.example.com/v1")
        )

    def test_public_custom_endpoint_is_blocked_before_network(self):
        config = {
            "provider": "custom",
            "custom_base_url": "https://inference.example.com/v1",
            "custom_model": "model",
            "local_only": "true",
        }
        with patch("urllib.request.urlopen") as urlopen:
            result = raw_custom(config, [])
        self.assertTrue(result["_error"])
        self.assertIn("local_only blocks", result["content"])
        urlopen.assert_not_called()

    def test_public_ollama_endpoint_is_blocked_before_network(self):
        config = {
            "provider": "ollama",
            "ollama_base_url": "https://ollama.example.com",
            "local_only": "true",
        }
        with patch("urllib.request.urlopen") as urlopen:
            ok, reason = validate_ollama_model("llama", config)
        self.assertFalse(ok)
        self.assertIn("local_only blocks", reason)
        urlopen.assert_not_called()

    def test_ollama_fallback_never_crosses_to_cloud_by_default(self):
        config = {"provider": "ollama", "local_only": "auto"}
        with patch(
            "conch.providers.list_ollama_models",
            return_value=["llama:latest"],
        ), patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "set",
                "ANTHROPIC_API_KEY": "set",
                "CEREBRAS_API_KEY": "set",
            },
            clear=False,
        ):
            chain = get_fallback_chain("ollama", "llama:latest", config)
        self.assertFalse(
            any(provider not in ("ollama", "custom") for provider, _, _ in chain)
        )

    def test_explicit_opt_out_allows_cloud_fallback(self):
        config = {"provider": "ollama", "local_only": "false"}
        with patch(
            "conch.providers.list_ollama_models",
            return_value=["llama:latest"],
        ), patch.dict(
            os.environ, {"OPENAI_API_KEY": "set"}, clear=False
        ):
            chain = get_fallback_chain("ollama", "llama:latest", config)
        self.assertIn("openai", [provider for provider, _, _ in chain])

    def test_cloud_weak_model_is_disabled_for_local_session(self):
        config = {
            "provider": "ollama",
            "weak_provider": "openai",
            "weak_model": "gpt-4o-mini",
        }
        self.assertIsNone(weak_model_config(config))

    def test_slash_switch_to_cloud_is_blocked(self):
        config = {"provider": "ollama", "local_only": "true"}
        output = io.StringIO()
        with patch.dict(
            os.environ, {"OPENAI_API_KEY": "set"}, clear=False
        ), patch("sys.stdout", output):
            result = handle_slash_command(
                "/model gpt-4o-mini",
                config,
                "ollama",
                "llama:latest",
                lambda _enabled: None,
            )
        self.assertIsNone(result)
        self.assertIn("local_only", output.getvalue())
        self.assertNotIn("model", config)


if __name__ == "__main__":
    unittest.main()
