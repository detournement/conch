"""Golden equivalence scenarios for the eBay Capitol flows (refactor R0).

Each scenario drives the *public* pilot surfaces — ``EbayPilot`` (the
``/ebay`` shell flow) and ``RemoteLoop``/``ChannelListingFlow`` (channel
intake) — against the scripted fake A2A gateway, and captures everything
the flow-pack refactor must reproduce byte-identically:

- the exact wire-call sequence (skill id, JSON-RPC method, context
  threading, request payloads including idempotency keys, challenge and
  confirmation bytes, FilePart shapes as content digests),
- artifact uploads (presigned PUT bytes as digests),
- SSE subscribe cursors (``since_sequence`` resume behavior),
- approval-store records after every step (kind, origin binding, pinned
  revision identity),
- the durable pilot state file, and
- every user-visible reply / notification / shell line.

Normalization (the *only* allowed divergences, per the control design):

- generated ids: JSON-RPC envelope ``id``/``messageId`` are not captured;
  shell listing-session ids (``conch-YYYYmmddHHMMSS-xxxxxx``) and
  presentation ids (``conch-presentation-…``/``conch-channel-…``) map to
  stable placeholders, consistently — every occurrence of the same value
  maps to the same token, so cross-references (idempotency keys, thread
  ids, state keys) remain provable;
- content hashes (``sha256:…``) map to consistent placeholders, and each
  mapped hash's 12-char challenge tail maps to ``<sha-N-tail>`` wherever
  it appears (challenges, summaries) — same-value-same-token again;
- timestamps: ``created_at``/``updated_at``/``started_at`` values become
  0 (message ``ts`` fields are scenario constants and stay literal);
- the caller version inside ``handshake`` payloads becomes
  ``<caller-version>`` (it tracks the package version);
- temp-dir path prefixes become ``<tmp>``.

Everything else — request field bytes, key formulas, approval payload
pins, reply wording, state phases/kinds/cursors — is compared exactly.

``tests/test_capitol_golden.py`` asserts recorded fixtures; re-record
with ``CONCH_GOLDEN_RECORD=1 python -m unittest tests.test_capitol_golden``.
This module is intentionally not itself a test module so the pack
acceptance drill (``/capitol pack verify ebay-listing``) can run the same
scenarios.
"""

import base64
import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.capitol.client import CapitolRuntime
from conch.channels import Attachment, InboundMessage
from conch.remote import ApprovalStore, RemoteLoop

from tests.test_capitol_client import BEARER, FakeGateway
from tests.test_ebay_pilot import (
    BASE_CONFIG,
    EbayEngine,
    ScriptedUI,
    chat_orchestrator,
)

ORG = "org-ebay"
AGENT = "agent-ebay"
JPEG_FRONT = b"\xff\xd8\xff\xe0golden-front-jpeg"
JPEG_BACK = b"\xff\xd8\xff\xe0golden-back-jpeg"

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "capitol_golden"

_APPROVAL_ID_RE = re.compile(r"\[#(\d+)\]")
_TS_KEYS = frozenset({"created_at", "updated_at", "started_at"})


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class Normalizer:
    """Consistent placeholder mapping for the allowed volatile values."""

    _PATTERNS = (
        ("shell-session", re.compile(r"conch-\d{14}-[0-9a-f]{6}")),
        ("presentation",
         re.compile(r"conch-(?:presentation|channel)-[0-9a-f]{10}")),
        ("sha", re.compile(r"sha256:[0-9a-f]{64}")),
    )

    def __init__(self, tmp_prefix: str = ""):
        self._tmp_prefix = tmp_prefix
        self._tokens = {}
        self._counts = {}
        self._sha_tails = []  # (tail, token) in registration order

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._tokens:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"<{kind}-{self._counts[kind]}>"
            self._tokens[key] = token
            if kind == "sha":
                self._sha_tails.append((value[-12:], f"{token[:-1]}-tail>"))
        return self._tokens[key]

    def string(self, text: str) -> str:
        if self._tmp_prefix and self._tmp_prefix in text:
            text = text.replace(self._tmp_prefix, "<tmp>")
        for kind, pattern in self._PATTERNS:
            text = pattern.sub(
                lambda match, kind=kind: self._token(kind, match.group(0)),
                text,
            )
        for tail, token in self._sha_tails:
            if tail in text:
                text = text.replace(tail, token)
        return text

    def walk(self, node):
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                clean_key = self.string(key) if isinstance(key, str) else key
                if key in _TS_KEYS and isinstance(value, (int, float)):
                    result[clean_key] = 0
                else:
                    result[clean_key] = self.walk(value)
            return result
        if isinstance(node, (list, tuple)):
            return [self.walk(item) for item in node]
        if isinstance(node, str):
            return self.string(node)
        return node


