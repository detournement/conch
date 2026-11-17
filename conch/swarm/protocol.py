"""Versioned swarm wire protocol: envelopes, events, receipts, leases.

Swarm Phase 0 foundations. Every controller/worker message is one of the
frozen dataclasses below, serialized as canonical JSON (sorted keys, compact
separators, ASCII) so equal values always produce identical bytes and
content digests are stable across hosts.

Fail-closed by construction:

- Unknown fields are rejected — a newer peer cannot smuggle semantics past
  an older one by adding keys.
- Missing fields are rejected — every shape is carried in full.
- ``schema_version``/``protocol_version`` must match exactly; anything else
  (older, newer, wrong type) is rejected, never "best-effort" parsed.
- Field types, enum memberships, ID shapes, and cross-field consistency
  (e.g. a ``failed`` event must carry a failure class; a ``success``
  receipt must not) are validated on construction, so an invalid shape can
  never exist in memory, let alone on the wire.

Secret bytes never belong in any of these shapes (non-negotiable invariant);
payloads are plain JSON business data and are size-bounded.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Tuple

PROTOCOL_VERSION = 1

#: Hard bound on any single serialized protocol object.
MAX_WIRE_BYTES = 1024 * 1024


class ProtocolError(ValueError):
    """A protocol shape failed validation. Always fail closed on this."""


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

class FailureClass:
    """Why an attempt failed — drives retry policy, never free-text."""

    TRANSIENT = "transient"                # retry-safe infrastructure blip
    RESOURCE = "resource"                  # out of budget/quota/capacity
    POLICY = "policy"                      # denied by deterministic policy
    AUTH = "auth"                          # credential/permission failure
    USER_INPUT = "user_input"              # needs a human decision or input
    BUG = "bug"                            # deterministic defect; do not retry
    UNKNOWN_EXTERNAL_OUTCOME = "unknown_external_outcome"  # query, never blind-retry

    ALL = frozenset({
        TRANSIENT, RESOURCE, POLICY, AUTH, USER_INPUT, BUG,
        UNKNOWN_EXTERNAL_OUTCOME,
    })


class ActionClass:
    """What kind of authority an action exercises (policy taxonomy)."""

    READ = "read"                          # observe data already in scope
    COMPUTE = "compute"                    # pure computation, no side effects
    WRITE_LOCAL = "write_local"            # mutate task-local workspace only
    COMMUNICATE = "communicate"            # send messages to people/systems
    PUBLISH = "publish"                    # externally visible publication
    PURCHASE = "purchase"                  # money movement or commitments
    ACCOUNT_CHANGE = "account_change"      # identities, credentials, settings
    DELETE = "delete"                      # destroy data or resources
    PROVISION = "provision"                # create/activate managed assets

    ALL = frozenset({
        READ, COMPUTE, WRITE_LOCAL, COMMUNICATE, PUBLISH, PURCHASE,
        ACCOUNT_CHANGE, DELETE, PROVISION,
    })


class DataClassification:
    """Sensitivity ceiling for data a task may receive or emit."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    #: Ascending sensitivity; used to compare ceilings.
    LEVELS: Tuple[str, ...] = (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED)
    ALL = frozenset(LEVELS)


def classification_rank(value: str) -> int:
    """Ascending sensitivity rank; raises ProtocolError on unknown values."""
    try:
        return DataClassification.LEVELS.index(value)
    except ValueError:
        raise ProtocolError(f"unknown data classification: {value!r}")


EVENT_KINDS = frozenset({
    "started", "progress", "log", "heartbeat", "result", "failed",
    "cancelled", "delegation_requested",
})

RECEIPT_OUTCOMES = frozenset({"success", "failure", "unknown"})

#: Worker RPC operations (Swarm Phase 2). The transport is fixed:
#: ``conch-hostctl rpc <worker>`` relaying one bounded JSON line each way
#: over SSH stdio. Anything not in this set fails closed on both ends.
RPC_OPS = frozenset({
    "worker.status",
    "task.offer",
    "task.start",
    "task.cancel",
    "task.status",
    "task.events",
    "task.events_ack",
    "task.resume",
    "artifact.put",
    "artifact.get",
})


