"""Tests for Ollama support: tool calling (streaming + multi-turn + qwen
recovery) and live model discovery/validation.

Covers the failure modes fixed in the ollama-tools work:
- stream_ollama dropped tool_calls emitted in intermediate (done:false) chunks
- qwen3 <think> blocks leaking into content
- qwen <tool_call>{...}</tool_call> textual tool calls not recovered
- tool_call arguments sent back to Ollama as JSON strings instead of objects
- tool result messages missing tool_name linkage
- hardcoded model list allowing switches to models the server doesn't have
- models without the "tools" capability being offered/accepted
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from conch import providers
from conch.providers import (
    get_fallback_chain,
    get_fallback_model,
    get_ollama_base_url,
    list_ollama_models,
    ollama_model_available,
    ollama_model_matches,
    ollama_model_supports_tools,
    raw_ollama,
    stream_ollama,
    strip_think_blocks,
    validate_ollama_model,
)
from conch.runtime import (
    extract_textual_tool_use_blocks,
    normalize_messages_for_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    """Stand-in for urllib's response: context manager + line iterator."""

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


def _stream_lines(chunks):
    return [json.dumps(c).encode() + b"\n" for c in chunks]


def _req_url(req):
    return req if isinstance(req, str) else req.full_url


def _fake_ollama_server(installed, tool_capable=(), reachable=True):
    """Return a urlopen side_effect emulating /api/tags and /api/show."""

    def side_effect(req, timeout=None):
        if not reachable:
            raise urllib.error.URLError("connection refused")
        url = _req_url(req)
        if url.endswith("/api/tags"):
            return _FakeHTTPResponse({"models": [{"name": n} for n in installed]})
        if url.endswith("/api/show"):
            model = json.loads(req.data.decode())["model"]
            caps = ["completion"]
            if model in tool_capable:
                caps.append("tools")
            return _FakeHTTPResponse({"capabilities": caps})
        raise AssertionError(f"unexpected URL {url}")

    return side_effect


class OllamaCacheTestCase(unittest.TestCase):
    """Base that isolates the module-level caches between tests."""

    def setUp(self):
        providers._ollama_tags_cache.clear()
        providers._ollama_caps_cache.clear()
        providers._ollama_ctx_cache.clear()


# ---------------------------------------------------------------------------
# <think> block stripping (qwen3, deepseek-r1 style reasoning)
# ---------------------------------------------------------------------------

class TestStripThinkBlocks(unittest.TestCase):
    def test_plain_text_untouched(self):
        self.assertEqual(strip_think_blocks("hello"), "hello")

    def test_removes_think_block(self):
        text = "<think>I should run ls</think>Here is the listing."
        self.assertEqual(strip_think_blocks(text), "Here is the listing.")

    def test_removes_multiple_blocks(self):
        text = "<think>a</think>one<think>b</think> two"
        self.assertEqual(strip_think_blocks(text), "one two")

    def test_unterminated_block_dropped(self):
        text = "answer<think>never closed..."
        self.assertEqual(strip_think_blocks(text), "answer")

    def test_multiline_block(self):
        text = "<think>\nline1\nline2\n</think>\nresult"
        self.assertEqual(strip_think_blocks(text), "result")

    def test_empty_input(self):
        self.assertEqual(strip_think_blocks(""), "")


# ---------------------------------------------------------------------------
# raw_ollama response parsing
# ---------------------------------------------------------------------------

