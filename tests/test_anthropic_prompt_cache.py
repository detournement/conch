"""Anthropic prompt caching: the request opts in, usage reports the full
prompt size (so context gauges and token calibration stay right), and cost
prices cached input at its discounted rate."""

import io
import json
import os
import unittest
from unittest import mock

from conch import providers

TOOLS = [{"type": "function", "function": {
    "name": "say", "parameters": {"type": "object", "properties": {}}}}]
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def _capture(fn, config):
    sent = {}

    def urlopen(request, timeout=None):
        sent.update(json.loads(request.data))
        raise OSError("stop here")

    with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), \
            mock.patch("urllib.request.urlopen", urlopen):
        fn(config, MESSAGES, TOOLS)
    return sent


class _SSE(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(*events):
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return _SSE("".join(lines).encode())


class RequestShape(unittest.TestCase):
    def test_both_send_paths_enable_automatic_caching(self):
        config = {"chat_model": "claude-opus-5-5"}
        for fn in (providers.raw_anthropic, providers.stream_anthropic):
            with self.subTest(fn=fn.__name__):
                body = _capture(fn, config)
                self.assertEqual(body["cache_control"], {"type": "ephemeral"})

    def test_opt_out(self):
        for value in ("false", False, "off", "0"):
            config = {"chat_model": "claude-opus-5-5", "anthropic_prompt_cache": value}
            with self.subTest(value=value):
                self.assertNotIn("cache_control", _capture(providers.raw_anthropic, config))


class UsageAccounting(unittest.TestCase):
    def test_input_tokens_is_full_prompt_with_cache_counters_alongside(self):
        usage = providers._normalize_usage({"usage": {
            "input_tokens": 50, "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 950, "output_tokens": 7}}, "anthropic")
        self.assertEqual(usage, {
            "input_tokens": 10000, "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 950, "output_tokens": 7})

    def test_uncached_response_unchanged(self):
        usage = providers._normalize_usage(
            {"usage": {"input_tokens": 12, "output_tokens": 3}}, "anthropic")
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 3})

    def test_stream_reports_full_prompt_size(self):
        stream = _sse(
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 20, "cache_read_input_tokens": 4000,
                "cache_creation_input_tokens": 80}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        )
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), \
                mock.patch("urllib.request.urlopen", return_value=stream):
            response = providers.stream_anthropic(
                {"chat_model": "claude-opus-5-5"}, MESSAGES, None)
        self.assertEqual(response["_usage"]["input_tokens"], 4100)
        self.assertEqual(response["_usage"]["cache_read_input_tokens"], 4000)
        self.assertEqual(response["_usage"]["cache_creation_input_tokens"], 80)
        self.assertEqual(response["_usage"]["output_tokens"], 2)

    def test_turn_totals_carry_cache_counters(self):
        from conch.runtime import _add_cache_usage

        totals = {"input_tokens": 0, "output_tokens": 0}
        _add_cache_usage(totals, {"cache_read_input_tokens": 10})
        _add_cache_usage(totals, {"cache_read_input_tokens": 5,
                                  "cache_creation_input_tokens": 3})
        self.assertEqual(totals["cache_read_input_tokens"], 15)
        self.assertEqual(totals["cache_creation_input_tokens"], 3)


class CachedCost(unittest.TestCase):
    def test_cached_input_is_discounted(self):
        model = next(iter(providers.MODEL_PRICING))
        rate_in, _ = providers.MODEL_PRICING[model]
        full = providers.estimate_cost(model, 1_000_000, 0)
        cached = providers.estimate_cost(
            model, 1_000_000, 0, cache_read_tokens=900_000, cache_write_tokens=100_000)
        self.assertAlmostEqual(full, rate_in)
        self.assertAlmostEqual(cached, rate_in * (0.9 * 0.1 + 0.1 * 1.25))

    def test_record_session_usage_passes_cache_counts(self):
        model = next(iter(providers.MODEL_PRICING))
        usage = {}
        with mock.patch("conch.providers._billing_kind", return_value="api"):
            providers.record_session_usage(
                usage, "anthropic", model, 1_000_000, 0, cache_read_tokens=1_000_000)
        rate_in, _ = providers.MODEL_PRICING[model]
        self.assertAlmostEqual(usage["cost"], rate_in * 0.1)
        self.assertEqual(usage["input_tokens"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
