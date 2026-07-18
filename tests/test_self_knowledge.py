"""Tests for Conch's self-knowledge: context-window lookup (static table for
cloud providers, live /api/show for Ollama), the /status command, the
system-prompt self-description, and the runtime context limit derived from
the real window.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from conch import providers
from conch.config import get_config_path
from conch.prompts import build_self_description
from conch.providers import (
    MODEL_CONTEXT_WINDOWS,
    PROVIDER_DEFAULT_CONTEXT_WINDOWS,
    get_context_window,
    get_ollama_context_length,
)
from conch.runtime import CONTEXT_LIMITS, get_context_limit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_show_server(model_info, reachable=True):
    """urlopen side_effect emulating POST /api/show with given model_info."""

    def side_effect(req, timeout=None):
        if not reachable:
            raise urllib.error.URLError("connection refused")
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/show"):
            return _FakeHTTPResponse({"model_info": model_info})
        raise AssertionError(f"unexpected URL {url}")

    return side_effect


class CtxCacheTestCase(unittest.TestCase):
    """Isolates the module-level context cache between tests."""

    def setUp(self):
        providers._ollama_ctx_cache.clear()


# ---------------------------------------------------------------------------
# Static context-window table (cloud providers)
# ---------------------------------------------------------------------------

class TestCloudContextWindows(unittest.TestCase):
    def test_known_models_covered(self):
        # Every non-ollama model in the catalog must have a known window.
        for provider_name, models in providers.KNOWN_MODELS.items():
            if provider_name == "ollama":
                continue
            for model in models:
                self.assertIn(model, MODEL_CONTEXT_WINDOWS, f"{model} missing a context window")

    def test_lookup_uses_table(self):
        self.assertEqual(get_context_window("anthropic", "claude-sonnet-4-6"), 200000)
        self.assertEqual(get_context_window("openai", "gpt-4o-mini"), 128000)
        self.assertEqual(get_context_window("cerebras", "zai-glm-4.7"), 131072)

    def test_unknown_model_falls_back_to_provider_default(self):
        self.assertEqual(
            get_context_window("anthropic", "claude-future-9"),
            PROVIDER_DEFAULT_CONTEXT_WINDOWS["anthropic"],
        )

    def test_unknown_provider_has_sane_fallback(self):
        self.assertGreater(get_context_window("mystery", "some-model"), 0)


# ---------------------------------------------------------------------------
# Ollama: live context length from /api/show
# ---------------------------------------------------------------------------

class TestOllamaContextLength(CtxCacheTestCase):
    def test_reads_arch_context_length(self):
        side_effect = _fake_show_server({"llama.context_length": 131072})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertEqual(get_ollama_context_length("llama3.3", {}), 131072)

    def test_any_arch_prefix_accepted(self):
        side_effect = _fake_show_server({"qwen2.context_length": 32768})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertEqual(get_ollama_context_length("qwen2.5-coder", {}), 32768)

    def test_unreachable_returns_none(self):
        side_effect = _fake_show_server({}, reachable=False)
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertIsNone(get_ollama_context_length("llama3.3", {}))

    def test_result_cached(self):
        side_effect = _fake_show_server({"llama.context_length": 131072})
        with patch("urllib.request.urlopen", side_effect=side_effect) as mock_urlopen:
            get_ollama_context_length("llama3.3", {})
            calls = mock_urlopen.call_count
            get_ollama_context_length("llama3.3", {})
            self.assertEqual(mock_urlopen.call_count, calls, "second call must hit the cache")

    def test_ollama_window_is_effective_num_ctx(self):
        # The window is what requests actually run with: the default num_ctx
        # clamped to the model max — not the model's theoretical maximum.
        from conch.providers import DEFAULT_OLLAMA_NUM_CTX
        side_effect = _fake_show_server({"llama.context_length": 131072})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertEqual(
                get_context_window("ollama", "llama3.3", {}), DEFAULT_OLLAMA_NUM_CTX
            )

    def test_small_model_max_clamps_default(self):
        side_effect = _fake_show_server({"llama.context_length": 8192})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertEqual(get_context_window("ollama", "llama3.3", {}), 8192)

    def test_num_ctx_clamps_model_max(self):
        side_effect = _fake_show_server({"llama.context_length": 131072})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            window = get_context_window("ollama", "llama3.3", {"ollama_num_ctx": "16384"})
        self.assertEqual(window, 16384)

    def test_num_ctx_used_when_server_unreachable(self):
        side_effect = _fake_show_server({}, reachable=False)
        with patch("urllib.request.urlopen", side_effect=side_effect):
            window = get_context_window("ollama", "llama3.3", {"ollama_num_ctx": "8192"})
        self.assertEqual(window, 8192)

    def test_default_num_ctx_when_nothing_known(self):
        # Requests always send num_ctx explicitly, so the effective window is
        # the default even when the server can't report a model max.
        from conch.providers import DEFAULT_OLLAMA_NUM_CTX
        side_effect = _fake_show_server({}, reachable=False)
        with patch("urllib.request.urlopen", side_effect=side_effect):
            window = get_context_window("ollama", "llama3.3", {})
        self.assertEqual(window, DEFAULT_OLLAMA_NUM_CTX)


# ---------------------------------------------------------------------------
# Runtime context limit derived from the real window
# ---------------------------------------------------------------------------

class TestGetContextLimit(CtxCacheTestCase):
    def test_no_config_uses_legacy_limits(self):
        self.assertEqual(get_context_limit("ollama"), CONTEXT_LIMITS["ollama"])
        self.assertEqual(get_context_limit("anthropic"), CONTEXT_LIMITS["anthropic"])

    def test_config_uses_real_window_with_headroom(self):
        config = {"provider": "anthropic", "chat_model": "claude-sonnet-4-6"}
        self.assertEqual(get_context_limit("anthropic", config), int(200000 * 0.9))

    def test_ollama_config_uses_effective_num_ctx(self):
        side_effect = _fake_show_server({"llama.context_length": 131072})
        config = {"provider": "ollama", "chat_model": "llama3.3", "ollama_num_ctx": "16384"}
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertEqual(get_context_limit("ollama", config), int(16384 * 0.9))

    def test_tiny_window_floors_at_minimum(self):
        side_effect = _fake_show_server({}, reachable=False)
        config = {"provider": "ollama", "chat_model": "llama3.3", "ollama_num_ctx": "512"}
        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertEqual(get_context_limit("ollama", config), 2048)


# ---------------------------------------------------------------------------
# Model-facing self-description (system prompt)
# ---------------------------------------------------------------------------

class TestBuildSelfDescription(CtxCacheTestCase):
    def test_cloud_description(self):
        text = build_self_description("anthropic", "claude-sonnet-4-6", {})
        self.assertIn("anthropic/claude-sonnet-4-6", text)
        self.assertIn("200,000 tokens", text)
        self.assertIn(get_config_path(), text)
        self.assertNotIn("Ollama server", text)

    def test_ollama_description_includes_base_url(self):
        side_effect = _fake_show_server({"llama.context_length": 131072})
        config = {"provider": "ollama", "ollama_base_url": "http://192.168.1.247:11434"}
        with patch("urllib.request.urlopen", side_effect=side_effect):
            text = build_self_description("ollama", "llama3.3", config)
        self.assertIn("ollama/llama3.3", text)
        self.assertIn("32,768 tokens", text)  # effective num_ctx, not model max
        self.assertIn("http://192.168.1.247:11434", text)

    def test_description_stays_compact(self):
        text = build_self_description("openai", "gpt-4o-mini", {})
        self.assertLess(len(text), 300, "self-description must stay tiny")

    def test_injected_into_system_prompt(self):
        from conch.app import _build_system_prompt
        prompt = _build_system_prompt(
            "base", provider="anthropic", model="claude-sonnet-4-6", config={}
        )
        self.assertIn("anthropic/claude-sonnet-4-6", prompt)
        self.assertIn("200,000 tokens", prompt)


# ---------------------------------------------------------------------------
# /status command
# ---------------------------------------------------------------------------

class TestStatusCommand(CtxCacheTestCase):
    def _run(self, config, provider, model, session_usage=None, messages=None):
        import contextlib

        from conch.commands import handle_slash_command

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handle_slash_command(
                "/status", config, provider, model, lambda v: None,
                session_usage=session_usage, messages=messages,
            )
        return result, out.getvalue()

    def test_shows_provider_model_window_and_config(self):
        result, output = self._run({"provider": "anthropic"}, "anthropic", "claude-sonnet-4-6")
        self.assertIsNone(result)
        self.assertIn("anthropic", output)
        self.assertIn("claude-sonnet-4-6", output)
        self.assertIn("200,000 tokens", output)
        self.assertIn(get_config_path(), output)

    def test_shows_context_usage_vs_window(self):
        messages = [{"role": "user", "content": "x" * 3500}]  # ~1000 tokens
        result, output = self._run(
            {"provider": "anthropic"}, "anthropic", "claude-sonnet-4-6", messages=messages,
        )
        self.assertIn("Context used", output)
        self.assertIn("~1,000 tokens", output)
        self.assertIn("of window", output)

    def test_shows_session_usage(self):
        usage = {"input_tokens": 1234, "output_tokens": 567, "cost": 0.0, "turns": 3}
        result, output = self._run(
            {"provider": "anthropic"}, "anthropic", "claude-sonnet-4-6", session_usage=usage,
        )
        self.assertIn("3 turns", output)
        self.assertIn("1,234 in / 567 out", output)

    def test_ollama_shows_base_url(self):
        side_effect = _fake_show_server({"llama.context_length": 131072})
        config = {"provider": "ollama", "ollama_base_url": "http://192.168.1.247:11434"}
        with patch("urllib.request.urlopen", side_effect=side_effect):
            result, output = self._run(config, "ollama", "llama3.3")
        self.assertIn("http://192.168.1.247:11434", output)
        self.assertIn("32,768 tokens", output)  # effective num_ctx, not model max

    def test_secretlike_settings_hidden(self):
        config = {
            "provider": "anthropic",
            "some_api_token": "sekrit",
            "api_key_env": "ANTHROPIC_API_KEY",
        }
        result, output = self._run(config, "anthropic", "claude-sonnet-4-6")
        self.assertNotIn("sekrit", output)
        self.assertIn("ANTHROPIC_API_KEY", output)


if __name__ == "__main__":
    unittest.main()