class TestRawOllama(unittest.TestCase):
    def _call(self, api_response):
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(api_response)):
            return raw_ollama({"provider": "ollama", "model": "qwen3"}, [])

    def test_dict_arguments_serialized(self):
        result = self._call({
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "local_shell", "arguments": {"command": "ls"}}},
                ],
            },
        })
        tc = result["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "local_shell")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"command": "ls"})

    def test_string_arguments_not_double_encoded(self):
        result = self._call({
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "t", "arguments": '{"x": 1}'}},
                ],
            },
        })
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]), {"x": 1})

    def test_think_blocks_stripped_from_content(self):
        result = self._call({
            "message": {"role": "assistant", "content": "<think>hmm</think>The answer is 4."},
        })
        self.assertEqual(result["content"], "The answer is 4.")

    def test_none_content_tolerated(self):
        result = self._call({
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "t", "arguments": {}}}],
            },
        })
        self.assertEqual(result["content"], "")
        self.assertEqual(len(result["tool_calls"]), 1)


# ---------------------------------------------------------------------------
# Request options: num_ctx + keep_alive must go out on every request
# ---------------------------------------------------------------------------

class TestOllamaRequestOptions(OllamaCacheTestCase):
    """raw_ollama and stream_ollama must send options.num_ctx and keep_alive;
    otherwise Ollama silently truncates at its own (tiny) default window."""

    def _serve(self, model_info=None, chat_payload=None, chat_lines=None):
        """urlopen side_effect answering /api/show and /api/chat, recording
        the chat request body."""
        recorded = {}

        def side_effect(req, timeout=None):
            url = _req_url(req)
            if url.endswith("/api/show"):
                return _FakeHTTPResponse({"model_info": model_info or {}})
            if url.endswith("/api/chat"):
                recorded["body"] = json.loads(req.data.decode())
                return _FakeHTTPResponse(
                    payload=chat_payload or {"message": {"role": "assistant", "content": "ok"}},
                    lines=chat_lines,
                )
            raise AssertionError(f"unexpected URL {url}")

        return side_effect, recorded

    def test_raw_ollama_sends_default_num_ctx_and_keep_alive(self):
        side_effect, recorded = self._serve()
        with patch("urllib.request.urlopen", side_effect=side_effect):
            raw_ollama({"provider": "ollama", "model": "qwen3"}, [])
        body = recorded["body"]
        self.assertEqual(body["options"]["num_ctx"], providers.DEFAULT_OLLAMA_NUM_CTX)
        self.assertEqual(body["keep_alive"], providers.DEFAULT_OLLAMA_KEEP_ALIVE)

    def test_raw_ollama_num_ctx_clamped_to_model_max(self):
        side_effect, recorded = self._serve(model_info={"qwen2.context_length": 8192})
        with patch("urllib.request.urlopen", side_effect=side_effect):
            raw_ollama({"provider": "ollama", "model": "qwen3", "ollama_num_ctx": "65536"}, [])
        self.assertEqual(recorded["body"]["options"]["num_ctx"], 8192)

    def test_raw_ollama_config_num_ctx_and_keep_alive_respected(self):
        side_effect, recorded = self._serve(model_info={"qwen2.context_length": 131072})
        config = {
            "provider": "ollama", "model": "qwen3",
            "ollama_num_ctx": "16384", "ollama_keep_alive": "30m",
        }
        with patch("urllib.request.urlopen", side_effect=side_effect):
            raw_ollama(config, [])
        body = recorded["body"]
        self.assertEqual(body["options"]["num_ctx"], 16384)
        self.assertEqual(body["keep_alive"], "30m")

    def test_stream_ollama_sends_num_ctx_and_keep_alive(self):
        side_effect, recorded = self._serve(
            chat_lines=_stream_lines([{"message": {"content": "hi"}, "done": True}])
        )
        with patch("urllib.request.urlopen", side_effect=side_effect):
            stream_ollama({"provider": "ollama", "model": "qwen3"}, [])
        body = recorded["body"]
        self.assertEqual(body["options"]["num_ctx"], providers.DEFAULT_OLLAMA_NUM_CTX)
        self.assertEqual(body["keep_alive"], providers.DEFAULT_OLLAMA_KEEP_ALIVE)

    def test_get_ollama_num_ctx_invalid_config_falls_back(self):
        with patch("conch.providers.get_ollama_context_length", return_value=None):
            self.assertEqual(
                providers.get_ollama_num_ctx("m", {"ollama_num_ctx": "not-a-number"}),
                providers.DEFAULT_OLLAMA_NUM_CTX,
            )