# ---------------------------------------------------------------------------
# Canonical IDs
# ---------------------------------------------------------------------------

ID_KINDS = frozenset({"msn", "task", "evt", "rcpt", "lease", "wrk", "rpc"})

_ID_RE = re.compile(r"^([a-z]{2,8})-([0-9a-f]{13})-([0-9a-f]{16})$")


def new_id(kind: str) -> str:
    """Canonical ID: ``{kind}-{unix_ms:013x}-{random 8 bytes hex}``.

    Time-prefixed so IDs of one kind sort roughly by creation, with 64 bits
    of randomness so collisions are not a practical concern.
    """
    if kind not in ID_KINDS:
        raise ProtocolError(f"unknown ID kind: {kind!r}")
    return f"{kind}-{int(time.time() * 1000):013x}-{secrets.token_hex(8)}"


def parse_id(value: str) -> str:
    """Return the kind of a canonical ID, failing closed on malformed input."""
    match = _ID_RE.match(value or "")
    if not match or match.group(1) not in ID_KINDS:
        raise ProtocolError(f"malformed canonical ID: {value!r}")
    return match.group(1)


def _check_id(name: str, value: str, kind: str, *, allow_empty: bool = False):
    if value == "" and allow_empty:
        return
    try:
        actual = parse_id(value)
    except ProtocolError:
        raise ProtocolError(f"{name} is not a canonical ID: {value!r}")
    if actual != kind:
        raise ProtocolError(
            f"{name} must be a {kind!r} ID, got {actual!r}: {value!r}"
        )


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------

def canonical_json(data: Dict[str, Any]) -> str:
    """Canonical serialization: sorted keys, compact, ASCII, size-bounded."""
    try:
        text = json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"value is not canonically serializable: {exc}")
    if len(text.encode("ascii")) > MAX_WIRE_BYTES:
        raise ProtocolError(
            f"serialized object exceeds {MAX_WIRE_BYTES} bytes"
        )
    return text


def _check_json_safe(name: str, value: Dict[str, Any]):
    try:
        json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{name} is not JSON-safe: {exc}")


# ---------------------------------------------------------------------------
# Field validation core
# ---------------------------------------------------------------------------

def _is_bool(value) -> bool:
    return isinstance(value, bool)


def _coerce(cls_name: str, name: str, kind: str, value):
    if kind == "str":
        if not isinstance(value, str):
            raise ProtocolError(f"{cls_name}.{name} must be a string")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ProtocolError(f"{cls_name}.{name} must be a boolean")
        return value
    if kind == "int":
        if _is_bool(value) or not isinstance(value, int):
            raise ProtocolError(f"{cls_name}.{name} must be an integer")
        return value
    if kind == "num":
        if _is_bool(value) or not isinstance(value, (int, float)):
            raise ProtocolError(f"{cls_name}.{name} must be a number")
        return float(value)
    if kind == "str_tuple":
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) for item in value
        ):
            raise ProtocolError(
                f"{cls_name}.{name} must be a list of strings"
            )
        return tuple(value)
    if kind == "dict":
        if not isinstance(value, dict) or any(
            not isinstance(key, str) for key in value
        ):
            raise ProtocolError(
                f"{cls_name}.{name} must be an object with string keys"
            )
        _check_json_safe(f"{cls_name}.{name}", value)
        return value
    raise ProtocolError(f"{cls_name}.{name}: unknown field kind {kind!r}")


