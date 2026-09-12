"""Stdlib A2A client for Capitol agents: the ``CapitolRuntime`` adapter.

Wire contract (mirrors the A2Actrl reference client and the a2a-client
skill, targeting gateway wire schema 1.0.x):

- ``GET  {base}/a2a/{org}/{agent}/.well-known/agent-card.json`` — discovery
- ``POST {base}/a2a/{org}/{agent}`` — JSON-RPC 2.0, methods ``SendMessage``
  and ``SendStreamingMessage`` (SSE); every skill is multiplexed through
  ``params.message.parts[0].data.skill_id``
- ``GET  {base}/a2a/{org}/{agent}/files/{id}/download-url`` — click-time
  presigned download resolution (the oversight-app pattern; used for
  future label PDFs)

Fail-closed posture:

- An unsupported AgentCard ``wireSchemaVersion`` refuses to proceed.
- JSON-RPC error envelopes become typed :class:`CapitolError`s carrying
  ``retryable``/``category``/``actionable_hint``; unknown retryability is
  treated as not retryable.
- HTTP 401 / ``PermissionDenied`` raise :class:`CapitolAuthError`; there
  is no automatic re-auth (park as "credential needed").
- Bearer bytes are scrubbed from every exception message; nothing here
  logs, stores, or prints the token.

Streaming: ``watch_run`` consumes ``subscribe_workflow_events`` over a
hand-rolled SSE line parser and resumes with ``since_sequence=last+1``
after transport drops, reconciling stream-local closes against
``get_workflow_status`` so a dropped stream is never read as a run
verdict.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import urllib.error
import urllib.request

from .credentials import redact_text
from .errors import CapitolAuthError, CapitolError, CapitolProtocolError, parse_error_info

A2A_VERSION = "1.0"
SUPPORTED_WIRE_PREFIX = "1.0"
DEFAULT_TIMEOUT = 120.0
STREAM_READ_TIMEOUT = 630.0
UPLOAD_MAX_BYTES = 500 * 1024 * 1024
INLINE_MAX_BYTES = 50 * 1024 * 1024

#: ``get_workflow_status`` terminal values (lowercase on the wire).
TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "stopped", "cancelled"})

#: A2A ``TaskState`` terminal set — v1.0 SCREAMING_SNAKE plus legacy v0.3.
TERMINAL_TASK_STATES = frozenset({
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
    "completed", "failed", "canceled", "rejected",
})

TERMINAL_EVENT_TYPES = frozenset({
    "workflow.run_completed", "workflow.run_failed",
})

KEEPALIVE_EVENT = "a2a.stream_keepalive"
FINAL_STATUS_EVENT = "_final_status"


def build_file_part(
    path: Optional[str] = None,
    *,
    data: Optional[bytes] = None,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Canonical A2A FilePart (FileWithBytes) for inline attachments.

    Image FileParts on a chat message are the one A2A path the gateway
    promotes into durable org Artifact rows at ingestion (the form the
    eBay image node resolves); other uploads stay in the 24 h registry.
    """
    import base64

    if data is None:
        if not path:
            raise CapitolError("build_file_part needs a path or data")
        data = Path(path).read_bytes()
    name = filename or (Path(path).name if path else "attachment.bin")
    if len(data) > INLINE_MAX_BYTES:
        raise CapitolError(
            f"{name} is {len(data):,} bytes — over the 50 MB inline "
            "FilePart cap"
        )
    mt = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {
        "kind": "file",
        "file": {
            "bytes": base64.b64encode(data).decode("ascii"),
            "name": name,
            "mimeType": mt,
        },
    }


def extract_payload(task_result: Dict[str, Any]) -> Any:
    """First ``data`` part of a Task response (status message, then
    artifacts), matching the reference client's extraction order."""
    if not isinstance(task_result, dict):
        return task_result
    message = (task_result.get("status") or {}).get("message") or {}
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("data") is not None:
            return part["data"]
    for artifact in task_result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if isinstance(part, dict) and part.get("data") is not None:
                return part["data"]
    return task_result