# ---------------------------------------------------------------------------
# stream_ollama: tool calls arrive mid-stream, not in the final done chunk
# ---------------------------------------------------------------------------

class TestStreamOllama(unittest.TestCase):
    def _stream(self, chunks, on_token=None):
        response = _FakeHTTPResponse(lines=_stream_lines(chunks))
        with patch("urllib.request.urlopen", return_value=response):
            return stream_ollama(
                {"provider": "ollama", "model": "qwen2.5-coder"}, [], tools=[{}], on_token=on_token
            )

    def test_mid_stream_tool_calls_collected(self):
        result = self._stream([
            {"message": {"role": "assistant", "content": ""}, "done": False},
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "local_shell", "arguments": {"command": "ls"}}},
                    ],
                },
                "done": False,
            },
            {"done": True, "prompt_eval_count": 12, "eval_count": 7},
        ])
        self.assertIsNotNone(result["tool_calls"], "tool calls emitted mid-stream must be kept")
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "local_shell")
        self.assertEqual(
            json.loads(result["tool_calls"][0]["function"]["arguments"]),
            {"command": "ls"},
        )
        self.assertEqual(result["_usage"], {"input_tokens": 12, "output_tokens": 7})

    def test_multiple_tool_calls_across_chunks(self):
        result = self._stream([
            {"message": {"tool_calls": [{"function": {"name": "a", "arguments": {}}}]}, "done": False},
            {"message": {"tool_calls": [{"function": {"name": "b", "arguments": {}}}]}, "done": False},
            {"done": True},
        ])
        names = [tc["function"]["name"] for tc in result["tool_calls"]]
        self.assertEqual(names, ["a", "b"])
        ids = [tc["id"] for tc in result["tool_calls"]]
        self.assertEqual(len(set(ids)), 2, "tool call ids must be unique")

    def test_content_streams_and_think_stripped(self):
        tokens = []
        result = self._stream([
            {"message": {"content": "<think>plan"}, "done": False},
            {"message": {"content": "</think>"}, "done": False},
            {"message": {"content": "hello"}, "done": False},
            {"done": True},
        ], on_token=tokens.append)
        self.assertEqual(result["content"], "hello")
        self.assertEqual("".join(tokens), "<think>plan</think>hello")

    def test_thinking_field_not_treated_as_content(self):
        result = self._stream([
            {"message": {"thinking": "let me reason"}, "done": False},
            {"message": {"content": "answer"}, "done": False},
            {"done": True},
        ])
        self.assertEqual(result["content"], "answer")

    def test_error_chunk_reported(self):
        result = self._stream([{"error": "model 'nope' not found"}])
        # Ollama failures use the same unified error signaling as every
        # other provider (plan 0.3): [API error: ...] prefix + _error flag.
        self.assertIn("[API error", result["content"])
        self.assertIn("not found", result["content"])
        self.assertTrue(result.get("_error"))
        self.assertIsNone(result["tool_calls"])

    def test_no_tool_calls_returns_none(self):
        result = self._stream([
            {"message": {"content": "plain reply"}, "done": False},
            {"done": True},
        ])
        self.assertIsNone(result["tool_calls"])
        self.assertEqual(result["content"], "plain reply")


# ---------------------------------------------------------------------------
# qwen textual <tool_call> recovery
# ---------------------------------------------------------------------------

