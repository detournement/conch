"""Tests for Phase 1 context economy (plan 1.1–1.5).

1.1: byte-stable system prompt (KV-cache reuse); volatile context rides on
     the user message.
1.2: compact local system prompt; no credential injection anywhere.
1.3: token estimates calibrated from Ollama's prompt_eval_count; context
     gauge helper.
1.4: model-generated compaction of older history at ~70% of the window.
1.5: token-aware tool-result truncation.
"""

import unittest
from unittest.mock import patch

from conch import app as app_mod
from conch import runtime
from conch.app import _augment_user_message, _build_system_prompt
from conch.prompts import get_chat_prompt
from conch.providers import error_response
from conch.runtime import (
    CHARS_PER_TOKEN,
    auto_compact,
    estimate_tokens,
    format_context_gauge,
    get_chars_per_token,
    record_token_calibration,
    reset_token_calibration,
    tool_result_char_budget,
    truncate_middle,
    truncate_tool_result,
)


class TestStableSystemPrompt(unittest.TestCase):
    def test_no_timestamp_in_system_prompt(self):
        prompt = _build_system_prompt("base", provider="anthropic",
                                      model="claude-sonnet-4-6", config={})
        self.assertNotIn("Current date and time", prompt)
        self.assertNotIn("Current date/time", prompt)

    def test_byte_stable_across_calls(self):
        args = dict(provider="anthropic", model="claude-sonnet-4-6", config={})
        first = _build_system_prompt("base", **args)
        second = _build_system_prompt("base", **args)
        self.assertEqual(first, second)

    def test_location_and_self_description_kept(self):
        prompt = _build_system_prompt("base", location="Austin, TX",
                                      provider="anthropic",
                                      model="claude-sonnet-4-6", config={})
        self.assertIn("Austin, TX", prompt)
        self.assertIn("anthropic/claude-sonnet-4-6", prompt)


class TestAugmentUserMessage(unittest.TestCase):
    def test_timestamp_attached_to_user_message(self):
        text = _augment_user_message("list files")
        self.assertTrue(text.startswith("list files"))
        self.assertIn("[context]", text)
        self.assertIn("Current date/time", text)

    def test_memory_context_attached(self):
        text = _augment_user_message("hi", mem_context="Relevant memories:\n- likes cats")
        self.assertIn("likes cats", text)

    def test_no_memory_context_omitted(self):
        text = _augment_user_message("hi")
        self.assertNotIn("memories", text.lower())


class TestSlimLocalPrompt(unittest.TestCase):
    def test_ollama_prompt_is_compact(self):
        prompt = get_chat_prompt("ollama", "qwen3.6:27b")
        # ~250 tokens at ~3.5 chars/token — allow headroom but stay far
        # below the old ~990-token prompt.
        self.assertLess(len(prompt), 1600, "local prompt must stay compact")

    def test_ollama_prompt_has_core_tools(self):
        prompt = get_chat_prompt("ollama", "qwen3.6:27b")
        for tool in ("local_shell", "save_memory", "conch_config"):
            self.assertIn(tool, prompt)

    def test_ollama_prompt_drops_slash_command_docs(self):
        prompt = get_chat_prompt("ollama", "qwen3.6:27b")
        self.assertNotIn("/schedule", prompt)
        self.assertNotIn("api_layer", prompt)

    def test_no_credential_saving_instruction_anywhere(self):
        for provider in ("cerebras", "anthropic", "openai", "ollama"):
            prompt = get_chat_prompt(provider, "m")
            self.assertNotIn("ALWAYS save it to memory", prompt)
            self.assertNotIn("API key, credential", prompt)

    def test_credential_injection_removed_from_app(self):
        self.assertFalse(
            hasattr(app_mod, "_load_config_credentials"),
            "config credential dump must not be injected into prompts",
        )
        prompt = _build_system_prompt("base", provider="anthropic",
                                      model="claude-sonnet-4-6", config={})
        self.assertNotIn("Available credentials", prompt)


class CalibrationTestCase(unittest.TestCase):
    def setUp(self):
        reset_token_calibration()
        self.addCleanup(reset_token_calibration)


class TestTokenCalibration(CalibrationTestCase):
    def test_default_before_calibration(self):
        self.assertEqual(get_chars_per_token(), CHARS_PER_TOKEN)

    def test_tiny_samples_ignored(self):
        record_token_calibration(100, 10)  # below the trust threshold
        self.assertEqual(get_chars_per_token(), CHARS_PER_TOKEN)

    def test_calibrates_to_real_ratio(self):
        record_token_calibration(3000, 1000)  # 3.0 chars/token
        self.assertAlmostEqual(get_chars_per_token(), 3.0)

    def test_ratio_clamped(self):
        record_token_calibration(100000, 1000)  # absurd 100 chars/token
        self.assertLessEqual(get_chars_per_token(), 8.0)

    def test_zero_inputs_ignored(self):
        record_token_calibration(0, 500)
        record_token_calibration(500, 0)
        self.assertEqual(get_chars_per_token(), CHARS_PER_TOKEN)

    def test_estimate_tokens_uses_calibration(self):
        msgs = [{"role": "user", "content": "x" * 3000}]
        before = estimate_tokens(msgs)
        record_token_calibration(3000, 1500)  # 2.0 chars/token
        after = estimate_tokens(msgs)
        self.assertEqual(after, 1500)
        self.assertGreater(after, before)


