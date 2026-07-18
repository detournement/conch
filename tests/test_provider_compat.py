"""Tests for provider compatibility: OpenAI model support, tool schema
sanitization, message normalization, fallback behavior, and conch_config
model switching.

Covers the failure modes fixed in the openai-models work:
- o-series request shaping (max_completion_tokens, no temperature)
- Tool schema sanitization (missing array items)
- Tool capping per provider limit
- content:null in messages
- Reasoning token separation
- Fallback structural-error detection
- conch_config queued-switch semantics
- HTTP error body parsing
"""

import copy
import io
import json
import unittest
from unittest.mock import patch

from conch.providers import (
    KNOWN_MODELS,
    MODEL_PRICING,
    PROVIDER_TOOL_LIMITS,
    DEFAULT_CHAT_MODEL_BY_PROVIDER,
    _openai_is_strict_reasoning_model,
    _fix_tool_schema,
    _sanitize_tools_for_openai,
    build_openai_chat_request_body,
    format_http_api_error,
    get_fallback_chain,
    get_fallback_model,
    estimate_cost,
)
from conch.runtime import (
    append_results_openai,
    normalize_messages_for_provider,
    normalize_messages_on_switch,
)
from conch.tooling import (
    PINNED_TOOL_NAMES,
    cap_tools,
    ConchConfigClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str) -> dict:
    return {"function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


def _make_tools(n: int, pinned_names=()) -> list:
    tools = [_make_tool(name) for name in pinned_names]
    for i in range(n - len(tools)):
        tools.append(_make_tool(f"mcp_tool_{i}"))
    return tools


# ---------------------------------------------------------------------------
# o-series request shaping
# ---------------------------------------------------------------------------

class TestReasoningModelDetection(unittest.TestCase):
    def test_o_series_detected(self):
        for model in ("o3", "o3-mini", "o4-mini", "o1", "o1-pro", "o3-pro"):
            self.assertTrue(
                _openai_is_strict_reasoning_model(model),
                f"{model} should be detected as strict reasoning model",
            )

    def test_gpt_not_detected(self):
        for model in ("gpt-4o", "gpt-4o-mini", "gpt-5.4", "gpt-4.1-nano"):
            self.assertFalse(
                _openai_is_strict_reasoning_model(model),
                f"{model} should NOT be detected as strict reasoning model",
            )

    def test_other_not_detected(self):
        self.assertFalse(_openai_is_strict_reasoning_model("claude-sonnet-4-6"))
        self.assertFalse(_openai_is_strict_reasoning_model("llama3.3"))


class TestBuildOpenAIRequestBody(unittest.TestCase):
    def test_gpt_includes_temperature(self):
        body = build_openai_chat_request_body(
            "gpt-4o", [{"role": "user", "content": "hi"}],
            temperature=0.7, max_completion_tokens=1024,
        )
        self.assertIn("temperature", body)
        self.assertEqual(body["temperature"], 0.7)

    def test_o_series_omits_temperature(self):
        body = build_openai_chat_request_body(
            "o3-mini", [{"role": "user", "content": "hi"}],
            temperature=0.7, max_completion_tokens=1024,
        )
        self.assertNotIn("temperature", body)

    def test_uses_max_completion_tokens(self):
        body = build_openai_chat_request_body(
            "gpt-4o", [{"role": "user", "content": "hi"}],
            temperature=0.7, max_completion_tokens=2048,
        )
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", body)

    def test_tools_sanitized(self):
        tools = [{"function": {"name": "t", "parameters": {
            "type": "object",
            "properties": {"arr": {"type": "array"}},  # missing items
        }}}]
        body = build_openai_chat_request_body(
            "gpt-4o", [], temperature=0.7, max_completion_tokens=1024,
            tools=tools,
        )
        param = body["tools"][0]["function"]["parameters"]["properties"]["arr"]
        self.assertIn("items", param)

    def test_tools_sanitization_does_not_mutate_original(self):
        tools = [{"function": {"name": "t", "parameters": {
            "type": "object",
            "properties": {"arr": {"type": "array"}},
        }}}]
        build_openai_chat_request_body(
            "gpt-4o", [], temperature=0.7, max_completion_tokens=1024,
            tools=tools,
        )
        self.assertNotIn("items", tools[0]["function"]["parameters"]["properties"]["arr"])


# ---------------------------------------------------------------------------
# Tool schema sanitization
# ---------------------------------------------------------------------------

class TestFixToolSchema(unittest.TestCase):
    def test_adds_items_to_bare_array(self):
        schema = {"type": "array"}
        _fix_tool_schema(schema)
        self.assertEqual(schema["items"], {})

    def test_preserves_existing_items(self):
        schema = {"type": "array", "items": {"type": "string"}}
        _fix_tool_schema(schema)
        self.assertEqual(schema["items"], {"type": "string"})

    def test_fixes_nested_array_in_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array"},
            },
        }
        _fix_tool_schema(schema)
        self.assertEqual(schema["properties"]["tags"]["items"], {})

    def test_fixes_array_in_anyof(self):
        """Reproduces the GOOGLECALENDAR_BATCH_EVENTS failure pattern."""
        schema = {
            "type": "object",
            "properties": {
                "body": {
                    "additionalProperties": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array"},
                        ]
                    }
                }
            },
        }
        _fix_tool_schema(schema)
        array_entry = schema["properties"]["body"]["additionalProperties"]["anyOf"][1]
        self.assertEqual(array_entry["items"], {})

    def test_deeply_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nested": {"type": "array"},
                        },
                    },
                }
            },
        }
        _fix_tool_schema(schema)
        nested = schema["properties"]["ops"]["items"]["properties"]["nested"]
        self.assertEqual(nested["items"], {})

    def test_non_dict_input_passthrough(self):
        self.assertEqual(_fix_tool_schema("hello"), "hello")
        self.assertIsNone(_fix_tool_schema(None))

    def test_no_type_field_untouched(self):
        schema = {"description": "just a desc"}
        result = _fix_tool_schema(schema)
        self.assertNotIn("items", result)