def _task_failure_text(task_result: Dict[str, Any]) -> str:
    """Collect status-message text parts from a failed Task for the error."""
    message = (task_result.get("status") or {}).get("message") or {}
    texts = [
        str(part.get("text"))
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("text")
    ]
    return "; ".join(texts)


def is_stream_local_close(status: Dict[str, Any]) -> bool:
    """True when a closing stream frame reports a STREAM-side close (idle
    timeout, event-bus hiccup) rather than a run verdict."""
    if not isinstance(status, dict):
        return False
    message = status.get("message") or {}
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        if isinstance(data, dict) and (
            data.get("stream_local_close") is True
            or data.get("streamLocalClose") is True
            or data.get("stream_close_reason")
            or data.get("streamCloseReason")
        ):
            return True
        text = part.get("text")
        if isinstance(text, str) and text.startswith((
            "Stream idle timeout reached",
            "Event stream closed after inactivity timeout",
            "Redis pubsub unavailable:",
            "Redis subscribe failed:",
        )):
            return True
    return False


def sse_events(lines) -> Iterator[str]:
    """Yield SSE event payloads from an iterable of text lines.

    Implements the SSE framing rules that matter for this gateway: an
    event is terminated by a blank line; multiple ``data:`` lines are
    joined with newlines; comment (``:``) and non-data fields are ignored.
    """
    buffer: List[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n") if isinstance(raw, str) else raw
        if line == "":
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[len("data:"):].lstrip(" "))
    if buffer:
        yield "\n".join(buffer)