# ---------------------------------------------------------------------------
# Environment + capture plumbing
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def scenario_env():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp = tempfile.TemporaryDirectory()
    try:
        FakeGateway.reset(port)
        EbayEngine.reset()
        FakeGateway.on_call_workflow = EbayEngine.handle
        with patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(tmp.name) / "config"),
            "CAPITOL_A2A_BEARER": BEARER,
        }):
            front = Path(tmp.name) / "front.jpg"
            front.write_bytes(JPEG_FRONT)
            back = Path(tmp.name) / "back.jpg"
            back.write_bytes(JPEG_BACK)
            yield types.SimpleNamespace(
                port=port, tmp=Path(tmp.name),
                front=str(front), back=str(back),
            )
    finally:
        FakeGateway.on_call_workflow = None
        FakeGateway.chat_script = None
        server.shutdown()
        server.server_close()
        tmp.cleanup()


def _channel_config(env, **extra):
    config = dict(BASE_CONFIG)
    config.update({
        "provider": "ollama",
        "capitol_base_url": f"http://127.0.0.1:{env.port}",
        "capitol_org": ORG,
        "capitol_agent": AGENT,
        "slack_channel": "C123",
        "slack_allowed_senders": "U111, U222",
    })
    config.update(extra)
    return config


def _shell_config(env, **extra):
    config = dict(BASE_CONFIG)
    config.update({
        "capitol_base_url": f"http://127.0.0.1:{env.port}",
        "capitol_org": ORG,
        "capitol_agent": AGENT,
    })
    config.update(extra)
    return config


class _FakeConvManager:
    def __init__(self):
        self._convs = {}

    def create(self, model, provider):
        conv = types.SimpleNamespace(
            id=f"conv{len(self._convs) + 1}", title="", model=model,
            provider=provider, messages=[],
        )
        self._convs[conv.id] = conv
        return conv

    def load(self, conv_id):
        return self._convs.get(conv_id)

    def save(self, conv):
        pass


def _make_loop(env, config):
    loop = RemoteLoop(
        config, conv_mgr=_FakeConvManager(), chat_state=None,
        builtin_clients={},
    )
    posts = []
    loop.manager.notify = lambda text, channel="", thread_id="": (
        posts.append({"text": text, "channel": channel,
                      "thread_id": thread_id}) or (True, "")
    )
    return loop, posts


def _photo_msg(env, ts="100.0", thread=None, sender="U111",
               text="sell this book", photo=None):
    path = photo or env.front
    return InboundMessage(
        channel="slack", sender=sender, text=text,
        thread_id=thread or ts, ts=ts,
        attachments=[Attachment(
            filename=Path(path).name, mime_type="image/jpeg",
            size_bytes=Path(path).stat().st_size, path=path,
            remote_id="F1",
        )],
    )


def _reply(text, thread="100.0", sender="U111", ts="101.0"):
    return InboundMessage(
        channel="slack", sender=sender, text=text,
        thread_id=thread, ts=ts,
    )


def approval_id_in(text) -> int:
    match = _APPROVAL_ID_RE.search(text or "")
    if match is None:
        raise AssertionError(f"no approval id in reply: {text!r}")
    return int(match.group(1))


def _capture_wire():
    rows = []
    for skill, data, envelope in FakeGateway.calls:
        params = envelope.get("params") or {}
        message = params.get("message") or {}
        parts = message.get("parts") or []
        payload = dict(data)
        if skill == "handshake":
            caller = dict((payload.get("caller") or {}))
            caller["version"] = "<caller-version>"
            payload["caller"] = caller
        row = {
            "skill": skill,
            "method": envelope.get("method"),
            "context_id": message.get("contextId"),
            "data": payload,
        }
        extra = []
        for part in parts[1:]:
            file_part = (part or {}).get("file") or {}
            if file_part:
                raw = base64.b64decode(file_part.get("bytes") or "")
                extra.append({
                    "kind": "file",
                    "name": file_part.get("name"),
                    "mime_type": file_part.get("mimeType"),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                })
            else:
                extra.append({"kind": sorted(part)})
        if extra:
            row["extra_parts"] = extra
        rows.append(row)
    return rows