class TestSanitizeToolsForOpenAI(unittest.TestCase):
    def test_returns_deep_copy(self):
        tools = [{"function": {"name": "t", "parameters": {"type": "object", "properties": {
            "x": {"type": "array"}
        }}}}]
        sanitized = _sanitize_tools_for_openai(tools)
        self.assertIn("items", sanitized[0]["function"]["parameters"]["properties"]["x"])
        self.assertNotIn("items", tools[0]["function"]["parameters"]["properties"]["x"])


# ---------------------------------------------------------------------------
# Tool capping & pinning
# ---------------------------------------------------------------------------

class TestToolCapping(unittest.TestCase):
    def test_under_limit_unchanged(self):
        tools = _make_tools(50)
        result = cap_tools(tools, max_tools=128)
        self.assertEqual(len(result), 50)

    def test_capped_to_limit(self):
        tools = _make_tools(200)
        result = cap_tools(tools, max_tools=128)
        self.assertEqual(len(result), 128)

    def test_pinned_tools_survive_cap(self):
        pinned = list(PINNED_TOOL_NAMES)
        tools = _make_tools(200, pinned_names=pinned)
        result = cap_tools(tools, max_tools=128)
        result_names = {t["function"]["name"] for t in result}
        for name in PINNED_TOOL_NAMES:
            self.assertIn(name, result_names, f"pinned tool {name} was dropped")

    def test_conch_config_is_pinned(self):
        self.assertIn("conch_config", PINNED_TOOL_NAMES)

    def test_provider_tool_limits_defined(self):
        self.assertEqual(PROVIDER_TOOL_LIMITS.get("openai"), 128)
        self.assertEqual(PROVIDER_TOOL_LIMITS.get("cerebras"), 128)
        self.assertIsNone(PROVIDER_TOOL_LIMITS.get("anthropic"))


# ---------------------------------------------------------------------------
# content:null in messages
# ---------------------------------------------------------------------------

