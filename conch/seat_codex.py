"""Subscription transport through the unmodified, installed Codex CLI.

Requires exec's --ignore-user-config/--ignore-rules isolation flags. Native
login remains entirely owned by Codex; this module never opens its auth store.
See https://developers.openai.com/codex/config-reference and
https://developers.openai.com/codex/noninteractive.
"""

from __future__ import annotations

import json
import logging
import math
import os
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import __version__
from .seat_common import HOST_TOOL_SCOPE, SeatError, environment, run
from .secretguard import redact_credentials

logger = logging.getLogger(__name__)


_LOGIN_REMEDY = 'Run conch auth login codex to sign in with ChatGPT.'
_AUTH_CONFIG = ['-c', 'forced_login_method="chatgpt"',
                '-c', 'model_provider="openai"']
# shell_tool, not unified_exec, is the command execution gate. The latter's
# user opt-out is ignored by current Codex. Disable every other execution
# surface separately, including shell snapshots (which run before generation).
_DISABLED_FEATURES = (
    'shell_tool', 'shell_snapshot', 'shell_snapshot_v2',
    'code_mode', 'code_mode_host', 'code_mode_only',
    'apps', 'enable_mcp_apps', 'plugins', 'remote_plugin', 'hooks',
    'browser_use', 'browser_use_external', 'computer_use',
    'image_generation', 'view_image', 'multi_agent', 'multi_agent_v2',
    'skill_mcp_dependency_install', 'skill_search', 'workspace_dependencies',
    'memories', 'goals', 'tool_suggest', 'request_permissions_tool',
    'standalone_web_search', 'daemon_auto_start',
)


def auth_command(action: str) -> list[str]:
    """Return only native login commands, forcing ChatGPT rather than API auth."""
    commands = {'login': ['login'], 'logout': ['logout'],
                'status': ['login', 'status']}
    if action not in commands:
        raise SeatError('unsupported Codex authentication action')
    return ['codex', *_AUTH_CONFIG, *commands[action]]


def status(timeout: float = 10) -> tuple[bool, str]:
    try:
        result = run(auth_command('status'), timeout=timeout)
    except SeatError as exc:
        return False, f'{exc}. {_LOGIN_REMEDY}'
    # Key login status can contain a masked key. Never echo native output.
    lines = (result.stdout + '\n' + result.stderr).lower().splitlines()
    if (result.returncode == 0
            and any(line.strip() == 'logged in using chatgpt' for line in lines)
            and not any('api key' in line or 'api-key' in line for line in lines)):
        return True, 'Codex is signed in with ChatGPT.'
    return False, f'Codex requires a ChatGPT subscription login. {_LOGIN_REMEDY}'


