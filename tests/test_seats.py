"""Seat billing boundaries and untrusted structured-tool response validation."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from conch import seat_codex, seats
from conch.providers import api_key_env_for, get_fallback_chain
from conch.seat_common import SeatError, run
from conch.runtime import parse_and_validate_tool_arguments
from conch.seats import normalize, response_schema

TOOL = {"type": "function", "function": {"name": "local_shell"}}
TYPED_TOOL = {"type": "function", "function": {
    "name": "local_shell",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer"},
            "mode": {"type": "string", "enum": ["fast", "safe"]},
        },
        "required": ["command"],
    },
}}


class SeatBoundaryTests(unittest.TestCase):
    def test_unknown_tool_never_reaches_dispatch(self):
        for arguments in ("{}", {}):
            with self.subTest(arguments=arguments), self.assertRaises(SeatError):
                normalize({"content": "", "tool_calls": [
                    {"name": "unapproved", "arguments": arguments}]}, [TOOL])

    def test_bad_string_arguments_are_rejected_before_dispatch_not_by_failing_the_turn(self):
        for arguments in ("[]", "null", "{bad", '{"value":NaN}',
                          '{"command":"awk "unescaped" quote"}'):
            with self.subTest(arguments=arguments):
                reply = normalize({"content": "", "tool_calls": [
                    {"name": "local_shell", "arguments": arguments}]}, [TYPED_TOOL])
                parsed, error = parse_and_validate_tool_arguments(
                    reply["tool_calls"][0], TYPED_TOOL)
                self.assertIsNone(parsed)
                self.assertTrue(error)

    def test_arguments_that_are_neither_object_nor_string_fail_closed(self):
        for arguments in (None, 7, ["ls"]):
            with self.subTest(arguments=arguments), self.assertRaises(SeatError):
                normalize({"content": "", "tool_calls": [
                    {"name": "local_shell", "arguments": arguments}]}, [TYPED_TOOL])

    def test_object_arguments_drop_unused_optionals_and_pass_validation(self):
        command = 'awk \'{ pixels[x,y]="#" }\'\nprintf "\\033[H"'
        reply = normalize({"content": "", "tool_calls": [{
            "name": "local_shell",
            "arguments": {"command": command, "timeout": None, "mode": None},
        }]}, [TYPED_TOOL])
        parsed, error = parse_and_validate_tool_arguments(reply["tool_calls"][0], TYPED_TOOL)
        self.assertIsNone(error)
        self.assertEqual(parsed, {"command": command})

    def test_schema_types_arguments_per_tool_and_keeps_strings_for_open_schemas(self):
        open_tool = {"type": "function", "function": {
            "name": "open_tool", "parameters": {"type": "object", "properties": {
                "headers": {"type": "object"}}}}}
        variants = response_schema([TYPED_TOOL, open_tool, {
            "type": "function", "function": {"name": "bare"}}])[
            "properties"]["tool_calls"]["items"]["anyOf"]
        by_name = {name: variant["properties"]["arguments"]
                   for variant in variants
                   for name in variant["properties"]["name"]["enum"]}
        typed = by_name["local_shell"]
        self.assertEqual(typed["type"], "object")
        self.assertEqual(set(typed["required"]), {"command", "timeout", "mode"})
        self.assertIs(typed["additionalProperties"], False)
        self.assertEqual(typed["properties"]["command"]["type"], "string")
        self.assertEqual(typed["properties"]["timeout"]["type"], ["integer", "null"])
        self.assertEqual(typed["properties"]["mode"]["enum"], ["fast", "safe", None])
        self.assertEqual(by_name["open_tool"]["type"], "string")
        self.assertEqual(by_name["bare"]["type"], "string")
        self.assertNotIn("null", str(TYPED_TOOL))  # The tool's own schema is untouched.

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


class NativeStreamTests(unittest.TestCase):
    def test_live_lines_run_on_caller_while_stderr_and_stdin_are_drained(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ack"
            owner = threading.get_ident()
            lines = []

            def receive(line):
                self.assertEqual(threading.get_ident(), owner)
                lines.append(line)
                if line == "ready\n":
                    marker.touch()

            script = (
                "import os,sys,time,pathlib\n"
                "sys.stderr.write('private auth diagnostic' * 10000); sys.stderr.flush()\n"
                "print('ready', flush=True)\n"
                "deadline = time.monotonic() + 3\n"
                "while not pathlib.Path(sys.argv[1]).exists():\n"
                "    if time.monotonic() > deadline: sys.exit(7)\n"
                "    time.sleep(.01)\n"
                "assert len(sys.stdin.read()) == 200000\n"
                "os.write(1, b'caf\\xc3'); os.write(1, b'\\xa9\\r\\nlast')\n"
            )
            result = run([sys.executable, "-c", script, str(marker)],
                         prompt="x" * 200000, timeout=5, on_stdout_line=receive)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["ready\n", "café\n", "last"])
        self.assertEqual(result.stdout, "".join(lines))
        self.assertIn("private auth diagnostic", result.stderr)
        self.assertNotIn("private auth", "".join(lines))

    def test_callback_interrupt_kills_native_process_and_its_children(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "escaped"
            child = (
                "import time,pathlib; time.sleep(.4); "
                f"pathlib.Path({str(marker)!r}).touch()"
            )
            script = (
                "import subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "print('ready', flush=True)\n"
                "time.sleep(30)\n"
            )
            lines = []

            def cancel(line):
                lines.append(line)
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                run([sys.executable, "-c", script], timeout=5, on_stdout_line=cancel)
            self.assertEqual(lines, ["ready\n"])
            time.sleep(.6)
            self.assertFalse(marker.exists())

    def test_stream_deadline_kills_descendant_retaining_output_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "escaped"
            child = (
                "import time,pathlib; time.sleep(1.6); "
                f"pathlib.Path({str(marker)!r}).touch()"
            )
            script = (
                "import subprocess,sys\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "print('ready', flush=True)\n"
            )
            lines = []
            with self.assertRaisesRegex(SeatError, "timed out"):
                run([sys.executable, "-c", script], timeout=1, on_stdout_line=lines.append)
            self.assertEqual(lines, ["ready\n"])
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_exit_after_closing_pipes_is_still_deadline_bounded(self):
        script = "import os,time; os.close(1); os.close(2); time.sleep(30)"
        with self.assertRaisesRegex(SeatError, "timed out"):
            run([sys.executable, "-c", script], timeout=.1, on_stdout_line=lambda _: None)

    def test_callback_error_after_child_exit_is_not_replaced_by_cleanup(self):
        def reject(line):
            time.sleep(.3)  # The child has exited but is not yet reaped.
            raise SeatError("rejected event")

        with self.assertRaisesRegex(SeatError, "rejected event"):
            run([sys.executable, "-c", "print('x', flush=True)"],
                timeout=5, on_stdout_line=reject)


class DetailConsumer:
    def __init__(self):
        self.tokens = []
        self.details = []

    def feed(self, text):
        self.tokens.append(text)

    def feed_detail(self, kind, text):
        self.details.append((kind, text))


class SeatDetailBoundaryTests(unittest.TestCase):
    def complete(self, response, on_token=None, *, failure=False, tools=None):
        events = [
            {"type": "turn.started"},
            {"type": "item.updated", "item": {
                "id": "r", "type": "reasoning", "text": "Published reasoning"}},
            {"type": "item.completed", "item": {
                "id": "a", "type": "agent_message", "text": json.dumps(response)}},
            {"type": "turn.failed", "error": {"message": "private native error"}} if failure else
            {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}},
        ]

        def native(argv, *, on_stdout_line=None, **kwargs):
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps(response), encoding="utf-8")
            lines = [json.dumps(event) for event in events]
            if on_stdout_line is not None:
                for line in lines:
                    on_stdout_line(line)
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines), "private auth stderr")

        with patch.object(seat_codex, "run", side_effect=native):
            return seats.complete({"provider": "codex"}, [{"role": "user", "content": "hello"}],
                                  tools=tools, on_token=on_token)

    def test_details_are_visible_without_contaminating_final_answer_or_history(self):
        consumer = DetailConsumer()
        reply = self.complete({"content": "Final answer", "tool_calls": []}, consumer.feed)
        self.assertEqual(consumer.tokens, ["Final answer"])
        self.assertIn(("reasoning", "Published reasoning"), consumer.details)
        self.assertEqual(reply["content"], "Final answer")
        self.assertEqual(reply["_usage"], {"input_tokens": 2, "output_tokens": 3})
        self.assertNotIn("Published reasoning", repr(reply))
        self.assertNotIn("tool_calls", repr(consumer.details))

    def test_plain_and_headless_consumers_receive_only_the_final_answer(self):
        tokens = []
        for callback in (None, tokens.append):
            reply = self.complete({"content": "Final answer", "tool_calls": []}, callback)
            self.assertEqual(reply["content"], "Final answer")
        self.assertEqual(tokens, ["Final answer"])

    def test_tool_arguments_never_enter_the_detail_or_answer_channels(self):
        consumer = DetailConsumer()
        response = {"content": "", "tool_calls": [
            {"name": "local_shell", "arguments": '{"command":"private-command"}'},
        ]}
        reply = self.complete(response, consumer.feed, tools=[TOOL])
        self.assertEqual(consumer.tokens, [])
        self.assertEqual(reply["tool_calls"][0]["function"]["arguments"], '{"command":"private-command"}')
        self.assertNotIn("private-command", repr(consumer.details))

    def test_failed_live_generation_never_delivers_a_final_response(self):
        consumer = DetailConsumer()
        reply = self.complete({"content": "Must not display", "tool_calls": []},
                              consumer.feed, failure=True)
        self.assertTrue(reply["_error"])
        self.assertEqual(consumer.tokens, [])
        self.assertNotIn("private", repr(reply) + repr(consumer.details))