class TestNullContent(unittest.TestCase):
    def test_append_results_openai_stores_empty_string(self):
        messages = []
        response = {"content": "", "tool_calls": [{"id": "1"}]}
        append_results_openai(messages, response, [{"id": "1", "content": "ok"}])
        assistant_msg = messages[0]
        self.assertEqual(assistant_msg["content"], "")
        self.assertIsNotNone(assistant_msg["content"])

    def test_append_results_openai_none_content_becomes_empty(self):
        messages = []
        response = {"content": None, "tool_calls": [{"id": "1"}]}
        append_results_openai(messages, response, [{"id": "1", "content": "ok"}])
        self.assertEqual(messages[0]["content"], "")

    def test_normalize_for_openai_fixes_none_content_on_tool_call(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        result = normalize_messages_for_provider(msgs, "openai")
        for msg in result:
            self.assertIsNotNone(msg["content"])
            self.assertIsInstance(msg["content"], str)
        # Tool call messages should be preserved for multi-turn tool use
        self.assertEqual(len(result), 4)
        self.assertEqual(result[1]["content"], "")
        self.assertIn("tool_calls", result[1])

    def test_normalize_for_anthropic_fixes_none_content(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None},
        ]
        result = normalize_messages_for_provider(msgs, "anthropic")
        for msg in result:
            self.assertIsNotNone(msg["content"])

    def test_openai_multi_turn_tool_use_preserved(self):
        """After a tool call, assistant+tool_calls and tool results must
        remain in the message list so the model sees them on the next round."""
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "make a map"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"q":"map"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "search results here"},
            {"role": "assistant", "content": "Here's step 1. Now doing step 2.", "tool_calls": [
                {"id": "call_2", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"q":"step2"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_2", "content": "more results"},
        ]
        result = normalize_messages_for_provider(msgs, "openai")
        roles = [m["role"] for m in result]
        self.assertEqual(roles.count("tool"), 2, "tool results must be kept")
        self.assertEqual(roles.count("assistant"), 2, "tool-call assistants must be kept")
        tc_msgs = [m for m in result if m.get("tool_calls")]
        self.assertEqual(len(tc_msgs), 2, "tool_calls must be preserved")

    def test_normalize_on_switch_drops_none_content(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "assistant", "content": "done"},
        ]
        normalize_messages_on_switch(msgs, "openai")
        for msg in msgs:
            self.assertIsNotNone(msg["content"])
            self.assertIsInstance(msg["content"], str)

    def test_normalize_on_switch_drops_empty_messages(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "hi"},
        ]
        normalize_messages_on_switch(msgs, "openai")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["content"], "hi")


# ---------------------------------------------------------------------------
# Fallback chain & structural error detection
# ---------------------------------------------------------------------------

class TestFallbackChain(unittest.TestCase):
    def setUp(self):
        # Ollama model discovery is live; keep these tests network-free by
        # simulating an unreachable server.
        patcher = patch("conch.providers.list_ollama_models", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_provider_models_first(self):
        chain = get_fallback_chain("openai", "gpt-5.4")
        same_provider = [(p, m) for p, m, _ in chain if p == "openai"]
        cross_provider = [(p, m) for p, m, _ in chain if p != "openai"]
        self.assertTrue(len(same_provider) > 0)
        all_indices = [i for i, (p, m, _) in enumerate(chain)]
        if same_provider and cross_provider:
            last_same = max(i for i, (p, m, _) in enumerate(chain) if p == "openai")
            first_cross = min(i for i, (p, m, _) in enumerate(chain) if p != "openai")
            self.assertLess(last_same, first_cross)

    def test_unknown_model_tries_all(self):
        chain = get_fallback_chain("openai", "nonexistent-model")
        models = [m for p, m, _ in chain if p == "openai"]
        self.assertEqual(len(models), len(KNOWN_MODELS["openai"]))

    def test_cross_provider_needs_context_switch(self):
        chain = get_fallback_chain("openai", "gpt-5.4")
        cross = [(p, m, s) for p, m, s in chain if p != "openai"]
        for p, m, needs_switch in cross:
            self.assertTrue(needs_switch)


class TestGetFallbackModel(unittest.TestCase):
    def test_returns_stable_default(self):
        self.assertEqual(get_fallback_model("openai"), "gpt-4o-mini")
        self.assertEqual(get_fallback_model("anthropic"), "claude-sonnet-4-6")

    def test_unknown_provider_empty(self):
        self.assertEqual(get_fallback_model("nonexistent"), "")


class TestDefaultChatModelByProvider(unittest.TestCase):
    def test_openai_default_is_gpt4o_mini(self):
        self.assertEqual(DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"], "gpt-4o-mini")

    def test_all_providers_have_default(self):
        for provider in KNOWN_MODELS:
            self.assertIn(provider, DEFAULT_CHAT_MODEL_BY_PROVIDER)


# ---------------------------------------------------------------------------
# HTTP error body parsing
# ---------------------------------------------------------------------------

class TestFormatHttpApiError(unittest.TestCase):
    def test_non_http_error_passthrough(self):
        self.assertEqual(format_http_api_error(ValueError("oops")), "oops")

    def test_openai_json_error_body(self):
        import urllib.error
        body = json.dumps({
            "error": {
                "message": "The model 'gpt-99' does not exist",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        }).encode()
        exc = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            404, "Not Found", {}, io.BytesIO(body),
        )
        result = format_http_api_error(exc)
        self.assertIn("gpt-99", result)
        self.assertIn("invalid_request_error", result)
        self.assertIn("model_not_found", result)

    def test_anthropic_json_error_body(self):
        import urllib.error
        body = json.dumps({
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "messages.5.content: must be a string",
            }
        }).encode()
        exc = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages",
            400, "Bad Request", {}, io.BytesIO(body),
        )
        result = format_http_api_error(exc)
        self.assertIn("must be a string", result)

    def test_non_json_body_fallback(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            "https://example.com", 500, "Internal Server Error",
            {}, io.BytesIO(b"not json"),
        )
        result = format_http_api_error(exc)
        self.assertIn("500", result)


