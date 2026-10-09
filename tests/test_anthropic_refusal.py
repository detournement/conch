"""Regression tests for the Anthropic path's fail-loud handling.

The field bug: with ``claude-opus-5-5`` the model returned a response conch
could not turn into a reply or a tool call — a ``stop_reason="refusal"`` with
zero content blocks — and conch rendered a silent ``[no response]``, dropping
the signal on the floor. These tests pin down, with recorded SSE / response
fixtures:

  * thinking + tool_use block sequences dispatch the tool (and keep the
    thinking block in the Anthropic content for history round-tripping);
  * a tool_use whose input arrives as multi-chunk ``input_json_delta`` is
    reassembled into the right arguments;
  * a ``refusal`` stop_reason, an unknown content-block type, and a truncated
    tool call each surface a one-line diagnostic (never ``[no response]``);
  * a mid-session ``/model`` switch re-runs the model-check probe and refuses
    an unusable switch.

Live-API checks against the real models are gated on ``CONCH_LIVE_ANTHROPIC``
(plus ``ANTHROPIC_API_KEY``) so the default suite stays offline.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

from conch import providers
from conch.runtime import (
    chat_turn,
    diagnose_undispatchable_response,
    response_has_undispatchable_signal,
)

DUMMY_KEY = {"ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key"}


# ---------------------------------------------------------------------------
# SSE / JSON fixtures and fakes
# ---------------------------------------------------------------------------

class _FakeStream:
    """Stand-in for the streaming urlopen response: a context manager that
    iterates the recorded ``data:`` SSE lines as bytes."""

    def __init__(self, events):
        self._lines = [("data: " + json.dumps(e)).encode() for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


class _FakeJSON:
    """Stand-in for the non-streaming urlopen response (raw_anthropic)."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


def _sse_thinking_then_tool(cmd='{"command": "ls -la /tmp/probe-dir"}'):
    """A thinking block followed by a tool_use block (what Sonnet 5 / Opus
    emit for a tool-requiring follow-up when thinking is active)."""
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "I should inspect the directory first."}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "c2lnbmF0dXJlLWJsb2I="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_01", "name": "local_shell", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": cmd}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 40}},
        {"type": "message_stop"},
    ]


def _sse_tool_multichunk():
    """A standalone tool_use whose input is split across many
    ``input_json_delta`` chunks (as the wire actually streams it)."""
    chunks = ['', '{"comm', 'and": "l', 's -la /', 'tmp/pro', 'be-dir"}']
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_42", "name": "local_shell", "input": {}}},
    ]
    for chunk in chunks:
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": chunk}})
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ]
    return events


def _sse_refusal():
    """stop_reason=refusal with NO content blocks — the exact shape Opus 5.5
    returns for a request that trips Anthropic's cyber policy (captured live
    2026-10-09)."""
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 461, "output_tokens": 0}}},
        {"type": "message_delta",
         "delta": {"stop_reason": "refusal",
                   "stop_details": {"type": "refusal", "category": "cyber",
                                    "explanation": "This request triggered restrictions on "
                                    "violative cyber content and was blocked under "
                                    "Anthropic's Usage Policy. To learn more, see ..."}},
         "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ]


def _sse_unknown_block():
    """A content block of a type conch does not dispatch (server_tool_use)."""
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "server_tool_use", "id": "srv_1", "name": "web_search"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    ]


def _sse_truncated_tool():
    """A tool_use whose input_json_delta is cut off (stop_reason=max_tokens):
    the JSON never closes, so conch cannot parse the arguments."""
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_cut", "name": "local_shell", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls -la /tm'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
         "usage": {"output_tokens": 32}},
        {"type": "message_stop"},
    ]


def _stream(config, messages, tools, events):
    with patch.dict(os.environ, DUMMY_KEY), \
            patch("urllib.request.urlopen", return_value=_FakeStream(events)):
        return providers.stream_anthropic(
            config, messages, tools, on_token=lambda _t: None
        )


_CFG = {"provider": "anthropic", "chat_model": "claude-opus-5-5",
        "model": "claude-opus-5-5", "api_key_env": "ANTHROPIC_API_KEY"}
