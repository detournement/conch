"""Seat billing boundaries and untrusted structured-tool response validation."""

import json
import os
import sys
import unittest
from unittest.mock import patch

from conch.providers import api_key_env_for, get_fallback_chain
from conch.seat_common import SeatError, run
from conch.seats import normalize

TOOL = {"type": "function", "function": {"name": "local_shell"}}


class SeatBoundaryTests(unittest.TestCase):
    def test_unknown_tool_and_nonobject_arguments_never_reach_dispatch(self):
        for call in (
            {"name": "unapproved", "arguments": "{}"},
            {"name": "local_shell", "arguments": "[]"},
            {"name": "local_shell", "arguments": "null"},
            {"name": "local_shell", "arguments": "{bad"},
            {"name": "local_shell", "arguments": '{"value":NaN}'},
        ):
            with self.subTest(call=call), self.assertRaises(SeatError):
                normalize({"content": "", "tool_calls": [call]}, [TOOL])

    def test_no_tool_request_is_accepted_when_tools_are_disabled(self):
        with self.assertRaises(SeatError):
            normalize({"content": "", "tool_calls": [
                {"name": "local_shell", "arguments": "{}"},
            ]}, [])

    def test_saved_api_keys_cannot_enable_seat_api_billing(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "key", "ANTHROPIC_API_KEY": "key"}):
            for provider in ("claude-code", "codex"):
                self.assertEqual(api_key_env_for(provider, {"api_key_env": "OPENAI_API_KEY"}), "")
                self.assertEqual(get_fallback_chain(provider, "model", {}), [])

    def test_native_process_cannot_inherit_api_billing_overrides(self):
        names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY")
        with patch.dict(os.environ, {name: "must-not-forward" for name in names}):
            result = run([sys.executable, "-c",
                          "import os,json; print(json.dumps([k for k in "
                          + repr(names) + " if k in os.environ]))"], timeout=5)
        self.assertEqual(json.loads(result.stdout), [])

    def test_hung_native_process_is_bounded(self):
        with self.assertRaisesRegex(SeatError, "timed out"):
            run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1)

    def test_blank_reply_is_a_provider_failure(self):
        with self.assertRaisesRegex(SeatError, "empty response"):
            normalize({"content": "  ", "tool_calls": []}, [])
