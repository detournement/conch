"""Consumer-visible parsing and fail-closed behavior of the native transport."""

import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from conch import seat_codex
from conch.seat_common import SeatError
from conch.seats import response_schema


_TOOL = {'type': 'function', 'function': {
    'name': 'read_file', 'parameters': {'type': 'object'},
}}
_RESPONSE = {'content': '', 'tool_calls': [
    {'name': 'read_file', 'arguments': '{"path":"example.txt"}'},
]}


def _stream(*events):
    return '\n'.join(json.dumps(event) for event in events)


class CodexTransportTests(unittest.TestCase):
    def invoke(self, response=None, *, events=None, returncode=0, stderr=''):
        if response is None:
            response = _RESPONSE
        if events is None:
            events = [
                {'type': 'thread.started', 'thread_id': 'native-thread'},
                {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': json.dumps(response)}},
                {'type': 'turn.completed', 'usage': {
                    'input_tokens': 17, 'cached_input_tokens': 3,
                    'output_tokens': 9}},
            ]
        self.work = None

        def native(argv, *, prompt, timeout, cwd):
            self.assertEqual(prompt, 'private conversation')
            self.assertEqual(timeout, 12)
            self.assertNotIn(prompt, argv)
            self.work = cwd
            self.assertNotEqual(Path(cwd), Path.cwd())
            self.assertEqual(os.stat(cwd).st_mode & 0o777, 0o700)
            output = Path(argv[argv.index('--output-last-message') + 1])
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            output.write_text(response if isinstance(response, str)
                              else json.dumps(response), encoding='utf-8')
            return subprocess.CompletedProcess(argv, returncode,
                                               _stream(*events), stderr)

        with patch.object(seat_codex, 'run', side_effect=native):
            return seat_codex.invoke('default', 'private conversation',
                                     response_schema([_TOOL]), 12)

    def test_returns_structured_tool_request_without_executing_it(self):
        result = self.invoke()
        self.assertEqual(result['response'], _RESPONSE)
        self.assertEqual(result['usage'], {'input_tokens': 17, 'output_tokens': 9})
        self.assertEqual(result['model'], 'default')
        self.assertFalse(Path(self.work).exists())

    def test_usage_is_optional(self):
        result = self.invoke(events=[{'type': 'turn.completed'}])
        self.assertEqual(result['usage'], {})

    def test_invalid_usage_is_not_reported_as_known(self):
        result = self.invoke(events=[{'type': 'turn.completed', 'usage': {
            'input_tokens': True, 'output_tokens': -1}}])
        self.assertEqual(result['usage'], {})

    def test_rejects_unrecognized_tool_name(self):
        response = {'content': '', 'tool_calls': [
            {'name': 'execute_shell', 'arguments': '{}'}]}
        with self.assertRaises(SeatError):
            self.invoke(response)

    def test_rejects_arguments_that_are_not_json_objects(self):
        for arguments in ('[]', 'null', '{broken', '{"x":NaN}'):
            with self.subTest(arguments=arguments), self.assertRaises(SeatError):
                self.invoke({'content': '', 'tool_calls': [
                    {'name': 'read_file', 'arguments': arguments}]})

    def test_rejects_missing_or_wrong_envelope_fields(self):
        for response in ({'content': 'hello'}, {'content': None, 'tool_calls': []},
                         {'content': 'hello', 'tool_calls': [], 'extra': True},
                         [], '```json\n{}\n```'):
            with self.subTest(response=response), self.assertRaises(SeatError):
                self.invoke(response)

    def test_nonfatal_native_diagnostic_preserves_completed_response(self):
        with self.assertLogs('conch.seat_codex', level='WARNING'):
            result = self.invoke(events=[
                {'type': 'thread.started', 'thread_id': 'native-thread'},
                {'type': 'item.completed', 'item': {
                    'id': 'warning', 'type': 'error',
                    'message': 'Code Mode host is disabled; Code Mode will fail closed.'}},
                {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': json.dumps(_RESPONSE)}},
                {'type': 'turn.completed', 'usage': {
                    'input_tokens': 17, 'output_tokens': 9}},
            ])
        self.assertEqual(result['response']['tool_calls'][0]['name'], 'read_file')
        self.assertEqual(result['usage'], {'input_tokens': 17, 'output_tokens': 9})

    def test_native_diagnostic_does_not_expose_credentials(self):
        secret = 'sk-' + 'aB7cD9' * 8
        with self.assertLogs('conch.seat_codex', level='WARNING') as logs:
            self.invoke(events=[
                {'type': 'item.completed', 'item': {
                    'type': 'error', 'message': 'API key: ' + secret}},
                {'type': 'turn.completed'},
            ])
        self.assertNotIn(secret, '\n'.join(logs.output))

    def test_nonfatal_diagnostic_cannot_mask_a_failed_turn(self):
        with self.assertLogs('conch.seat_codex', level='WARNING'):
            with self.assertRaises(SeatError):
                self.invoke(events=[
                    {'type': 'item.completed', 'item': {
                        'type': 'error', 'message': 'Recoverable warning'}},
                    {'type': 'turn.failed', 'error': {'message': 'Provider failed'}},
                ])

    def test_rejects_native_tool_events(self):
        for kind in ('command_execution', 'file_change', 'mcp_tool_call',
                     'web_search', 'future_native_tool'):
            with self.subTest(kind=kind), self.assertRaises(SeatError):
                self.invoke(events=[
                    {'type': 'item.started', 'item': {'type': kind}},
                    {'type': 'turn.completed'},
                ])

    def test_rejects_failed_provider_even_with_structured_output(self):
        for event in ({'type': 'error', 'message': 'secret'},
                      {'type': 'turn.failed', 'error': {'message': 'secret'}}):
            with self.subTest(event=event), self.assertRaises(SeatError) as error:
                self.invoke(events=[event, {'type': 'turn.completed'}])
            self.assertNotIn('secret', str(error.exception))

    def test_rejects_incomplete_stream(self):
        with self.assertRaises(SeatError):
            self.invoke(events=[{'type': 'turn.started'}])

    def test_rejects_nonzero_exit_without_echoing_stderr(self):
        with self.assertRaises(SeatError) as error:
            self.invoke(returncode=1, stderr='OPENAI_API_KEY=secret private prompt')
        self.assertNotIn('secret', str(error.exception))
        self.assertFalse(Path(self.work).exists())

    def test_rejects_malformed_event_stream(self):
        with patch.object(seat_codex, 'run', return_value=
                          subprocess.CompletedProcess([], 0, 'not JSON', '')):
            with self.assertRaises(SeatError):
                seat_codex.invoke('default', 'prompt', response_schema([]), 12)

    def test_rejects_missing_output(self):
        with patch.object(seat_codex, 'run', return_value=
                          subprocess.CompletedProcess([], 0,
                                                      _stream({'type': 'turn.completed'}), '')):
            with self.assertRaises(SeatError):
                seat_codex.invoke('default', 'prompt', response_schema([]), 12)


class CodexLoginTests(unittest.TestCase):
    def test_accepts_native_chatgpt_login(self):
        with patch.object(seat_codex, 'run', return_value=
                          subprocess.CompletedProcess([], 0, '', 'Logged in using ChatGPT\n')):
            self.assertTrue(seat_codex.status()[0])

    def test_rejects_key_only_or_unknown_login_without_echoing_credentials(self):
        for code, text in ((0, 'Logged in using an API key: sk-secret'),
                           (0, 'unknown login format'), (1, 'Not logged in')):
            with self.subTest(text=text), patch.object(seat_codex, 'run', return_value=
                    subprocess.CompletedProcess([], code, '', text)):
                ok, diagnosis = seat_codex.status()
            self.assertFalse(ok)
            self.assertNotIn('sk-secret', diagnosis)

    def test_missing_native_cli_includes_login_remedy(self):
        with patch.object(seat_codex, 'run', side_effect=SeatError('codex is not installed')):
            ok, diagnosis = seat_codex.status()
        self.assertFalse(ok)
        self.assertIn('not installed', diagnosis)

    def test_unknown_auth_action_is_rejected(self):
        with self.assertRaises(SeatError):
            seat_codex.auth_command('token')
