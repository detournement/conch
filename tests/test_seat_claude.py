"""Consumer-visible parsing and failure boundaries for the native Claude seat."""

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from conch import seat_claude
from conch.seat_common import SeatError

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
