"""Consumer-visible parsing and fail-closed behavior of the native transport."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from conch import seat_codex
from conch.seat_common import SeatError, run
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
    def invoke(self, response=None, *, events=None, returncode=0, stderr='', on_detail=None):
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

        def native(argv, *, prompt, timeout, cwd, on_stdout_line=None):
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
            if on_stdout_line is not None:
                for line in _stream(*events).splitlines():
                    on_stdout_line(line)
            return subprocess.CompletedProcess(argv, returncode,
                                               _stream(*events), stderr)

        with patch.object(seat_codex, 'run', side_effect=native):
            return seat_codex.invoke('default', 'private conversation',
                                     response_schema([_TOOL]), 12, on_detail=on_detail)

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

    def test_string_arguments_are_left_for_conch_argument_validation(self):
        for arguments in ('[]', 'null', '{broken', '{"x":NaN}'):
            with self.subTest(arguments=arguments):
                response = {'content': '', 'tool_calls': [
                    {'name': 'read_file', 'arguments': arguments}]}
                self.assertEqual(self.invoke(response)['response'], response)

    def test_typed_arguments_must_match_their_own_tool(self):
        shell = {'type': 'function', 'function': {'name': 'shell', 'parameters': {
            'type': 'object', 'properties': {
                'command': {'type': 'string'}, 'timeout': {'type': 'integer'}},
            'required': ['command']}}}
        note = {'type': 'function', 'function': {'name': 'note', 'parameters': {
            'type': 'object', 'properties': {'text': {'type': 'string'}},
            'required': ['text']}}}
        schema = response_schema([shell, note])

        def check(name, arguments):
            seat_codex._validate(
                {'content': '', 'tool_calls': [{'name': name, 'arguments': arguments}]},
                schema)

        check('shell', {'command': 'echo "quoted"', 'timeout': None})
        check('shell', {'command': 'ls', 'timeout': 5})
        check('note', {'text': 'hello'})
        for name, arguments in (
            ('shell', {'command': 'ls'}),                      # strict: every key present
            ('shell', {'command': 'ls', 'timeout': '5'}),
            ('shell', {'command': 'ls', 'timeout': True}),
            ('shell', {'command': 'ls', 'timeout': None, 'extra': 1}),
            ('shell', {'text': 'hello'}),                      # another tool's arguments
            ('note', '{"text":"hello"}'),
            ('other', {'text': 'hello'}),
        ):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(SeatError):
                check(name, arguments)

    def test_rejects_missing_or_wrong_envelope_fields(self):
        for response in ({'content': 'hello'}, {'content': None, 'tool_calls': []},
                         {'content': 'hello', 'tool_calls': [], 'extra': True},
                         [], '```json\n{}\n```'):
            with self.subTest(response=response), self.assertRaises(SeatError):
                self.invoke(response)

    def test_nonfatal_native_diagnostic_preserves_completed_response(self):
        with self.assertLogs('conch.seat_codex', level='DEBUG'):
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
        with self.assertLogs('conch.seat_codex', level='DEBUG') as logs:
            self.invoke(events=[
                {'type': 'item.completed', 'item': {
                    'type': 'error', 'message': 'API key: ' + secret}},
                {'type': 'turn.completed'},
            ])
        self.assertNotIn(secret, '\n'.join(logs.output))

    def test_nonfatal_diagnostic_cannot_mask_a_failed_turn(self):
        with self.assertLogs('conch.seat_codex', level='DEBUG'), self.assertRaises(SeatError):
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
        with patch.object(
            seat_codex, 'run', return_value=subprocess.CompletedProcess([], 0, 'not JSON', ''),
        ), self.assertRaises(SeatError):
            seat_codex.invoke('default', 'prompt', response_schema([]), 12)

    def test_rejects_missing_output(self):
        with patch.object(
            seat_codex, 'run', return_value=subprocess.CompletedProcess(
                [], 0, _stream({'type': 'turn.completed'}), ''),
        ), self.assertRaises(SeatError):
            seat_codex.invoke('default', 'prompt', response_schema([]), 12)


    def test_snapshot_reasoning_is_live_and_never_replays_the_response_envelope(self):
        owner = threading.get_ident()
        details = []
        work = []
        events = [
            {'type': 'thread.started', 'thread_id': 'private-thread'},
            {'type': 'turn.started'},
            {'type': 'item.started', 'item': {'id': 'r', 'type': 'reasoning', 'text': ''}},
            {'type': 'item.updated', 'item': {'id': 'r', 'type': 'reasoning', 'text': 'Compare'}},
            {'type': 'item.updated', 'item': {'id': 'r', 'type': 'reasoning', 'text': 'Compare options.'}},
            {'type': 'item.updated', 'item': {'id': 'r', 'type': 'reasoning', 'text': 'Compare'}},
            {'type': 'item.completed', 'item': {
                'id': 'r', 'type': 'reasoning', 'text': 'Compare options.', 'encrypted_content': 'opaque-secret'}},
            {'type': 'item.completed', 'item': {
                'id': 'a', 'type': 'agent_message', 'text': json.dumps(_RESPONSE)}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 17, 'output_tokens': 9}},
        ]

        def receive(kind, text):
            self.assertEqual(threading.get_ident(), owner)
            details.append((kind, text))
            if kind == 'reasoning':
                Path(work[0], 'ack').touch()

        def native(argv, **kwargs):
            work.append(kwargs['cwd'])
            self.assertIn('--ignore-user-config', argv)
            self.assertIn('--ignore-rules', argv)
            self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
            self.assertIn('shell_tool', argv)
            self.assertIn('forced_login_method="chatgpt"', argv)
            output = argv[argv.index('--output-last-message') + 1]
            script = (
                "import json,pathlib,sys,time\n"
                f"events = {events!r}\n"
                "sys.stdin.read()\n"
                "for event in events:\n"
                "    print(json.dumps(event), flush=True)\n"
                "    if event.get('item', {}).get('text') == 'Compare':\n"
                "        deadline = time.monotonic() + 3\n"
                "        while not pathlib.Path('ack').exists():\n"
                "            if time.monotonic() > deadline: sys.exit(7)\n"
                "            time.sleep(.01)\n"
                f"pathlib.Path(sys.argv[1]).write_text({json.dumps(_RESPONSE)!r})\n"
            )
            kwargs['timeout'] = 5
            return run([sys.executable, '-c', script, output], **kwargs)

        with patch.object(seat_codex, 'run', side_effect=native):
            result = seat_codex.invoke('default', 'private conversation',
                                       response_schema([_TOOL]), 5, on_detail=receive)
        self.assertEqual(result['response'], _RESPONSE)
        self.assertEqual([text for kind, text in details if kind == 'reasoning'],
                         ['Compare', ' options.'])
        self.assertTrue(any(kind == 'progress' for kind, _ in details))
        self.assertNotIn('opaque-secret', repr(details))
        self.assertNotIn('example.txt', repr(details))
        self.assertNotIn('tool_calls', repr(details))
        self.assertFalse(Path(work[0]).exists())

    def test_live_diagnostics_are_redacted_and_not_replayed_at_completion(self):
        details = []
        secret = 'sk-' + 'aB7cD9' * 8
        with self.assertLogs('conch.seat_codex', level='DEBUG') as logs:
            result = self.invoke(events=[
                {'type': 'item.completed', 'item': {
                    'type': 'error', 'message': 'API key: ' + secret}},
                {'type': 'turn.completed'},
            ], on_detail=lambda kind, text: details.append((kind, text)))
        self.assertEqual(result['response'], _RESPONSE)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(secret, repr(logs.output) + repr(details))

    def test_native_execution_is_rejected_during_live_delivery(self):
        for kind in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search'):
            with self.subTest(kind=kind), self.assertRaisesRegex(SeatError, 'native tool'):
                self.invoke(events=[
                    {'type': 'item.started', 'item': {
                        'type': kind, 'arguments': 'private-command'}},
                    {'type': 'turn.completed'},
                ], on_detail=lambda kind, text: self.assertNotIn('private-command', text))


class CodexModelDiscoveryTests(unittest.TestCase):
    @contextmanager
    def native(self, body):
        """Run a real stdio peer, leaving it alive for Conch to clean up."""
        with tempfile.TemporaryDirectory(prefix='conch-model-fixture-') as directory:
            trace = Path(directory) / 'requests.jsonl'
            script = textwrap.dedent("""
                import json, os, pathlib, sys, time
                trace = pathlib.Path(sys.argv[1])

                def receive(method):
                    line = sys.stdin.readline()
                    if not line:
                        sys.exit(10)  # stdin must remain open while responses are pending.
                    request = json.loads(line)
                    with trace.open('a') as handle:
                        handle.write(json.dumps(request) + '\\n')
                    assert request['method'] == method
                    return request

                def emit(message):
                    print(json.dumps(message), flush=True)

                def initialize():
                    request = receive('initialize')
                    info = request['params']['clientInfo']
                    assert info['name'] == 'conch' and info['version']
                    assert 'OPENAI_API_KEY' not in os.environ
                    assert 'CODEX_API_KEY' not in os.environ
                    # A full diagnostic pipe must not block metadata discovery.
                    sys.stderr.write('private diagnostic\\n' * 8192)
                    sys.stderr.flush()
                    emit({'id': request['id'], 'result': {'userAgent': 'fixture'}})
                    receive('initialized')
                    return page()

                def page():
                    request = receive('model/list')
                    assert request['params']['includeHidden'] is False
                    assert request['params']['modelProvider'] == 'openai'
                    return request
            """) + textwrap.dedent(body) + '\nwhile True: time.sleep(1)\n'
            native_popen = subprocess.Popen
            processes = []
            work = []

            def launch(argv, **kwargs):
                self.assertEqual(argv[0], 'codex')
                self.assertIn('forced_login_method="chatgpt"', argv)
                self.assertIn('model_provider="openai"', argv)
                self.assertIn('app-server', argv)
                work.append(kwargs['cwd'])
                process = native_popen([sys.executable, '-u', '-c', script, str(trace)],
                                       **kwargs)
                processes.append(process)
                return process

            with patch.object(seat_codex.subprocess, 'Popen', side_effect=launch), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'private-key',
                                            'CODEX_API_KEY': 'private-key'}):
                try:
                    yield processes, trace, work
                finally:
                    # Avoid stranding the fixture even when a regression fails an assertion.
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                            process.wait()

    def assert_cleaned_up(self, processes, work):
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        for pipe in (processes[0].stdin, processes[0].stdout, processes[0].stderr):
            self.assertTrue(pipe.closed)
        self.assertFalse(Path(work[0]).exists())

    def test_lists_native_pages_in_order_without_hidden_or_duplicate_models(self):
        body = """
            request = initialize()
            assert 'cursor' not in request['params']
            emit({'method': 'account/updated', 'params': {'authMode': 'chatgpt'}})
            emit({'id': request['id'], 'result': {'data': [
                {'id': 'picker-alpha', 'model': 'seat-alpha', 'hidden': False},
                {'id': 'hidden', 'model': 'seat-hidden', 'hidden': True},
                {'id': 'seat-legacy'},
            ], 'nextCursor': 'opaque-page-two'}})
            request = page()
            assert request['params']['cursor'] == 'opaque-page-two'
            emit({'id': request['id'], 'result': {'data': [
                {'model': 'seat-alpha'}, {'model': 'seat-beta'},
                {'model': 'seat-legacy'}, {'model': 'seat-hidden', 'hidden': True},
            ], 'nextCursor': None}})
        """
        with self.native(body) as (processes, trace, work):
            self.assertEqual(seat_codex.list_models(timeout=5),
                             ['seat-alpha', 'seat-legacy', 'seat-beta'])
            self.assert_cleaned_up(processes, work)
            requests = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([request['method'] for request in requests],
                             ['initialize', 'initialized', 'model/list', 'model/list'])

    def test_rpc_errors_raise_without_exposing_native_details_and_clean_up(self):
        for stage in ('initialize', 'model/list'):
            with self.subTest(stage=stage):
                body = ("request = receive('initialize')\n" if stage == 'initialize'
                        else 'request = initialize()\n')
                body += ("emit({'id': request['id'], 'error': "
                         "{'code': -32603, 'message': 'private-auth-detail'}})\n")
                with self.native(body) as (processes, trace, work):
                    with self.assertRaisesRegex(SeatError, 'request failed') as error:
                        seat_codex.list_models(timeout=5)
                    self.assertNotIn('private-auth-detail', str(error.exception))
                    self.assert_cleaned_up(processes, work)

    def test_one_deadline_bounds_all_pages_and_cleans_up_timed_out_server(self):
        body = """
            request = initialize()
            deadline = time.monotonic() + 3
            number = 0
            while time.monotonic() < deadline:
                time.sleep(.1)
                number += 1
                emit({'id': request['id'], 'result': {
                    'data': [{'model': 'seat-' + str(number)}],
                    'nextCursor': 'page-' + str(number)}})
                request = page()
            emit({'id': request['id'], 'error': {
                'code': -32603, 'message': 'deadline was reset between pages'}})
        """
        with self.native(body) as (processes, trace, work):
            started = time.monotonic()
            with self.assertRaisesRegex(SeatError, 'timed out'):
                seat_codex.list_models(timeout=1)
            self.assertLess(time.monotonic() - started, 2.5)
            self.assert_cleaned_up(processes, work)
            methods = [json.loads(line)['method'] for line in trace.read_text().splitlines()]
            self.assertGreater(methods.count('model/list'), 1)

    def test_protocol_failures_never_become_an_empty_inventory(self):
        responses = [
            "print('not-json', flush=True)",
            "emit({'id': request['id'], 'result': {'data': 'not-a-list'}})",
            "emit({'id': request['id'] + 1, 'result': {'data': []}})",
            "emit({'id': request['id'], 'result': {'data': [{'model': None}]}})",
            "sys.exit(0)",
        ]
        for response in responses:
            with self.subTest(response=response):
                with self.native('request = initialize()\n' + response + '\n') as (
                        processes, trace, work):
                    with self.assertRaises(SeatError):
                        seat_codex.list_models(timeout=5)
                    self.assert_cleaned_up(processes, work)

    def test_repeated_cursor_is_a_protocol_error_not_partial_success(self):
        body = """
            request = initialize()
            for model in ('seat-alpha', 'seat-beta'):
                emit({'id': request['id'], 'result': {
                    'data': [{'model': model}], 'nextCursor': 'same-page'}})
                request = page()
        """
        with self.native(body) as (processes, trace, work):
            with self.assertRaisesRegex(SeatError, 'repeated'):
                seat_codex.list_models(timeout=5)
            self.assert_cleaned_up(processes, work)

    def test_native_launch_failure_is_not_an_empty_inventory(self):
        with patch.object(seat_codex.subprocess, 'Popen', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(SeatError, 'not installed'):
                seat_codex.list_models()


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