class TestQwenToolCallRecovery(unittest.TestCase):
    def test_single_tool_call(self):
        text = '<tool_call>\n{"name": "local_shell", "arguments": {"command": "ls -la"}}\n</tool_call>'
        blocks = extract_textual_tool_use_blocks(text)
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[0]["name"], "local_shell")
        self.assertEqual(blocks[0]["input"], {"command": "ls -la"})

    def test_multiple_tool_calls(self):
        text = (
            '<tool_call>{"name": "a", "arguments": {}}</tool_call>\n'
            '<tool_call>{"name": "b", "arguments": {"x": 1}}</tool_call>'
        )
        blocks = extract_textual_tool_use_blocks(text)
        self.assertEqual([b["name"] for b in blocks], ["a", "b"])
        self.assertEqual(blocks[1]["input"], {"x": 1})

    def test_string_arguments_parsed(self):
        text = '<tool_call>{"name": "t", "arguments": "{\\"k\\": \\"v\\"}"}</tool_call>'
        blocks = extract_textual_tool_use_blocks(text)
        self.assertEqual(blocks[0]["input"], {"k": "v"})

    def test_surrounding_prose_tolerated(self):
        text = 'I will run the command now.\n<tool_call>{"name": "t", "arguments": {}}</tool_call>'
        blocks = extract_textual_tool_use_blocks(text)
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[0]["name"], "t")

    def test_invalid_json_ignored(self):
        self.assertIsNone(extract_textual_tool_use_blocks("<tool_call>not json</tool_call>"))

    def test_missing_name_ignored(self):
        self.assertIsNone(
            extract_textual_tool_use_blocks('<tool_call>{"arguments": {}}</tool_call>')
        )

    def test_bare_json_tool_call(self):
        # qwen2.5 via Ollama emits the call as the entire content, no tags
        text = '{"name": "get_secret_number", "arguments": {"codename": "conch"}}'
        blocks = extract_textual_tool_use_blocks(text)
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[0]["name"], "get_secret_number")
        self.assertEqual(blocks[0]["input"], {"codename": "conch"})

    def test_bare_json_array_of_tool_calls(self):
        text = '[{"name": "a", "arguments": {}}, {"name": "b", "arguments": {"x": 1}}]'
        blocks = extract_textual_tool_use_blocks(text)
        self.assertEqual([b["name"] for b in blocks], ["a", "b"])

    def test_fenced_json_tool_call(self):
        text = '```json\n{"name": "t", "arguments": {"k": "v"}}\n```'
        blocks = extract_textual_tool_use_blocks(text)
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[0]["input"], {"k": "v"})

    def test_ordinary_json_reply_not_treated_as_tool_call(self):
        # A JSON answer that merely *contains* a name key must not execute
        self.assertIsNone(
            extract_textual_tool_use_blocks('{"name": "Alice", "age": 30}')
        )
        self.assertIsNone(
            extract_textual_tool_use_blocks('{"result": [1, 2, 3]}')
        )

    def test_json_missing_arguments_not_recovered(self):
        self.assertIsNone(extract_textual_tool_use_blocks('{"name": "tool_x"}'))


# ---------------------------------------------------------------------------
# Sending history back to Ollama: arguments must be objects, results linked
# ---------------------------------------------------------------------------

class TestNormalizeMessagesForOllama(unittest.TestCase):
    def _history(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "ollama_0", "type": "function",
                 "function": {"name": "local_shell", "arguments": '{"command": "ls"}'}},
            ]},
            {"role": "tool", "tool_call_id": "ollama_0", "content": "file1 file2"},
        ]

    def test_arguments_converted_to_objects(self):
        result = normalize_messages_for_provider(self._history(), "ollama")
        assistant = [m for m in result if m.get("tool_calls")][0]
        args = assistant["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(args, dict)
        self.assertEqual(args, {"command": "ls"})

    def test_tool_result_gets_tool_name(self):
        result = normalize_messages_for_provider(self._history(), "ollama")
        tool_msg = [m for m in result if m["role"] == "tool"][0]
        self.assertEqual(tool_msg["tool_name"], "local_shell")
        self.assertEqual(tool_msg["content"], "file1 file2")

    def test_openai_arguments_stay_strings(self):
        result = normalize_messages_for_provider(self._history(), "openai")
        assistant = [m for m in result if m.get("tool_calls")][0]
        self.assertIsInstance(assistant["tool_calls"][0]["function"]["arguments"], str)

    def test_malformed_argument_string_becomes_empty_object(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "t", "arguments": "{broken"}},
            ]},
        ]
        result = normalize_messages_for_provider(msgs, "ollama")
        self.assertEqual(result[0]["tool_calls"][0]["function"]["arguments"], {})


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------