def _capture_uploads():
    return {
        artifact_id: {
            "sha256": hashlib.sha256(info["bytes"]).hexdigest(),
            "size_bytes": len(info["bytes"]),
            "content_type": info["content_type"],
        }
        for artifact_id, info in FakeGateway.uploads.items()
    }


def _pending_approvals():
    return ApprovalStore().pending()


def _finish_capture(env, capture):
    """Attach the shared trailing sections and normalize the document."""
    capture["wire"] = _capture_wire()
    capture["uploads"] = _capture_uploads()
    capture["idempotency_keys"] = sorted(FakeGateway.idempotency)
    capture["stream_requests"] = list(FakeGateway.stream_requests)
    from conch.capitol.ebay import PilotState

    capture["state"] = PilotState().load()
    normalizer = Normalizer(tmp_prefix=str(env.tmp))
    normalized = normalizer.walk(capture)
    return json.loads(json.dumps(normalized, sort_keys=True))


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def _shell_runtime(env):
    runtime = CapitolRuntime(
        f"http://127.0.0.1:{env.port}", ORG, AGENT, BEARER,
        caller_system="conch-tests", caller_version="0.0",
    )
    runtime.discover()
    return runtime


def shell_typed_clarify_auto_publish(env):
    """Shell intake (typed): upload → draft needs_info → clarify answer →
    revise → caps auto (default sandbox policy) → confirm → publish."""
    from conch.capitol.ebay import EbayPilot

    config = _shell_config(env)
    ui = ScriptedUI(answers=["Acme Press"], confirms=[True])
    pilot = EbayPilot(
        _shell_runtime(env), config,
        say=ui.say, ask=ui.ask, confirm=ui.confirm,
    )
    result = pilot.sell([env.front, env.back], notes="paperback, light wear")
    return _finish_capture(env, {
        "said": ui.said,
        "prompts": ui.prompts,
        "result": {
            "published": result["published"],
            "session_id": result["session_id"],
            "listing": result["listing"],
            "revision_status": result["revision"].get("status"),
            "revision_number": result["revision"].get("revision"),
        },
    })


def shell_caps_gate_exact_approval(env):
    """Shell intake with a breached price cap: exact-approval phrase
    required; the typed phrase publishes."""
    from conch.capitol.ebay import EbayPilot

    EbayEngine.first_draft_needs_info = False
    config = _shell_config(env, ebay_max_price_usd="10")
    ui = ScriptedUI(answers=["proceed to post"])
    pilot = EbayPilot(
        _shell_runtime(env), config,
        say=ui.say, ask=ui.ask, confirm=ui.confirm,
    )
    result = pilot.sell([env.front])
    return _finish_capture(env, {
        "said": ui.said,
        "prompts": ui.prompts,
        "result": {
            "published": result["published"],
            "listing": result["listing"],
        },
    })


