"""Tests for the OpenRouter provider (OpenAI-compatible gateway).

Covers catalog wiring, request body shaping (low reasoning effort, no
temperature — the Kimi K3 tool-calling quirk), config loading, and the
handling of streamed `reasoning` deltas on tool-call turns.
"""

import json
import unittest
from unittest.mock import patch

from conch.providers import (
    DEFAULT_API_KEY_ENVS,
    DEFAULT_CHAT_MODEL_BY_PROVIDER,
    KNOWN_MODELS,
    MODEL_CONTEXT_WINDOWS,
    MODEL_PRICING,
    OPENROUTER_BASE_URL,
    RAW_FNS,
    STREAM_FNS,
    _openrouter_body,
    raw_openrouter,
    stream_openrouter,
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


class TestOpenRouterCatalog(unittest.TestCase):
    def test_registered_everywhere(self):
        self.assertIn("openrouter", KNOWN_MODELS)
        self.assertIn("openrouter", RAW_FNS)
        self.assertIn("openrouter", STREAM_FNS)
        self.assertEqual(DEFAULT_API_KEY_ENVS["openrouter"], "OPENROUTER_API_KEY")

    def test_default_model_in_catalog(self):
        self.assertIn(
            DEFAULT_CHAT_MODEL_BY_PROVIDER["openrouter"], KNOWN_MODELS["openrouter"]
        )

    def test_models_registered(self):
        for model in (
            "moonshotai/kimi-k3",
            "z-ai/glm-5.2",
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-flash",
        ):
            self.assertIn(model, KNOWN_MODELS["openrouter"])
            self.assertIn(model, MODEL_CONTEXT_WINDOWS)
            self.assertIn(model, MODEL_PRICING)

    def test_million_token_windows(self):
        self.assertEqual(MODEL_CONTEXT_WINDOWS["moonshotai/kimi-k3"], 1048576)
        self.assertEqual(MODEL_CONTEXT_WINDOWS["z-ai/glm-5.2"], 1048576)
        self.assertEqual(MODEL_CONTEXT_WINDOWS["deepseek/deepseek-v4-pro"], 1048576)
        self.assertEqual(MODEL_CONTEXT_WINDOWS["deepseek/deepseek-v4-flash"], 1048576)


class TestOpenRouterBody(unittest.TestCase):
    def test_default_model_and_shape(self):
        body = _openrouter_body({}, [{"role": "user", "content": "hi"}], None)
        self.assertEqual(body["model"], "moonshotai/kimi-k3")
        self.assertIn("max_tokens", body)
        self.assertNotIn("tools", body)

    def test_low_effort_no_temperature(self):
        # Kimi K3 quirk: default (max) reasoning effort + temperature makes
        # it answer in prose instead of tool-calling.
        body = _openrouter_body({}, [], None)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertNotIn("temperature", body)

    def test_tools_are_sanitized_copies(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "t",
                "parameters": {"type": "object", "properties": {
                    "xs": {"type": "array"},  # missing items
                }},
            },
        }]
        body = _openrouter_body({}, [], tools)
        sent = body["tools"][0]["function"]["parameters"]["properties"]["xs"]
        self.assertIn("items", sent)
        self.assertNotIn("items", tools[0]["function"]["parameters"]["properties"]["xs"])


class TestRawOpenRouter(unittest.TestCase):
    def test_missing_key_is_silent_empty(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
            result = raw_openrouter({}, [])
        self.assertEqual(result["content"], "")
        self.assertIsNone(result["tool_calls"])

    def test_request_and_response(self):
        recorded = {}
        payload = {
            "choices": [{"message": {"content": "hi", "tool_calls": None}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }

        def side_effect(req, timeout=None):
            recorded["url"] = req.full_url
            recorded["headers"] = dict(req.headers)
            recorded["body"] = json.loads(req.data.decode())
            return _FakeHTTPResponse(payload)

        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-test"}):
            result = raw_openrouter({"model": "z-ai/glm-5.2"}, [])
        self.assertEqual(recorded["url"], f"{OPENROUTER_BASE_URL}/chat/completions")
        self.assertEqual(recorded["headers"].get("Authorization"), "Bearer sk-or-test")
        self.assertEqual(recorded["body"]["model"], "z-ai/glm-5.2")
        self.assertEqual(result["content"], "hi")
        self.assertEqual(result["_usage"], {"input_tokens": 7, "output_tokens": 3})


class TestStreamOpenRouter(unittest.TestCase):
    def _stream(self, lines, on_token=None):
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(lines=lines)), \
             patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-test"}):
            return stream_openrouter({"model": "z-ai/glm-5.2"}, [], on_token=on_token)

    def test_content_streams(self):
        lines = [
            b'data: {"choices": [{"delta": {"content": "str"}}]}\n',
            b'data: {"choices": [{"delta": {"content": "eamed"}}]}\n',
            b"data: [DONE]\n",
        ]
        tokens = []
        result = self._stream(lines, on_token=tokens.append)
        self.assertEqual(result["content"], "streamed")
        self.assertEqual("".join(tokens), "streamed")

    def test_reasoning_not_promoted_on_tool_call_turns(self):
        # GLM-5.2/K3 stream a `reasoning` delta on every tool call; it must
        # not leak into content (which would persist chain-of-thought).
        lines = [
            b'data: {"choices": [{"delta": {"reasoning": "let me think"}}]}\n',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", '
            b'"function": {"name": "local_shell", "arguments": "{}"}}]}}]}\n',
            b"data: [DONE]\n",
        ]
        tokens = []
        result = self._stream(lines, on_token=tokens.append)
        self.assertEqual(result["content"], "")
        self.assertEqual(tokens, [])
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "local_shell")

    def test_reasoning_only_reply_surfaces_reasoning(self):
        lines = [
            b'data: {"choices": [{"delta": {"reasoning": "the answer is 4"}}]}\n',
            b"data: [DONE]\n",
        ]
        result = self._stream(lines)
        self.assertEqual(result["content"], "the answer is 4")


class TestOpenRouterConfigLoading(unittest.TestCase):
    def test_defaults_applied(self):
        import os, tempfile
        from pathlib import Path
        from unittest import mock

        from conch.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            conch_dir = Path(tmp) / "conch"
            conch_dir.mkdir()
            (conch_dir / "config").write_text("provider = openrouter\n")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
                 mock.patch.object(Path, "home", return_value=Path(tmp)):
                config = load_config()
        self.assertEqual(config["provider"], "openrouter")
        self.assertEqual(config["api_key_env"], "OPENROUTER_API_KEY")
        self.assertEqual(config["model"], "moonshotai/kimi-k3")
        self.assertEqual(config["chat_model"], "moonshotai/kimi-k3")


if __name__ == "__main__":
    unittest.main()