class CapitolRuntime:
    """One (base_url, org, agent, bearer) binding to a Capitol A2A agent."""

    def __init__(
        self,
        base_url: str,
        org_id: str,
        agent_id: str,
        bearer: str,
        *,
        caller_system: str = "conch",
        caller_version: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.org_id = str(org_id or "")
        self.agent_id = str(agent_id or "")
        self._bearer = str(bearer or "")
        self.caller_system = caller_system
        if not caller_version:
            try:
                from .. import __version__ as caller_version
            except Exception:
                caller_version = "0"
        self.caller_version = str(caller_version)
        self.timeout = float(timeout)
        self.context_id: Optional[str] = None
        self._card: Optional[Dict[str, Any]] = None
        if not (self.base_url and self.org_id and self.agent_id):
            raise CapitolError(
                "CapitolRuntime needs base_url, org_id, and agent_id "
                "(config keys capitol_base_url / capitol_org / capitol_agent)"
            )
        if not self._bearer:
            raise CapitolAuthError("CapitolRuntime needs a non-empty bearer")

    def __repr__(self) -> str:  # never expose the bearer
        return (
            f"CapitolRuntime(base_url={self.base_url!r}, "
            f"org_id={self.org_id!r}, agent_id={self.agent_id!r})"
        )

    @classmethod
    def from_config(cls, config: dict, **kwargs) -> "CapitolRuntime":
        """Build a runtime from conch config keys, resolving the bearer
        from the named env var or the A2Actrl registry (never stored)."""
        from .credentials import ensure_endpoint_allowed, resolve_bearer

        base_url = str((config or {}).get("capitol_base_url") or "").strip()
        org_id = str((config or {}).get("capitol_org") or "").strip()
        agent_id = str((config or {}).get("capitol_agent") or "").strip()
        if not (base_url and org_id and agent_id):
            raise CapitolError(
                "Capitol is not configured: set capitol_base_url, "
                "capitol_org, and capitol_agent in the conch config"
            )
        ensure_endpoint_allowed(config, base_url)
        bearer, _source = resolve_bearer(config, org_id, agent_id, base_url)
        return cls(base_url, org_id, agent_id, bearer, **kwargs)

    # -- endpoints ----------------------------------------------------------

    @property
    def rpc_url(self) -> str:
        return f"{self.base_url}/a2a/{self.org_id}/{self.agent_id}"

    @property
    def card_url(self) -> str:
        return f"{self.rpc_url}/.well-known/agent-card.json"

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._bearer}",
            "A2A-Version": A2A_VERSION,
        }
        if extra:
            headers.update(extra)
        return headers

    # -- transport ----------------------------------------------------------

    def _redact(self, text: str) -> str:
        return redact_text(str(text), (self._bearer,))

    def _fail(self, message: str, cls=CapitolError, **kwargs) -> Exception:
        return cls(self._redact(message), **kwargs)

    def _request(
        self,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        method: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers if headers is not None else self._headers(),
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            detail = body.decode("utf-8", "replace")[:2000]
            if exc.code in (401, 403):
                raise self._fail(
                    f"Capitol auth failed (HTTP {exc.code}) at {url}: "
                    f"{detail} — credential needed; no automatic re-auth",
                    CapitolAuthError,
                    http_status=exc.code,
                ) from None
            raise self._fail(
                f"Capitol HTTP {exc.code} at {url}: {detail}",
                http_status=exc.code,
            ) from None
        except OSError as exc:
            raise self._fail(
                f"Capitol endpoint unreachable at {url}: {exc}",
                retryable=True,
                category="transport",
            ) from None

    def _envelope(
        self,
        method: str,
        skill_id: str,
        data: Optional[Dict[str, Any]],
        context_id: Optional[str],
        extra_parts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "messageId": f"msg-{uuid.uuid4().hex[:12]}",
            "role": "user",
            "parts": [
                {"data": dict({"skill_id": skill_id}, **(data or {}))},
                *(extra_parts or []),
            ],
        }
        if context_id:
            message["contextId"] = context_id
        return {
            "jsonrpc": "2.0",
            "id": f"req-{uuid.uuid4().hex[:12]}",
            "method": method,
            "params": {"message": message},
        }

    def _raise_rpc_error(self, error: Dict[str, Any]):
        info = parse_error_info(error)
        message = (
            f"A2A error {info['code']} {info['message']}"
            f"{' (' + info['reason'] + ')' if info['reason'] else ''}"
            f"{' — ' + info['hint'] if info['hint'] else ''}"
        )
        cls = CapitolError
        if info["code"] == -32004 or info["reason"] in (
            "PERMISSION_DENIED", "INVALID_TOKEN",
        ):
            cls = CapitolAuthError
        raise self._fail(
            message,
            cls,
            code=info["code"],
            reason=info["reason"],
            retryable=info["retryable"],
            category=info["category"],
            hint=info["hint"],
        )

    def call_skill(
        self,
        skill_id: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        context_id: Optional[str] = None,
        extra_parts: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None,
        check_task: bool = True,
    ) -> Dict[str, Any]:
        """POST one ``SendMessage`` envelope; return the JSON-RPC result.

        Raises a typed error on JSON-RPC ``error`` envelopes and (when
        ``check_task``) on terminal-failed Task results, so callers never
        mistake a refused call for progress.
        """
        envelope = self._envelope(
            "SendMessage", skill_id, data,
            context_id if context_id is not None else self.context_id,
            extra_parts,
        )
        raw = self._request(
            self.rpc_url,
            data=json.dumps(envelope).encode("utf-8"),
            headers=self._headers({"Content-Type": "application/json"}),
            timeout=timeout,
        )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._fail(
                f"Capitol returned a non-JSON body for {skill_id}: {exc}",
                CapitolProtocolError,
            ) from None
        if not isinstance(body, dict):
            raise self._fail(
                f"Capitol returned a non-object JSON-RPC body for {skill_id}",
                CapitolProtocolError,
            )
        if body.get("error"):
            self._raise_rpc_error(body["error"])
        result = body.get("result") or {}
        if check_task and isinstance(result, dict):
            state = str((result.get("status") or {}).get("state") or "")
            if state in ("TASK_STATE_FAILED", "TASK_STATE_REJECTED",
                         "failed", "rejected"):
                raise self._fail(
                    f"A2A task failed for {skill_id}: "
                    f"{_task_failure_text(result) or state}"
                )
        return result

    def call(self, skill_id: str, data: Optional[Dict[str, Any]] = None,
             **kwargs) -> Any:
        """``call_skill`` + payload extraction."""
        return extract_payload(self.call_skill(skill_id, data, **kwargs))

    # -- discovery / session --------------------------------------------------

    def discover(self, refresh: bool = False) -> Dict[str, Any]:
        """Fetch (and cache) the AgentCard; refuse unsupported wire majors."""
        if self._card is not None and not refresh:
            return self._card
        raw = self._request(self.card_url)
        try:
            card = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._fail(
                f"agent card at {self.card_url} is not JSON: {exc}",
                CapitolProtocolError,
            ) from None
        wire = str(
            card.get("wireSchemaVersion")
            or (card.get("capabilities") or {}).get("wireSchemaVersion")
            or card.get("version")
            or ""
        )
        if wire and not (
            wire == SUPPORTED_WIRE_PREFIX
            or wire.startswith(SUPPORTED_WIRE_PREFIX + ".")
        ):
            raise self._fail(
                f"unsupported A2A wire schema {wire!r} on agent card "
                f"(supported: {SUPPORTED_WIRE_PREFIX}.x) — failing closed",
                CapitolProtocolError,
            )
        self._card = card
        return card

    def handshake(self) -> str:
        """Open a session; thread the returned ``context_id`` everywhere."""
        payload = self.call(
            "handshake",
            {"caller": {
                "system": self.caller_system,
                "version": self.caller_version,
            }},
            context_id="",
        )
        context_id = str(
            ((payload or {}).get("session") or {}).get("context_id") or ""
        )
        if not context_id:
            raise self._fail(
                "handshake returned no session.context_id",
                CapitolProtocolError,
            )
        self.context_id = context_id
        return context_id

    # -- workflows ------------------------------------------------------------

    def list_workflows(self) -> List[Dict[str, Any]]:
        payload = self.call("list_workflows", {})
        workflows = (payload or {}).get("workflows")
        return workflows if isinstance(workflows, list) else []

    def describe_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self.call("get_workflow_details", {"workflow_id": workflow_id})

    def call_workflow(
        self,
        workflow_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        allow_clarifications: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Start a run. Callers supply the idempotency key: retrying with
        the same key + same inputs returns the original ``run_id``."""
        data: Dict[str, Any] = {"workflow_id": workflow_id}
        if inputs is not None:
            data["inputs"] = inputs
        if idempotency_key:
            data["idempotency_key"] = idempotency_key
        if artifacts:
            data["artifacts"] = artifacts
        if allow_clarifications is not None:
            data["allow_clarifications"] = bool(allow_clarifications)
        payload = self.call("call_workflow", data, timeout=timeout)
        if not isinstance(payload, dict) or not payload.get("run_id"):
            raise self._fail(
                f"call_workflow({workflow_id}) returned no run_id: "
                f"{json.dumps(payload)[:500] if payload else payload}"
            )
        return payload

    def chat(
        self,
        message: str,
        *,
        files: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """One conversational turn (the agent's designed intake path).

        ``files`` are attached as inline FileParts; image attachments are
        promoted to durable org artifacts server-side. Returns the chat
        payload (``assistant_reply``, ``run_id`` when the orchestrator
        launched a workflow, ...). The reply text is untrusted data —
        callers must read machine contracts from run outputs, never parse
        them out of prose (the gateway redacts hashes there).
        """
        parts = [build_file_part(path) for path in files or []]
        return self.call(
            "chat",
            {"message": message},
            extra_parts=parts,
            timeout=timeout or max(self.timeout, 600.0),
        )

    def list_runs(
        self,
        workflow_id: str,
        *,
        limit: int = 20,
        status_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {"workflow_id": workflow_id, "limit": limit}
        if status_filter:
            data["status_filter"] = status_filter
        return self.call("list_workflow_runs", data)

    def run_status(self, run_id: str) -> Dict[str, Any]:
        return self.call("get_workflow_status", {"run_id": run_id})

    def run_events(
        self,
        run_id: str,
        *,
        since_sequence: int = 0,
        types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {"run_id": run_id}
        if since_sequence:
            data["since_sequence"] = int(since_sequence)
        if types:
            data["types"] = list(types)
        return self.call("get_workflow_events", data)

    def workflow_output(self, run_id: str) -> Dict[str, Any]:
        return self.call("get_workflow_output", {"run_id": run_id})

    # -- HITL -----------------------------------------------------------------

    def submit_intervention(
        self, run_id: str, node_id: str, request_id: str, response: str
    ) -> Dict[str, Any]:
        """Answer a Human-Intervention ``node.input_required`` checkpoint.

        Continue/stop panels advance only on the literal ``"continue"``;
        map approvals to that token before calling.
        """
        return self.call("submit_intervention_response", {
            "run_id": run_id,
            "node_id": node_id,
            "request_id": request_id,
            "response": response,
        })

    def submit_clarification(
        self,
        run_id: str,
        request_id: str,
        response: str,
        *,
        declined: bool = False,
    ) -> Dict[str, Any]:
        """Answer an agent clarification (``data.input_kind ==
        "clarification"`` on ``node.input_required``)."""
        return self.call("submit_clarification_response", {
            "run_id": run_id,
            "request_id": request_id,
            "response": response,
            "declined": bool(declined),
        })

    # -- lifecycle ------------------------------------------------------------

    def pause_run(self, run_id: str, reason: str = "") -> Dict[str, Any]:
        data: Dict[str, Any] = {"run_id": run_id}
        if reason:
            data["reason"] = reason
        return self.call("pause_workflow", data)

    def stop_run(self, run_id: str, reason: str = "") -> Dict[str, Any]:
        data: Dict[str, Any] = {"run_id": run_id}
        if reason:
            data["reason"] = reason
        return self.call("stop_workflow", data)

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """Pure A2A ``CancelTask`` (spec method, not a skill)."""
        envelope = {
            "jsonrpc": "2.0",
            "id": f"req-{uuid.uuid4().hex[:12]}",
            "method": "CancelTask",
            "params": {"id": task_id},
        }
        raw = self._request(
            self.rpc_url,
            data=json.dumps(envelope).encode("utf-8"),
            headers=self._headers({"Content-Type": "application/json"}),
        )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._fail(
                f"CancelTask returned a non-JSON body: {exc}",
                CapitolProtocolError,
            ) from None
        if isinstance(body, dict) and body.get("error"):
            self._raise_rpc_error(body["error"])
        return body.get("result") or {}

    # -- streaming ------------------------------------------------------------

    def _stream_frames(
        self, skill_id: str, data: Dict[str, Any]
    ) -> Iterator[Dict[str, Any]]:
        """One SSE connection; yields parsed JSON-RPC frames until EOF."""
        envelope = self._envelope(
            "SendStreamingMessage", skill_id, data, self.context_id
        )
        request = urllib.request.Request(
            self.rpc_url,
            data=json.dumps(envelope).encode("utf-8"),
            headers=self._headers({
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(
                request, timeout=STREAM_READ_TIMEOUT
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise self._fail(
                    f"Capitol auth failed (HTTP {exc.code}) on stream",
                    CapitolAuthError, http_status=exc.code,
                ) from None
            raise self._fail(
                f"Capitol stream HTTP {exc.code}", http_status=exc.code
            ) from None
        except OSError as exc:
            raise self._fail(
                f"Capitol stream connect failed: {exc}",
                retryable=True, category="transport",
            ) from None
        try:
            lines = (
                raw.decode("utf-8", "replace")
                for raw in iter(response.readline, b"")
            )
            for payload in sse_events(lines):
                try:
                    frame = json.loads(payload)
                except ValueError:
                    continue
                if frame.get("error"):
                    self._raise_rpc_error(frame["error"])
                yield frame
        finally:
            try:
                response.close()
            except Exception:
                pass

    def watch_run(
        self,
        run_id: str,
        *,
        since_sequence: int = 0,
        types: Optional[List[str]] = None,
        max_reconnects: int = 5,
        reconnect_delay: float = 1.0,
        _sleep=time.sleep,
    ) -> Iterator[Dict[str, Any]]:
        """Stream a run's WorkflowEvents, resuming across disconnects.

        Yields WorkflowEvent dicts (keepalives skipped) and finally one
        ``{"event_type": "_final_status", ...}`` frame. On any transport
        drop it reconnects with ``since_sequence=last_seen+1`` — the
        gateway backfills from its persisted store, so the caller sees a
        contiguous, dedupe-free sequence. Stream-local closes are
        reconciled against ``get_workflow_status`` and never surfaced as
        a run verdict. The reconnect budget applies to *consecutive*
        failures; any delivered event resets it.
        """
        last_seen = max(0, int(since_sequence) - 1)
        failures = 0
        while True:
            data: Dict[str, Any] = {
                "run_id": run_id,
                "since_sequence": last_seen + 1,
            }
            if types:
                data["types"] = list(types)
            progressed = False
            stream_local = False
            terminal_event = False
            try:
                for frame in self._stream_frames(
                    "subscribe_workflow_events", data
                ):
                    result = frame.get("result") or {}
                    artifact = result.get("artifact")
                    if artifact:
                        for part in artifact.get("parts") or []:
                            event = (
                                part.get("data")
                                if isinstance(part, dict) else None
                            )
                            if not isinstance(event, dict):
                                continue
                            if event.get("event_type") == KEEPALIVE_EVENT:
                                continue
                            sequence = event.get("sequence")
                            if isinstance(sequence, (int, float)):
                                last_seen = max(last_seen, int(sequence))
                                progressed = True
                                failures = 0
                            if event.get("event_type") in TERMINAL_EVENT_TYPES:
                                terminal_event = True
                            yield event
                        continue
                    status = result.get("status")
                    if not isinstance(status, dict):
                        continue
                    if is_stream_local_close(status):
                        stream_local = True
                        break
                    state = str(status.get("state") or "")
                    if state in TERMINAL_TASK_STATES or status.get("final") is True:
                        yield {
                            "event_type": FINAL_STATUS_EVENT,
                            "scope": "workflow",
                            "data": status,
                        }
                        return
            except (CapitolAuthError, CapitolProtocolError):
                raise
            except CapitolError:
                pass  # transport drop — reconcile and maybe resume below
            # Stream ended without a terminal status frame: reconcile.
            try:
                status_payload = self.run_status(run_id)
            except CapitolError:
                status_payload = {}
            run_state = str(
                (status_payload or {}).get("status") or ""
            ).lower()
            if run_state in TERMINAL_RUN_STATUSES or terminal_event:
                yield {
                    "event_type": FINAL_STATUS_EVENT,
                    "scope": "workflow",
                    "data": {"state": run_state or "unknown",
                             "reconciled": True},
                }
                return
            failures = 1 if progressed else failures + 1
            if failures > max_reconnects:
                raise self._fail(
                    f"event stream for run {run_id} kept dropping "
                    f"({max_reconnects} consecutive reconnects failed; "
                    f"last sequence {last_seen}"
                    f"{'; stream-local close' if stream_local else ''})",
                    retryable=True,
                    category="transport",
                )
            _sleep(reconnect_delay * failures)

    # -- files / artifacts ----------------------------------------------------

    def upload_artifact(
        self,
        path: Optional[str] = None,
        *,
        data: Optional[bytes] = None,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload one private artifact via ``request_upload_url`` + PUT.

        Bytes go directly to the presigned URL (never through prompts or
        state). Returns ``{artifact_id, digest, size_bytes, filename,
        mime_type}`` with a locally computed ``sha256:`` digest so callers
        can bind content-addressed media into workflow requests.
        """
        if data is None:
            if not path:
                raise CapitolError("upload_artifact needs a path or data")
            data = Path(path).read_bytes()
        name = filename or (Path(path).name if path else "artifact.bin")
        if len(data) > UPLOAD_MAX_BYTES:
            raise CapitolError(
                f"{name} is {len(data):,} bytes — over the 500 MB "
                "presigned-upload cap"
            )
        ctype = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        handshake = self.call("request_upload_url", {
            "filename": name,
            "mime_type": ctype,
            "size_bytes": len(data),
        })
        upload_url = (handshake or {}).get("upload_url")
        artifact_id = (handshake or {}).get("artifact_id")
        if not upload_url or not artifact_id:
            raise self._fail(
                "request_upload_url returned no upload_url/artifact_id: "
                f"{json.dumps(handshake)[:500]}"
            )
        put_headers = handshake.get("headers") or {"Content-Type": ctype}
        # The presigned PUT goes to storage, not the gateway: no bearer.
        self._request(
            upload_url,
            data=data,
            headers=dict(put_headers),
            method=str(handshake.get("method") or "PUT"),
            timeout=max(self.timeout, len(data) / 125_000),
        )
        return {
            "artifact_id": str(artifact_id),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "filename": name,
            "mime_type": ctype,
        }

    def upload_file_inline(
        self,
        path: Optional[str] = None,
        *,
        data: Optional[bytes] = None,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Inline-base64 ``upload_file`` fallback (≤ 50 MB decoded)."""
        import base64

        if data is None:
            if not path:
                raise CapitolError("upload_file_inline needs a path or data")
            data = Path(path).read_bytes()
        name = filename or (Path(path).name if path else "artifact.bin")
        if len(data) > INLINE_MAX_BYTES:
            raise CapitolError(
                f"{name} is {len(data):,} bytes — over the 50 MB inline "
                "cap; use upload_artifact (presigned PUT)"
            )
        ctype = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        payload = self.call("upload_file", {
            "filename": name,
            "content_base64": base64.b64encode(data).decode("ascii"),
            "content_type": ctype,
        })
        file_id = (payload or {}).get("file_id")
        if not file_id:
            raise self._fail(
                f"upload_file returned no file_id: {json.dumps(payload)[:500]}"
            )
        return {
            "artifact_id": str(file_id),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "filename": name,
            "mime_type": ctype,
        }

    def download_url(self, file_id: str) -> Dict[str, Any]:
        """Click-time presigned download resolution (REST endpoint)."""
        raw = self._request(f"{self.rpc_url}/files/{file_id}/download-url")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._fail(
                f"download-url returned a non-JSON body: {exc}",
                CapitolProtocolError,
            ) from None
        return payload

    def download_artifact(self, file_id: str, dest_path: str) -> Dict[str, Any]:
        """Resolve + fetch an artifact to *dest_path* (future label PDFs)."""
        payload = self.download_url(file_id)
        url = (payload or {}).get("download_url")
        if not url:
            raise self._fail(
                f"no download_url for file {file_id}: "
                f"{json.dumps(payload)[:500]}"
            )
        # Presigned storage URL: authenticated by the URL itself, no bearer.
        blob = self._request(url, headers={})
        destination = Path(dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob)
        return {
            "path": str(destination),
            "size_bytes": len(blob),
            "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
            "filename": payload.get("filename"),
        }