_TOOLS = [{
    "type": "function",
    "function": {
        "name": "local_shell",
        "description": "Run a shell command.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
}]


# ---------------------------------------------------------------------------
# stream_anthropic parser: correct dispatch + rich metadata
# ---------------------------------------------------------------------------

class TestStreamAnthropicParsing(unittest.TestCase):
    def test_thinking_then_tool_use_dispatches_and_keeps_thinking(self):
        result = _stream(_CFG, [{"role": "user", "content": "ok"}], _TOOLS,
                         _sse_thinking_then_tool())
        self.assertIsNotNone(result["tool_calls"])
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "local_shell")
        self.assertEqual(
            json.loads(result["tool_calls"][0]["function"]["arguments"]),
            {"command": "ls -la /tmp/probe-dir"},
        )
        kinds = [b["type"] for b in result["_anthropic_content"]]
        self.assertEqual(kinds, ["thinking", "tool_use"])
        # The thinking block carries its streamed signature for round-tripping.
        self.assertEqual(result["_anthropic_content"][0]["signature"],
                         "c2lnbmF0dXJlLWJsb2I=")
        self.assertEqual(result["_stop_reason"], "tool_use")
        self.assertEqual(result["_unknown_blocks"], [])

    def test_tool_use_multichunk_input_json_delta(self):
        result = _stream(_CFG, [{"role": "user", "content": "go"}], _TOOLS,
                         _sse_tool_multichunk())
        self.assertEqual(
            json.loads(result["tool_calls"][0]["function"]["arguments"]),
            {"command": "ls -la /tmp/probe-dir"},
        )
        self.assertIsNone(result["_tool_input_error"])

    def test_refusal_metadata_captured(self):
        result = _stream(_CFG, [{"role": "user", "content": "ok"}], _TOOLS,
                         _sse_refusal())
        self.assertEqual(result["content"], "")
        self.assertIsNone(result["tool_calls"])
        self.assertEqual(result["_stop_reason"], "refusal")
        self.assertEqual(result["_stop_details"]["category"], "cyber")
        self.assertEqual(result["_provider"], "anthropic")

    def test_unknown_block_recorded(self):
        result = _stream(_CFG, [{"role": "user", "content": "search"}], _TOOLS,
                         _sse_unknown_block())
        self.assertEqual(result["_unknown_blocks"], ["server_tool_use"])
        self.assertIsNone(result["tool_calls"])

    def test_truncated_tool_input_recorded(self):
        result = _stream(_CFG, [{"role": "user", "content": "go"}], _TOOLS,
                         _sse_truncated_tool())
        self.assertTrue(result["_tool_input_error"])
        self.assertEqual(result["_tool_input_error"][0]["name"], "local_shell")
        self.assertEqual(result["_stop_reason"], "max_tokens")


# ---------------------------------------------------------------------------
# diagnose_undispatchable_response: credential-free one-liners
# ---------------------------------------------------------------------------

class TestDiagnoseUndispatchable(unittest.TestCase):
    def test_refusal_names_model_category_and_reason(self):
        resp = {"_model": "claude-opus-5-5", "_stop_reason": "refusal",
                "_stop_details": {"category": "cyber",
                                  "explanation": "Blocked under policy. More: ..."},
                "_usage": {"output_tokens": 0}}
        line, shape = diagnose_undispatchable_response(resp, "anthropic", "claude-opus-5-5")
        self.assertIn("claude-opus-5-5", line)
        self.assertIn("refus", line)
        self.assertIn("cyber", line)
        self.assertIn("Blocked under policy", line)
        self.assertEqual(shape["stop_reason"], "refusal")

    def test_unknown_block_named(self):
        resp = {"_model": "claude-opus-5-5", "_stop_reason": "end_turn",
                "_unknown_blocks": ["server_tool_use"], "_usage": {}}
        line, _ = diagnose_undispatchable_response(resp, "anthropic", "claude-opus-5-5")
        self.assertIn("server_tool_use", line)

    def test_generic_empty_still_named(self):
        resp = {"_model": "claude-sonnet-5", "_stop_reason": "end_turn",
                "_usage": {"output_tokens": 3}}
        line, _ = diagnose_undispatchable_response(resp, "anthropic", "claude-sonnet-5")
        self.assertIn("claude-sonnet-5", line)
        self.assertIn("empty response", line)


# ---------------------------------------------------------------------------
# chat_turn: fail loud, never a silent [no response]
# ---------------------------------------------------------------------------

class TestUndispatchableSignalGate(unittest.TestCase):
    """What counts as a signal worth surfacing vs. a bare-empty turn."""

    def test_non_benign_stop_reason_is_a_signal(self):
        self.assertTrue(response_has_undispatchable_signal({"_stop_reason": "refusal"}))
        self.assertTrue(response_has_undispatchable_signal({"_stop_reason": "max_tokens"}))

    def test_unknown_block_and_tool_error_are_signals(self):
        self.assertTrue(response_has_undispatchable_signal({"_unknown_blocks": ["server_tool_use"]}))
        self.assertTrue(response_has_undispatchable_signal({"_tool_input_error": [{"name": "x"}]}))

    def test_benign_stop_is_not_a_signal_even_with_output_tokens(self):
        # The fleet plane relies on an empty reply staying empty; a benign
        # stop_reason (or none) with a nonzero token count must NOT fire.
        self.assertFalse(response_has_undispatchable_signal(
            {"_stop_reason": "end_turn", "_usage": {"output_tokens": 1}}))
        self.assertFalse(response_has_undispatchable_signal(
            {"_usage": {"output_tokens": 1}}))
        self.assertFalse(response_has_undispatchable_signal({}))