def list_models(timeout: float = 8.0) -> list[str]:
    """Read the account's picker-visible models without starting a thread or turn.

    App-server is interactive: initialization must finish before listing, and
    each page supplies the cursor for the next. Keep one deadline for all pages.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise SeatError('native client timeout must be a positive finite number')
    deadline = time.monotonic() + timeout
    argv = ['codex', *_AUTH_CONFIG, 'app-server', '--listen', 'stdio://']
    process = None
    with tempfile.TemporaryDirectory(prefix='conch-codex-models-') as directory:
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=environment(), cwd=directory,
                start_new_session=True,
            )
            with selectors.DefaultSelector() as selector:
                for name in ('stdout', 'stderr'):
                    pipe = getattr(process, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                os.set_blocking(process.stdin.fileno(), False)
                outgoing = bytearray()

                def send(message):
                    if not outgoing:
                        selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                    outgoing.extend((json.dumps(message) + '\n').encode('utf-8'))

                request_id = 1
                initializing = True
                send({'id': request_id, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'conch', 'version': __version__},
                }})
                pending = b''
                models = []
                seen_models = set()
                seen_cursors = set()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(remaining):
                        pipe, name = key.fileobj, key.data
                        if name == 'stdin':
                            try:
                                written = os.write(pipe.fileno(), outgoing[:4096])
                            except BlockingIOError:
                                continue
                            del outgoing[:written]
                            if not outgoing:
                                selector.unregister(pipe)
                            continue
                        try:
                            chunk = os.read(pipe.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if name == 'stderr':
                            # Drain diagnostics without retaining or exposing auth details.
                            if not chunk:
                                selector.unregister(pipe)
                            continue
                        if not chunk:
                            raise SeatError('Codex model discovery ended before its response')
                        pending += chunk
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            if not line.strip():
                                continue
                            message = _decode(line.decode('utf-8'), 'model discovery JSON')
                            if not isinstance(message, dict):
                                raise SeatError('Codex returned an invalid model discovery response')
                            if 'id' not in message and isinstance(message.get('method'), str):
                                continue  # App-server notifications are not model inventory.
                            if (type(message.get('id')) is not int
                                    or message['id'] != request_id or 'method' in message):
                                raise SeatError('Codex returned an unexpected model discovery response')
                            if 'error' in message:
                                raise SeatError('Codex model discovery request failed; '
                                                + _LOGIN_REMEDY)
                            result = message.get('result')
                            if not isinstance(result, dict):
                                raise SeatError('Codex returned an invalid model discovery result')
                            if initializing:
                                initializing = False
                                send({'method': 'initialized', 'params': {}})
                                cursor = None
                            else:
                                data = result.get('data')
                                cursor = result.get('nextCursor')
                                if (not isinstance(data, list)
                                        or (cursor is not None
                                            and (not isinstance(cursor, str) or not cursor))):
                                    raise SeatError('Codex returned an invalid model inventory page')
                                for entry in data:
                                    if not isinstance(entry, dict):
                                        raise SeatError('Codex returned an invalid model entry')
                                    if entry.get('hidden') is True:
                                        continue
                                    model = entry.get('model', entry.get('id'))
                                    if not isinstance(model, str) or not model.strip():
                                        raise SeatError('Codex returned an invalid model identifier')
                                    if model not in seen_models:
                                        seen_models.add(model)
                                        models.append(model)
                                if cursor is None:
                                    return models
                                if cursor in seen_cursors:
                                    raise SeatError('Codex repeated a model inventory cursor')
                                seen_cursors.add(cursor)
                            request_id += 1
                            params = {'includeHidden': False, 'modelProvider': 'openai',
                                      'limit': 100}
                            if cursor is not None:
                                params['cursor'] = cursor
                            send({'id': request_id, 'method': 'model/list', 'params': params})
        except FileNotFoundError as exc:
            raise SeatError('codex is not installed; install the official CLI first') from exc
        except subprocess.TimeoutExpired as exc:
            raise SeatError(f'Codex model discovery timed out after {timeout:g}s') from exc
        except UnicodeError as exc:
            raise SeatError('Codex returned invalid model discovery text encoding') from exc
        except OSError as exc:
            raise SeatError(f'Codex model discovery failed ({type(exc).__name__})') from exc
        finally:
            if process is not None:
                # Match the inference boundary, including native helper descendants.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
                process.wait()


def _invalid_constant(value: str):
    raise ValueError('non-JSON constant')


def _decode(text: str, source: str):
    try:
        return json.loads(text, parse_constant=_invalid_constant)
    except (ValueError, TypeError) as exc:
        raise SeatError(f'Codex returned malformed {source}') from exc


def _mismatch():
    return SeatError('Codex output does not match the response schema')


def _validate(value, schema: dict) -> None:
    """Validate the JSON-schema subset used by Conch's response envelope."""
    if 'anyOf' in schema:
        for variant in schema['anyOf']:
            try:
                return _validate(value, variant)
            except SeatError:
                continue
        raise _mismatch()
    kinds = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str,
             'boolean': bool, 'integer': int, 'number': (int, float),
             'null': type(None)}
    kind = next(
        (name for name in ([kinds] if isinstance(kinds, str) else kinds or ())
         if name in types and isinstance(value, types[name])
         and not (name in ('integer', 'number') and isinstance(value, bool))),
        None,
    )
    if kind is None or ('enum' in schema and value not in schema['enum']):
        raise _mismatch()
    if kind == 'object':
        properties = schema.get('properties', {})
        if (not set(schema.get('required', ())).issubset(value)
                or (schema.get('additionalProperties') is False
                    and not set(value).issubset(properties))):
            raise _mismatch()
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key])
    elif kind == 'array':
        if (len(value) < schema.get('minItems', 0)
                or ('maxItems' in schema and len(value) > schema['maxItems'])):
            raise _mismatch()
        for item in value:
            _validate(item, schema.get('items', {}))


