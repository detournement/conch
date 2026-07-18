"""Tests for structured ask-mode output (plan 0.6).

Ask mode no longer scrapes commands out of free text. Ollama requests are
constrained by a JSON schema via the ``format`` parameter; OpenAI, Anthropic,
and Cerebras are forced into a single shell_command tool call. These tests
mock urlopen and verify both the request shape and the response parsing.
"""

import json
import unittest
from unittest.mock import patch

from conch import providers
from conch.llm import (
    COMMAND_SCHEMA,
    SHELL_COMMAND_TOOL,
    call_anthropic,
    call_cerebras,
    call_ollama,
    call_openai,
    command_from_tool_calls,
    parse_command_json,
)


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _req_url(req):
    return req if isinstance(req, str) else req.full_url


MESSAGES = [
    {"role": "system", "content": "You are a shell assistant."},
    {"role": "user", "content": "list files"},
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

class TestParseCommandJson(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_command_json('{"command": "ls -la"}'), "ls -la")

    def test_whitespace_stripped(self):
        self.assertEqual(parse_command_json('  {"command": "  ls "}  '), "ls")

    def test_invalid_json(self):
        self.assertEqual(parse_command_json("here is ls -la"), "")

    def test_missing_command_key(self):
        self.assertEqual(parse_command_json('{"cmd": "ls"}'), "")

    def test_non_string_command(self):
        self.assertEqual(parse_command_json('{"command": 42}'), "")

    def test_empty(self):
        self.assertEqual(parse_command_json(""), "")
        self.assertEqual(parse_command_json(None), "")


class TestCommandFromToolCalls(unittest.TestCase):
    def test_string_arguments(self):
        msg = {"tool_calls": [{"function": {
            "name": "shell_command", "arguments": '{"command": "df -h"}',
        }}]}
        self.assertEqual(command_from_tool_calls(msg), "df -h")

    def test_dict_arguments(self):
        msg = {"tool_calls": [{"function": {
            "name": "shell_command", "arguments": {"command": "uptime"},
        }}]}
        self.assertEqual(command_from_tool_calls(msg), "uptime")

    def test_other_tool_ignored(self):
        msg = {"tool_calls": [{"function": {"name": "other", "arguments": "{}"}}]}
        self.assertEqual(command_from_tool_calls(msg), "")

    def test_no_tool_calls(self):
        self.assertEqual(command_from_tool_calls({"content": "ls"}), "")
        self.assertEqual(command_from_tool_calls({"tool_calls": None}), "")


# ---------------------------------------------------------------------------
# call_ollama: structured format, proper roles, request options
# ---------------------------------------------------------------------------

class TestCallOllamaStructured(unittest.TestCase):
    def setUp(self):
        providers._ollama_ctx_cache.clear()

    def _serve(self, chat_payload):
        recorded = {}

        def side_effect(req, timeout=None):
            url = _req_url(req)
            if url.endswith("/api/show"):
                return _FakeHTTPResponse({"model_info": {}})
            if url.endswith("/api/chat"):
                recorded["body"] = json.loads(req.data.decode())
                return _FakeHTTPResponse(chat_payload)
            raise AssertionError(f"unexpected URL {url}")

        return side_effect, recorded

    def test_sends_schema_roles_and_options(self):
        side_effect, recorded = self._serve(
            {"message": {"content": '{"command": "ls -la"}'}}
        )
        with patch("urllib.request.urlopen", side_effect=side_effect):
            cmd = call_ollama({"provider": "ollama", "model": "qwen3.6:27b"}, MESSAGES)
        self.assertEqual(cmd, "ls -la")
        body = recorded["body"]
        self.assertEqual(body["format"], COMMAND_SCHEMA)
        # System and user roles must be preserved, not flattened into one prompt
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        # Every Ollama request carries num_ctx (plan 0.1)
        self.assertIn("num_ctx", body.get("options", {}))
        self.assertIn("keep_alive", body)

    def test_default_model_aligned_with_config(self):
        side_effect, recorded = self._serve({"message": {"content": '{"command": "ls"}'}})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            call_ollama({"provider": "ollama"}, MESSAGES)
        self.assertEqual(recorded["body"]["model"], "llama3.3")

    def test_unstructured_reply_returns_empty(self):
        side_effect, _ = self._serve({"message": {"content": "Sure! Run `ls -la`."}})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            cmd = call_ollama({"provider": "ollama", "model": "qwen3"}, MESSAGES)
        self.assertEqual(cmd, "", "free text must not be scraped for commands")

    def test_old_server_schema_format_rejected_falls_back_to_json(self):
        # Ollama < 0.5 only accepts format="json" — a schema object gets a
        # 400 unmarshal error. Ask mode must retry with the legacy mode
        # instead of dying with sys.exit.
        import io
        import urllib.error

        bodies = []

        def side_effect(req, timeout=None):
            url = _req_url(req)
            if url.endswith("/api/show"):
                return _FakeHTTPResponse({"model_info": {}})
            if url.endswith("/api/chat"):
                body = json.loads(req.data.decode())
                bodies.append(body)
                if isinstance(body.get("format"), dict):
                    raise urllib.error.HTTPError(
                        url, 400,
                        "json: cannot unmarshal object into Go struct field "
                        "ChatRequest.format of type string",
                        {},
                        io.BytesIO(json.dumps({"error": (
                            "json: cannot unmarshal object into Go struct "
                            "field ChatRequest.format of type string"
                        )}).encode()),
                    )
                return _FakeHTTPResponse({"message": {"content": '{"command": "ls"}'}})
            raise AssertionError(f"unexpected URL {url}")

        with patch("urllib.request.urlopen", side_effect=side_effect):
            cmd = call_ollama({"provider": "ollama", "model": "qwen2.5:3b"}, MESSAGES)
        self.assertEqual(cmd, "ls")
        self.assertEqual(bodies[-1]["format"], "json")

    def test_non_format_http_error_still_exits(self):
        import io
        import urllib.error

        def side_effect(req, timeout=None):
            url = _req_url(req)
            if url.endswith("/api/show"):
                return _FakeHTTPResponse({"model_info": {}})
            raise urllib.error.HTTPError(
                url, 500, "boom", {},
                io.BytesIO(json.dumps({"error": "server exploded"}).encode()),
            )

        with patch("urllib.request.urlopen", side_effect=side_effect):
            with self.assertRaises(SystemExit):
                call_ollama({"provider": "ollama", "model": "qwen2.5:3b"}, MESSAGES)


# ---------------------------------------------------------------------------
# OpenAI / Cerebras: forced tool call
# ---------------------------------------------------------------------------

class TestCallOpenAIStructured(unittest.TestCase):
    def _serve(self, payload):
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["body"] = json.loads(req.data.decode())
            return _FakeHTTPResponse(payload)

        return side_effect, recorded

    def test_forces_shell_command_tool(self):
        payload = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "shell_command", "arguments": '{"command": "git status"}',
        }}]}}]}
        side_effect, recorded = self._serve(payload)
        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            cmd = call_openai({"provider": "openai"}, MESSAGES)
        self.assertEqual(cmd, "git status")
        body = recorded["body"]
        self.assertEqual(body["tools"], [SHELL_COMMAND_TOOL])
        self.assertEqual(
            body["tool_choice"],
            {"type": "function", "function": {"name": "shell_command"}},
        )

    def test_prose_reply_returns_empty(self):
        payload = {"choices": [{"message": {"content": "Run `ls` to list files."}}]}
        side_effect, _ = self._serve(payload)
        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            cmd = call_openai({"provider": "openai"}, MESSAGES)
        self.assertEqual(cmd, "")


