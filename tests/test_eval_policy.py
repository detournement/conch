"""Evaluation safety and accounting regressions; no live inference required."""
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from conch import providers, runtime, seat_claude, seat_codex
from conch.eval_policy import EvaluationError, EvaluationPolicy
from conch.session import AgentSession


TOOLS = [{"type": "function", "function": {
    "name": "save_fixture", "description": "Save the synthetic value",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                   "required": ["value"], "additionalProperties": False},
}}]


def call(name="save_fixture", arguments=None):
    return {"id": "request", "type": "function", "function": {
        "name": name, "arguments": json.dumps({"value": "saved"}) if arguments is None else arguments,
    }}


def response(content="done", calls=None, observed=None, usage=None):
    result = {"role": "assistant", "content": content, "tool_calls": calls,
              "_model": "requested", "_usage": {"input_tokens": 5, "output_tokens": 2}}
    if observed is not None:
        result["_evaluation"] = {"observed_models": [observed],
                                 "usage": usage or {"input_tokens": 5, "output_tokens": 2},
                                 "complete": True}
    return result


class FileClient:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        self.path.write_text(arguments["value"], encoding="utf-8")
        return {"content": [{"type": "text", "text": "saved"}], "isError": False}


class EvaluationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.txt"
        self.client = FileClient(self.path)
        self.config = {"provider": "openai", "chat_model": "requested"}
        self.messages = [{"role": "system", "content": "Synthetic workspace only."},
                         {"role": "user", "content": "Save the fixture value."}]

    def turn(self, raw, policy, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return runtime.chat_turn(
                self.config, "openai", raw, self.messages, TOOLS,
                {"save_fixture": self.client}, {}, evaluation=policy, **kwargs)

    def test_no_helpers_hooks_self_description_or_calibration(self):
        raw = Mock(side_effect=[response("", [call()]), response("saved")])
        policy = EvaluationPolicy("openai", "requested")
        names = ("conch.runtime.auto_compact", "conch.runtime.compress_context",
                 "conch.runtime._graceful_exhaustion", "conch.runtime.record_token_calibration",
                 "conch.prompts.build_self_description", "conch.tooling.run_hook",
                 "conch.mcp.execute_tool")
        with contextlib.ExitStack() as stack:
            forbidden = [stack.enter_context(patch(name)) for name in names]
            reply, _ = self.turn(raw, policy)
            for helper in forbidden:
                helper.assert_not_called()
        self.assertEqual(reply, "saved")
        self.assertEqual(self.path.read_text(), "saved")
        self.assertEqual(raw.call_count, 2)
        for request in raw.call_args_list:
            self.assertEqual(request.args[0]["chat_model"], "requested")
            self.assertEqual(request.args[0]["max_output_tokens"], 1024)
            self.assertEqual(request.args[1][0]["content"], "Synthetic workspace only.")
        self.assertNotIn("_evaluation_policy", self.config)

    def test_error_never_retries_reloads_keys_or_falls_back(self):
        for detail, expected in (("429 rate limit", "rate_limit"),
                                 ("401 unauthorized", "auth"),
                                 ("connection refused", "unreachable")):
            with self.subTest(detail=detail):
                raw = Mock(return_value=providers.error_response(detail))
                policy = EvaluationPolicy("openai", "requested")
                with patch("conch.providers.get_fallback_chain") as fallback, \
                        patch("conch.runtime.retry_after_key_reload") as reload_key:
                    with self.assertRaises(EvaluationError) as caught:
                        self.turn(raw, policy)
                self.assertEqual(caught.exception.kind, expected)
                self.assertEqual(raw.call_count, 1)
                fallback.assert_not_called()
                reload_key.assert_not_called()
                self.assertEqual(policy.snapshot()["calls"], 1)
                self.assertFalse(policy.snapshot()["usage"]["complete"])

    def test_round_exhaustion_does_not_request_summary(self):
        raw = Mock(return_value=response("", [call()]))
        policy = EvaluationPolicy("openai", "requested")
        with patch("conch.runtime._graceful_exhaustion") as summary:
            with self.assertRaises(EvaluationError) as caught:
                self.turn(raw, policy, max_tool_rounds=1)
        self.assertEqual(caught.exception.kind, "limit")
        self.assertEqual(raw.call_count, 1)
        summary.assert_not_called()

    def test_token_exhaustion_does_not_request_summary(self):
        self.config["turn_token_budget"] = 1
        policy = EvaluationPolicy("openai", "requested")
        raw = Mock(return_value=response("", [call()]))
        with patch("conch.runtime._graceful_exhaustion") as summary:
            with self.assertRaises(EvaluationError) as caught:
                self.turn(raw, policy)
        self.assertEqual(caught.exception.kind, "limit")
        self.assertEqual(raw.call_count, 1)
        summary.assert_not_called()

    def test_call_budget_persists_across_session_followups(self):
        policy = EvaluationPolicy("openai", "requested", max_calls=2)
        session = AgentSession(self.config)
        self.addCleanup(session.close)
        raw = Mock(return_value=response())
        with patch.dict(providers.RAW_FNS, {"openai": raw}), \
                contextlib.redirect_stderr(io.StringIO()):
            for question in ("first", "followup"):
                session.run_turn([{"role": "user", "content": question}],
                                 tools=[], tool_map={}, evaluation=policy)
            with self.assertRaises(EvaluationError) as caught:
                session.run_turn([{"role": "user", "content": "third"}],
                                 tools=[], tool_map={}, evaluation=policy)
        self.assertEqual(caught.exception.kind, "limit")
        self.assertEqual(raw.call_count, 2)
        self.assertEqual(policy.snapshot()["calls"], 2)

    def test_tool_budget_persists_across_followups(self):
        policy = EvaluationPolicy("openai", "requested", max_tool_calls=1)
        raw = Mock(side_effect=[response("", [call()]), response(),
                                response("", [call(arguments='{"value":"changed"}')])])
        self.turn(raw, policy)
        self.messages.append({"role": "user", "content": "Change it"})
        with self.assertRaises(EvaluationError) as caught:
            self.turn(raw, policy)
        self.assertEqual(caught.exception.kind, "limit")
        self.assertEqual(len(policy.snapshot()["attempts"]), 2)
        self.assertEqual(self.path.read_text(), "saved")
        self.assertEqual(len(self.client.calls), 1)

    def test_overbudget_batch_records_every_attempt_before_rejection(self):
        attempts = [call(), call("forbidden", "{}"), call(arguments="not json")]
        policy = EvaluationPolicy("openai", "requested", max_tool_calls=2)
        raw = Mock(return_value=response("", attempts, observed="wire-model"))
        with self.assertRaises(EvaluationError) as caught:
            self.turn(raw, policy)
        self.assertEqual(caught.exception.kind, "limit")
        self.assertEqual([item["name"] for item in policy.snapshot()["attempts"]],
                         ["save_fixture", "forbidden", "save_fixture"])
        self.assertEqual(policy.snapshot()["attempts"][2]["arguments"], "not json")
        self.assertFalse(self.path.exists())
        self.assertEqual(policy.snapshot()["usage"]["output_tokens"], 2)

    def test_unoffered_and_invalid_tools_are_recorded_but_not_executed(self):
        raw = Mock(side_effect=[response("", [call("delete_everything", "{}"),
                                              call(arguments='{"value":42}')]), response()])
        policy = EvaluationPolicy("openai", "requested")
        self.turn(raw, policy)
        self.assertFalse(self.path.exists())
        self.assertEqual(policy.snapshot()["attempts"], [
            {"name": "delete_everything", "arguments": {}},
            {"name": "save_fixture", "arguments": {"value": 42}},
        ])
        tool_results = [message["content"] for message in self.messages if message["role"] == "tool"]
        self.assertEqual(len(tool_results), 2)
        self.assertTrue(all("Rejected" in item for item in tool_results))

    def test_textual_tool_attempt_is_captured_without_dispatch(self):
        raw = Mock(return_value=response('{"name":"delete_everything","arguments":{"path":"outside"}}'))
        policy = EvaluationPolicy("openai", "requested")
        with self.assertRaises(EvaluationError) as caught:
            self.turn(raw, policy)
        self.assertEqual(caught.exception.kind, "limit")
        self.assertEqual(policy.snapshot()["attempts"], [
            {"name": "delete_everything", "arguments": {"path": "outside"}}])
        self.assertFalse(self.path.exists())

    def test_expired_deadline_admits_no_request(self):
        # Monotonic's epoch can be process-relative on macOS/Python 3.9.
        with patch("conch.eval_policy.time.monotonic", return_value=100):
            policy = EvaluationPolicy("openai", "requested", deadline=99)
            raw = Mock()
            with self.assertRaises(EvaluationError) as caught:
                self.turn(raw, policy)
            self.assertEqual(caught.exception.kind, "timeout")
            raw.assert_not_called()

    def test_deadline_after_response_preserves_attempt_and_usage(self):
        policy = EvaluationPolicy("openai", "requested")
        def raw(*args):
            policy.deadline = time.monotonic() - 1
            return response("", [call()], observed="wire-model")
        with self.assertRaises(EvaluationError) as caught:
            self.turn(raw, policy)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(len(policy.snapshot()["attempts"]), 1)
        self.assertEqual(policy.snapshot()["usage"]["output_tokens"], 2)
        self.assertFalse(self.path.exists())

    def test_route_change_is_not_silently_evaluated(self):
        policy = EvaluationPolicy("openai", "another-model")
        raw = Mock()
        with self.assertRaises(EvaluationError) as caught:
            self.turn(raw, policy)
        self.assertEqual(caught.exception.kind, "model_fallback")
        self.assertEqual(policy.snapshot()["identity"], "fallback")
        raw.assert_not_called()


class EvaluationObservationTests(unittest.TestCase):
    def test_requested_display_model_does_not_become_observed(self):
        policy = EvaluationPolicy("openai", "requested")
        policy.begin_call({"chat_model": "requested"}, "openai")
        policy.observe(response())
        snapshot = policy.snapshot()
        self.assertEqual(snapshot["identity"], "unverified")
        self.assertEqual(snapshot["observed_models"], [])
        self.assertEqual(snapshot["usage"], {"input_tokens": None, "output_tokens": None, "complete": False})

    def test_partial_usage_and_unverified_call_never_become_complete(self):
        policy = EvaluationPolicy("openai", "requested")
        policy.begin_call({"chat_model": "requested"}, "openai")
        policy.observe(response(observed="canonical", usage={"input_tokens": 11}))
        policy.begin_call({"chat_model": "requested"}, "openai")
        policy.observe(response())
        snapshot = policy.snapshot()
        self.assertEqual(snapshot["observed_models"], ["canonical"])
        self.assertEqual(snapshot["identity"], "unverified")
        self.assertEqual(snapshot["usage"], {"input_tokens": 11, "output_tokens": None, "complete": False})

    def test_interrupted_call_marks_prior_usage_partial(self):
        policy = EvaluationPolicy("openai", "requested")
        config = {"chat_model": "requested"}
        policy.begin_call(config, "openai")
        policy.observe(response(observed="canonical"))
        policy.begin_call(config, "openai")
        self.assertFalse(policy.snapshot()["usage"]["complete"])
        self.assertEqual(policy.snapshot()["usage"]["input_tokens"], 5)
        self.assertEqual(policy.snapshot()["identity"], "unverified")

    def test_inflight_context_does_not_inherit_the_previous_requests_value(self):
        policy = EvaluationPolicy("ollama", "requested")
        config = {"chat_model": "requested"}
        policy.begin_call(config, "ollama")
        policy.record_context_length(4096)
        policy.begin_call(config, "ollama")
        self.assertEqual(policy.snapshot()["context_lengths"], [4096, None])
        policy.record_context_length(2048)
        self.assertEqual(policy.snapshot()["context_lengths"], [4096, 2048])

    def test_explicit_fallback_is_not_comparable(self):
        policy = EvaluationPolicy("openai", "requested")
        policy.begin_call({"chat_model": "requested"}, "openai")
        answer = response(observed="replacement")
        answer["_evaluation"]["fallback"] = True
        with self.assertRaises(EvaluationError) as caught:
            policy.observe(answer)
        self.assertEqual(caught.exception.kind, "model_fallback")
        self.assertEqual(policy.snapshot()["identity"], "fallback")
        self.assertEqual(policy.snapshot()["observed_models"], ["replacement"])

    def test_snapshot_does_not_expose_mutable_policy_state(self):
        policy = EvaluationPolicy("openai", "requested")
        policy.record_attempts([call()])
        snapshot = policy.snapshot()
        snapshot["attempts"][0]["arguments"]["value"] = "changed"
        self.assertEqual(policy.snapshot()["attempts"][0]["arguments"], {"value": "saved"})


class EvaluationTransportTests(unittest.TestCase):
    def http_response(self, payload):
        handle = Mock()
        handle.read.return_value = json.dumps(payload).encode()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock(return_value=False)
        return handle

    def test_custom_has_one_request_no_probe_and_wire_provenance(self):
        policy = EvaluationPolicy("custom", "private-alias", output_tokens=77)
        config = policy.configure({"provider": "custom", "chat_model": "private-alias",
                                   "custom_base_url": "http://127.0.0.1:8080/v1"}, "custom")
        payload = {"model": "actual-server-model", "usage": {"prompt_tokens": 7},
                   "choices": [{"message": {"content": "answer"}}]}
        with patch("conch.providers.urllib.request.urlopen", return_value=self.http_response(payload)) as http, \
                patch("conch.providers.validate_custom_model") as probe, \
                patch("conch.providers.get_custom_context_window") as context:
            policy.begin_call(config, "custom")
            result = providers.raw_custom(config, [{"role": "user", "content": "hello"}])
            policy.observe(result)
        self.assertEqual(http.call_count, 1)
        probe.assert_not_called()
        context.assert_not_called()
        body = json.loads(http.call_args.args[0].data)
        self.assertEqual(body["model"], "private-alias")
        self.assertEqual(body["max_tokens"], 77)
        self.assertEqual(policy.snapshot()["observed_models"], ["actual-server-model"])
        self.assertEqual(policy.snapshot()["usage"], {"input_tokens": 7, "output_tokens": None, "complete": False})

    def test_api_adapters_use_output_cap_and_genuine_metadata(self):
        targets = (("openai", providers.raw_openai, "OPENAI_API_KEY", "max_completion_tokens"),
                   ("cerebras", providers.raw_cerebras, "CEREBRAS_API_KEY", "max_completion_tokens"),
                   ("bedrock", providers.raw_bedrock, "AWS_BEARER_TOKEN_BEDROCK", "max_tokens"),
                   ("openrouter", providers.raw_openrouter, "OPENROUTER_API_KEY", "max_tokens"),
                   ("anthropic", providers.raw_anthropic, "ANTHROPIC_API_KEY", "max_tokens"))
        for provider, adapter, key, output_key in targets:
            with self.subTest(provider=provider):
                policy = EvaluationPolicy(provider, "requested", output_tokens=89)
                config = policy.configure({"provider": provider, "chat_model": "requested", "api_key_env": key}, provider)
                payload = {"choices": [{"message": {"content": "answer"}}],
                           "content": [{"type": "text", "text": "answer"}]}
                with patch.dict("os.environ", {key: "test-key"}), \
                        patch("conch.providers.urllib.request.urlopen", return_value=self.http_response(payload)) as http:
                    policy.begin_call(config, provider)
                    result = adapter(config, [{"role": "user", "content": "hello"}])
                    policy.observe(result)
                self.assertEqual(json.loads(http.call_args.args[0].data)[output_key], 89)
                self.assertEqual(policy.snapshot()["observed_models"], [])
                self.assertIsNone(policy.snapshot()["usage"]["input_tokens"])
                self.assertFalse(policy.snapshot()["usage"]["complete"])

    def test_ollama_avoids_discovery_and_preserves_malformed_attempts(self):
        policy = EvaluationPolicy("ollama", "requested", output_tokens=91)
        config = policy.configure({"provider": "ollama", "chat_model": "requested"}, "ollama")
        malformed = {"function": {"name": "forbidden", "arguments": "invalid"}}
        payload = {"model": "observed", "message": {"content": "", "tool_calls": [malformed]},
                   "prompt_eval_count": 3, "eval_count": 4}
        with patch("conch.providers.urllib.request.urlopen", return_value=self.http_response(payload)) as http, \
                patch("conch.providers.validate_ollama_model") as probe, \
                patch("conch.providers.get_ollama_effective_context") as context:
            policy.begin_call(config, "ollama")
            result = providers.raw_ollama(config, [{"role": "user", "content": "hello"}])
            policy.observe(result)
            policy.record_attempts(result["tool_calls"])
        probe.assert_not_called()
        context.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(json.loads(http.call_args.args[0].data)["options"]["num_predict"], 91)
        self.assertEqual(policy.snapshot()["attempts"], [{"name": "forbidden", "arguments": "invalid"}])
        self.assertTrue(policy.snapshot()["usage"]["complete"])

    def test_native_evaluations_never_launch_auth_or_inference(self):
        for provider, module in (("claude-code", seat_claude), ("codex", seat_codex)):
            with self.subTest(provider=provider):
                policy = EvaluationPolicy(provider, "selected")
                with patch.object(module, "run") as run:
                    with self.assertRaises(EvaluationError) as caught:
                        module.invoke("selected", "synthetic", {}, 5, evaluation=policy)
                self.assertEqual(caught.exception.kind, "unsupported")
                run.assert_not_called()
                with self.assertRaises(EvaluationError) as admission:
                    policy.configure({"provider": provider, "chat_model": "selected"}, provider)
                self.assertEqual(admission.exception.kind, "unsupported")
                self.assertEqual(policy.snapshot()["calls"], 0)


if __name__ == "__main__":
    unittest.main()
