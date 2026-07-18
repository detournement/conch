"""Tests for the custom OpenAI-compatible provider (plan 2.4): provider=custom
with custom_base_url/custom_model, reusing the OpenAI adapter, plus the
startup tool-capability probe."""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from conch.config import load_config
from conch.providers import (
    RAW_FNS,
    STREAM_FNS,
    get_context_window,
    get_custom_base_url,
    get_fallback_model,
    probe_custom_provider,
    raw_custom,
    stream_custom,
)


class _FakeHTTPResponse:
    def __init__(self, payload=None, lines=None):
        self._payload = payload
        self._lines = lines or []

    def read(self):
        return json.dumps(self._payload).encode()

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CONFIG = {
    "provider": "custom",
    "custom_base_url": "http://192.168.1.50:8000/v1",
    "custom_model": "qwen2.5-32b-vllm",
    "chat_model": "qwen2.5-32b-vllm",
    "model": "qwen2.5-32b-vllm",
}


class TestCustomBaseUrl(unittest.TestCase):
    def test_custom_base_url_key(self):
        self.assertEqual(get_custom_base_url(CONFIG), "http://192.168.1.50:8000/v1")

    def test_shared_base_url_when_provider_custom(self):
        cfg = {"provider": "custom", "base_url": "http://host:8000/v1/"}
        self.assertEqual(get_custom_base_url(cfg), "http://host:8000/v1")

    def test_foreign_base_url_ignored(self):
        cfg = {"provider": "ollama", "base_url": "http://host:11434"}
        self.assertEqual(get_custom_base_url(cfg), "")

    def test_scheme_added(self):
        cfg = {"custom_base_url": "host:8000/v1"}
        self.assertEqual(get_custom_base_url(cfg), "http://host:8000/v1")


class TestRawCustom(unittest.TestCase):
    def test_registered_in_provider_tables(self):
        self.assertIn("custom", RAW_FNS)
        self.assertIn("custom", STREAM_FNS)

    def test_openai_shaped_request_and_response(self):
        recorded = {}
        payload = {
            "choices": [{"message": {"content": "hi there", "tool_calls": None}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

        def side_effect(req, timeout=None):
            recorded["url"] = req.full_url
            recorded["body"] = json.loads(req.data.decode())
            recorded["headers"] = dict(req.headers)
            return _FakeHTTPResponse(payload)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = raw_custom(CONFIG, [{"role": "user", "content": "hi"}])
        self.assertEqual(recorded["url"], "http://192.168.1.50:8000/v1/chat/completions")
        self.assertEqual(recorded["body"]["model"], "qwen2.5-32b-vllm")
        self.assertEqual(result["content"], "hi there")
        self.assertEqual(result["_usage"], {"input_tokens": 5, "output_tokens": 2})
        # No api_key_env configured → no Authorization header
        self.assertNotIn("Authorization", recorded["headers"])

    def test_api_key_env_used_when_set(self):
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["headers"] = dict(req.headers)
            return _FakeHTTPResponse({"choices": [{"message": {"content": "x"}}]})

        cfg = dict(CONFIG, api_key_env="MY_VLLM_KEY")
        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"MY_VLLM_KEY": "sk-local"}):
            raw_custom(cfg, [])
        self.assertEqual(recorded["headers"].get("Authorization"), "Bearer sk-local")

    def test_missing_base_url_is_error(self):
        result = raw_custom({"provider": "custom"}, [])
        self.assertTrue(result.get("_error"))
        self.assertIn("custom_base_url", result["content"])

    def test_connection_error_unified(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            result = raw_custom(CONFIG, [])
        self.assertTrue(result.get("_error"))
        self.assertTrue(result["content"].startswith("[API error:"))

    def test_streaming_reuses_openai_compat(self):
        lines = [
            b'data: {"choices": [{"delta": {"content": "str"}}]}\n',
            b'data: {"choices": [{"delta": {"content": "eamed"}}]}\n',
            b"data: [DONE]\n",
        ]
        tokens = []
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(lines=lines)):
            result = stream_custom(CONFIG, [], on_token=tokens.append)
        self.assertEqual(result["content"], "streamed")
        self.assertEqual("".join(tokens), "streamed")


class TestCustomProbe(unittest.TestCase):
    def test_probe_sends_tools(self):
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["body"] = json.loads(req.data.decode())
            return _FakeHTTPResponse({"choices": [{"message": {"content": ""}}]})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, reason = probe_custom_provider(CONFIG)
        self.assertTrue(ok)
        self.assertIn("tools", recorded["body"])

    def test_probe_fails_on_unreachable(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            ok, reason = probe_custom_provider(CONFIG)
        self.assertFalse(ok)
        self.assertIn("unreachable", reason)

    def test_probe_fails_without_config(self):
        ok, reason = probe_custom_provider({})
        self.assertFalse(ok)
        self.assertIn("custom_base_url", reason)


class TestCustomFallbackAndWindow(unittest.TestCase):
    def test_fallback_model_from_config(self):
        self.assertEqual(get_fallback_model("custom", CONFIG), "qwen2.5-32b-vllm")
        self.assertEqual(get_fallback_model("custom", {}), "")

    def test_context_window_default_and_override(self):
        self.assertEqual(get_context_window("custom", "m", {}), 32768)
        self.assertEqual(
            get_context_window("custom", "m", {"custom_context_window": "65536"}),
            65536,
        )


class TestCustomProviderSwitch(unittest.TestCase):
    def _run(self, cmd, config):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from conch.commands import handle_slash_command
            result = handle_slash_command(cmd, config, "anthropic",
                                          "claude-sonnet-4-6", lambda v: None)
        return result, out.getvalue()

    def test_switch_rejected_when_unconfigured(self):
        result, output = self._run("/provider custom", {"provider": "anthropic"})
        self.assertIsNone(result)
        self.assertIn("custom_base_url", output)

    def test_switch_accepted_after_probe(self):
        config = dict(CONFIG, provider="anthropic")
        with patch("conch.providers.probe_custom_provider", return_value=(True, "")) as _, \
             patch("conch.commands.get_fallback_model", return_value="qwen2.5-32b-vllm"):
            # patch the probe where commands imports it (function-level import)
            with patch("urllib.request.urlopen",
                       return_value=_FakeHTTPResponse({"choices": [{"message": {"content": ""}}]})):
                result, output = self._run("/provider custom", config)
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], ("custom", "qwen2.5-32b-vllm"))


class TestCustomConfigLoading(unittest.TestCase):
    def test_base_url_and_model_normalized(self):
        import os, tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            conch_dir = Path(tmp) / "conch"
            conch_dir.mkdir()
            (conch_dir / "config").write_text(
                "provider = custom\n"
                "base_url = http://192.168.1.50:8000/v1\n"
                "model = local-model\n"
            )
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
                 mock.patch.object(Path, "home", return_value=Path(tmp)):
                config = load_config()
        self.assertEqual(config["provider"], "custom")
        self.assertEqual(config["custom_base_url"], "http://192.168.1.50:8000/v1")
        self.assertEqual(config["custom_model"], "local-model")
        self.assertEqual(config["chat_model"], "local-model")


if __name__ == "__main__":
    unittest.main()