class _WireShape:
    """Shared (de)serialization for the frozen protocol dataclasses.

    Subclasses define ``SCHEMA_VERSION``, ``_KINDS`` (field name → wire
    kind), and ``_validate()`` for enum/cross-field/ID rules. Validation
    runs in ``__post_init__``, so invalid shapes cannot be constructed at
    all — from the wire or locally.
    """

    SCHEMA_VERSION = 1
    _KINDS: Dict[str, str] = {}

    def __post_init__(self):
        cls = type(self)
        for spec in fields(self):
            kind = cls._KINDS[spec.name]
            coerced = _coerce(
                cls.__name__, spec.name, kind, getattr(self, spec.name)
            )
            object.__setattr__(self, spec.name, coerced)
        if self.schema_version != cls.SCHEMA_VERSION:
            raise ProtocolError(
                f"unsupported {cls.__name__} schema_version "
                f"{self.schema_version!r} (supported: {cls.SCHEMA_VERSION})"
            )
        if self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"unsupported protocol_version {self.protocol_version!r} "
                f"(supported: {PROTOCOL_VERSION})"
            )
        self._validate()
        # Canonical serializability (and the size bound) is part of validity.
        self.to_json()

    def _validate(self):
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data) -> "Any":
        if not isinstance(data, dict):
            raise ProtocolError(f"{cls.__name__} payload must be an object")
        known = set(cls._KINDS)
        unknown = set(data) - known
        if unknown:
            raise ProtocolError(
                f"{cls.__name__}: unknown field(s) "
                f"{sorted(unknown)} — failing closed"
            )
        missing = known - set(data)
        if missing:
            raise ProtocolError(
                f"{cls.__name__}: missing field(s) {sorted(missing)}"
            )
        return cls(**data)

    @classmethod
    def from_json(cls, text) -> "Any":
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="strict")
        if not isinstance(text, str):
            raise ProtocolError(f"{cls.__name__} wire input must be text")
        if len(text.encode("utf-8")) > MAX_WIRE_BYTES:
            raise ProtocolError(
                f"wire input exceeds {MAX_WIRE_BYTES} bytes"
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"{cls.__name__}: invalid JSON: {exc}")
        return cls.from_dict(data)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for spec in fields(self):
            value = getattr(self, spec.name)
            data[spec.name] = list(value) if isinstance(value, tuple) else value
        return data

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def digest(self) -> str:
        """SHA-256 of the canonical serialization (stable content address)."""
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskEnvelope(_WireShape):
    """The exact, bounded authority a worker receives for one task.

    Workers are replaceable compute: everything they may do is named here —
    role/skill/tool manifests, model, data ceiling, action classes, and
    budgets. Credentials are never part of an envelope.
    """

    task_id: str
    mission_id: str
    principal: str
    task: str
    idempotency_key: str
    issued_at: float
    parent_task_id: str = ""
    delegator: str = ""
    role: str = ""
    context: str = ""
    model: str = ""
    skills: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    action_classes: Tuple[str, ...] = (ActionClass.READ,)
    data_classification: str = DataClassification.INTERNAL
    max_tool_rounds: int = 10
    token_budget: int = 0
    wall_clock_seconds: int = 600
    schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION

    SCHEMA_VERSION = 1
    _KINDS = {
        "task_id": "str", "mission_id": "str", "principal": "str",
        "task": "str", "idempotency_key": "str", "issued_at": "num",
        "parent_task_id": "str", "delegator": "str", "role": "str",
        "context": "str", "model": "str", "skills": "str_tuple",
        "tools": "str_tuple", "action_classes": "str_tuple",
        "data_classification": "str", "max_tool_rounds": "int",
        "token_budget": "int", "wall_clock_seconds": "int",
        "schema_version": "int", "protocol_version": "int",
    }

    def _validate(self):
        _check_id("task_id", self.task_id, "task")
        _check_id("mission_id", self.mission_id, "msn")
        _check_id("parent_task_id", self.parent_task_id, "task",
                  allow_empty=True)
        if not self.principal.strip():
            raise ProtocolError("TaskEnvelope.principal is required")
        if not self.task.strip():
            raise ProtocolError("TaskEnvelope.task is required")
        if not self.idempotency_key.strip():
            raise ProtocolError("TaskEnvelope.idempotency_key is required")
        if self.issued_at <= 0:
            raise ProtocolError("TaskEnvelope.issued_at must be positive")
        bad_actions = set(self.action_classes) - ActionClass.ALL
        if bad_actions:
            raise ProtocolError(
                f"TaskEnvelope.action_classes contains unknown class(es) "
                f"{sorted(bad_actions)}"
            )
        if self.data_classification not in DataClassification.ALL:
            raise ProtocolError(
                "TaskEnvelope.data_classification must be one of "
                f"{sorted(DataClassification.ALL)}, got "
                f"{self.data_classification!r}"
            )
        if self.max_tool_rounds < 1:
            raise ProtocolError(
                "TaskEnvelope.max_tool_rounds must be at least 1"
            )
        if self.token_budget < 0:
            raise ProtocolError(
                "TaskEnvelope.token_budget must not be negative"
            )
        if self.wall_clock_seconds < 1:
            raise ProtocolError(
                "TaskEnvelope.wall_clock_seconds must be at least 1"
            )