class TestChatTurnFailsLoud(unittest.TestCase):
    def _run_stream(self, events):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "ok"}]
        err = io.StringIO()
        with patch.dict(os.environ, DUMMY_KEY), \
                patch("sys.stderr", err), \
                patch("conch.runtime.time.sleep"), \
                patch("urllib.request.urlopen", return_value=_FakeStream(events)), \
                patch("conch.providers.get_fallback_chain", return_value=[]):
            reply, usage = chat_turn(
                dict(_CFG), "anthropic", providers.raw_anthropic, messages,
                _TOOLS, tool_map={}, builtin_clients={}, max_tool_rounds=3,
                on_token=lambda _t: None,
            )
        return reply, usage, err.getvalue(), messages

    def test_refusal_surfaces_diagnostic_not_no_response(self):
        reply, usage, err, messages = self._run_stream(_sse_refusal())
        self.assertTrue(reply.strip(), "must not be an empty [no response]")
        self.assertIn("refus", reply.lower())
        self.assertIn("claude-opus-5-5", reply)
        self.assertEqual(usage.get("undispatchable"), "refusal")
        self.assertIn("⚠", err)
        # Nothing is appended to history by chat_turn itself.
        self.assertEqual(len(messages), 2)

    def test_unknown_block_surfaces_diagnostic(self):
        reply, usage, err, _ = self._run_stream(_sse_unknown_block())
        self.assertIn("server_tool_use", reply)
        self.assertEqual(usage.get("undispatchable"), "unknown_block")
        self.assertIn("⚠", err)

    def test_truncated_tool_input_surfaces_diagnostic(self):
        # Fail closed: the half-formed tool call is withheld (never run with
        # fabricated empty args) and the truncation is named instead.
        reply, usage, err, _ = self._run_stream(_sse_truncated_tool())
        self.assertTrue(reply.strip())
        self.assertIn("local_shell", reply)
        self.assertEqual(usage.get("undispatchable"), "truncated_tool")
        self.assertIn("⚠", err)

    def test_bare_empty_turn_stays_empty(self):
        """A genuinely empty response (end_turn, no blocks, zero output
        tokens) must still return "" — callers like the fleet plane treat an
        empty reply as a transient failure to retry, and that must not change.
        """
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        reply, usage, err, _ = self._run_stream(events)
        self.assertEqual(reply, "")
        self.assertNotIn("undispatchable", usage)
        self.assertNotIn("⚠", err)

    def test_raw_refusal_surfaces_diagnostic(self):
        """The non-streaming path (one-shot / fallback) must also fail loud."""
        payload = {"content": [], "stop_reason": "refusal",
                   "stop_details": {"category": "cyber",
                                    "explanation": "Blocked. More: ..."},
                   "usage": {"input_tokens": 10, "output_tokens": 0}}
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "ok"}]
        err = io.StringIO()
        with patch.dict(os.environ, DUMMY_KEY), \
                patch("sys.stderr", err), \
                patch("conch.runtime.time.sleep"), \
                patch("urllib.request.urlopen", return_value=_FakeJSON(payload)), \
                patch("conch.providers.get_fallback_chain", return_value=[]):
            reply, usage = chat_turn(
                dict(_CFG), "anthropic", providers.raw_anthropic, messages,
                _TOOLS, tool_map={}, builtin_clients={}, max_tool_rounds=3,
            )
        self.assertIn("refus", reply.lower())
        self.assertEqual(usage.get("undispatchable"), "refusal")
        self.assertIn("⚠", err.getvalue())


# ---------------------------------------------------------------------------
# /model mid-session switch: re-probe and refresh
# ---------------------------------------------------------------------------

