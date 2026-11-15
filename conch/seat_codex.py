"""Subscription transport through the unmodified, installed Codex CLI.

Requires exec's --ignore-user-config/--ignore-rules isolation flags. Native
login remains entirely owned by Codex; this module never opens its auth store.
See https://developers.openai.com/codex/config-reference and
https://developers.openai.com/codex/noninteractive.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .seat_common import SeatError, run
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


def _invalid_constant(value: str):
    raise ValueError('non-JSON constant')


def _decode(text: str):
    try:
        return json.loads(text, parse_constant=_invalid_constant)
    except (ValueError, TypeError) as exc:
        raise SeatError('Codex returned malformed JSON') from exc


def _validate(value, schema: dict) -> None:
    """Validate the JSON-schema subset used by Conch's response envelope."""
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str,
             'boolean': bool, 'integer': int, 'number': (int, float),
             'null': type(None)}
    if kind not in types or not isinstance(value, types[kind]):
        raise SeatError('Codex output does not match the response schema')
    if kind in ('integer', 'number') and isinstance(value, bool):
        raise SeatError('Codex output does not match the response schema')
    if 'enum' in schema and value not in schema['enum']:
        raise SeatError('Codex returned a tool outside the response schema')
    if kind == 'object':
        properties = schema.get('properties', {})
        if (not set(schema.get('required', ())).issubset(value)
                or (schema.get('additionalProperties') is False
                    and not set(value).issubset(properties))):
            raise SeatError('Codex output does not match the response schema')
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key])
    elif kind == 'array':
        if (len(value) < schema.get('minItems', 0)
                or ('maxItems' in schema and len(value) > schema['maxItems'])):
            raise SeatError('Codex output does not match the response schema')
        for item in value:
            _validate(item, schema.get('items', {}))


def _events(stdout: str) -> dict:
    usage = {}
    completed = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = _decode(line)
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
                logger.warning('Codex diagnostic: %s', ' '.join(safe_message.split())[:500])
            elif item.get('type') not in ('agent_message', 'reasoning'):
                # Read-only also denies apply_patch, which has no disable switch.
                # Do not accept even an attempted native tool execution.
                raise SeatError('Codex attempted a native tool; Conch must own all tool execution')
        elif kind == 'turn.completed':
            completed = True
            reported = event.get('usage')
            if isinstance(reported, dict):
                counts = {key: reported.get(key)
                          for key in ('input_tokens', 'output_tokens')}
                if all(type(value) is int and value >= 0 for value in counts.values()):
                    usage = counts
        elif kind not in ('thread.started', 'turn.started'):
            raise SeatError('Codex returned an unsupported event; update Conch or the official CLI')
    if not completed:
        raise SeatError('Codex did not complete its response')
    return usage


def invoke(model: str, prompt: str, schema: dict, timeout: float) -> dict:
    """Generate structured content only; never delegate Conch tools to Codex."""
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
        argv.append('-')
        result = run(argv, prompt=prompt, timeout=timeout, cwd=str(work))
        if result.returncode:
            # Native stderr may include auth details, prompts, or tool output.
            raise SeatError(f'Codex exited with status {result.returncode}; '
                            f'ensure the official CLI supports isolated exec. {_LOGIN_REMEDY}')
        usage = _events(result.stdout)
        try:
            response = _decode(output_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError) as exc:
            raise SeatError('Codex did not write a structured response') from exc
        _validate(response, schema)
        if not isinstance(response, dict):
            raise SeatError('Codex did not return a structured response object')
        for call in response.get('tool_calls', []):
            if not isinstance(_decode(call['arguments']), dict):
                raise SeatError('Codex returned tool arguments that are not a JSON object')
        return {'response': response, 'usage': usage, 'model': model or 'default'}
