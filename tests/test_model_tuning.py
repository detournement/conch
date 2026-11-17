"""Per-model reasoning effort and context window: what each model offers,
how choices are stored and cycled, and what reaches each provider."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from conch import model_tuning as mt
from conch.model_catalog import ModelEntry
from conch.model_picker import ModelPicker

TOOLS = [{"type": "function", "function": {
    "name": "say", "parameters": {"type": "object", "properties": {}}}}]


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name,
                                           "ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "x"})
        env.start()
        self.addCleanup(env.stop)

    def saved(self):
        from conch.config import load_config

        return load_config()


class StorageTests(_ConfigCase):
    def test_each_model_keeps_its_own_effort_and_cycling_returns_to_default(self):
        config = {}
        seen = [mt.cycle_effort(config, "claude-code", "opus")[0] for _ in range(6)]
        self.assertEqual(seen, ["low", "medium", "high", "xhigh", "max", None])
        mt.cycle_effort(config, "claude-code", "opus")
        mt.cycle_effort(config, "codex", "default")
        mt.cycle_effort(config, "codex", "default")
        saved = self.saved()
        self.assertEqual(mt.effort_for(saved, "claude-code", "opus"), "low")
        self.assertEqual(mt.effort_for(saved, "codex", "default"), "medium")
        self.assertEqual(mt.effort_for(saved, "claude-code", "sonnet"), "")

    def test_ollama_names_match_with_or_without_the_latest_tag(self):
        config = {"model_effort": "ollama/qwen3:latest=on", "model_context": "ollama/qwen3:latest=32768"}
        self.assertEqual(mt.effort_for(config, "ollama", "qwen3"), "on")
        self.assertEqual(mt.context_for(config, "ollama", "qwen3"), 32768)
        self.assertEqual(mt.describe(config, "ollama", "qwen3"), "on · 32K ctx")

    def test_values_a_model_does_not_accept_are_never_sent(self):
        config = {"model_effort": "anthropic/claude-haiku-4-5=high, anthropic/claude-opus-4-6=xhigh,"
                                  " openai/gpt-6-sol=high, claude-code/opus=turbo"}
        for provider, model in (("anthropic", "claude-haiku-4-5"), ("anthropic", "claude-opus-4-6"),
                                ("openai", "gpt-6-sol"), ("claude-code", "opus")):
            with self.subTest(model=model):
                self.assertEqual(mt.effort_for(config, provider, model), "")

    def test_only_ollama_offers_context_sizes_up_to_the_model_maximum(self):
        self.assertEqual(mt.context_sizes("anthropic", "claude-opus-5-5"), ())
        self.assertEqual(mt.context_sizes("custom", "qwen"), ())
        with mock.patch("conch.providers.get_ollama_context_length", return_value=40960):
            self.assertEqual(mt.context_sizes("ollama", "qwen3"), (4096, 8192, 16384, 32768, 40960))


class RequestTests(unittest.TestCase):
    def capture_anthropic(self, config):
        from conch import providers

        sent = {}

        def urlopen(request, timeout=None):
            sent.update(json.loads(request.data))
            raise OSError("stop here")

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), \
                mock.patch("urllib.request.urlopen", urlopen):
            providers.raw_anthropic(config, [{"role": "user", "content": "hi"}], TOOLS)
        return sent

    def test_anthropic_sends_output_config_effort_only_when_chosen(self):
        base = {"chat_model": "claude-opus-5-5"}
        self.assertNotIn("output_config", self.capture_anthropic(base))
        chosen = self.capture_anthropic({**base, "model_effort": "anthropic/claude-opus-5-5=xhigh"})
        self.assertEqual(chosen["output_config"], {"effort": "xhigh"})

    def test_openai_effort_except_where_tools_need_none(self):
        from conch.providers import build_openai_chat_request_body

        body = build_openai_chat_request_body("gpt-5.5", [], temperature=0.7, max_completion_tokens=8,
                                              tools=TOOLS, reasoning_effort="high")
        self.assertEqual(body["reasoning_effort"], "high")
        body = build_openai_chat_request_body("gpt-6-sol", [], temperature=0.7, max_completion_tokens=8,
                                              tools=TOOLS, reasoning_effort="high")
        self.assertEqual(body["reasoning_effort"], "none")

    def test_openrouter_keeps_low_unless_a_model_has_its_own(self):
        from conch.providers import _openrouter_body

        self.assertEqual(_openrouter_body({"chat_model": "z-ai/glm-5.2"}, [], None)["reasoning_effort"], "low")
        config = {"chat_model": "moonshotai/kimi-k3", "model_effort": "openrouter/moonshotai/kimi-k3=high"}
        self.assertEqual(_openrouter_body(config, [], None)["reasoning_effort"], "high")

    def test_ollama_model_choice_wins_over_global_keys_for_that_model_only(self):
        from conch.providers import apply_ollama_request_options

        config = {"ollama_think": "false", "ollama_num_ctx": "8192",
                  "model_effort": "ollama/qwen3:32b=on", "model_context": "ollama/qwen3:32b=32768"}
        with mock.patch("conch.providers.get_ollama_context_length", return_value=262144):
            tuned, other = {}, {}
            apply_ollama_request_options(tuned, config, "qwen3:32b")
            apply_ollama_request_options(other, config, "llama3:8b")
        self.assertEqual((tuned["think"], tuned["options"]["num_ctx"]), (True, 32768))
        self.assertEqual((other["think"], other["options"]["num_ctx"]), (False, 8192))


class SeatTests(unittest.TestCase):
    def test_seat_effort_reaches_the_transport_only_when_chosen(self):
        from conch import seats

        calls = []

        class Transport:
            @staticmethod
            def invoke(model, prompt, schema, timeout, on_detail=None, **extra):
                calls.append(extra)
                return {"response": {"content": "ok", "tool_calls": []}, "usage": {}, "model": model}

        with mock.patch("conch.seats.transport", return_value=Transport):
            seats.complete({"provider": "claude-code", "chat_model": "opus"}, [], TOOLS)
            seats.complete({"provider": "claude-code", "chat_model": "opus",
                            "model_effort": "claude-code/opus=max"}, [], TOOLS)
        self.assertEqual(calls, [{}, {"effort": "max"}])

    def test_claude_cli_receives_the_effort_flag(self):
        from conch import seat_claude

        identity = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
        result = {"type": "result", "subtype": "success", "is_error": False,
                  "structured_output": {"content": "ok", "tool_calls": []}}
        argvs = []

        def run(argv, **kwargs):
            argvs.append(argv)
            payload = identity if "auth" in argv else result
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with mock.patch.object(seat_claude, "run", side_effect=run):
            seat_claude.invoke("opus", "prompt", {"type": "object"}, 20, effort="high")
        self.assertEqual(argvs[-1][argvs[-1].index("--effort") + 1], "high")


class PickerTests(_ConfigCase):
    def picker(self, entries):
        picker = ModelPicker({"provider": "claude-code", "chat_model": "opus"}, "claude-code", "opus")
        picker.entries = entries
        picker._refresh_rows()
        return picker

    def test_ctrl_e_steps_effort_and_the_row_shows_it(self):
        picker = self.picker([ModelEntry("claude-code", "opus", billing="seat")])
        picker.selected_key = ("claude-code", "opus")
        picker.cycle_effort()
        picker.cycle_effort()
        self.assertEqual(mt.effort_for(self.saved(), "claude-code", "opus"), "medium")
        self.assertIn("medium", picker.notice)
        row = "".join(text for _, text in picker._render_list(120, 5)[1])
        self.assertIn("[medium]", row)

    def test_providers_without_the_setting_say_so_and_store_nothing(self):
        picker = self.picker([ModelEntry("custom", "qwen", source="server")])
        picker.selected_key = ("custom", "qwen")
        picker.cycle_effort()
        self.assertIn("no effort setting", picker.notice)
        picker.cycle_context()
        self.assertIn("fixed by its server", picker.notice)
        self.assertNotIn("model_effort", self.saved())


if __name__ == "__main__":
    unittest.main()