class TestGetOllamaBaseUrl(unittest.TestCase):
    def test_config_base_url_when_provider_ollama(self):
        cfg = {"provider": "ollama", "base_url": "http://192.168.1.247:11434/"}
        self.assertEqual(get_ollama_base_url(cfg), "http://192.168.1.247:11434")

    def test_foreign_base_url_ignored(self):
        cfg = {"provider": "cerebras", "base_url": "https://api.cerebras.ai/v1"}
        with patch.dict("os.environ", {"OLLAMA_HOST": ""}, clear=False):
            self.assertEqual(get_ollama_base_url(cfg), "http://localhost:11434")

    def test_ollama_base_url_key_wins_after_provider_switch(self):
        cfg = {"provider": "anthropic", "ollama_base_url": "http://192.168.1.247:11434"}
        self.assertEqual(get_ollama_base_url(cfg), "http://192.168.1.247:11434")

    def test_env_fallback_and_scheme_added(self):
        with patch.dict("os.environ", {"OLLAMA_HOST": "192.168.1.247:11434"}):
            self.assertEqual(get_ollama_base_url({}), "http://192.168.1.247:11434")


# ---------------------------------------------------------------------------
# Live model discovery + tools capability filter
# ---------------------------------------------------------------------------

class TestListOllamaModels(OllamaCacheTestCase):
    def test_reachable_lists_tool_capable_only(self):
        side_effect = _fake_ollama_server(
            installed=["qwen3:latest", "qwen2.5-coder:7b", "gemma3:latest"],
            tool_capable=["qwen3:latest", "qwen2.5-coder:7b"],
        )
        with patch("urllib.request.urlopen", side_effect=side_effect):
            models = list_ollama_models({})
        self.assertEqual(models, ["qwen3:latest", "qwen2.5-coder:7b"])

    def test_unfiltered_list_includes_all(self):
        side_effect = _fake_ollama_server(
            installed=["qwen3:latest", "gemma3:latest"], tool_capable=["qwen3:latest"],
        )
        with patch("urllib.request.urlopen", side_effect=side_effect):
            models = list_ollama_models({}, tool_capable_only=False)
        self.assertEqual(models, ["qwen3:latest", "gemma3:latest"])

    def test_unreachable_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=_fake_ollama_server([], reachable=False)):
            self.assertIsNone(list_ollama_models({}))

    def test_tags_result_cached(self):
        side_effect = _fake_ollama_server(installed=["qwen3:latest"], tool_capable=["qwen3:latest"])
        with patch("urllib.request.urlopen", side_effect=side_effect) as mock_urlopen:
            list_ollama_models({})
            first_calls = mock_urlopen.call_count
            list_ollama_models({})
            self.assertEqual(
                mock_urlopen.call_count, first_calls,
                "second call within TTL must not re-hit the network",
            )

    def test_force_refresh_bypasses_cache(self):
        side_effect = _fake_ollama_server(installed=["qwen3:latest"], tool_capable=["qwen3:latest"])
        with patch("urllib.request.urlopen", side_effect=side_effect) as mock_urlopen:
            list_ollama_models({})
            first_calls = mock_urlopen.call_count
            list_ollama_models({}, force_refresh=True)
            self.assertGreater(mock_urlopen.call_count, first_calls)

    def test_capabilities_cached_per_model(self):
        side_effect = _fake_ollama_server(installed=["qwen3:latest"], tool_capable=["qwen3:latest"])
        with patch("urllib.request.urlopen", side_effect=side_effect) as mock_urlopen:
            self.assertTrue(ollama_model_supports_tools("qwen3:latest", {}))
            calls = mock_urlopen.call_count
            self.assertTrue(ollama_model_supports_tools("qwen3:latest", {}))
            self.assertEqual(mock_urlopen.call_count, calls)

    def test_old_server_without_capabilities_uses_template(self):
        def side_effect(req, timeout=None):
            url = _req_url(req)
            if url.endswith("/api/show"):
                return _FakeHTTPResponse({"template": "{{ if .Tools }}...{{ end }}"})
            raise AssertionError(url)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertTrue(ollama_model_supports_tools("qwen2.5:latest", {}))


