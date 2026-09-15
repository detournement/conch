"""The per-message token stats line: gating, toggle, and tok/s sources.

The line is on by default (show_token_stats), /tks flips it for the
session, and tok/s prefers server-reported generation time (Ollama's
eval_duration, llama.cpp's timings) over the ~-labeled wall-clock
estimate.
"""

import io
import unittest
from unittest.mock import patch

from conch.app import format_turn_stats_line
from conch.commands import handle_slash_command, slash_command_names
from conch.config import DEFAULT_CONFIG
from conch.providers import _normalize_usage
from conch.runtime import format_token_speed, record_call_timing


class TestSpeedSources(unittest.TestCase):
    def test_ollama_eval_duration_is_exact(self):
        usage = _normalize_usage(
            {"prompt_eval_count": 100, "eval_count": 50,
             "eval_duration": 2_000_000_000},  # 2s in ns
            "ollama",
        )
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertAlmostEqual(usage["gen_seconds"], 2.0)

    def test_llamacpp_timings_predicted_ms(self):
        usage = _normalize_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 200},
             "timings": {"predicted_ms": 4000.0}},
            "openai",
        )
        self.assertAlmostEqual(usage["gen_seconds"], 4.0)

    def test_llamacpp_timings_per_second_fallback(self):
        usage = _normalize_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 120},
             "timings": {"predicted_n": 120, "predicted_per_second": 60.0}},
            "openai",
        )
        self.assertAlmostEqual(usage["gen_seconds"], 2.0)

    def test_cloud_without_timings_has_no_gen_seconds(self):
        for provider, payload in (
            ("anthropic", {"usage": {"input_tokens": 5, "output_tokens": 7}}),
            ("openai", {"usage": {"prompt_tokens": 5, "completion_tokens": 7}}),
        ):
            usage = _normalize_usage(payload, provider)
            self.assertNotIn("gen_seconds", usage, provider)

    def test_malformed_timings_ignored(self):
        usage = _normalize_usage(
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1},
             "timings": {"predicted_ms": "soon"}},
            "openai",
        )
        self.assertNotIn("gen_seconds", usage)


class TestFormatTokenSpeed(unittest.TestCase):
    def test_server_time_is_unlabeled(self):
        line = format_token_speed(
            {"output_tokens": 120, "gen_seconds": 2.0, "wall_seconds": 5.0}
        )
        self.assertEqual(line, "60.0 tok/s")

    def test_wall_clock_is_estimated(self):
        line = format_token_speed(
            {"output_tokens": 100, "wall_seconds": 4.0}
        )
        self.assertEqual(line, "~25.0 tok/s")

    def test_fast_speeds_drop_the_decimal(self):
        line = format_token_speed(
            {"output_tokens": 500, "gen_seconds": 2.0}
        )
        self.assertEqual(line, "250 tok/s")

    def test_mixed_turn_downgrades_to_estimate(self):
        # One call had server timing, another didn't: the whole turn is
        # a wall-clock estimate rather than a mixed denominator.
        line = format_token_speed({
            "output_tokens": 100, "gen_seconds": 1.0,
            "wall_seconds": 10.0, "speed_estimated": True,
        })
        self.assertEqual(line, "~10.0 tok/s")

    def test_silent_without_tokens_or_duration(self):
        self.assertEqual(format_token_speed({"output_tokens": 0}), "")
        self.assertEqual(format_token_speed({"output_tokens": 10}), "")


class TestRecordCallTiming(unittest.TestCase):
    def test_server_timed_calls_accumulate(self):
        total = {"input_tokens": 0, "output_tokens": 0, "model": ""}
        record_call_timing(total, {"output_tokens": 10, "gen_seconds": 1.5}, 2.0)
        record_call_timing(total, {"output_tokens": 20, "gen_seconds": 0.5}, 1.0)
        self.assertAlmostEqual(total["gen_seconds"], 2.0)
        self.assertAlmostEqual(total["wall_seconds"], 3.0)
        self.assertNotIn("speed_estimated", total)

    def test_untimed_generating_call_marks_estimated(self):
        total = {"input_tokens": 0, "output_tokens": 0, "model": ""}
        record_call_timing(total, {"output_tokens": 10, "gen_seconds": 2.0}, 3.0)
        record_call_timing(total, {"output_tokens": 5}, 1.0)
        self.assertTrue(total["speed_estimated"])

    def test_zero_output_call_does_not_mark_estimated(self):
        # e.g. an error response with no usage: no honesty downgrade.
        total = {"input_tokens": 0, "output_tokens": 0, "model": ""}
        record_call_timing(total, {}, 0.5)
        self.assertNotIn("speed_estimated", total)


TURN = {"input_tokens": 1200, "output_tokens": 300, "gen_seconds": 5.0}


class TestStatsLineGating(unittest.TestCase):
    def test_on_by_default_with_empty_config(self):
        line = format_turn_stats_line(TURN, "free", "ctx 3%", "m1", {})
        self.assertIn("1,200 in / 300 out", line)
        self.assertIn("60.0 tok/s", line)
        self.assertIn("(m1)", line)

    def test_default_config_carries_the_key_on(self):
        self.assertEqual(DEFAULT_CONFIG.get("show_token_stats"), "true")

    def test_config_off_suppresses_the_line(self):
        line = format_turn_stats_line(
            TURN, "free", "", "m1", {"show_token_stats": "false"}
        )
        self.assertEqual(line, "")

    def test_line_omits_speed_when_unmeasured(self):
        line = format_turn_stats_line(
            {"input_tokens": 10, "output_tokens": 5}, "free", "", "m1", {}
        )
        self.assertIn("10 in / 5 out", line)
        self.assertNotIn("tok/s", line)


class TestTksToggle(unittest.TestCase):
    def _run(self, arg, config):
        with patch("sys.stdout", io.StringIO()) as out:
            result = handle_slash_command(
                f"/tks {arg}".strip(), config, "openai", "gpt-4o-mini",
                lambda *_: None,
            )
        return result, out.getvalue()

    def test_toggle_flips_from_default_on(self):
        config = {}
        result, out = self._run("", config)
        self.assertIsNone(result)
        self.assertEqual(config["show_token_stats"], "false")
        self.assertIn("off", out)
        self._run("", config)
        self.assertEqual(config["show_token_stats"], "true")

    def test_explicit_on_off(self):
        config = {}
        self._run("off", config)
        self.assertEqual(config["show_token_stats"], "false")
        self._run("on", config)
        self.assertEqual(config["show_token_stats"], "true")

    def test_registered_in_slash_commands(self):
        self.assertIn("/tks", slash_command_names())


if __name__ == "__main__":
    unittest.main()