@dataclass(frozen=True)
class TaskEvent(_WireShape):
    """One worker→controller fact about a task attempt.

    Events are immutable and sequence-numbered per attempt; the controller
    (never the worker) owns mission truth derived from them.
    """

    event_id: str
    task_id: str
    attempt: int
    sequence: int
    kind: str
    created_at: float
    failure_class: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION

    SCHEMA_VERSION = 1
    _KINDS = {
        "event_id": "str", "task_id": "str", "attempt": "int",
        "sequence": "int", "kind": "str", "created_at": "num",
        "failure_class": "str", "payload": "dict",
        "schema_version": "int", "protocol_version": "int",
    }

    def _validate(self):
        _check_id("event_id", self.event_id, "evt")
        _check_id("task_id", self.task_id, "task")
        if self.attempt < 1:
            raise ProtocolError("TaskEvent.attempt must be at least 1")
        if self.sequence < 0:
            raise ProtocolError("TaskEvent.sequence must not be negative")
        if self.kind not in EVENT_KINDS:
            raise ProtocolError(
                f"TaskEvent.kind must be one of {sorted(EVENT_KINDS)}, "
                f"got {self.kind!r}"
            )
        if self.created_at <= 0:
            raise ProtocolError("TaskEvent.created_at must be positive")
        if self.kind == "failed":
            if self.failure_class not in FailureClass.ALL:
                raise ProtocolError(
                    "TaskEvent: a 'failed' event requires a failure_class "
                    f"from {sorted(FailureClass.ALL)}"
                )
        elif self.failure_class != "":
            raise ProtocolError(
                f"TaskEvent: failure_class is only valid on 'failed' "
                f"events, not {self.kind!r}"
            )


@dataclass(frozen=True)
class TaskReceipt(_WireShape):
    """Deterministic record of one action's outcome.

    ``unknown`` outcomes exist so uncertain external submissions are
    reconciled by querying, never blind-retried (non-negotiable invariant);
    they must carry ``UNKNOWN_EXTERNAL_OUTCOME``.
    """

    receipt_id: str
    task_id: str
    attempt: int
    action_class: str
    outcome: str
    created_at: float
    failure_class: str = ""
    artifact_digest: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION

    SCHEMA_VERSION = 1
    _KINDS = {
        "receipt_id": "str", "task_id": "str", "attempt": "int",
        "action_class": "str", "outcome": "str", "created_at": "num",
        "failure_class": "str", "artifact_digest": "str",
        "details": "dict",
        "schema_version": "int", "protocol_version": "int",
    }

    def _validate(self):
        _check_id("receipt_id", self.receipt_id, "rcpt")
        _check_id("task_id", self.task_id, "task")
        if self.attempt < 1:
            raise ProtocolError("TaskReceipt.attempt must be at least 1")
        if self.action_class not in ActionClass.ALL:
            raise ProtocolError(
                f"TaskReceipt.action_class must be one of "
                f"{sorted(ActionClass.ALL)}, got {self.action_class!r}"
            )
        if self.outcome not in RECEIPT_OUTCOMES:
            raise ProtocolError(
                f"TaskReceipt.outcome must be one of "
                f"{sorted(RECEIPT_OUTCOMES)}, got {self.outcome!r}"
            )
        if self.created_at <= 0:
            raise ProtocolError("TaskReceipt.created_at must be positive")
        if self.outcome == "success":
            if self.failure_class != "":
                raise ProtocolError(
                    "TaskReceipt: a success receipt must not carry a "
                    "failure_class"
                )
        elif self.outcome == "failure":
            if self.failure_class not in FailureClass.ALL:
                raise ProtocolError(
                    "TaskReceipt: a failure receipt requires a "
                    f"failure_class from {sorted(FailureClass.ALL)}"
                )
        else:  # unknown
            if self.failure_class != FailureClass.UNKNOWN_EXTERNAL_OUTCOME:
                raise ProtocolError(
                    "TaskReceipt: an unknown outcome must carry "
                    "failure_class 'unknown_external_outcome' (query, "
                    "never blind-retry)"
                )
        if self.artifact_digest and not re.fullmatch(
            r"[0-9a-f]{64}", self.artifact_digest
        ):
            raise ProtocolError(
                "TaskReceipt.artifact_digest must be a lowercase sha256 hex "
                "digest"
            )