class _Events:
    """Validate JSONL as it arrives, exposing only published reasoning text."""

    def __init__(self, on_detail=None):
        self.on_detail = on_detail
        self.usage = {}
        self.completed = False
        self.reasoning = {}
        self.responses = set()

    def detail(self, kind, text):
        if self.on_detail is not None and text:
            self.on_detail(kind, text)

    def feed(self, line):
        if not line.strip():
            return
        event = _decode(line, 'event JSON')
        if not isinstance(event, dict):
            raise SeatError('Codex returned an invalid event stream')
        kind = event.get('type')
        if kind in ('error', 'turn.failed'):
            raise SeatError('Codex reported a provider error; check native login and seat limits')
        if kind in ('item.started', 'item.updated', 'item.completed'):
            item = event.get('item')
            if not isinstance(item, dict):
                raise SeatError('Codex returned an invalid stream item')
            if item.get('type') == 'error':
                # Codex ErrorItem is a nonfatal diagnostic, unlike the fatal
                # top-level error/turn.failed events. It is not tool execution.
                message = item.get('message')
                if not isinstance(message, str) or not message.strip():
                    raise SeatError('Codex returned an invalid diagnostic')
                safe_message, _ = redact_credentials(message, '[credential redacted]')
                # Conch disables Codex's execution features on purpose, so the
                # CLI reports them unavailable on every turn. Keep the record
                # for debugging; it is not something the user must act on.
                logger.debug('Codex diagnostic: %s', ' '.join(safe_message.split())[:500])
            elif item.get('type') == 'reasoning':
                text = item.get('text', '')
                identity = item.get('id', '')
                if not isinstance(text, str) or not isinstance(identity, str):
                    raise SeatError('Codex returned invalid reasoning text')
                previous = self.reasoning.get(identity, '')
                # item.updated/completed carry snapshots, not token deltas.
                # A repeated, stale or revised prefix cannot be appended safely.
                if text.startswith(previous):
                    self.reasoning[identity] = text
                    self.detail('reasoning', text[len(previous):])
            elif item.get('type') == 'agent_message':
                identity = item.get('id', '')
                if not isinstance(identity, str):
                    raise SeatError('Codex returned an invalid stream item')
                if identity not in self.responses:
                    self.responses.add(identity)
                    self.detail('progress', 'Codex preparing structured response')
            else:
                # Read-only also denies apply_patch, which has no disable switch.
                # Do not accept even an attempted native tool execution.
                raise SeatError('Codex attempted a native tool; Conch must own all tool execution')
        elif kind == 'turn.completed':
            self.completed = True
            reported = event.get('usage')
            if isinstance(reported, dict):
                counts = {key: reported.get(key)
                          for key in ('input_tokens', 'output_tokens')}
                if all(type(value) is int and value >= 0 for value in counts.values()):
                    self.usage = counts
            self.detail('progress', 'Codex generation complete')
        elif kind == 'thread.started':
            self.detail('progress', 'Codex session started')
        elif kind == 'turn.started':
            self.detail('progress', 'Codex generating')
        else:
            raise SeatError('Codex returned an unsupported event; update Conch or the official CLI')

    def finish(self):
        if not self.completed:
            raise SeatError('Codex did not complete its response')
        return self.usage


def invoke(model: str, prompt: str, schema: dict, timeout: float, on_detail=None,
           effort: str = '', evaluation=None) -> dict:
    """Generate structured content only; never delegate Conch tools to Codex."""
    if evaluation is not None:
        from .seat_common import reject_native_evaluation
        reject_native_evaluation("codex")
    with tempfile.TemporaryDirectory(prefix='conch-codex-') as directory:
        root = Path(directory)
        schema_path = root / 'response-schema.json'
        output_path = root / 'response.json'
        work = root / 'work'
        work.mkdir(mode=0o700)
        # Pre-create both files privately, independent of the caller's umask.
        for path, text in ((schema_path, json.dumps(schema)), (output_path, '')):
            with path.open('x', encoding='utf-8') as handle:
                os.chmod(path, 0o600)
                handle.write(text)
        argv = ['codex', 'exec', *_AUTH_CONFIG,
                '--json', '--output-schema', str(schema_path),
                '--output-last-message', str(output_path),
                '--sandbox', 'read-only', '--skip-git-repo-check',
                '--ephemeral', '--ignore-user-config', '--ignore-rules',
                '-c', 'approval_policy="never"',
                '-c', 'developer_instructions=' + json.dumps(HOST_TOOL_SCOPE),
                '-c', 'agents.enabled=false',
                '-c', 'web_search="disabled"', '-c', 'tools.web_search=false',
                '-c', 'tools.update_plan.enabled=false',
                '-c', 'tools.experimental_request_user_input.enabled=false',
                '-c', 'history.persistence="none"',
                '-c', 'project_doc_max_bytes=0',
                '-c', 'skills.bundled.enabled=false',
                '-c', 'skills.include_instructions=false']
        for feature in _DISABLED_FEATURES:
            argv.extend(['--disable', feature])
        if model and model != 'default':
            argv.extend(['--model', model])
        if effort in ('low', 'medium', 'high', 'xhigh'):
            # Chosen in the model picker (model_tuning); verified live with
            # codex-cli 0.169.
            argv.extend(['-c', f'model_reasoning_effort="{effort}"'])
        argv.append('-')
        events = _Events(on_detail)
        if on_detail is None:
            result = run(argv, prompt=prompt, timeout=timeout, cwd=str(work))
        else:
            result = run(argv, prompt=prompt, timeout=timeout, cwd=str(work),
                         on_stdout_line=events.feed)
        if result.returncode:
            # Native stderr may include auth details, prompts, or tool output.
            raise SeatError(f'Codex exited with status {result.returncode}; '
                            f'ensure the official CLI supports isolated exec. {_LOGIN_REMEDY}')
        if on_detail is None:
            for line in result.stdout.splitlines():
                events.feed(line)
        usage = events.finish()
        try:
            response = _decode(output_path.read_text(encoding='utf-8'), 'response JSON')
        except (OSError, UnicodeError) as exc:
            raise SeatError('Codex did not write a structured response') from exc
        _validate(response, schema)
        if not isinstance(response, dict):
            raise SeatError('Codex did not return a structured response object')
        return {'response': response, 'usage': usage, 'model': model or 'default'}