def channel_clarify_revise_exact_approval(env):
    """Channel intake: photo → clarify questions relayed → reply revises
    (parent+1, feedback echoed) → origin-bound approval → approve
    consumes it into the byte-exact publish request."""
    loop, posts = _make_loop(env, _channel_config(env))
    steps = []

    reply = loop.handle_inbound(_photo_msg(env))
    steps.append({"action": "photo message ts=100.0", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    reply = loop.handle_inbound(_reply("Acme Press", ts="101.0"))
    steps.append({"action": "clarify answer", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    request_id = approval_id_in(reply)
    reply = loop.handle_inbound(
        _reply(f"approve {request_id}", ts="103.0")
    )
    steps.append({"action": f"approve {request_id}", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    return _finish_capture(env, {"steps": steps, "posts": posts})


def channel_caps_auto_optin(env):
    """Within caps + explicit channel auto opt-in: publishes without an
    approval entry; the decision and challenge are announced in-thread."""
    EbayEngine.first_draft_needs_info = False
    loop, posts = _make_loop(
        env, _channel_config(env, ebay_channel_auto_publish="true")
    )
    reply = loop.handle_inbound(_photo_msg(env))
    steps = [{"action": "photo message (auto opt-in)", "reply": reply,
              "pending_approvals": _pending_approvals()}]
    return _finish_capture(env, {"steps": steps, "posts": posts})


def channel_caps_gate(env):
    """A breached cap forces the approval path even with auto opt-in;
    approving publishes."""
    EbayEngine.first_draft_needs_info = False
    loop, posts = _make_loop(env, _channel_config(
        env, ebay_channel_auto_publish="true", ebay_max_price_usd="10",
    ))
    steps = []
    reply = loop.handle_inbound(_photo_msg(env))
    steps.append({"action": "photo message (cap breach)", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    request_id = approval_id_in(reply)
    reply = loop.handle_inbound(
        _reply(f"approve {request_id}", ts="103.0")
    )
    steps.append({"action": f"approve {request_id}", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    return _finish_capture(env, {"steps": steps, "posts": posts})


def channel_effect_failure_rearm(env):
    """The publish effect fails upstream: the failure surfaces in-thread,
    a fresh approval re-arms, and the retry keeps the exact contract key
    while the gateway call key gains the retry suffix."""
    EbayEngine.first_draft_needs_info = False
    EbayEngine.fail_publish_run = True
    loop, posts = _make_loop(env, _channel_config(env))
    steps = []
    reply = loop.handle_inbound(_photo_msg(env))
    steps.append({"action": "photo message", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    request_id = approval_id_in(reply)
    reply = loop.handle_inbound(
        _reply(f"approve {request_id}", ts="103.0")
    )
    steps.append({"action": f"approve {request_id} (effect fails)",
                  "reply": reply,
                  "pending_approvals": _pending_approvals()})
    retry_id = approval_id_in(
        reply[reply.index("Publish approval needed"):]
    )
    EbayEngine.fail_publish_run = False
    reply = loop.handle_inbound(_reply(f"approve {retry_id}", ts="104.0"))
    steps.append({"action": f"approve {retry_id} (retry succeeds)",
                  "reply": reply,
                  "pending_approvals": _pending_approvals()})
    return _finish_capture(env, {"steps": steps, "posts": posts})


def channel_chat_filepart_intake(env):
    """Chat intake: photos ride the chat message as FileParts, the
    orchestrator launches the run, later turns echo the server-bound
    session identity, and no typed upload happens."""
    EbayEngine.first_draft_needs_info = False
    FakeGateway.chat_script = chat_orchestrator
    loop, posts = _make_loop(env, _channel_config(env, ebay_intake="chat"))
    steps = []
    reply = loop.handle_inbound(_photo_msg(env))
    steps.append({"action": "photo message (chat intake)", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    request_id = approval_id_in(reply)
    reply = loop.handle_inbound(
        _reply(f"approve {request_id}", ts="103.0")
    )
    steps.append({"action": f"approve {request_id}", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    return _finish_capture(env, {"steps": steps, "posts": posts})


def channel_hitl_park_resume(env):
    """Mid-run ``node.input_required``: the run parks durably with its
    sequence cursor; the thread reply feeds the clarification and the
    resumed watch continues from cursor+1."""
    from conch.capitol.channel_flow import ChannelListingFlow

    EbayEngine.first_draft_needs_info = False
    EbayEngine.hitl_on_first_draft = True
    loop, posts = _make_loop(env, _channel_config(env))
    steps = []
    reply = loop.handle_inbound(_photo_msg(env))
    steps.append({"action": "photo message (parks on HITL)", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    from conch.capitol.ebay import PilotState

    state = PilotState()
    session = state.session(state.thread_session("slack:100.0")) or {}
    steps.append({"action": "parked state snapshot",
                  "phase": session.get("phase"),
                  "hitl": session.get("hitl")})
    reply = loop.handle_inbound(_reply("hardback", ts="101.0"))
    steps.append({"action": "hitl answer resumes", "reply": reply,
                  "pending_approvals": _pending_approvals()})
    assert ChannelListingFlow  # imported for parity with the flow surface
    return _finish_capture(env, {"steps": steps, "posts": posts})


def channel_thread_session_mapping(env):
    """Thread↔session mapping: deterministic per-message sessions, same-ts
    dedupe (including after a lost thread binding), distinct sessions per
    message, and text-only messages in unbound threads falling through."""
    from conch.capitol.channel_flow import ChannelListingFlow
    from conch.capitol.ebay import PilotState

    posts = []

    def notify(text, channel, thread_id):
        posts.append({"text": text, "channel": channel,
                      "thread_id": thread_id})
        return True, ""

    flow = ChannelListingFlow(_channel_config(env), ApprovalStore(), notify)
    steps = []

    reply = flow.handle_message(_photo_msg(env))
    state = PilotState()
    first_session = state.thread_session("slack:100.0")
    steps.append({"action": "photo message ts=100.0", "reply": reply,
                  "bound_session": first_session})

    reply = flow.handle_message(_photo_msg(env))
    steps.append({"action": "same message ts replayed", "reply": reply,
                  "sessions_count": len(PilotState().sessions())})

    data = state.load()
    data["threads"] = {}
    state._save(data)
    reply = flow.handle_message(_photo_msg(env))
    steps.append({
        "action": "replay after lost thread binding", "reply": reply,
        "binding_healed":
            PilotState().thread_session("slack:100.0") == first_session,
        "draft_requests": len(EbayEngine.draft_requests),
    })

    reply = flow.handle_message(_photo_msg(env, ts="200.0", photo=env.back))
    second_session = PilotState().thread_session("slack:200.0")
    steps.append({"action": "distinct message ts=200.0", "reply": reply,
                  "bound_session": second_session,
                  "distinct": second_session != first_session})

    reply = flow.handle_message(_reply("hello there", thread="999.9",
                                       ts="300.0"))
    steps.append({"action": "text-only unbound thread",
                  "reply": reply, "fell_through": reply is None})
    return _finish_capture(env, {"steps": steps, "posts": posts})


SCENARIOS = {
    "shell_typed_clarify_auto_publish": shell_typed_clarify_auto_publish,
    "shell_caps_gate_exact_approval": shell_caps_gate_exact_approval,
    "channel_clarify_revise_exact_approval":
        channel_clarify_revise_exact_approval,
    "channel_caps_auto_optin": channel_caps_auto_optin,
    "channel_caps_gate": channel_caps_gate,
    "channel_effect_failure_rearm": channel_effect_failure_rearm,
    "channel_chat_filepart_intake": channel_chat_filepart_intake,
    "channel_hitl_park_resume": channel_hitl_park_resume,
    "channel_thread_session_mapping": channel_thread_session_mapping,
}


def run_scenario(name: str):
    """Run one scenario in a fresh isolated environment; return the
    normalized capture document (JSON-safe)."""
    fn = SCENARIOS[name]
    with scenario_env() as env:
        return fn(env)


def fixture_path(name: str) -> Path:
    return FIXTURES_DIR / f"{name}.json"


def load_fixture(name: str):
    return json.loads(fixture_path(name).read_text())


def record_fixture(name: str):
    capture = run_scenario(name)
    path = fixture_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capture, indent=2, sort_keys=True) + "\n")
    return capture


def verify_all(log=print):
    """Run every scenario against its recorded fixture; returns a report
    list. This is the acceptance-drill entry the pack verify surface uses."""
    report = []
    for name in sorted(SCENARIOS):
        expected = load_fixture(name)
        actual = run_scenario(name)
        ok = expected == actual
        report.append({"scenario": name, "ok": ok})
        log(f"  {'ok' if ok else 'DIVERGED'}: {name}")
        if not ok:
            report[-1]["diff_hint"] = _first_divergence(expected, actual)
    return report


def _first_divergence(expected, actual, path="$"):
    if type(expected) is not type(actual):
        return f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{path}.{key}: unexpected key"
            if key not in actual:
                return f"{path}.{key}: missing key"
            hint = _first_divergence(expected[key], actual[key],
                                     f"{path}.{key}")
            if hint:
                return hint
        return ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            hint = _first_divergence(left, right, f"{path}[{index}]")
            if hint:
                return hint
        return ""
    if expected != actual:
        return f"{path}: {expected!r} != {actual!r}"
    return ""