@dataclass(frozen=True)
class RpcRequest(_WireShape):
    """One controller→worker request over the fixed SSH-stdio transport.

    ``args`` carries op-specific JSON (envelopes travel as validated
    ``TaskEnvelope`` dicts inside it). No secrets, ever — the transport
    equals argv+stdio on a remote host.
    """

    rpc_id: str
    op: str
    args: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION

    SCHEMA_VERSION = 1
    _KINDS = {
        "rpc_id": "str", "op": "str", "args": "dict",
        "schema_version": "int", "protocol_version": "int",
    }

    def _validate(self):
        _check_id("rpc_id", self.rpc_id, "rpc")
        if self.op not in RPC_OPS:
            raise ProtocolError(
                f"RpcRequest.op must be one of {sorted(RPC_OPS)},"
                f" got {self.op!r}"
            )


@dataclass(frozen=True)
class RpcResponse(_WireShape):
    """One worker→controller reply.

    Failures always carry a :class:`FailureClass` so retry policy is
    deterministic; ``retry_after`` lets a loaded worker push back
    (bounded-queue rejection) without the controller guessing.
    """

    rpc_id: str
    ok: bool
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_class: str = ""
    retry_after: float = 0.0
    schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION

    SCHEMA_VERSION = 1
    _KINDS = {
        "rpc_id": "str", "ok": "bool", "result": "dict", "error": "str",
        "error_class": "str", "retry_after": "num",
        "schema_version": "int", "protocol_version": "int",
    }

    def _validate(self):
        _check_id("rpc_id", self.rpc_id, "rpc")
        if self.retry_after < 0:
            raise ProtocolError(
                "RpcResponse.retry_after must not be negative"
            )
        if self.ok:
            if self.error or self.error_class:
                raise ProtocolError(
                    "RpcResponse: a successful response must not carry an"
                    " error or error_class"
                )
        else:
            if not self.error.strip():
                raise ProtocolError(
                    "RpcResponse: a failed response requires an error"
                    " message"
                )
            if self.error_class not in FailureClass.ALL:
                raise ProtocolError(
                    "RpcResponse: a failed response requires a"
                    f" failure class from {sorted(FailureClass.ALL)}"
                )


@dataclass(frozen=True)
class Lease(_WireShape):
    """A time-bounded, fenced grant of one task to one worker.

    ``controller_epoch`` and ``fencing_token`` exist so a stale worker (or a
    superseded controller) can never commit results after losing the lease.
    """

    lease_id: str
    task_id: str
    worker_id: str
    controller_epoch: int
    fencing_token: int
    granted_at: float
    expires_at: float
    schema_version: int = 1
    protocol_version: int = PROTOCOL_VERSION

    SCHEMA_VERSION = 1
    _KINDS = {
        "lease_id": "str", "task_id": "str", "worker_id": "str",
        "controller_epoch": "int", "fencing_token": "int",
        "granted_at": "num", "expires_at": "num",
        "schema_version": "int", "protocol_version": "int",
    }

    def _validate(self):
        _check_id("lease_id", self.lease_id, "lease")
        _check_id("task_id", self.task_id, "task")
        _check_id("worker_id", self.worker_id, "wrk")
        if self.controller_epoch < 0:
            raise ProtocolError("Lease.controller_epoch must not be negative")
        if self.fencing_token < 0:
            raise ProtocolError("Lease.fencing_token must not be negative")
        if self.granted_at <= 0:
            raise ProtocolError("Lease.granted_at must be positive")
        if self.expires_at <= self.granted_at:
            raise ProtocolError(
                "Lease.expires_at must be after granted_at"
            )