class TestContextGauge(unittest.TestCase):
    def test_percentage_shown(self):
        gauge = format_context_gauge(5000, 10000)
        self.assertIn("50%", gauge)
        self.assertIn("ctx", gauge)

    def test_colors_by_severity(self):
        self.assertIn("\033[32m", format_context_gauge(10, 100))   # green
        self.assertIn("\033[33m", format_context_gauge(65, 100))   # yellow
        self.assertIn("\033[31m", format_context_gauge(85, 100))   # red

    def test_empty_for_zero_window(self):
        self.assertEqual(format_context_gauge(10, 0), "")


class TestToolResultTruncation(CalibrationTestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(truncate_middle("hello", 100), "hello")

    def test_keeps_head_and_tail(self):
        text = "HEAD" + "x" * 10000 + "TAIL"
        result = truncate_middle(text, 1000)
        self.assertLess(len(result), 1200)
        self.assertTrue(result.startswith("HEAD"))
        self.assertTrue(result.endswith("TAIL"))
        self.assertIn("truncated", result)

    def test_budget_scales_with_window(self):
        with patch("conch.runtime.get_context_limit", return_value=100000):
            big = tool_result_char_budget("ollama", {})
        with patch("conch.runtime.get_context_limit", return_value=10000):
            small = tool_result_char_budget("ollama", {})
        self.assertGreater(big, small)

    def test_budget_has_floor(self):
        with patch("conch.runtime.get_context_limit", return_value=2048):
            budget = tool_result_char_budget("ollama", {})
        self.assertGreaterEqual(budget, int(500 * CHARS_PER_TOKEN))

    def test_truncate_tool_result_applies_budget(self):
        with patch("conch.runtime.get_context_limit", return_value=10000):
            result = truncate_tool_result("y" * 100000, "ollama", {})
        self.assertLess(len(result), 100000)
        self.assertIn("truncated", result)


class TestAutoCompact(CalibrationTestCase):
    def _history(self, n_pairs=10, size=200):
        msgs = [{"role": "system", "content": "sys prompt"}]
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"question {i} " + "x" * size})
            msgs.append({"role": "assistant", "content": f"answer {i} " + "y" * size})
        return msgs

    def test_below_threshold_untouched(self):
        msgs = self._history()
        raw_fn_calls = []
        with patch("conch.runtime.get_context_limit", return_value=10**9):
            changed = auto_compact(msgs, None, "ollama", {}, lambda *a: raw_fn_calls.append(a))
        self.assertFalse(changed)
        self.assertEqual(len(msgs), 21)
        self.assertEqual(raw_fn_calls, [])

    def test_compacts_old_history_with_llm_summary(self):
        msgs = self._history()
        seen = {}

        def raw_fn(config, summary_messages, tools):
            seen["messages"] = summary_messages
            return {"content": "- fact one\n- decision two", "tool_calls": None}

        with patch("conch.runtime.get_context_limit", return_value=100):
            changed = auto_compact(msgs, None, "ollama", {}, raw_fn)
        self.assertTrue(changed)
        self.assertEqual(msgs[0]["content"], "sys prompt", "system prompt kept verbatim")
        self.assertIn("[Earlier conversation summarized]", msgs[1]["content"])
        self.assertIn("fact one", msgs[1]["content"])
        # last 6 messages kept verbatim
        self.assertEqual(msgs[-1]["content"], "answer 9 " + "y" * 200)
        self.assertEqual(len(msgs), 1 + 1 + 6)
        # The summarizer saw the *old* turns
        transcript = seen["messages"][1]["content"]
        self.assertIn("question 0", transcript)
        self.assertNotIn("question 9", transcript)

    def test_error_summary_leaves_history_alone(self):
        msgs = self._history()
        original = [dict(m) for m in msgs]

        def raw_fn(config, summary_messages, tools):
            return error_response("connection refused")

        with patch("conch.runtime.get_context_limit", return_value=100):
            changed = auto_compact(msgs, None, "ollama", {}, raw_fn)
        self.assertFalse(changed)
        self.assertEqual(msgs, original)

    def test_tool_result_boundary_respected(self):
        # The cut point must not orphan a tool result from its tool call.
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(8):
            msgs.append({"role": "user", "content": f"u{i} " + "x" * 100})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 100})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "t", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "c1", "content": "result " + "z" * 100})
        for i in range(2):
            msgs.append({"role": "user", "content": f"late{i} " + "x" * 100})
            msgs.append({"role": "assistant", "content": f"lateans{i} " + "y" * 100})

        def raw_fn(config, summary_messages, tools):
            return {"content": "- summary", "tool_calls": None}

        with patch("conch.runtime.get_context_limit", return_value=100):
            auto_compact(msgs, None, "ollama", {}, raw_fn)
        for i, m in enumerate(msgs):
            if m.get("role") == "tool":
                self.assertTrue(
                    msgs[i - 1].get("tool_calls"),
                    "tool result must directly follow its tool call",
                )


if __name__ == "__main__":
    unittest.main()
