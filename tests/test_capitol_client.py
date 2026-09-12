"""CapitolRuntime adapter tests against a scripted fake A2A gateway.

Recorded-wire-shape coverage for the Phase 3 contract-test gate: the fake
gateway speaks the same envelopes as the reference acceptance scripts
(JSON-RPC ``SendMessage``/``SendStreamingMessage``, Task results with
status-message data parts, ``google.rpc.ErrorInfo`` error envelopes, SSE
``TaskArtifactUpdateEvent`` frames, presigned-PUT uploads), so these tests
prove the exact bytes conch emits and accepts:

- handshake + context threading,
- idempotent ``call_workflow`` retry (same key replays the original run),
- SSE resume with ``since_sequence`` after a mid-stream disconnect,
- HITL round-trips (intervention + clarification),
- artifact upload (``request_upload_url`` handshake + presigned PUT),
- unknown-wire-version / error-envelope / auth failures failing closed,
- bearer redaction and bearer resolution, and local_only enforcement.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.capitol.client import (
    CapitolRuntime,
    extract_payload,
    sse_events,
)
from conch.capitol.credentials import (
    ensure_endpoint_allowed,
    is_local_endpoint,
    parse_agents_yaml,
    resolve_bearer,
)
from conch.capitol.errors import (
    CapitolAuthError,
    CapitolError,
    CapitolProtocolError,
    parse_error_info,
)

ORG = "org-0000"
AGENT = "agent-0000"
BEARER = "cap_a2a_TESTTOKENxx0123456789abcdef"


def _task(payload):
    return {
        "status": {
            "state": "TASK_STATE_COMPLETED",
            "message": {"parts": [{"data": payload}]},
        }
    }


class FakeGateway(BaseHTTPRequestHandler):
    """Scripted Capitol A2A gateway. Class-level state, reset per test."""

    card = {}
    calls = []          # (skill_id, data, envelope)
    runs = {}           # run_id -> {"status", "output"}
    idempotency = {}    # key -> (inputs_json, run_id)
    stream_plans = {}   # run_id -> [segment, ...]; segment: {"events", "end"}
    stream_requests = []
    uploads = {}
    blobs = {}
    echo_bearer_500 = False
    error_script = None
    chat_script = None
    run_counter = 0

    @classmethod
    def reset(cls, port):
        cls.card = {
            "name": "Fake eBay Sales Operator",
            "wireSchemaVersion": "1.0.11",
            "skills": [{"id": "handshake"}, {"id": "call_workflow"}],
        }
        cls.calls = []
        cls.runs = {}
        cls.idempotency = {}
        cls.stream_plans = {}
        cls.stream_requests = []
        cls.uploads = {}
        cls.blobs = {}
        cls.echo_bearer_500 = False
        cls.error_script = None
        cls.chat_script = None
        cls.run_counter = 0
        cls.port = port

    def log_message(self, *_args):
        pass

    # -- plumbing ---------------------------------------------------------

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {BEARER}"

    # -- HTTP surface ------------------------------------------------------

    def do_GET(self):
        cls = self.__class__
        if self.path.endswith("/.well-known/agent-card.json"):
            if not self._authorized():
                self._json({"detail": "Bearer token not recognized"}, 401)
                return
            self._json(cls.card)
            return
        if "/files/" in self.path and self.path.endswith("/download-url"):
            if not self._authorized():
                self._json({"detail": "no"}, 401)
                return
            file_id = self.path.split("/files/")[1].split("/")[0]
            self._json({
                "file_id": file_id,
                "filename": f"{file_id}.bin",
                "download_url": f"http://127.0.0.1:{cls.port}/blob/{file_id}",
            })
            return
        if self.path.startswith("/blob/"):
            blob = cls.blobs.get(self.path.split("/blob/")[1], b"")
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self.send_error(404)

    def do_PUT(self):
        cls = self.__class__
        if self.path.startswith("/upload/"):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            cls.uploads[self.path.split("/upload/")[1]] = {
                "bytes": body,
                "content_type": self.headers.get("Content-Type", ""),
            }
            self._json({"ok": True})
            return
        self.send_error(404)

    def do_POST(self):
        cls = self.__class__
        length = int(self.headers.get("Content-Length", "0"))
        envelope = json.loads(self.rfile.read(length) or b"{}")
        if cls.echo_bearer_500:
            data = json.dumps({
                "detail": "boom",
                "auth_echo": self.headers.get("Authorization", ""),
            }).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._authorized():
            self._json({"detail": "Bearer token not recognized"}, 401)
            return
        method = envelope.get("method")
        if method == "CancelTask":
            self._json({
                "jsonrpc": "2.0", "id": envelope.get("id"),
                "result": {"status": {"state": "TASK_STATE_CANCELED"}},
            })
            return
        message = (envelope.get("params") or {}).get("message") or {}
        parts = message.get("parts") or []
        data = dict((parts[0].get("data") or {})) if parts else {}
        skill = data.pop("skill_id", "")
        cls.calls.append((skill, data, envelope))
        if method == "SendStreamingMessage":
            self._stream(skill, data)
            return
        if cls.error_script is not None:
            error = cls.error_script
            cls.error_script = None
            self._json({
                "jsonrpc": "2.0", "id": envelope.get("id"), "error": error,
            })
            return
        payload = self._dispatch(skill, data, message)
        if isinstance(payload, dict) and payload.get("__rpc_error__"):
            self._json({
                "jsonrpc": "2.0", "id": envelope.get("id"),
                "error": payload["__rpc_error__"],
            })
            return
        if isinstance(payload, dict) and payload.get("__task_failed__"):
            self._json({
                "jsonrpc": "2.0", "id": envelope.get("id"),
                "result": {"status": {
                    "state": "TASK_STATE_FAILED",
                    "message": {"parts": [
                        {"text": payload["__task_failed__"]}
                    ]},
                }},
            })
            return
        self._json({
            "jsonrpc": "2.0", "id": envelope.get("id"),
            "result": _task(payload),
        })

    # -- skills -------------------------------------------------------------

    def _dispatch(self, skill, data, message):
        cls = self.__class__
        if skill == "handshake":
            caller = data.get("caller") or {}
            if not caller.get("system") or not caller.get("version"):
                return {"__task_failed__": (
                    "handshake requires data.caller.system and "
                    "data.caller.version"
                )}
            return {
                "session": {"context_id": "ctx-fake-1"},
                "wire_schema_version": cls.card.get("wireSchemaVersion"),
            }
        if skill == "list_workflows":
            return {"workflows": [
                {"id": "draft-wf", "workflow_id": "draft-wf",
                 "name": "Draft or Revise Listing"},
                {"id": "publish-wf", "workflow_id": "publish-wf",
                 "name": "Approve and Publish Listing"},
            ]}
        if skill == "get_workflow_details":
            return {"fields": [{
                "node_instance_id": "node-json-input",
                "field_id": "value",
                "valid_types": ["dict"],
                "required": True,
            }]}
        if skill == "call_workflow":
            return self._call_workflow(data)
        if skill == "get_workflow_status":
            run = cls.runs.get(data.get("run_id")) or {}
            return {"status": run.get("status", "running")}
        if skill == "get_workflow_output":
            run = cls.runs.get(data.get("run_id")) or {}
            return run.get("output", {})
        if skill == "get_workflow_events":
            return {"events": []}
        if skill in ("submit_intervention_response",
                     "submit_clarification_response"):
            return {"delivered": True, "request_id": data.get("request_id")}
        if skill == "request_upload_url":
            artifact_id = f"art-{len(cls.uploads) + 1}"
            return {
                "artifact_id": artifact_id,
                "upload_url": f"http://127.0.0.1:{cls.port}/upload/{artifact_id}",
                "method": "PUT",
                "headers": {"Content-Type": data.get("mime_type", "")},
                "expires_at": "2099-01-01T00:00:00Z",
            }
        if skill == "upload_file":
            return {"file_id": "file-inline-1",
                    "filename": data.get("filename"),
                    "s3_key": "k", "size_bytes": 3,
                    "download_url": "http://example.invalid/x"}
        if skill in ("pause_workflow", "stop_workflow"):
            return {"ok": True, "run_id": data.get("run_id")}
        if skill == "chat":
            script = getattr(cls, "chat_script", None)
            if script:
                return script(data, message)
            return {"assistant_reply": "hello", "conversation_id": "ctx-fake-1"}
        if skill == "list_workflow_runs":
            rows = [
                {"run_id": run_id,
                 "started_by_context": info.get("started_by_context", "")}
                for run_id, info in cls.runs.items()
            ]
            return {"runs": rows}
        return {"__task_failed__": f"Skill not supported: {skill}"}

    def _call_workflow(self, data):
        cls = self.__class__
        key = data.get("idempotency_key")
        inputs_json = json.dumps(data.get("inputs"), sort_keys=True)
        if key and key in cls.idempotency:
            previous_inputs, run_id = cls.idempotency[key]
            if previous_inputs == inputs_json:
                return {"run_id": run_id, "session_id": f"s-{run_id}",
                        "status": "queued", "replayed": True}
            return {"__rpc_error__": {
                "code": -32005, "message": "IdempotencyConflict",
                "data": [{"reason": "IDEMPOTENCY_CONFLICT",
                          "metadata": {"retryable": False}}],
            }}
        cls.run_counter += 1
        run_id = f"run-{cls.run_counter}"
        if key:
            cls.idempotency[key] = (inputs_json, run_id)
        cls.runs.setdefault(run_id, {"status": "running", "output": {}})
        hook = getattr(cls, "on_call_workflow", None)
        if hook:
            hook(run_id, data)
        return {"run_id": run_id, "session_id": f"s-{run_id}",
                "status": "queued",
                "workflow_id": data.get("workflow_id")}

    # -- SSE ------------------------------------------------------------------

    def _stream(self, skill, data):
        cls = self.__class__
        run_id = data.get("run_id")
        since = int(data.get("since_sequence") or 0)
        cls.stream_requests.append({"run_id": run_id, "since": since})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def frame(payload):
            body = json.dumps({"jsonrpc": "2.0", "result": payload})
            self.wfile.write(f"data: {body}\n\n".encode())
            self.wfile.flush()

        plan = cls.stream_plans.get(run_id) or []
        segment = plan.pop(0) if plan else {"events": [], "end": "terminal"}
        for event in segment.get("events", []):
            # Persisted events honor the since_sequence cursor; transport
            # frames without a sequence (keepalives) always pass through.
            if "sequence" in event and int(event["sequence"]) < since:
                continue
            frame({"artifact": {"parts": [{"data": event}]}})
        end = segment.get("end", "terminal")
        if end == "terminal":
            cls.runs.setdefault(run_id, {}).update(status="success")
            frame({"status": {"state": "TASK_STATE_COMPLETED"}})
        elif end == "stream_local":
            frame({"status": {
                "state": "TASK_STATE_WORKING",
                "message": {"parts": [{"data": {
                    "stream_close_reason": "inactivity_timeout",
                }}]},
            }})
        # end == "drop": close without a terminal frame


def _event(sequence, event_type="node.node_started", **extra):
    event = {
        "run_id": "r", "sequence": sequence, "event_type": event_type,
        "scope": "node",
        "node": {"node_id": f"n{sequence}", "node_type": "agent",
                 "display_name": f"Node {sequence}"},
        "data": {},
    }
    event.update(extra)
    return event


class CapitolClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeGateway.reset(self.port)
        self.runtime = CapitolRuntime(
            f"http://127.0.0.1:{self.port}", ORG, AGENT, BEARER,
            caller_system="conch-tests", caller_version="0.0",
        )

    # -- discovery / handshake -------------------------------------------

    def test_discover_and_handshake_thread_context(self):
        card = self.runtime.discover()
        self.assertEqual(card["name"], "Fake eBay Sales Operator")
        context = self.runtime.handshake()
        self.assertEqual(context, "ctx-fake-1")
        self.runtime.list_workflows()
        skill, _data, envelope = FakeGateway.calls[-1]
        self.assertEqual(skill, "list_workflows")
        message = envelope["params"]["message"]
        self.assertEqual(message["contextId"], "ctx-fake-1")
        self.assertEqual(envelope["method"], "SendMessage")

    def test_handshake_requires_caller_identity(self):
        # The fake enforces the real gateway's rule; an empty caller is a
        # failed Task, which the client must surface as an error.
        self.runtime.caller_system = ""
        with self.assertRaises(CapitolError):
            self.runtime.handshake()

    def test_unknown_wire_version_fails_closed(self):
        FakeGateway.card = dict(FakeGateway.card, wireSchemaVersion="2.1.0")
        with self.assertRaises(CapitolProtocolError):
            self.runtime.discover()

    # -- errors ------------------------------------------------------------

    def test_error_envelope_wrapped_shape(self):
        FakeGateway.error_script = {
            "code": -32603, "message": "InternalError",
            "data": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "INTERNAL_ERROR", "domain": "a2a-protocol.org",
                "metadata": {"retryable": True, "category": "server",
                             "actionable_hint": "retry with backoff"},
            }],
        }
        with self.assertRaises(CapitolError) as raised:
            self.runtime.list_workflows()
        error = raised.exception
        self.assertEqual(error.code, -32603)
        self.assertEqual(error.reason, "INTERNAL_ERROR")
        self.assertTrue(error.retryable)
        self.assertEqual(error.hint, "retry with backoff")

    def test_error_envelope_flat_legacy_shape(self):
        info = parse_error_info({
            "code": -32005, "message": "IdempotencyConflict",
            "data": {"reason": "IDEMPOTENCY_CONFLICT", "retryable": False},
        })
        self.assertEqual(info["reason"], "IDEMPOTENCY_CONFLICT")
        self.assertIs(info["retryable"], False)

    def test_unknown_retryability_fails_closed(self):
        info = parse_error_info({"code": -32000, "message": "X", "data": None})
        self.assertIsNone(info["retryable"])  # callers treat None as no-retry

    def test_http_401_is_auth_error(self):
        bad = CapitolRuntime(
            f"http://127.0.0.1:{self.port}", ORG, AGENT, "cap_a2a_WRONG"
        )
        with self.assertRaises(CapitolAuthError):
            bad.list_workflows()

    def test_permission_denied_rpc_is_auth_error(self):
        FakeGateway.error_script = {
            "code": -32004, "message": "PermissionDenied",
            "data": [{"reason": "PERMISSION_DENIED",
                      "metadata": {"retryable": False}}],
        }
        with self.assertRaises(CapitolAuthError):
            self.runtime.list_workflows()

    def test_bearer_never_appears_in_errors(self):
        FakeGateway.echo_bearer_500 = True
        with self.assertRaises(CapitolError) as raised:
            self.runtime.list_workflows()
        text = str(raised.exception)
        self.assertNotIn(BEARER, text)
        self.assertIn("[redacted]", text)

    def test_task_failed_result_raises(self):
        with self.assertRaises(CapitolError) as raised:
            self.runtime.call("definitely_not_a_skill", {})
        self.assertIn("Skill not supported", str(raised.exception))

    # -- idempotency ---------------------------------------------------------

    def test_idempotent_call_workflow_retry(self):
        first = self.runtime.call_workflow(
            "draft-wf", {"value": {"n": 1}}, idempotency_key="key-1"
        )
        second = self.runtime.call_workflow(
            "draft-wf", {"value": {"n": 1}}, idempotency_key="key-1"
        )
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second.get("replayed"))

    def test_idempotency_conflict_on_changed_inputs(self):
        self.runtime.call_workflow(
            "draft-wf", {"value": {"n": 1}}, idempotency_key="key-2"
        )
        with self.assertRaises(CapitolError) as raised:
            self.runtime.call_workflow(
                "draft-wf", {"value": {"n": 2}}, idempotency_key="key-2"
            )
        self.assertEqual(raised.exception.code, -32005)
        self.assertIs(raised.exception.retryable, False)

    # -- streaming -------------------------------------------------------------

    def test_watch_run_resumes_after_mid_stream_disconnect(self):
        run_id = "run-sse"
        FakeGateway.runs[run_id] = {"status": "running", "output": {}}
        FakeGateway.stream_plans[run_id] = [
            {"events": [_event(1), _event(2)], "end": "drop"},
            {"events": [_event(1), _event(2), _event(3), _event(4)],
             "end": "terminal"},
        ]
        events = list(self.runtime.watch_run(
            run_id, reconnect_delay=0, _sleep=lambda _s: None
        ))
        sequences = [
            event["sequence"] for event in events
            if event.get("event_type") == "node.node_started"
        ]
        self.assertEqual(sequences, [1, 2, 3, 4])  # contiguous, no dupes
        self.assertEqual(events[-1]["event_type"], "_final_status")
        self.assertEqual(
            [request["since"] for request in FakeGateway.stream_requests],
            [1, 3],  # resumed from last_seen + 1
        )

    def test_watch_run_reconciles_terminal_after_drop(self):
        run_id = "run-endrace"
        FakeGateway.runs[run_id] = {"status": "success", "output": {}}
        FakeGateway.stream_plans[run_id] = [
            {"events": [_event(1)], "end": "drop"},
        ]
        events = list(self.runtime.watch_run(
            run_id, reconnect_delay=0, _sleep=lambda _s: None
        ))
        self.assertEqual(events[-1]["event_type"], "_final_status")
        self.assertTrue(events[-1]["data"].get("reconciled"))

    def test_watch_run_stream_local_close_is_not_a_verdict(self):
        run_id = "run-local-close"
        FakeGateway.runs[run_id] = {"status": "running", "output": {}}
        FakeGateway.stream_plans[run_id] = [
            {"events": [_event(1)], "end": "stream_local"},
            {"events": [_event(2)], "end": "terminal"},
        ]
        events = list(self.runtime.watch_run(
            run_id, reconnect_delay=0, _sleep=lambda _s: None
        ))
        kinds = [event.get("event_type") for event in events]
        self.assertNotIn("_final_status", kinds[:-1])
        self.assertEqual(events[-1]["event_type"], "_final_status")
        self.assertEqual(len(FakeGateway.stream_requests), 2)

    def test_watch_run_skips_keepalives(self):
        run_id = "run-keepalive"
        FakeGateway.runs[run_id] = {"status": "running", "output": {}}
        FakeGateway.stream_plans[run_id] = [
            {"events": [
                _event(1),
                {"event_type": "a2a.stream_keepalive"},
                _event(2),
            ], "end": "terminal"},
        ]
        events = list(self.runtime.watch_run(run_id))
        kinds = [event.get("event_type") for event in events]
        self.assertNotIn("a2a.stream_keepalive", kinds)

    def test_sse_parser_multiline_and_comments(self):
        lines = [
            ": keepalive comment",
            "event: message",
            "data: {\"a\":",
            "data:  1}",
            "",
            "data: {\"b\": 2}",
            "",
        ]
        payloads = [json.loads(p) for p in sse_events(lines)]
        self.assertEqual(payloads, [{"a": 1}, {"b": 2}])

    # -- HITL --------------------------------------------------------------------

    def test_hitl_round_trip_wire_shapes(self):
        self.runtime.submit_clarification("run-9", "req-1", "blue variant")
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_clarification_response")
        self.assertEqual(data, {
            "run_id": "run-9", "request_id": "req-1",
            "response": "blue variant", "declined": False,
        })
        self.runtime.submit_intervention("run-9", "node-3", "req-2", "continue")
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_intervention_response")
        self.assertEqual(data, {
            "run_id": "run-9", "node_id": "node-3",
            "request_id": "req-2", "response": "continue",
        })

    # -- artifacts -----------------------------------------------------------------

    def test_artifact_upload_presigned_put(self):
        blob = b"\xff\xd8\xff fake jpeg bytes"
        result = self.runtime.upload_artifact(
            data=blob, filename="item.jpg", mime_type="image/jpeg"
        )
        self.assertEqual(result["artifact_id"], "art-1")
        self.assertEqual(result["size_bytes"], len(blob))
        import hashlib
        self.assertEqual(
            result["digest"], "sha256:" + hashlib.sha256(blob).hexdigest()
        )
        stored = FakeGateway.uploads["art-1"]
        self.assertEqual(stored["bytes"], blob)
        self.assertEqual(stored["content_type"], "image/jpeg")
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "request_upload_url")
        self.assertEqual(data["filename"], "item.jpg")
        self.assertEqual(data["size_bytes"], len(blob))

    def test_download_artifact_round_trip(self):
        FakeGateway.blobs["file-7"] = b"label pdf bytes"
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "label.pdf"
            result = self.runtime.download_artifact("file-7", str(destination))
            self.assertEqual(destination.read_bytes(), b"label pdf bytes")
            self.assertEqual(result["size_bytes"], 15)

    def test_cancel_task_uses_spec_method(self):
        result = self.runtime.cancel_task("run-1")
        self.assertEqual(result["status"]["state"], "TASK_STATE_CANCELED")

    def test_extract_payload_prefers_status_message(self):
        task = {
            "status": {"message": {"parts": [{"data": {"x": 1}}]}},
            "artifacts": [{"parts": [{"data": {"x": 2}}]}],
        }
        self.assertEqual(extract_payload(task), {"x": 1})


class CredentialTests(unittest.TestCase):
    SAMPLE = (
        "# comment\n"
        "agents:\n"
        "- name: eu-staging\n"
        "  base_url: https://staging.example\n"
        "  org_id: org-eu\n"
        "  agent_id: agent-eu\n"
        "  bearer: cap_a2a_EU\n"
        "  description: multi line description that wraps onto\n"
        "    a continuation line without a colon-value shape\n"
        "- name: local-ebay\n"
        "  base_url: http://localhost:8300\n"
        "  org_id: org-local\n"
        "  agent_id: agent-local\n"
        "  bearer: cap_a2a_LOCAL\n"
        "  x_user_token: eyJnot-a-bearer\n"
    )

    def test_parse_agents_yaml(self):
        agents = parse_agents_yaml(self.SAMPLE)
        self.assertEqual(len(agents), 2)
        self.assertEqual(agents[0]["name"], "eu-staging")
        self.assertEqual(agents[1]["bearer"], "cap_a2a_LOCAL")
        self.assertEqual(agents[1]["base_url"], "http://localhost:8300")

    def test_resolve_bearer_env_wins(self):
        with patch.dict(os.environ, {"CAPITOL_A2A_BEARER": "cap_a2a_ENV"}):
            bearer, source = resolve_bearer({}, "org-x", "agent-x")
            self.assertEqual(bearer, "cap_a2a_ENV")
            self.assertTrue(source.startswith("env:"))

    def test_resolve_bearer_custom_env_name(self):
        with patch.dict(
            os.environ, {"MY_PILOT_BEARER": "cap_a2a_CUSTOM"}, clear=False
        ):
            os.environ.pop("CAPITOL_A2A_BEARER", None)
            bearer, source = resolve_bearer(
                {"capitol_bearer_env": "MY_PILOT_BEARER"}, "o", "a"
            )
            self.assertEqual(bearer, "cap_a2a_CUSTOM")
            self.assertEqual(source, "env:MY_PILOT_BEARER")

    def test_resolve_bearer_from_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "agents.yaml"
            registry.write_text(self.SAMPLE)
            with patch(
                "conch.capitol.credentials.REGISTRY_PATH", registry
            ), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAPITOL_A2A_BEARER", None)
                bearer, source = resolve_bearer(
                    {}, "org-local", "agent-local",
                    "http://localhost:8300",
                )
                self.assertEqual(bearer, "cap_a2a_LOCAL")
                self.assertEqual(source, "registry:local-ebay")

    def test_resolve_bearer_missing_names_sources_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "agents.yaml"
            with patch(
                "conch.capitol.credentials.REGISTRY_PATH", registry
            ), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAPITOL_A2A_BEARER", None)
                with self.assertRaises(CapitolAuthError) as raised:
                    resolve_bearer({}, "org-x", "agent-x")
                text = str(raised.exception)
                self.assertIn("CAPITOL_A2A_BEARER", text)
                self.assertNotIn("cap_a2a_", text)

    def test_local_endpoint_classification(self):
        self.assertTrue(is_local_endpoint("http://localhost:8300"))
        self.assertTrue(is_local_endpoint("http://127.0.0.1:8300"))
        self.assertTrue(is_local_endpoint("http://[::1]:8300"))
        self.assertTrue(is_local_endpoint("http://192.168.1.20"))
        self.assertTrue(is_local_endpoint("http://10.1.2.3:9000"))
        self.assertTrue(is_local_endpoint("http://capitol.local:8300"))
        self.assertFalse(is_local_endpoint("https://api.capitol.ai"))
        self.assertFalse(is_local_endpoint("http://8.8.8.8"))
        self.assertFalse(is_local_endpoint(""))

    def test_local_only_refuses_remote_capitol(self):
        config = {"local_only": "true", "provider": "ollama"}
        with self.assertRaises(CapitolError):
            ensure_endpoint_allowed(config, "https://api.capitol.ai")
        ensure_endpoint_allowed(config, "http://localhost:8300")  # fine
        ensure_endpoint_allowed(
            {"local_only": "false"}, "https://api.capitol.ai"
        )  # fine when not local-only


class RuntimeConstructionTests(unittest.TestCase):
    def test_missing_config_is_clear(self):
        with self.assertRaises(CapitolError):
            CapitolRuntime.from_config({})

    def test_repr_never_contains_bearer(self):
        runtime = CapitolRuntime("http://localhost:1", "o", "a", BEARER)
        self.assertNotIn(BEARER, repr(runtime))


if __name__ == "__main__":
    unittest.main()