class TestCallCerebrasStructured(unittest.TestCase):
    def test_forces_shell_command_tool(self):
        recorded = {}
        payload = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "shell_command", "arguments": '{"command": "kubectl get pods"}',
        }}]}}]}

        def side_effect(req, timeout=None):
            recorded["body"] = json.loads(req.data.decode())
            return _FakeHTTPResponse(payload)

        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"CEREBRAS_API_KEY": "csk-test"}):
            cmd = call_cerebras({"provider": "cerebras"}, MESSAGES)
        self.assertEqual(cmd, "kubectl get pods")
        body = recorded["body"]
        self.assertEqual(body["tools"], [SHELL_COMMAND_TOOL])
        self.assertEqual(
            body["tool_choice"],
            {"type": "function", "function": {"name": "shell_command"}},
        )


# ---------------------------------------------------------------------------
# Anthropic: forced tool_use
# ---------------------------------------------------------------------------

class TestCallAnthropicStructured(unittest.TestCase):
    def test_forces_tool_use(self):
        recorded = {}
        payload = {"content": [
            {"type": "tool_use", "name": "shell_command",
             "input": {"command": "docker ps"}},
        ]}

        def side_effect(req, timeout=None):
            recorded["body"] = json.loads(req.data.decode())
            return _FakeHTTPResponse(payload)

        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            cmd = call_anthropic({"provider": "anthropic"}, MESSAGES)
        self.assertEqual(cmd, "docker ps")
        body = recorded["body"]
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "shell_command"})
        self.assertEqual(body["tools"][0]["name"], "shell_command")
        self.assertEqual(body["tools"][0]["input_schema"], COMMAND_SCHEMA)

    def test_text_reply_returns_empty(self):
        payload = {"content": [{"type": "text", "text": "ls -la"}]}

        def side_effect(req, timeout=None):
            return _FakeHTTPResponse(payload)

        with patch("urllib.request.urlopen", side_effect=side_effect), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            cmd = call_anthropic({"provider": "anthropic"}, MESSAGES)
        self.assertEqual(cmd, "")


if __name__ == "__main__":
    unittest.main()
