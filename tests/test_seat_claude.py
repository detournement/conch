"""Consumer-visible parsing and failure boundaries for the native Claude seat."""

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import seat_claude
from conch.seat_common import SeatError, run

RESPONSE = {"content": "hello", "tool_calls": []}
SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string"}, "tool_calls": {"type": "array"}},
    "required": ["content", "tool_calls"],
}
IDENTITY = {
    "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
    "subscriptionType": "pro",
}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "structured_output": RESPONSE,
}


def completed(payload, code=0):
    return subprocess.CompletedProcess(["claude"], code, json.dumps(payload), "")


class ClaudeSeatTransportTests(unittest.TestCase):
    def invoke(self, result, code=0):
        with patch.object(seat_claude, "run", side_effect=[
            completed(IDENTITY), completed(result, code),
        ]):
            return seat_claude.invoke("sonnet", "conversation", SCHEMA, 20)

    def test_structured_response_and_all_model_usage_include_cache_tokens(self):
        result = {
            **RESULT,
            "usage": {"input_tokens": 9999, "output_tokens": 9999},
            "modelUsage": {
                "claude-sonnet-5": {
                    "inputTokens": 5, "outputTokens": 7,
                    "cacheReadInputTokens": 11, "cacheCreationInputTokens": 13,
                },
                "claude-haiku-4-5": {
                    "inputTokens": 2, "outputTokens": 3,
                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 1,
                },
            },
        }
        response = self.invoke(result)
        self.assertEqual(response["response"], RESPONSE)
        self.assertEqual(response["usage"], {"input_tokens": 32, "output_tokens": 10})

    def test_single_actual_model_is_reported(self):
        response = self.invoke({**RESULT, "modelUsage": {
            "claude-sonnet-5": {"inputTokens": 1, "outputTokens": 2},
        }})
        self.assertEqual(response["model"], "claude-sonnet-5")

    def test_main_loop_usage_fallback_includes_cache(self):
        response = self.invoke({**RESULT, "usage": {
            "input_tokens": 2, "output_tokens": 3,
            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7,
        }})
        self.assertEqual(response["usage"], {"input_tokens": 14, "output_tokens": 3})

    def test_unknown_usage_is_not_invented(self):
        self.assertEqual(self.invoke(RESULT)["usage"], {})

    def test_nonzero_exit_never_exposes_native_output(self):
        with self.assertRaises(SeatError) as caught:
            self.invoke({**RESULT, "result": "private-account-secret"}, code=1)
        self.assertNotIn("private-account-secret", str(caught.exception))

    def test_missing_or_incomplete_structured_output_fails_closed(self):
        cases = [
            {}, [],
            {**RESULT, "structured_output": None},
            {**RESULT, "structured_output": json.dumps(RESPONSE)},
            {**RESULT, "is_error": True},
            {**RESULT, "subtype": "error_max_structured_output_retries"},
            {**RESULT, "api_error_status": 429},
            {**RESULT, "errors": ["native private error"]},
            {**RESULT, "permission_denials": [{"tool_name": "Bash"}]},
        ]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(SeatError):
                self.invoke(result)

    def test_malformed_usage_fails_closed(self):
        for usage in ({"input_tokens": True, "output_tokens": 1},
                      {"input_tokens": 1, "output_tokens": -1},
                      {"input_tokens": "1", "output_tokens": 1}):
            with self.subTest(usage=usage), self.assertRaises(SeatError):
                self.invoke({**RESULT, "usage": usage})

    def test_malformed_native_json_is_not_treated_as_text_response(self):
        with patch.object(seat_claude, "run", side_effect=[
            completed(IDENTITY), subprocess.CompletedProcess(["claude"], 0, "not JSON", ""),
        ]), self.assertRaisesRegex(SeatError, "malformed result JSON"):
            seat_claude.invoke("sonnet", "conversation", SCHEMA, 20)

    def test_status_identifies_a_seat_without_claiming_model_verification(self):
        with patch.object(seat_claude, "run", return_value=completed(IDENTITY)):
            ready, _diagnosis = seat_claude.status()
        self.assertTrue(ready)

    def test_nonseat_logins_are_refused_before_generation(self):
        identities = [
            {**IDENTITY, "loggedIn": False},
            {**IDENTITY, "authMethod": "api_key"},
            {**IDENTITY, "authMethod": "api_key_helper"},
            {**IDENTITY, "authMethod": "oauth_token"},
            {**IDENTITY, "apiProvider": "bedrock"},
            {**IDENTITY, "subscriptionType": None},
            {**IDENTITY, "subscriptionType": "free"},
        ]
        for identity in identities:
            with self.subTest(identity=identity), patch.object(
                seat_claude, "run", return_value=completed(identity),
            ) as native:
                with self.assertRaisesRegex(SeatError, "conch auth login claude-code"):
                    seat_claude.invoke("sonnet", "conversation", SCHEMA, 20)
                self.assertEqual(native.call_count, 1)

    def test_missing_cli_has_actionable_status(self):
        with patch.object(seat_claude, "run", side_effect=SeatError("claude is not installed")):
            ready, diagnosis = seat_claude.status()
        self.assertFalse(ready)
        self.assertIn("not installed", diagnosis)
        self.assertIn("conch auth login claude-code", diagnosis)

    def test_private_working_directory_is_removed_on_transport_error(self):
        directories = []

        def native(argv, *, cwd, **kwargs):
            self.assertTrue(os.path.isdir(cwd))
            directories.append(cwd)
            if len(directories) == 1:
                return completed(IDENTITY)
            raise SeatError("claude timed out")

        with patch.object(seat_claude, "run", side_effect=native), \
                self.assertRaisesRegex(SeatError, "timed out"):
            seat_claude.invoke("sonnet", "conversation", SCHEMA, 20)
        self.assertEqual(directories[0], directories[1])
        self.assertFalse(os.path.exists(directories[0]))

    def test_login_preflight_uses_the_overall_invocation_deadline(self):
        with patch.object(seat_claude.time, "monotonic", side_effect=[0, 2]), patch.object(
            seat_claude, "run", return_value=completed(IDENTITY),
        ) as native:
            with self.assertRaisesRegex(SeatError, "timed out"):
                seat_claude.invoke("sonnet", "conversation", SCHEMA, 1)
            self.assertEqual(native.call_count, 1)