class TestOllamaModelMatching(OllamaCacheTestCase):
    def test_exact_match(self):
        self.assertTrue(ollama_model_matches("qwen3:latest", ["qwen3:latest"]))

    def test_bare_name_matches_tagged(self):
        self.assertTrue(ollama_model_matches("qwen3", ["qwen3:latest"]))
        self.assertTrue(ollama_model_matches("qwen2.5-coder", ["qwen2.5-coder:7b"]))

    def test_wrong_tag_no_match(self):
        self.assertFalse(ollama_model_matches("qwen3:8b", ["qwen3:latest"]))

    def test_no_partial_prefix_match(self):
        self.assertFalse(ollama_model_matches("qwen", ["qwen3:latest"]))

    def test_available_none_when_unreachable(self):
        with patch("conch.providers.list_ollama_models", return_value=None):
            self.assertIsNone(ollama_model_available("qwen3", {}))


class TestValidateOllamaModel(OllamaCacheTestCase):
    def test_ok_for_installed_tool_capable(self):
        side_effect = _fake_ollama_server(installed=["qwen3:latest"], tool_capable=["qwen3:latest"])
        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, reason = validate_ollama_model("qwen3", {})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_rejects_missing_model(self):
        side_effect = _fake_ollama_server(installed=["qwen3:latest"], tool_capable=["qwen3:latest"])
        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, reason = validate_ollama_model("llama3.3", {})
        self.assertFalse(ok)
        self.assertIn("not installed", reason)

    def test_rejects_non_tool_model(self):
        side_effect = _fake_ollama_server(installed=["gemma3:latest"], tool_capable=[])
        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, reason = validate_ollama_model("gemma3", {})
        self.assertFalse(ok)
        self.assertIn("doesn't support tool calling", reason)

    def test_unreachable_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=_fake_ollama_server([], reachable=False)):
            ok, reason = validate_ollama_model("qwen3", {})
        self.assertIsNone(ok)
        self.assertIn("unreachable", reason)


# ---------------------------------------------------------------------------
# Defaults and fallback chain must not assume models exist
# ---------------------------------------------------------------------------

class TestOllamaFallbacks(OllamaCacheTestCase):
    def test_default_used_when_present(self):
        with patch("conch.providers.list_ollama_models", return_value=["llama3.3:latest", "qwen3:latest"]):
            self.assertEqual(get_fallback_model("ollama"), "llama3.3")

    def test_first_available_when_default_missing(self):
        with patch("conch.providers.list_ollama_models", return_value=["qwen3:latest"]):
            self.assertEqual(get_fallback_model("ollama"), "qwen3:latest")

    def test_empty_when_unreachable(self):
        with patch("conch.providers.list_ollama_models", return_value=None):
            self.assertEqual(get_fallback_model("ollama"), "")

    def test_chain_uses_live_models_for_ollama(self):
        with patch("conch.providers.list_ollama_models", return_value=["qwen3:latest", "mistral:latest"]):
            chain = get_fallback_chain("ollama", "qwen3:latest")
        same = [(p, m) for p, m, _ in chain if p == "ollama"]
        self.assertEqual(same, [("ollama", "mistral:latest")])

    def test_chain_passes_config_to_live_list(self):
        # The fallback chain must query the *configured* server, not just
        # OLLAMA_HOST/localhost.
        config = {"provider": "ollama", "ollama_base_url": "http://192.168.1.247:11434"}
        with patch("conch.providers.list_ollama_models", return_value=["qwen3:latest"]) as mock_list:
            get_fallback_chain("ollama", "qwen3:latest", config)
        mock_list.assert_called_with(config)

    def test_chain_skips_ollama_when_unreachable(self):
        with patch("conch.providers.list_ollama_models", return_value=None), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            chain = get_fallback_chain("openai", "gpt-4o")
        self.assertNotIn("ollama", [p for p, _, _ in chain])