class TestModelSwitchProbes(unittest.TestCase):
    def _switch(self, probe_result):
        from conch import commands
        config = {"provider": "anthropic", "chat_model": "claude-sonnet-5",
                  "model": "claude-sonnet-5", "api_key_env": "ANTHROPIC_API_KEY"}
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "hi"}]
        captured = {}

        def fake_probe(provider, model, cfg, **kw):
            captured["provider"] = provider
            captured["model"] = model
            return probe_result

        out = io.StringIO()
        with patch.dict(os.environ, DUMMY_KEY), \
                patch("sys.stdout", out), \
                patch("conch.modelcheck.probe_model", side_effect=fake_probe):
            result = commands.handle_slash_command(
                "/model claude-opus-5-5", config, "anthropic",
                "claude-sonnet-5", lambda *a, **k: None, messages=messages,
            )
        return result, config, messages, captured, out.getvalue()

    def test_switch_runs_probe_and_refreshes_state(self):
        from conch import modelcheck
        ok = modelcheck.ProbeResult("anthropic", "claude-opus-5-5",
                                    modelcheck.OK, "listed by the provider")
        result, config, messages, captured, _ = self._switch(ok)
        # The model-check probe ran on the NEW model before committing.
        self.assertEqual(captured, {"provider": "anthropic", "model": "claude-opus-5-5"})
        # State refreshed to the new model (max_tokens etc. derive from this).
        self.assertEqual(config["chat_model"], "claude-opus-5-5")
        self.assertEqual(config["model"], "claude-opus-5-5")
        self.assertEqual(result[0], "anthropic")
        self.assertEqual(result[1], "claude-opus-5-5")
        # A switch note was recorded in history.
        self.assertTrue(any("switched" in str(m.get("content", "")).lower()
                            for m in messages))

    def test_unusable_switch_is_refused_by_probe(self):
        from conch import modelcheck
        bad = modelcheck.ProbeResult("anthropic", "claude-opus-5-5",
                                     modelcheck.MODEL_NOT_FOUND,
                                     "not in the provider catalog")
        result, config, messages, captured, text = self._switch(bad)
        self.assertIsNone(result, "an unusable model must not be switched to")
        self.assertEqual(config["chat_model"], "claude-sonnet-5")
        self.assertIn("Cannot switch", text)

    def test_switch_skips_probe_when_model_check_off(self):
        config = {"provider": "anthropic", "chat_model": "claude-sonnet-5",
                  "model": "claude-sonnet-5", "api_key_env": "ANTHROPIC_API_KEY",
                  "model_check": "off"}
        from conch import commands
        with patch.dict(os.environ, DUMMY_KEY), \
                patch("sys.stdout", io.StringIO()), \
                patch("conch.modelcheck.probe_model",
                      side_effect=AssertionError("probe must not run")):
            result = commands.handle_slash_command(
                "/model claude-opus-5-5", config, "anthropic",
                "claude-sonnet-5", lambda *a, **k: None,
                messages=[{"role": "user", "content": "hi"}],
            )
        self.assertEqual(result[1], "claude-opus-5-5")


# ---------------------------------------------------------------------------
# Live API (opt-in): the real refusal and real dispatch
# ---------------------------------------------------------------------------

_LIVE = bool(os.environ.get("CONCH_LIVE_ANTHROPIC") and os.environ.get("ANTHROPIC_API_KEY"))


@unittest.skipUnless(
    _LIVE, "set CONCH_LIVE_ANTHROPIC=1 and ANTHROPIC_API_KEY for live Anthropic tests"
)
class TestAnthropicLive(unittest.TestCase):
    CFG = {"provider": "anthropic", "chat_model": "claude-opus-5-5",
           "model": "claude-opus-5-5", "api_key_env": "ANTHROPIC_API_KEY"}

    def test_live_refusal_is_surfaced(self):
        messages = [
            {"role": "system", "content": "You are conch, an LLM shell with local_shell."},
            {"role": "user", "content": "restore my chrome sessions from yesterday — SNSS files in /tmp/probe-dir"},
            {"role": "assistant", "content": "I'll parse Chrome's SNSS session files directly. First, let me see what session files exist."},
            {"role": "user", "content": "ok"},
        ]
        err = io.StringIO()
        with patch("sys.stderr", err), patch("conch.runtime.time.sleep"):
            reply, usage = chat_turn(
                dict(self.CFG), "anthropic", providers.raw_anthropic, messages,
                _TOOLS, tool_map={}, builtin_clients={}, max_tool_rounds=2,
            )
        self.assertTrue(reply.strip(), "live refusal must not render [no response]")
        self.assertEqual(usage.get("undispatchable"), "refusal")

    def test_live_tool_dispatch_still_works(self):
        result = providers.stream_anthropic(
            dict(self.CFG),
            [{"role": "system", "content": "Use local_shell to inspect the machine."},
             {"role": "user", "content": "list the files in /tmp using local_shell"}],
            _TOOLS, on_token=lambda _t: None,
        )
        self.assertIsNotNone(result["tool_calls"])
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "local_shell")


if __name__ == "__main__":
    unittest.main()