def partial(kind, **fields):
    return {"type": "stream_event", "event": {"type": kind, **fields}}


class ClaudeLiveDetailTests(unittest.TestCase):
    def invoke(self, events, on_detail, *, wait_for_reasoning=False, code=0):
        work = []

        def receive(kind, text):
            on_detail(kind, text)
            if kind == "reasoning":
                Path(work[0], "ack").touch()

        def native(argv, **kwargs):
            if "auth" in argv:
                self.assertNotIn("on_stdout_line", kwargs)
                return completed(IDENTITY)
            work.append(kwargs["cwd"])
            self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
            self.assertIn("--verbose", argv)
            self.assertIn("--include-partial-messages", argv)
            self.assertIn("--restricted", argv)
            self.assertIn("--safe-mode", argv)
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertNotIn(kwargs["prompt"], argv)
            script = (
                "import json,pathlib,sys,time\n"
                f"events = {events!r}\n"
                "sys.stdin.read()\n"
                "sys.stderr.write('private-auth-diagnostic'); sys.stderr.flush()\n"
                "for event in events:\n"
                "    print(event if isinstance(event, str) else json.dumps(event), flush=True)\n"
                f"    if {wait_for_reasoning!r} and event.get('event', {{}}).get('delta', {{}}).get('type') == 'thinking_delta':\n"
                "        deadline = time.monotonic() + 3\n"
                "        while not pathlib.Path('ack').exists():\n"
                "            if time.monotonic() > deadline: sys.exit(7)\n"
                "            time.sleep(.01)\n"
                f"sys.exit({code})\n"
            )
            return run([sys.executable, "-c", script], **kwargs)

        try:
            with patch.object(seat_claude, "run", side_effect=native):
                return seat_claude.invoke("sonnet", "private conversation", SCHEMA, 5,
                                          on_detail=receive)
        finally:
            for directory in work:
                self.assertFalse(Path(directory).exists())

    def test_live_thinking_and_validator_never_leak_raw_envelope_or_signatures(self):
        details = []
        owner = threading.get_ident()

        def receive(kind, text):
            self.assertEqual(threading.get_ident(), owner)
            details.append((kind, text))

        events = [
            {"type": "system", "subtype": "init", "apiKeySource": "private-auth"},
            partial("message_start", message={"id": "m", "content": []}),
            partial("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""}),
            partial("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "Compare "}),
            partial("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "options."}),
            partial("content_block_delta", index=0, delta={"type": "signature_delta", "signature": "opaque-signature"}),
            {"type": "assistant", "message": {"id": "m", "content": [
                {"type": "thinking", "thinking": "Compare options.", "signature": "opaque-signature"}]}},
            partial("content_block_stop", index=0),
            partial("content_block_start", index=1, content_block={"type": "redacted_thinking", "data": "opaque-reasoning"}),
            partial("content_block_stop", index=1),
            partial("content_block_start", index=2, content_block={"type": "text", "text": ""}),
            partial("content_block_delta", index=2, delta={"type": "text_delta", "text": json.dumps(RESPONSE)}),
            partial("content_block_stop", index=2),
            partial("content_block_start", index=3, content_block={
                "type": "tool_use", "name": "StructuredOutput", "id": "validator", "input": {}}),
            partial("content_block_delta", index=3, delta={
                "type": "input_json_delta", "partial_json": '{"private-tool-arguments":'}),
            {"type": "assistant", "message": {"id": "m", "content": [
                {"type": "tool_use", "name": "StructuredOutput", "id": "validator", "input": RESPONSE}]}},
            partial("content_block_stop", index=3),
            partial("message_delta", delta={"stop_reason": "tool_use"}),
            partial("message_stop"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "validator", "content": "Structured output provided successfully"}]}},
            RESULT,
        ]
        result = self.invoke(events, receive, wait_for_reasoning=True)
        self.assertEqual(result["response"], RESPONSE)
        self.assertEqual([text for kind, text in details if kind == "reasoning"],
                         ["Compare ", "options."])
        self.assertTrue(any(kind == "progress" for kind, _ in details))
        for secret in ("opaque", "private", "tool_calls", "Structured output", "hello"):
            self.assertNotIn(secret, repr(details))

    def test_native_tool_attempts_and_unmatched_results_fail_closed(self):
        for block in (
            {"type": "tool_use", "name": "Bash", "id": "x", "input": {"command": "private-command"}},
            {"type": "tool_use", "name": "mcp__exec", "id": "x", "input": {}},
            {"type": "server_tool_use", "name": "web_search", "id": "x"},
            {"type": "tool_result", "tool_use_id": "unknown", "content": "private-output"},
        ):
            for partial_message in (True, False):
                event = (partial("content_block_start", index=0, content_block=block)
                         if partial_message else
                         {"type": "assistant", "message": {"id": "m", "content": [block]}})
                with self.subTest(block=block, partial=partial_message), self.assertRaises(SeatError) as caught:
                    self.invoke([event, RESULT], lambda kind, text: None)
                self.assertNotIn("private", str(caught.exception))

    def test_validator_retry_retains_authoritative_final_validation(self):
        validator = {"type": "tool_use", "name": "StructuredOutput", "id": "v", "input": {}}
        prefix = [
            {"type": "assistant", "message": {"id": "m", "content": [validator]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "v", "is_error": True, "content": "private validation error"}]}},
        ]
        self.assertEqual(self.invoke(prefix + [RESULT], lambda *args: None)["response"], RESPONSE)
        for result in (
            {**RESULT, "subtype": "error_max_structured_output_retries"},
            {**RESULT, "permission_denials": [{"tool_name": "Bash"}]},
            {**RESULT, "errors": ["private error"]},
            {**RESULT, "structured_output": None},
            {**RESULT, "usage": {"input_tokens": True, "output_tokens": 1}},
        ):
            with self.subTest(result=result), self.assertRaises(SeatError):
                self.invoke(prefix + [result], lambda *args: None)

    def test_live_failure_or_missing_final_result_never_becomes_success(self):
        for events in (
            [],
            ["not JSON"],
            [42],
            [{"type": "error", "error": {"message": "private error"}}],
            [{"type": "assistant", "error": "private error", "message": {"content": []}}, RESULT],
            [partial("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": "private"})],
            [RESULT, RESULT],
            [{"type": "future_native_tool", "arguments": "private"}, RESULT],
        ):
            with self.subTest(events=events), self.assertRaises(SeatError) as caught:
                self.invoke(events, lambda *args: None)
            self.assertNotIn("private", str(caught.exception))

    def test_transient_api_retry_does_not_abort_the_turn(self):
        details = []
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1,
                 "error": "overloaded", "error_status": 529}
        result = self.invoke([retry, RESULT],
                             lambda kind, text: details.append((kind, text)))
        self.assertEqual(result["response"], RESPONSE)
        self.assertEqual({kind for kind, _ in details}, {"progress"})
        self.assertNotIn("overloaded", repr(details))

    def test_cli_status_events_are_progress_not_failures(self):
        """A newer CLI's notices, model fallbacks, and internal status events
        must not abort a turn (they did: 'unsupported system event')."""
        details = []
        events = [
            {"type": "system", "subtype": "informational", "content": "Opus usage\x1b[31m is high",
             "level": "notice"},
            {"type": "system", "subtype": "model_fallback", "originalModel": "claude-opus-5-5",
             "fallbackModel": "claude-sonnet-5", "level": "warning"},
            {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 40},
            {"type": "system", "subtype": "turn_duration", "duration_ms": 900},
            RESULT,
        ]
        result = self.invoke(events, lambda kind, text: details.append((kind, text)))
        self.assertEqual(result["response"], RESPONSE)
        texts = [text for _, text in details]
        self.assertIn("Claude: Opus usage [31m is high", texts)
        self.assertIn("Claude switched models from claude-opus-5-5 to claude-sonnet-5", texts)

    def test_native_execution_system_events_fail_closed(self):
        for subtype in ("hook_started", "task_started", "permission_denied", "local_command",
                        "background_tasks_changed"):
            with self.subTest(subtype=subtype), self.assertRaisesRegex(SeatError, "native execution"):
                self.invoke([{"type": "system", "subtype": subtype}, RESULT], lambda *args: None)

    def test_nonzero_stream_exit_never_echoes_native_stderr(self):
        details = []
        with self.assertRaisesRegex(SeatError, "exit 1") as caught:
            self.invoke([RESULT], lambda kind, text: details.append((kind, text)), code=1)
        self.assertNotIn("private", str(caught.exception) + repr(details))