# ---------------------------------------------------------------------------
# /model and /provider validation
# ---------------------------------------------------------------------------

class TestModelSwitchCommands(OllamaCacheTestCase):
    def _run(self, cmd, config=None, provider="ollama", model="llama3.3"):
        import contextlib

        config = config if config is not None else {"provider": provider}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from conch.commands import handle_slash_command
            result = handle_slash_command(cmd, config, provider, model, lambda v: None)
        return result, out.getvalue(), config

    def test_model_switch_accepted_when_on_server(self):
        with patch("conch.commands.list_ollama_models", return_value=["qwen3:latest"]), \
             patch("conch.commands.validate_ollama_model", return_value=(True, "")):
            result, output, config = self._run("/model qwen3")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "ollama")
        self.assertEqual(result[1], "qwen3")
        self.assertEqual(config["model"], "qwen3")

    def test_model_switch_rejected_when_missing(self):
        with patch("conch.commands.list_ollama_models", return_value=["qwen3:latest"]), \
             patch("conch.commands.validate_ollama_model",
                   return_value=(False, "model 'llama4' is not installed on the Ollama server")):
            result, output, config = self._run("/model llama4")
        self.assertIsNone(result)
        self.assertIn("not installed", output)
        self.assertNotIn("model", config)

    def test_model_switch_rejected_without_tool_support(self):
        with patch("conch.commands.list_ollama_models", return_value=["qwen3:latest"]), \
             patch("conch.commands.validate_ollama_model",
                   return_value=(False, "model 'gemma3' doesn't support tool calling")):
            result, output, config = self._run("/model gemma3")
        self.assertIsNone(result)
        self.assertIn("doesn't support tool calling", output)

    def test_model_switch_rejected_when_unreachable(self):
        with patch("conch.commands.list_ollama_models", return_value=None), \
             patch("conch.commands.validate_ollama_model",
                   return_value=(None, "Ollama server unreachable at http://x:11434")):
            result, output, config = self._run("/model qwen3")
        self.assertIsNone(result)
        self.assertIn("unreachable", output)

    def test_provider_switch_picks_live_model(self):
        with patch("conch.commands.get_fallback_model", return_value="qwen3:latest"):
            result, output, config = self._run("/provider ollama", provider="anthropic",
                                               model="claude-sonnet-4-6")
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], ("ollama", "qwen3:latest"))

    def test_provider_switch_rejected_when_unreachable(self):
        with patch("conch.commands.get_fallback_model", return_value=""), \
             patch("conch.commands.list_ollama_models", return_value=None):
            result, output, config = self._run("/provider ollama", provider="anthropic",
                                               model="claude-sonnet-4-6")
        self.assertIsNone(result)
        self.assertIn("unreachable", output)

    def test_provider_switch_rejected_when_no_tool_models(self):
        with patch("conch.commands.get_fallback_model", return_value=""), \
             patch("conch.commands.list_ollama_models", return_value=[]):
            result, output, config = self._run("/provider ollama", provider="anthropic",
                                               model="claude-sonnet-4-6")
        self.assertIsNone(result)
        self.assertIn("No tool-capable models", output)

    def test_models_listing_shows_unreachable_note(self):
        with patch("conch.commands.list_ollama_models", return_value=None):
            result, output, config = self._run("/models", provider="anthropic",
                                               model="claude-sonnet-4-6")
        self.assertIn("unreachable", output)

    def test_models_listing_shows_live_models(self):
        with patch("conch.commands.list_ollama_models", return_value=["qwen3:latest"]):
            result, output, config = self._run("/models", provider="anthropic",
                                               model="claude-sonnet-4-6")
        self.assertIn("qwen3:latest", output)
        self.assertNotIn("llama4", output)