# ---------------------------------------------------------------------------
# conch_config tool: queued switch semantics
# ---------------------------------------------------------------------------

class TestConchConfigClient(unittest.TestCase):
    def setUp(self):
        self.client = ConchConfigClient()
        self.client.bind("anthropic", "claude-sonnet-4-6", {})

    def test_set_model_queues_action(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = self.client.call_tool("conch_config", {
                "action": "set_model", "value": "gpt-4o",
            })
        self.assertEqual(len(self.client.pending_actions), 1)
        self.assertEqual(self.client.pending_actions[0], ("set_model", "openai", "gpt-4o"))

    def test_set_model_response_says_queued(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = self.client.call_tool("conch_config", {
                "action": "set_model", "value": "gpt-4o",
            })
        text = result["content"][0]["text"]
        self.assertIn("queued", text.lower())
        self.assertIn("NEXT", text)
        self.assertIn("anthropic", text)

    def test_set_model_already_current(self):
        result = self.client.call_tool("conch_config", {
            "action": "set_model", "value": "claude-sonnet-4-6",
        })
        text = result["content"][0]["text"]
        self.assertIn("Already", text)
        self.assertEqual(len(self.client.pending_actions), 0)

    def test_set_provider_queues_default_model(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = self.client.call_tool("conch_config", {
                "action": "set_provider", "value": "openai",
            })
        self.assertEqual(len(self.client.pending_actions), 1)
        action = self.client.pending_actions[0]
        self.assertEqual(action[1], "openai")
        self.assertEqual(action[2], "gpt-4o-mini")

    def test_set_provider_already_current(self):
        result = self.client.call_tool("conch_config", {
            "action": "set_provider", "value": "anthropic",
        })
        text = result["content"][0]["text"]
        self.assertIn("Already", text)
        self.assertEqual(len(self.client.pending_actions), 0)

    def test_set_model_unknown(self):
        result = self.client.call_tool("conch_config", {
            "action": "set_model", "value": "gpt-99-turbo",
        })
        text = result["content"][0]["text"]
        self.assertIn("Unknown", text)
        self.assertEqual(len(self.client.pending_actions), 0)

    def test_get_returns_current_state(self):
        result = self.client.call_tool("conch_config", {"action": "get"})
        text = result["content"][0]["text"]
        self.assertIn("anthropic", text)
        self.assertIn("claude-sonnet-4-6", text)


# ---------------------------------------------------------------------------
# Model catalog consistency
# ---------------------------------------------------------------------------

class TestModelCatalog(unittest.TestCase):
    def test_all_known_models_have_pricing(self):
        for provider, models in KNOWN_MODELS.items():
            if provider == "ollama":
                continue
            for model in models:
                self.assertIn(
                    model, MODEL_PRICING,
                    f"{provider}/{model} missing from MODEL_PRICING",
                )

    def test_pricing_values_are_tuples(self):
        for model, price in MODEL_PRICING.items():
            self.assertIsInstance(price, tuple, f"{model} pricing should be a tuple")
            self.assertEqual(len(price), 2, f"{model} pricing should have 2 values")
            self.assertGreaterEqual(price[0], 0)
            self.assertGreaterEqual(price[1], 0)

    def test_estimate_cost_known_model(self):
        cost = estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 0.15 + 0.60)

    def test_estimate_cost_unknown_model_is_zero(self):
        cost = estimate_cost("nonexistent-model", 1_000_000, 1_000_000)
        self.assertEqual(cost, 0.0)

    def test_default_models_exist_in_catalog(self):
        for provider, model in DEFAULT_CHAT_MODEL_BY_PROVIDER.items():
            if provider == "ollama":
                continue  # ollama models are discovered live from the server
            self.assertIn(
                model, KNOWN_MODELS[provider],
                f"default {provider} model '{model}' not in KNOWN_MODELS",
            )

    def test_openai_has_current_models(self):
        openai_models = KNOWN_MODELS["openai"]
        self.assertIn("gpt-4o", openai_models)
        self.assertIn("gpt-4o-mini", openai_models)
        self.assertIn("o3", openai_models)
        self.assertIn("o4-mini", openai_models)
        self.assertIn("gpt-5.4", openai_models)


if __name__ == "__main__":
    unittest.main()