# ---------------------------------------------------------------------------
# conch_config tool validation
# ---------------------------------------------------------------------------

class TestConchConfigOllama(OllamaCacheTestCase):
    def setUp(self):
        super().setUp()
        from conch.tooling import ConchConfigClient
        self.client = ConchConfigClient()
        self.client.bind("ollama", "llama3.3", {}, {"provider": "ollama"})

    def _call(self, args):
        return self.client.call_tool("conch_config", args)["content"][0]["text"]

    def test_set_model_accepts_live_model(self):
        with patch("conch.providers.validate_ollama_model", return_value=(True, "")):
            text = self._call({"action": "set_model", "value": "qwen3"})
        self.assertIn("queued", text.lower())
        self.assertEqual(self.client.pending_actions[0], ("set_model", "ollama", "qwen3"))

    def test_set_model_rejects_missing_model(self):
        with patch("conch.providers.validate_ollama_model",
                   return_value=(False, "model 'llama4' is not installed on the Ollama server")):
            text = self._call({"action": "set_model", "value": "llama4"})
        self.assertIn("not installed", text)
        self.assertEqual(self.client.pending_actions, [])

    def test_set_model_rejects_non_tool_model(self):
        with patch("conch.providers.validate_ollama_model",
                   return_value=(False, "model 'gemma3' doesn't support tool calling")):
            text = self._call({"action": "set_model", "value": "gemma3"})
        self.assertIn("doesn't support tool calling", text)
        self.assertEqual(self.client.pending_actions, [])

    def test_set_model_unreachable(self):
        with patch("conch.providers.validate_ollama_model",
                   return_value=(None, "Ollama server unreachable at http://x:11434")):
            text = self._call({"action": "set_model", "value": "qwen3"})
        self.assertIn("unreachable", text)
        self.assertEqual(self.client.pending_actions, [])

    def test_set_provider_ollama_picks_live_model(self):
        self.client.bind("anthropic", "claude-sonnet-4-6", {}, {"provider": "anthropic"})
        with patch("conch.providers.list_ollama_models", return_value=["qwen3:latest"]):
            text = self._call({"action": "set_provider", "value": "ollama"})
        self.assertIn("queued", text.lower())
        self.assertEqual(self.client.pending_actions[0], ("set_model", "ollama", "qwen3:latest"))

    def test_set_provider_ollama_unreachable(self):
        self.client.bind("anthropic", "claude-sonnet-4-6", {}, {"provider": "anthropic"})
        with patch("conch.providers.list_ollama_models", return_value=None):
            text = self._call({"action": "set_provider", "value": "ollama"})
        self.assertIn("unreachable", text)
        self.assertEqual(self.client.pending_actions, [])

    def test_list_models_shows_live_ollama(self):
        with patch("conch.providers.list_ollama_models", return_value=["qwen3:latest"]):
            text = self._call({"action": "list_models"})
        self.assertIn("qwen3:latest", text)
        self.assertNotIn("llama4", text)

    def test_list_models_unreachable_note(self):
        with patch("conch.providers.list_ollama_models", return_value=None):
            text = self._call({"action": "list_models"})
        self.assertIn("unreachable", text)


if __name__ == "__main__":
    unittest.main()
