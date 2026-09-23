"""MissionStore: single-writer transactional SQLite event/projection store.

Design (Swarm Phase 1):

- One database under the XDG state dir, WAL mode, owned by exactly one
  writer thread per store instance. All mutations are submitted to that
  thread and run inside ``BEGIN IMMEDIATE`` transactions; readers use
  per-thread read-only connections (WAL permits concurrent reads).
- Every mutation appends one or more immutable, per-mission hash-chained
  events and applies them to projections **in the same transaction** —
  along with optimistic version checks, budget operations, and outbox
  inserts. There is no code path that updates a replayed projection without
  an event.
- Replay discipline: rows in replayed tables are inserted/updated ONLY by
  :func:`_apply_event`, so rebuilding projections from the event journal
  reproduces them exactly (proven by tests via :meth:`MissionStore.verify_integrity`).
  Operational coordination state — timer claims, leases, outbox delivery
  status, meta — is deliberately not mission truth and not replayed.
- Unknown event kinds or schema versions fail closed, matching
  ``conch.swarm.protocol`` conventions.
- Secret bytes never belong in kernel rows or events (non-negotiable
  invariant); payloads are bounded canonical JSON.
"""

from __future__ import annotations

import hashlib
import json as _json
import os
import queue
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import time as _time

from ..secretguard import CredentialRejected, credential_findings
from ..swarm.protocol import (
    ActionClass,
    DataClassification,
    ProtocolError,
    TaskEnvelope,
    canonical_json,
)
from .model import (
    ActionStatus,
    ApprovalError,
    ApprovalStatus,
    BindingStatus,
    BudgetExceededError,
    CATCH_UP_HARD_CAP,
    CompilationStatus,
    ConflictError,
    DispatchState,
    EVENT_KINDS,
    EVENT_SCHEMA_VERSION,
    InboxStatus,
    ItemStatus,
    KernelError,
    MISFIRE_GRACE_SECONDS,
    MisfirePolicy,
    MissionState,
    StaleGenerationError,
    TaskState,
    WorkerState,
    check_compilation_transition,
    check_dispatch_transition,
    check_item_transition,
    check_task_transition,
    check_transition,
    check_worker_transition,
    kernel_id,
    normalize_item_priority,
    normalize_item_tags,
    normalize_space,
    normalize_spec,
)

GENESIS_HASH = "0" * 64

#: Inline artifact content cap — larger artifacts live on disk and are
#: recorded by digest only.
ARTIFACT_INLINE_MAX_BYTES = 65536

#: Outbox redelivery backoff (seconds), doubled per attempt up to the cap.
OUTBOX_BASE_BACKOFF = 30.0
OUTBOX_MAX_BACKOFF = 3600.0


def default_state_dir() -> Path:
    return Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    ) / "conch"


def default_kernel_dir() -> Path:
    return default_state_dir() / "kernel"


def default_kernel_db_path() -> Path:
    return default_kernel_dir() / "kernel.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_mission
    ON mission_events(mission_id, seq);
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    spec TEXT NOT NULL,
    version INTEGER NOT NULL,
    root_scope_id TEXT NOT NULL,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    next_wake_at REAL,
    last_session_at REAL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(mission_id, version)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    plan_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL,
    failure_class TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(task_id, attempt)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_mission
    ON checkpoints(mission_id, created_at);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_mission
    ON reviews(mission_id, created_at);
CREATE TABLE IF NOT EXISTS budget_scopes (
    scope_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    parent_scope_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_lines (
    scope_id TEXT NOT NULL,
    line TEXT NOT NULL,
    cap INTEGER NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0,
    committed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(scope_id, line)
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    line TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    PRIMARY KEY(reservation_id, scope_id, line)
);
CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    line TEXT NOT NULL,
    op TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reservation_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    action_args TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    origin_channel TEXT NOT NULL DEFAULT 'local',
    origin_thread TEXT NOT NULL DEFAULT '',
    origin_sender TEXT NOT NULL DEFAULT '',
    nonce TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT NOT NULL DEFAULT '',
    decision_origin TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS timers (
    timer_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    logical_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    due_at REAL NOT NULL,
    interval_seconds INTEGER NOT NULL,
    misfire_policy TEXT NOT NULL,
    catch_up_limit INTEGER NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_by TEXT,
    claim_expires_at REAL,
    UNIQUE(mission_id, logical_key)
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    holder TEXT NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0,
    fencing_token INTEGER NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    UNIQUE(kind, resource)
);
CREATE TABLE IF NOT EXISTS external_actions (
    action_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    action_class TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    received_at REAL NOT NULL,
    processed_at REAL
);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    mission_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    transport TEXT NOT NULL DEFAULT '',
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    content TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_bindings (
    binding_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at REAL NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    cursor INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bindings_kind_status
    ON resource_bindings(kind, status);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL,
    ssh_user TEXT NOT NULL DEFAULT '',
    ssh_port INTEGER,
    state TEXT NOT NULL,
    trust_level INTEGER NOT NULL DEFAULT 0,
    data_ceiling TEXT NOT NULL DEFAULT 'internal',
    labels TEXT NOT NULL DEFAULT '{}',
    capabilities TEXT NOT NULL DEFAULT '{}',
    runtime_profile TEXT NOT NULL DEFAULT '',
    profiles TEXT NOT NULL DEFAULT '[]',
    resource_group TEXT NOT NULL DEFAULT '',
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    artifact_digest TEXT NOT NULL DEFAULT '',
    config_digest TEXT NOT NULL DEFAULT '',
    protocol_min INTEGER NOT NULL DEFAULT 1,
    protocol_max INTEGER NOT NULL DEFAULT 1,
    incarnation INTEGER NOT NULL DEFAULT 0,
    autonomy_capable INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    heartbeat_seq INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at REAL
);
CREATE TABLE IF NOT EXISTS dispatches (
    task_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    parent_task_id TEXT NOT NULL DEFAULT '',
    envelope TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worker_id TEXT NOT NULL DEFAULT '',
    fence INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    failure_class TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatches_state
    ON dispatches(state, not_before);
CREATE TABLE IF NOT EXISTS dispatch_events (
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    failure_class TEXT NOT NULL DEFAULT '',
    received_at REAL NOT NULL,
    PRIMARY KEY(task_id, attempt, seq)
);
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    space TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    due_at REAL,
    priority INTEGER,
    tags TEXT NOT NULL DEFAULT '{"list":[]}',
    source TEXT NOT NULL DEFAULT 'chat',
    mission_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_space_status
    ON items(space, status);
CREATE INDEX IF NOT EXISTS idx_items_mission ON items(mission_id);
CREATE TABLE IF NOT EXISTS compilations (
    compilation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    goal TEXT NOT NULL,
    card_version INTEGER NOT NULL,
    approved_version INTEGER NOT NULL DEFAULT 0,
    approved_digest TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decision_origin TEXT NOT NULL DEFAULT '',
    decided_at REAL,
    decision_reason TEXT NOT NULL DEFAULT '',
    materialization TEXT NOT NULL DEFAULT '{}',
    drill TEXT NOT NULL DEFAULT '{}',
    mission_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS compilation_cards (
    compilation_id TEXT NOT NULL,
    card_version INTEGER NOT NULL,
    card TEXT NOT NULL,
    digest TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    guidance TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY(compilation_id, card_version)
);
"""

#: Tables rebuilt from the event journal, with the columns that must match
#: exactly between a live database and a replay. Operational coordination
#: columns (timer claims, outbox delivery state) are excluded by design.
REPLAYED_TABLES: Dict[str, Tuple[str, ...]] = {
    "missions": (
        "mission_id", "kind", "status", "spec", "version", "root_scope_id",
        "stop_requested", "runs", "next_wake_at", "last_session_at",
        "last_error", "created_at", "updated_at",
    ),
    "plans": ("plan_id", "mission_id", "version", "content", "created_at"),
    "tasks": (
        "task_id", "mission_id", "plan_id", "title", "detail", "state",
        "version", "created_at", "updated_at",
    ),
    "task_attempts": (
        "attempt_id", "task_id", "attempt", "state", "failure_class",
        "detail", "started_at", "finished_at",
    ),
    "checkpoints": (
        "checkpoint_id", "mission_id", "session_id", "summary", "state",
        "created_at",
    ),
    "reviews": (
        "review_id", "mission_id", "session_id", "action", "content",
        "created_at",
    ),
    "budget_scopes": (
        "scope_id", "mission_id", "parent_scope_id", "status", "created_at",
    ),
    "budget_lines": ("scope_id", "line", "cap", "reserved", "committed"),
    "budget_reservations": (
        "reservation_id", "scope_id", "line", "amount", "status",
        "created_at",
    ),
    "budget_ledger": (
        "entry_id", "scope_id", "line", "op", "amount", "reservation_id",
        "note", "created_at",
    ),
    "approvals": (
        "approval_id", "mission_id", "action_kind", "action_args",
        "args_hash", "origin_channel", "origin_thread", "origin_sender",
        "nonce", "status", "created_at", "expires_at", "decided_at",
        "decided_by", "decision_origin",
    ),
    "timers": (
        "timer_id", "mission_id", "logical_key", "generation", "due_at",
        "interval_seconds", "misfire_policy", "catch_up_limit", "payload",
        "status", "version", "created_at", "updated_at",
    ),
    "external_actions": (
        "action_id", "mission_id", "task_id", "action_class",
        "idempotency_key", "status", "detail", "created_at", "updated_at",
    ),
    "inbox": (
        "inbox_id", "source", "idempotency_key", "mission_id", "payload",
        "status", "detail", "received_at", "processed_at",
    ),
    "outbox": (
        "outbox_id", "kind", "mission_id", "payload", "dedupe_key",
        "created_at",
    ),
    "artifacts": (
        "artifact_id", "mission_id", "task_id", "name", "digest", "size",
        "content", "created_at",
    ),
    "resource_bindings": (
        "binding_id", "mission_id", "kind", "resource", "created_at",
        "task_id", "status", "cursor", "detail", "updated_at",
    ),
    # Fleet registry / dispatch truth is replayed; heartbeat columns and
    # dispatch_events (worker observations) are operational by design.
    "workers": (
        "worker_id", "name", "host", "ssh_user", "ssh_port", "state",
        "trust_level", "data_ceiling", "labels", "capabilities",
        "runtime_profile", "profiles", "resource_group", "max_concurrency",
        "artifact_digest", "config_digest", "protocol_min", "protocol_max",
        "incarnation", "autonomy_capable", "version", "created_at",
        "updated_at",
    ),
    "dispatches": (
        "task_id", "mission_id", "parent_task_id", "envelope", "state",
        "attempt", "max_attempts", "worker_id", "fence", "not_before",
        "failure_class", "result", "error", "version", "created_at",
        "updated_at",
    ),
    # Personal items (personal-items plan P1): the whole row is mission
    # truth — every mutation is an item_* event on the item's own chain.
    "items": (
        "item_id", "space", "title", "body", "status", "due_at",
        "priority", "tags", "source", "mission_id", "version",
        "created_at", "updated_at",
    ),
    # Process compilations (process-compiler plan C1/C2): both tables are
    # mission truth — cards, approval, materialization refs, and drill
    # results all ride compilation_* events on the compilation's chain.
    "compilations": (
        "compilation_id", "status", "goal", "card_version",
        "approved_version", "approved_digest", "decided_by",
        "decision_origin", "decided_at", "decision_reason",
        "materialization", "drill", "mission_id", "version",
        "created_at", "updated_at",
    ),
    "compilation_cards": (
        "compilation_id", "card_version", "card", "digest", "author",
        "guidance", "created_at",
    ),
}

#: Item fields item_updated may change. Status changes ride their own
#: events (item_completed / item_archived); the only status an update may
#: carry is "open" — reopening a done or archived item.
ITEM_UPDATABLE_FIELDS = frozenset({
    "title", "body", "due_at", "priority", "tags", "space", "status",
})

#: Worker fields an admin/probe update may change through worker_updated.
#: Trust/data labels are admin-assigned; capabilities are observed — the
#: registry keeps them in separate columns so one can never masquerade as
#: the other.
WORKER_UPDATABLE_FIELDS = frozenset({
    "host", "ssh_user", "ssh_port", "trust_level", "data_ceiling",
    "labels", "capabilities", "runtime_profile", "profiles",
    "resource_group", "max_concurrency", "artifact_digest",
    "config_digest", "protocol_min", "protocol_max", "incarnation",
    "autonomy_capable",
})


def _canonical(data: Dict[str, Any]) -> str:
    return canonical_json(data)


def event_hash(mission_id: str, kind: str, data: Dict[str, Any],
               created_at: float, prev_hash: str) -> str:
    body = _canonical({
        "mission_id": mission_id,
        "kind": kind,
        "data": data,
        "created_at": created_at,
        "schema_version": EVENT_SCHEMA_VERSION,
        "prev_hash": prev_hash,
    })
    return hashlib.sha256(body.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Event application (the ONLY writer of replayed projections)
# ---------------------------------------------------------------------------

def _apply_event(conn: sqlite3.Connection, mission_id: str, kind: str,
                 data: Dict[str, Any], created_at: float) -> None:
    """Apply one event to the projections. Deterministic and total: replaying
    the journal through this function reproduces every replayed table."""
    if kind not in EVENT_KINDS:
        raise KernelError(f"unknown kernel event kind {kind!r} — failing closed")
    if kind == "mission_created":
        conn.execute(
            "INSERT INTO missions(mission_id, kind, status, spec, version,"
            " root_scope_id, stop_requested, runs, next_wake_at,"
            " last_session_at, last_error, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,0,0,NULL,NULL,'',?,?)",
            (mission_id, data["mission_kind"], data["status"],
             _canonical(data["spec"]), data["version"],
             data["root_scope_id"], created_at, created_at),
        )
    elif kind == "mission_transitioned":
        conn.execute(
            "UPDATE missions SET status=?, version=?, updated_at=?,"
            " last_error=? WHERE mission_id=?",
            (data["to"], data["version"], created_at,
             data.get("error", ""), mission_id),
        )
    elif kind == "mission_spec_updated":
        conn.execute(
            "UPDATE missions SET spec=?, version=?, updated_at=?"
            " WHERE mission_id=?",
            (_canonical(data["spec"]), data["version"], created_at,
             mission_id),
        )
    elif kind == "mission_stop_changed":
        conn.execute(
            "UPDATE missions SET stop_requested=?, version=?, updated_at=?"
            " WHERE mission_id=?",
            (1 if data["stopped"] else 0, data["version"], created_at,
             mission_id),
        )
    elif kind == "mission_note":
        pass  # journal-only fact; no projection
    elif kind == "plan_recorded":
        conn.execute(
            "INSERT INTO plans(plan_id, mission_id, version, content,"
            " created_at) VALUES (?,?,?,?,?)",
            (data["plan_id"], mission_id, data["plan_version"],
             _canonical(data["content"]), created_at),
        )
    elif kind == "task_created":
        conn.execute(
            "INSERT INTO tasks(task_id, mission_id, plan_id, title, detail,"
            " state, version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (data["task_id"], mission_id, data.get("plan_id", ""),
             data["title"], data.get("detail", ""), data["state"],
             data["version"], created_at, created_at),
        )
    elif kind == "task_transitioned":
        conn.execute(
            "UPDATE tasks SET state=?, version=?, updated_at=?"
            " WHERE task_id=?",
            (data["to"], data["version"], created_at, data["task_id"]),
        )
    elif kind == "attempt_started":
        conn.execute(
            "INSERT INTO task_attempts(attempt_id, task_id, attempt, state,"
            " failure_class, detail, started_at, finished_at)"
            " VALUES (?,?,?,?,'','',?,NULL)",
            (data["attempt_id"], data["task_id"], data["attempt"],
             "running", created_at),
        )
    elif kind == "attempt_finished":
        conn.execute(
            "UPDATE task_attempts SET state=?, failure_class=?, detail=?,"
            " finished_at=? WHERE attempt_id=?",
            (data["state"], data.get("failure_class", ""),
             data.get("detail", ""), created_at, data["attempt_id"]),
        )
    elif kind == "checkpoint_recorded":
        conn.execute(
            "INSERT INTO checkpoints(checkpoint_id, mission_id, session_id,"
            " summary, state, created_at) VALUES (?,?,?,?,?,?)",
            (data["checkpoint_id"], mission_id, data.get("session_id", ""),
             data["summary"], _canonical(data.get("state", {})), created_at),
        )
    elif kind == "review_recorded":
        conn.execute(
            "INSERT INTO reviews(review_id, mission_id, session_id, action,"
            " content, created_at) VALUES (?,?,?,?,?,?)",
            (data["review_id"], mission_id, data.get("session_id", ""),
             data["action"], _canonical(data.get("content", {})),
             created_at),
        )
    elif kind == "review_skipped":
        pass  # journal-only fact; advances the review cadence marker
    elif kind == "plan_revised":
        pass  # journal-only rationale; the plan itself rides plan_recorded
    elif kind == "completion_denied":
        pass  # journal-only fact; the mission state never moved
    elif kind == "budget_scope_created":
        conn.execute(
            "INSERT INTO budget_scopes(scope_id, mission_id,"
            " parent_scope_id, status, created_at) VALUES (?,?,?,'open',?)",
            (data["scope_id"], mission_id, data.get("parent_scope_id", ""),
             created_at),
        )
        for line in sorted(data["lines"]):
            conn.execute(
                "INSERT INTO budget_lines(scope_id, line, cap, reserved,"
                " committed) VALUES (?,?,?,0,0)",
                (data["scope_id"], line, data["lines"][line]),
            )
    elif kind == "budget_reserved":
        for line in sorted(data["lines"]):
            amount = data["lines"][line]
            conn.execute(
                "UPDATE budget_lines SET reserved=reserved+? WHERE"
                " scope_id=? AND line=?",
                (amount, data["scope_id"], line),
            )
            conn.execute(
                "INSERT INTO budget_reservations(reservation_id, scope_id,"
                " line, amount, status, created_at)"
                " VALUES (?,?,?,?,'active',?)",
                (data["reservation_id"], data["scope_id"], line, amount,
                 created_at),
            )
            conn.execute(
                "INSERT INTO budget_ledger(scope_id, line, op, amount,"
                " reservation_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (data["scope_id"], line, "reserve", amount,
                 data["reservation_id"], data.get("note", ""), created_at),
            )
    elif kind == "budget_committed":
        scope_id = data["scope_id"]
        reservation_id = data["reservation_id"]
        actuals = data["actuals"]
        rows = conn.execute(
            "SELECT line, amount FROM budget_reservations WHERE"
            " reservation_id=? AND scope_id=? AND status='active'",
            (reservation_id, scope_id),
        ).fetchall()
        for line, reserved_amount in sorted(rows):
            actual = int(actuals.get(line, 0))
            conn.execute(
                "UPDATE budget_lines SET reserved=reserved-?,"
                " committed=committed+? WHERE scope_id=? AND line=?",
                (reserved_amount, actual, scope_id, line),
            )
            conn.execute(
                "UPDATE budget_reservations SET status='committed' WHERE"
                " reservation_id=? AND scope_id=? AND line=?",
                (reservation_id, scope_id, line),
            )
            conn.execute(
                "INSERT INTO budget_ledger(scope_id, line, op, amount,"
                " reservation_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (scope_id, line, "commit", actual, reservation_id,
                 data.get("note", ""), created_at),
            )
        closes = data.get("closes_scope", "")
        if closes:
            conn.execute(
                "UPDATE budget_scopes SET status='closed' WHERE scope_id=?",
                (closes,),
            )
    elif kind == "budget_released":
        scope_id = data["scope_id"]
        reservation_id = data["reservation_id"]
        rows = conn.execute(
            "SELECT line, amount FROM budget_reservations WHERE"
            " reservation_id=? AND scope_id=? AND status='active'",
            (reservation_id, scope_id),
        ).fetchall()
        for line, reserved_amount in sorted(rows):
            conn.execute(
                "UPDATE budget_lines SET reserved=reserved-? WHERE"
                " scope_id=? AND line=?",
                (reserved_amount, scope_id, line),
            )
            conn.execute(
                "UPDATE budget_reservations SET status='released' WHERE"
                " reservation_id=? AND scope_id=? AND line=?",
                (reservation_id, scope_id, line),
            )
            conn.execute(
                "INSERT INTO budget_ledger(scope_id, line, op, amount,"
                " reservation_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (scope_id, line, "release", reserved_amount, reservation_id,
                 data.get("note", ""), created_at),
            )
    elif kind == "approval_requested":
        conn.execute(
            "INSERT INTO approvals(approval_id, mission_id, action_kind,"
            " action_args, args_hash, origin_channel, origin_thread,"
            " origin_sender, nonce, status, created_at, expires_at,"
            " decided_at, decided_by, decision_origin)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,'','')",
            (data["approval_id"], mission_id, data["action_kind"],
             _canonical(data["action_args"]), data["args_hash"],
             data["origin_channel"], data["origin_thread"],
             data["origin_sender"], data["nonce"], ApprovalStatus.PENDING,
             created_at, data["expires_at"]),
        )
    elif kind == "approval_decided":
        conn.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=?,"
            " decision_origin=? WHERE approval_id=?",
            (data["status"], created_at, data["decided_by"],
             data["decision_origin"], data["approval_id"]),
        )
    elif kind == "approval_expired":
        conn.execute(
            "UPDATE approvals SET status=? WHERE approval_id=?",
            (ApprovalStatus.EXPIRED, data["approval_id"]),
        )
    elif kind == "timer_created":
        conn.execute(
            "INSERT INTO timers(timer_id, mission_id, logical_key,"
            " generation, due_at, interval_seconds, misfire_policy,"
            " catch_up_limit, payload, status, version, created_at,"
            " updated_at, claimed_by, claim_expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
            (data["timer_id"], mission_id, data["logical_key"],
             data["generation"], data["due_at"], data["interval_seconds"],
             data["misfire_policy"], data["catch_up_limit"],
             _canonical(data.get("payload", {})), "active", 1, created_at,
             created_at),
        )
    elif kind == "timer_fired":
        if data.get("final"):
            conn.execute(
                "UPDATE timers SET status='completed', updated_at=?,"
                " claimed_by=NULL, claim_expires_at=NULL WHERE timer_id=?",
                (created_at, data["timer_id"]),
            )
    elif kind == "timer_rescheduled":
        conn.execute(
            "UPDATE timers SET generation=?, due_at=?, interval_seconds=?,"
            " version=version+1, updated_at=?, claimed_by=NULL,"
            " claim_expires_at=NULL WHERE timer_id=?",
            (data["generation"], data["due_at"], data["interval_seconds"],
             created_at, data["timer_id"]),
        )
    elif kind == "timer_cancelled":
        conn.execute(
            "UPDATE timers SET status='cancelled', version=version+1,"
            " updated_at=?, claimed_by=NULL, claim_expires_at=NULL"
            " WHERE timer_id=?",
            (created_at, data["timer_id"]),
        )
    elif kind == "action_recorded":
        conn.execute(
            "INSERT INTO external_actions(action_id, mission_id, task_id,"
            " action_class, idempotency_key, status, detail, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (data["action_id"], mission_id, data.get("task_id", ""),
             data["action_class"], data["idempotency_key"],
             ActionStatus.PENDING, _canonical(data.get("detail", {})),
             created_at, created_at),
        )
    elif kind == "action_resolved":
        conn.execute(
            "UPDATE external_actions SET status=?, detail=?, updated_at=?"
            " WHERE action_id=?",
            (data["status"], _canonical(data.get("detail", {})), created_at,
             data["action_id"]),
        )
    elif kind == "artifact_recorded":
        conn.execute(
            "INSERT INTO artifacts(artifact_id, mission_id, task_id, name,"
            " digest, size, content, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (data["artifact_id"], mission_id, data.get("task_id", ""),
             data["name"], data["digest"], data["size"],
             data.get("content"), created_at),
        )
    elif kind == "binding_recorded":
        # Events that predate the Phase 3 lifecycle carry no "status";
        # they project the schema defaults (updated_at 0) so replaying an
        # old journal reproduces a migrated live table byte-for-byte.
        lifecycle = "status" in data
        conn.execute(
            "INSERT INTO resource_bindings(binding_id, mission_id, kind,"
            " resource, created_at, task_id, status, cursor, detail,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (data["binding_id"], mission_id, data["binding_kind"],
             _canonical(data["resource"]), created_at,
             data.get("task_id", ""),
             data.get("status", BindingStatus.ACTIVE),
             int(data.get("cursor", 0)),
             _canonical(data.get("detail", {})),
             created_at if lifecycle else 0),
        )
    elif kind == "binding_updated":
        fields = data["fields"]
        assignments = []
        params: List[Any] = []
        for column in sorted(fields):
            value = fields[column]
            if column == "detail":
                value = _canonical(value)
            elif column == "cursor":
                value = int(value)
            assignments.append(f'"{column}"=?')
            params.append(value)
        assignments.append("updated_at=?")
        params.append(created_at)
        params.append(data["binding_id"])
        conn.execute(
            "UPDATE resource_bindings SET "
            + ", ".join(assignments) + " WHERE binding_id=?",
            params,
        )
    elif kind == "outbox_enqueued":
        conn.execute(
            "INSERT INTO outbox(kind, mission_id, payload, dedupe_key,"
            " created_at, status, attempts, next_attempt_at, last_error,"
            " transport, delivered_at)"
            " VALUES (?,?,?,?,?,'pending',0,?, '', '', NULL)",
            (data["outbox_kind"], mission_id, _canonical(data["payload"]),
             data["dedupe_key"], created_at, created_at),
        )
    elif kind == "inbox_received":
        conn.execute(
            "INSERT INTO inbox(source, idempotency_key, mission_id, payload,"
            " status, detail, received_at, processed_at)"
            " VALUES (?,?,?,?,?,'',?,NULL)",
            (data["source"], data["idempotency_key"], mission_id,
             _canonical(data["payload"]), InboxStatus.PENDING, created_at),
        )
    elif kind == "inbox_processed":
        conn.execute(
            "UPDATE inbox SET status=?, detail=?, processed_at=?"
            " WHERE idempotency_key=?",
            (data["status"], data.get("detail", ""), created_at,
             data["idempotency_key"]),
        )
    elif kind == "worker_enrolled":
        conn.execute(
            "INSERT INTO workers(worker_id, name, host, ssh_user, ssh_port,"
            " state, trust_level, data_ceiling, labels, capabilities,"
            " runtime_profile, profiles, resource_group, max_concurrency,"
            " artifact_digest, config_digest, protocol_min, protocol_max,"
            " incarnation, autonomy_capable, version, created_at,"
            " updated_at, heartbeat_seq, last_heartbeat_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL)",
            (data["worker_id"], data["name"], data["host"],
             data.get("ssh_user", ""), data.get("ssh_port"),
             data["state"], data.get("trust_level", 0),
             data.get("data_ceiling", "internal"),
             _canonical(data.get("labels", {})),
             _canonical(data.get("capabilities", {})),
             data.get("runtime_profile", ""),
             _canonical({"list": data.get("profiles", [])}),
             data.get("resource_group", ""),
             data.get("max_concurrency", 1),
             data.get("artifact_digest", ""),
             data.get("config_digest", ""),
             data.get("protocol_min", 1), data.get("protocol_max", 1),
             data.get("incarnation", 0),
             1 if data.get("autonomy_capable") else 0,
             data["version"], created_at, created_at),
        )
    elif kind == "worker_updated":
        fields = data["fields"]
        assignments = []
        params: List[Any] = []
        for column in sorted(fields):
            value = fields[column]
            if column in ("labels", "capabilities"):
                value = _canonical(value)
            elif column == "profiles":
                value = _canonical({"list": value})
            elif column == "autonomy_capable":
                value = 1 if value else 0
            assignments.append(f"{column}=?")
            params.append(value)
        assignments.append("version=?")
        params.append(data["version"])
        assignments.append("updated_at=?")
        params.append(created_at)
        params.append(data["worker_id"])
        conn.execute(
            f"UPDATE workers SET {', '.join(assignments)} WHERE worker_id=?",
            params,
        )
    elif kind == "worker_transitioned":
        conn.execute(
            "UPDATE workers SET state=?, version=?, updated_at=?"
            " WHERE worker_id=?",
            (data["to"], data["version"], created_at, data["worker_id"]),
        )
    elif kind == "dispatch_created":
        envelope = data["envelope"]
        conn.execute(
            "INSERT INTO dispatches(task_id, mission_id, parent_task_id,"
            " envelope, state, attempt, max_attempts, worker_id, fence,"
            " not_before, failure_class, result, error, version,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,0,?, '', 0, 0, '', '{}', '', ?, ?, ?)",
            (envelope["task_id"], mission_id,
             envelope.get("parent_task_id", ""), _canonical(envelope),
             data["state"], data["max_attempts"], data["version"],
             created_at, created_at),
        )
    elif kind == "dispatch_transitioned":
        conn.execute(
            "UPDATE dispatches SET state=?, attempt=?, worker_id=?,"
            " fence=?, not_before=?, failure_class=?, result=?, error=?,"
            " version=?, updated_at=? WHERE task_id=?",
            (data["to"], data["attempt"], data["worker_id"], data["fence"],
             data["not_before"], data.get("failure_class", ""),
             _canonical(data.get("result", {})), data.get("error", ""),
             data["version"], created_at, data["task_id"]),
        )
    elif kind == "item_added":
        conn.execute(
            "INSERT INTO items(item_id, space, title, body, status,"
            " due_at, priority, tags, source, mission_id, version,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data["item_id"], data["space"], data["title"],
             data.get("body", ""), data["status"], data.get("due_at"),
             data.get("priority"),
             _canonical({"list": data.get("tags", [])}),
             data.get("source", "chat"), data.get("mission_id", ""),
             data["version"], created_at, created_at),
        )
    elif kind == "item_updated":
        fields = data["fields"]
        assignments = []
        params: List[Any] = []
        for column in sorted(fields):
            value = fields[column]
            if column == "tags":
                value = _canonical({"list": value})
            assignments.append(f'"{column}"=?')
            params.append(value)
        assignments.append("version=?")
        params.append(data["version"])
        assignments.append("updated_at=?")
        params.append(created_at)
        params.append(data["item_id"])
        conn.execute(
            "UPDATE items SET " + ", ".join(assignments)
            + " WHERE item_id=?",
            params,
        )
    elif kind == "item_completed":
        conn.execute(
            "UPDATE items SET status=?, version=?, updated_at=?"
            " WHERE item_id=?",
            (ItemStatus.DONE, data["version"], created_at,
             data["item_id"]),
        )
    elif kind == "item_archived":
        conn.execute(
            "UPDATE items SET status=?, version=?, updated_at=?"
            " WHERE item_id=?",
            (ItemStatus.ARCHIVED, data["version"], created_at,
             data["item_id"]),
        )
    elif kind == "item_escalated":
        conn.execute(
            "UPDATE items SET mission_id=?, version=?, updated_at=?"
            " WHERE item_id=?",
            (data["mission_id"], data["version"], created_at,
             data["item_id"]),
        )
    elif kind == "item_mission_synced":
        pass  # journal-only proposal; the user decides the item's fate
    elif kind == "compilation_created":
        conn.execute(
            "INSERT INTO compilations(compilation_id, status, goal,"
            " card_version, approved_version, approved_digest, decided_by,"
            " decision_origin, decided_at, decision_reason,"
            " materialization, drill, mission_id, version, created_at,"
            " updated_at)"
            " VALUES (?,?,?,1,0,'','','',NULL,'','{}','{}','',?,?,?)",
            (data["compilation_id"], data["status"], data["goal"],
             data["version"], created_at, created_at),
        )
        conn.execute(
            "INSERT INTO compilation_cards(compilation_id, card_version,"
            " card, digest, author, guidance, created_at)"
            " VALUES (?,1,?,?,?,'',?)",
            (data["compilation_id"], _canonical(data["card"]),
             data["digest"], data.get("author", ""), created_at),
        )
    elif kind == "compilation_card_recorded":
        # A new card version always re-arms review: status back to
        # compiled, the prior approval pin cleared (revision invalidates
        # approval, the pack-engine rule).
        conn.execute(
            "INSERT INTO compilation_cards(compilation_id, card_version,"
            " card, digest, author, guidance, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (data["compilation_id"], data["card_version"],
             _canonical(data["card"]), data["digest"],
             data.get("author", ""), data.get("guidance", ""),
             created_at),
        )
        conn.execute(
            "UPDATE compilations SET status=?, card_version=?,"
            " approved_version=0, approved_digest='', decided_by='',"
            " decision_origin='', decided_at=NULL, decision_reason='',"
            " version=?, updated_at=? WHERE compilation_id=?",
            (data["status"], data["card_version"], data["version"],
             created_at, data["compilation_id"]),
        )
    elif kind == "compilation_decided":
        conn.execute(
            "UPDATE compilations SET status=?, approved_version=?,"
            " approved_digest=?, decided_by=?, decision_origin=?,"
            " decided_at=?, decision_reason=?, version=?, updated_at=?"
            " WHERE compilation_id=?",
            (data["status"],
             data["card_version"] if data["status"] == "approved" else 0,
             data["digest"] if data["status"] == "approved" else "",
             data["decided_by"], data["decision_origin"], created_at,
             data.get("reason", ""), data["version"], created_at,
             data["compilation_id"]),
        )
    elif kind == "compilation_transitioned":
        conn.execute(
            "UPDATE compilations SET status=?, mission_id=CASE WHEN ?=''"
            " THEN mission_id ELSE ? END, version=?, updated_at=?"
            " WHERE compilation_id=?",
            (data["to"], data.get("mission_id", ""),
             data.get("mission_id", ""), data["version"], created_at,
             data["compilation_id"]),
        )
    elif kind == "compilation_materialization_recorded":
        # The event carries the full cumulative materialization state, so
        # replaying any prefix of the journal reproduces the column.
        conn.execute(
            "UPDATE compilations SET materialization=?, version=?,"
            " updated_at=? WHERE compilation_id=?",
            (_canonical(data["materialization"]), data["version"],
             created_at, data["compilation_id"]),
        )
    elif kind == "compilation_drill_recorded":
        conn.execute(
            "UPDATE compilations SET drill=?, version=?, updated_at=?"
            " WHERE compilation_id=?",
            (_canonical(data["drill"]), data["version"], created_at,
             data["compilation_id"]),
        )
    elif kind == "session_started":
        conn.execute(
            "UPDATE missions SET last_session_at=? WHERE mission_id=?",
            (created_at, mission_id),
        )
    elif kind == "session_checkpointed":
        conn.execute(
            "UPDATE missions SET runs=runs+1 WHERE mission_id=?",
            (mission_id,),
        )
    elif kind == "session_abandoned":
        pass  # journal fact; the paired mission_transitioned carries state
    else:  # pragma: no cover — EVENT_KINDS check above is exhaustive
        raise KernelError(f"unhandled kernel event kind {kind!r}")


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------

class _WriteJob:
    __slots__ = ("fn", "raw", "done", "result", "error")

    def __init__(self, fn, raw=False):
        self.fn = fn
        #: raw jobs manage their own locking (e.g. the SQLite backup API,
        #: which deadlocks inside an explicit transaction).
        self.raw = raw
        self.done = threading.Event()
        self.result = None
        self.error = None


class MissionStore:
    """Single-writer transactional kernel store. Thread-safe for callers."""

    def __init__(self, path: Optional[Any] = None,
                 clock: Callable[[], float] = _time.time):
        self.path = Path(path) if path else default_kernel_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.clock = clock
        self._epoch: Optional[int] = None
        self._queue: "queue.Queue[Optional[_WriteJob]]" = queue.Queue()
        self._writer_ready = threading.Event()
        self._writer_error: Optional[BaseException] = None
        self._closed = False
        self._read_local = threading.local()
        self._writer = threading.Thread(
            target=self._writer_loop, name="conch-kernel-writer", daemon=True
        )
        self._writer.start()
        self._writer_ready.wait(timeout=10)
        if self._writer_error is not None:
            raise KernelError(
                f"kernel writer failed to start: {self._writer_error}"
            )

    # -- connections ---------------------------------------------------------

    def _configure(self, conn: sqlite3.Connection, *, writer: bool) -> None:
        conn.execute("PRAGMA busy_timeout=5000")
        if writer:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")

    def _writer_loop(self) -> None:
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None)
            self._configure(conn, writer=True)
            # Column migrations run before the schema script: _SCHEMA's
            # index statements may reference columns added after a
            # pre-existing table was created.
            self._add_missing_columns(conn)
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES"
                    " ('schema_version', ?), ('controller_epoch', '0')",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                raise KernelError(
                    f"kernel database schema version {row[0]} is not"
                    f" supported (expected {SCHEMA_VERSION}) — failing closed"
                )
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except BaseException as exc:
            self._writer_error = exc
            self._writer_ready.set()
            return
        self._writer_ready.set()
        while True:
            job = self._queue.get()
            if job is None:
                break
            try:
                if job.raw:
                    job.result = job.fn(conn)
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._check_epoch(conn)
                        job.result = job.fn(conn)
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
            except BaseException as exc:
                job.error = exc
            finally:
                job.done.set()
        conn.close()

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """Additive column micro-migrations for pre-existing databases.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so
        columns added to ``_SCHEMA`` after a database was created are
        backfilled here with the exact defaults the schema declares —
        replaying old journals into the new schema then reproduces the
        same bytes (the defaults are what ``_apply_event`` writes for
        events that predate the column).
        """
        additions = {
            "resource_bindings": (
                ("task_id", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("cursor", "INTEGER NOT NULL DEFAULT 0"),
                ("detail", "TEXT NOT NULL DEFAULT '{}'"),
                ("updated_at", "REAL NOT NULL DEFAULT 0"),
            ),
        }
        for table, columns in additions.items():
            existing = {
                row[1] for row in conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if not existing:
                continue  # table doesn't exist yet; _SCHEMA creates it
            for name, declaration in columns:
                if name not in existing:
                    conn.execute(
                        f'ALTER TABLE {table} ADD COLUMN "{name}"'
                        f" {declaration}"
                    )

    def _check_epoch(self, conn: sqlite3.Connection) -> None:
        if self._epoch is None:
            return
        row = conn.execute(
            "SELECT value FROM meta WHERE key='controller_epoch'"
        ).fetchone()
        current = int(row[0]) if row else 0
        if current != self._epoch:
            raise KernelError(
                f"stale controller epoch {self._epoch} (current {current})"
                " — a newer daemon owns this kernel; refusing to write"
            )

    def _mutate(self, fn: Callable[[sqlite3.Connection], Any],
                raw: bool = False) -> Any:
        if self._closed:
            raise KernelError("kernel store is closed")
        job = _WriteJob(fn, raw=raw)
        self._queue.put(job)
        job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result

    def _read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._read_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), isolation_level=None)
            self._configure(conn, writer=False)
            conn.row_factory = sqlite3.Row
            self._read_local.conn = conn
        return conn

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._writer.join(timeout=10)
        conn = getattr(self._read_local, "conn", None)
        if conn is not None:
            conn.close()
            self._read_local.conn = None

    def __enter__(self) -> "MissionStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- epoch (daemon fencing) ---------------------------------------------

    def adopt_epoch(self) -> int:
        """Increment the controller epoch and fence this store to it.

        A store instance that adopted an older epoch has every subsequent
        mutation rejected — a superseded daemon cannot write."""
        def fn(conn):
            row = conn.execute(
                "SELECT value FROM meta WHERE key='controller_epoch'"
            ).fetchone()
            epoch = (int(row[0]) if row else 0) + 1
            conn.execute(
                "UPDATE meta SET value=? WHERE key='controller_epoch'",
                (str(epoch),),
            )
            return epoch
        # Bypass the epoch check for adoption itself (self._epoch may be
        # stale by design here).
        old = self._epoch
        self._epoch = None
        try:
            epoch = self._mutate(fn)
        except BaseException:
            self._epoch = old
            raise
        self._epoch = epoch
        return epoch

    def current_epoch(self) -> int:
        row = self._read_conn().execute(
            "SELECT value FROM meta WHERE key='controller_epoch'"
        ).fetchone()
        return int(row[0]) if row else 0

    # -- event helpers (writer-thread context only) ---------------------------

    def _append(self, conn: sqlite3.Connection, mission_id: str, kind: str,
                data: Dict[str, Any]) -> int:
        if kind not in EVENT_KINDS:
            raise KernelError(f"unknown kernel event kind {kind!r}")
        created_at = float(self.clock())
        row = conn.execute(
            "SELECT hash FROM mission_events WHERE mission_id=?"
            " ORDER BY seq DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        prev_hash = row[0] if row else GENESIS_HASH
        digest = event_hash(mission_id, kind, data, created_at, prev_hash)
        cursor = conn.execute(
            "INSERT INTO mission_events(mission_id, kind, data,"
            " schema_version, created_at, prev_hash, hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (mission_id, kind, _canonical(data), EVENT_SCHEMA_VERSION,
             created_at, prev_hash, digest),
        )
        _apply_event(conn, mission_id, kind, data, created_at)
        return int(cursor.lastrowid)

    @staticmethod
    def _mission_row(conn: sqlite3.Connection, mission_id: str):
        row = conn.execute(
            "SELECT mission_id, kind, status, spec, version, root_scope_id,"
            " stop_requested FROM missions WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
        if row is None:
            raise KernelError(f"unknown mission {mission_id!r}")
        return row

    def _transition(self, conn: sqlite3.Connection, mission_id: str,
                    target: str, expected_version: Optional[int],
                    reason: str = "", error: str = "") -> int:
        row = self._mission_row(conn, mission_id)
        current, version = row[2], int(row[4])
        if expected_version is not None and version != expected_version:
            raise ConflictError(
                f"mission {mission_id} version {version} !="
                f" expected {expected_version}"
            )
        check_transition(current, target)
        new_version = version + 1
        self._append(conn, mission_id, "mission_transitioned", {
            "from": current, "to": target, "version": new_version,
            "reason": reason, "error": error,
        })
        if target in MissionState.TERMINAL:
            self._sync_escalated_items(conn, mission_id, target)
        return new_version

    def _sync_escalated_items(self, conn: sqlite3.Connection,
                              mission_id: str, outcome: str) -> None:
        """Escalation sync (personal-items plan): a terminal mission
        journals a proposal event onto every item linked to it, in the
        same transaction as the mission's transition. Journal-only — the
        item's status is the user's call, never moved by a mission."""
        rows = conn.execute(
            "SELECT item_id FROM items WHERE mission_id=? AND status!=?"
            " ORDER BY item_id",
            (mission_id, ItemStatus.ARCHIVED),
        ).fetchall()
        proposal = (
            "complete" if outcome == MissionState.SUCCEEDED else "review"
        )
        for (item_id,) in rows:
            self._append(conn, item_id, "item_mission_synced", {
                "item_id": item_id, "mission_id": mission_id,
                "outcome": outcome, "proposal": proposal,
            })

    def _enqueue_outbox(self, conn: sqlite3.Connection, mission_id: str,
                        kind: str, payload: Dict[str, Any],
                        dedupe_key: str) -> bool:
        existing = conn.execute(
            "SELECT outbox_id FROM outbox WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if existing is not None:
            return False
        self._append(conn, mission_id, "outbox_enqueued", {
            "outbox_kind": kind, "payload": payload,
            "dedupe_key": dedupe_key,
        })
        return True

    # ------------------------------------------------------------------
    # Missions
    # ------------------------------------------------------------------

    def create_mission(self, spec: Dict[str, Any],
                       mission_id: Optional[str] = None) -> str:
        normalized = normalize_spec(spec)
        mid = mission_id or kernel_id("msn")
        scope_id = kernel_id("scp")

        def fn(conn):
            self._append(conn, mid, "mission_created", {
                "mission_kind": normalized["kind"],
                "status": MissionState.DRAFT,
                "spec": normalized,
                "version": 1,
                "root_scope_id": scope_id,
            })
            self._append(conn, mid, "budget_scope_created", {
                "scope_id": scope_id,
                "parent_scope_id": "",
                "lines": normalized["budgets"],
            })
            return mid
        return self._mutate(fn)

    def transition_mission(self, mission_id: str, target: str,
                           expected_version: Optional[int] = None,
                           reason: str = "", error: str = "") -> int:
        return self._mutate(
            lambda conn: self._transition(
                conn, mission_id, target, expected_version, reason, error
            )
        )

    def update_spec(self, mission_id: str, spec: Dict[str, Any],
                    expected_version: Optional[int] = None) -> int:
        normalized = normalize_spec(spec)

        def fn(conn):
            row = self._mission_row(conn, mission_id)
            version = int(row[4])
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"mission {mission_id} version {version} !="
                    f" expected {expected_version}"
                )
            new_version = version + 1
            self._append(conn, mission_id, "mission_spec_updated", {
                "spec": normalized, "version": new_version,
            })
            return new_version
        return self._mutate(fn)

    def set_stop(self, mission_id: str, stopped: bool) -> int:
        def fn(conn):
            row = self._mission_row(conn, mission_id)
            new_version = int(row[4]) + 1
            self._append(conn, mission_id, "mission_stop_changed", {
                "stopped": bool(stopped), "version": new_version,
            })
            return new_version
        return self._mutate(fn)

    def record_note(self, mission_id: str, text: str,
                    author: str = "") -> None:
        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "mission_note", {
                "text": str(text), "author": str(author),
            })
        self._mutate(fn)

    def record_completion_denied(self, mission_id: str, reason: str,
                                 author: str = "") -> None:
        """Journal that a complete_mission request was refused by the
        allow_model_completion spec gate. Journal-only: the mission's
        state and version are untouched."""
        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "completion_denied", {
                "reason": str(reason), "author": str(author),
            })
        self._mutate(fn)

    # ------------------------------------------------------------------
    # Plans / tasks / attempts / checkpoints
    # ------------------------------------------------------------------

    def record_plan(self, mission_id: str, content: Dict[str, Any]) -> str:
        plan_id = kernel_id("pln")

        def fn(conn):
            self._mission_row(conn, mission_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM plans WHERE"
                " mission_id=?",
                (mission_id,),
            ).fetchone()
            self._append(conn, mission_id, "plan_recorded", {
                "plan_id": plan_id, "plan_version": int(row[0]) + 1,
                "content": content,
            })
            return plan_id
        return self._mutate(fn)

    def create_task(self, mission_id: str, title: str, detail: str = "",
                    plan_id: str = "") -> str:
        title = str(title).strip()
        if not title:
            raise KernelError("task title is required")
        task_id = kernel_id("task")

        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "task_created", {
                "task_id": task_id, "plan_id": plan_id, "title": title,
                "detail": str(detail), "state": TaskState.OPEN, "version": 1,
            })
            return task_id
        return self._mutate(fn)

    def transition_task(self, task_id: str, target: str,
                        expected_version: Optional[int] = None) -> int:
        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, state, version FROM tasks WHERE"
                " task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown task {task_id!r}")
            mission_id, current, version = row[0], row[1], int(row[2])
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"task {task_id} version {version} !="
                    f" expected {expected_version}"
                )
            check_task_transition(current, target)
            new_version = version + 1
            self._append(conn, mission_id, "task_transitioned", {
                "task_id": task_id, "from": current, "to": target,
                "version": new_version,
            })
            return new_version
        return self._mutate(fn)

    def start_attempt(self, task_id: str) -> str:
        attempt_id = kernel_id("att")

        def fn(conn):
            row = conn.execute(
                "SELECT mission_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown task {task_id!r}")
            count = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) FROM task_attempts WHERE"
                " task_id=?",
                (task_id,),
            ).fetchone()
            self._append(conn, row[0], "attempt_started", {
                "attempt_id": attempt_id, "task_id": task_id,
                "attempt": int(count[0]) + 1,
            })
            return attempt_id
        return self._mutate(fn)

    def finish_attempt(self, attempt_id: str, state: str,
                       failure_class: str = "", detail: str = "") -> None:
        if state not in ("succeeded", "failed", "abandoned"):
            raise KernelError(f"unknown attempt state {state!r}")

        def fn(conn):
            row = conn.execute(
                "SELECT t.mission_id FROM task_attempts a JOIN tasks t ON"
                " a.task_id = t.task_id WHERE a.attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown attempt {attempt_id!r}")
            self._append(conn, row[0], "attempt_finished", {
                "attempt_id": attempt_id, "state": state,
                "failure_class": failure_class, "detail": str(detail),
            })
        self._mutate(fn)

    def record_checkpoint(self, mission_id: str, summary: str,
                          state: Optional[Dict[str, Any]] = None,
                          session_id: str = "") -> str:
        checkpoint_id = kernel_id("ckpt")

        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "checkpoint_recorded", {
                "checkpoint_id": checkpoint_id, "session_id": session_id,
                "summary": str(summary), "state": state or {},
            })
            return checkpoint_id
        return self._mutate(fn)

    # ------------------------------------------------------------------
    # Budgets (integer units; reserve / commit / release)
    # ------------------------------------------------------------------

    @staticmethod
    def _line_available(conn: sqlite3.Connection, scope_id: str,
                        line: str) -> int:
        row = conn.execute(
            "SELECT cap, reserved, committed FROM budget_lines WHERE"
            " scope_id=? AND line=?",
            (scope_id, line),
        ).fetchone()
        if row is None:
            raise KernelError(
                f"budget scope {scope_id} has no line {line!r}"
            )
        return int(row[0]) - int(row[1]) - int(row[2])

    def _reserve(self, conn: sqlite3.Connection, mission_id: str,
                 scope_id: str, lines: Dict[str, int], reservation_id: str,
                 note: str = "") -> None:
        clean: Dict[str, int] = {}
        for line, amount in lines.items():
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise KernelError(f"budget amount for {line!r} must be int")
            if amount < 0:
                raise KernelError(f"budget amount for {line!r} negative")
            if amount == 0:
                continue
            available = self._line_available(conn, scope_id, line)
            if amount > available:
                raise BudgetExceededError(
                    f"budget line {line!r} on scope {scope_id}: requested"
                    f" {amount}, available {available} — refusing"
                )
            clean[line] = amount
        existing = conn.execute(
            "SELECT 1 FROM budget_reservations WHERE reservation_id=? AND"
            " scope_id=? LIMIT 1",
            (reservation_id, scope_id),
        ).fetchone()
        if existing is not None:
            raise KernelError(
                f"reservation {reservation_id!r} already exists on scope"
                f" {scope_id}"
            )
        if not clean:
            return
        self._append(conn, mission_id, "budget_reserved", {
            "scope_id": scope_id, "reservation_id": reservation_id,
            "lines": clean, "note": note,
        })

    def reserve_budget(self, mission_id: str, scope_id: str,
                       lines: Dict[str, int], reservation_id: str,
                       note: str = "") -> None:
        self._mutate(
            lambda conn: self._reserve(
                conn, mission_id, scope_id, lines, reservation_id, note
            )
        )

    def _commit_budget(self, conn: sqlite3.Connection, mission_id: str,
                       scope_id: str, reservation_id: str,
                       actuals: Dict[str, int], note: str = "",
                       closes_scope: str = "") -> None:
        rows = conn.execute(
            "SELECT line, amount FROM budget_reservations WHERE"
            " reservation_id=? AND scope_id=? AND status='active'",
            (reservation_id, scope_id),
        ).fetchall()
        if not rows:
            raise KernelError(
                f"no active reservation {reservation_id!r} on scope"
                f" {scope_id}"
            )
        reserved = {row[0]: int(row[1]) for row in rows}
        clean: Dict[str, int] = {}
        for line, actual in actuals.items():
            if isinstance(actual, bool) or not isinstance(actual, int):
                raise KernelError(f"actual for {line!r} must be int")
            if actual < 0:
                raise KernelError(f"actual for {line!r} negative")
            if line not in reserved:
                raise KernelError(
                    f"line {line!r} was not reserved by {reservation_id!r}"
                )
            if actual > reserved[line]:
                raise BudgetExceededError(
                    f"commit for {line!r} ({actual}) exceeds reservation"
                    f" ({reserved[line]}) — refusing"
                )
            clean[line] = actual
        self._append(conn, mission_id, "budget_committed", {
            "scope_id": scope_id, "reservation_id": reservation_id,
            "actuals": clean, "note": note, "closes_scope": closes_scope,
        })

    def commit_budget(self, mission_id: str, scope_id: str,
                      reservation_id: str, actuals: Dict[str, int],
                      note: str = "") -> None:
        self._mutate(
            lambda conn: self._commit_budget(
                conn, mission_id, scope_id, reservation_id, actuals, note
            )
        )

    def _release_budget(self, conn: sqlite3.Connection, mission_id: str,
                        scope_id: str, reservation_id: str,
                        note: str = "") -> None:
        row = conn.execute(
            "SELECT 1 FROM budget_reservations WHERE reservation_id=? AND"
            " scope_id=? AND status='active' LIMIT 1",
            (reservation_id, scope_id),
        ).fetchone()
        if row is None:
            raise KernelError(
                f"no active reservation {reservation_id!r} on scope"
                f" {scope_id}"
            )
        self._append(conn, mission_id, "budget_released", {
            "scope_id": scope_id, "reservation_id": reservation_id,
            "note": note,
        })

    def release_budget(self, mission_id: str, scope_id: str,
                       reservation_id: str, note: str = "") -> None:
        self._mutate(
            lambda conn: self._release_budget(
                conn, mission_id, scope_id, reservation_id, note
            )
        )

    def create_child_scope(self, mission_id: str, parent_scope_id: str,
                           lines: Dict[str, int],
                           scope_id: Optional[str] = None) -> str:
        """Create a child budget scope whose caps are reserved from the
        parent — child authority is a strict subset of the parent's."""
        child_id = scope_id or kernel_id("scp")

        def fn(conn):
            parent = conn.execute(
                "SELECT mission_id, status FROM budget_scopes WHERE"
                " scope_id=?",
                (parent_scope_id,),
            ).fetchone()
            if parent is None:
                raise KernelError(f"unknown scope {parent_scope_id!r}")
            if parent[1] != "open":
                raise KernelError(
                    f"scope {parent_scope_id} is {parent[1]}, not open"
                )
            # Reserving the child's full caps from the parent enforces the
            # subset rule; an over-ask aborts the whole transaction.
            self._reserve(
                conn, mission_id, parent_scope_id, lines, child_id,
                note="child scope",
            )
            self._append(conn, mission_id, "budget_scope_created", {
                "scope_id": child_id, "parent_scope_id": parent_scope_id,
                "lines": lines,
            })
            return child_id
        return self._mutate(fn)

    def close_child_scope(self, mission_id: str, scope_id: str,
                          note: str = "") -> Dict[str, int]:
        """Close a child scope: commit its actual usage against the parent's
        reservation and release the remainder."""
        def fn(conn):
            row = conn.execute(
                "SELECT parent_scope_id, status FROM budget_scopes WHERE"
                " scope_id=?",
                (scope_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown scope {scope_id!r}")
            parent_scope_id, status = row[0], row[1]
            if not parent_scope_id:
                raise KernelError(f"scope {scope_id} has no parent")
            if status != "open":
                raise KernelError(f"scope {scope_id} already {status}")
            usage_rows = conn.execute(
                "SELECT line, committed FROM budget_lines WHERE scope_id=?",
                (scope_id,),
            ).fetchall()
            actuals = {row[0]: int(row[1]) for row in usage_rows}
            self._commit_budget(
                conn, mission_id, parent_scope_id, scope_id, actuals,
                note=note or "child scope closed", closes_scope=scope_id,
            )
            return actuals
        return self._mutate(fn)

    def budget_status(self, scope_id: str) -> Dict[str, Dict[str, int]]:
        rows = self._read_conn().execute(
            "SELECT line, cap, reserved, committed FROM budget_lines WHERE"
            " scope_id=? ORDER BY line",
            (scope_id,),
        ).fetchall()
        return {
            row["line"]: {
                "cap": row["cap"], "reserved": row["reserved"],
                "committed": row["committed"],
                "available": row["cap"] - row["reserved"] - row["committed"],
            }
            for row in rows
        }

    # ------------------------------------------------------------------
    # Approvals (structured action + canonical-args hash + expiry + nonce)
    # ------------------------------------------------------------------

    def request_approval(self, mission_id: str, action_kind: str,
                         action_args: Dict[str, Any], *,
                         origin_channel: str = "local",
                         origin_thread: str = "", origin_sender: str = "",
                         ttl_seconds: float = 3600.0,
                         notify_dedupe_key: str = "",
                         notify_payload: Optional[Dict[str, Any]] = None,
                         ) -> Dict[str, str]:
        """Create a pending approval bound to its origin. Returns
        ``{"approval_id", "nonce", "args_hash"}`` — the nonce is one-use and
        must accompany the decision."""
        approval_id = kernel_id("apr")
        import secrets as _secrets
        nonce = _secrets.token_hex(8)
        args_hash = hashlib.sha256(
            _canonical(action_args).encode("ascii")
        ).hexdigest()

        def fn(conn):
            self._mission_row(conn, mission_id)
            expires_at = float(self.clock()) + float(ttl_seconds)
            self._append(conn, mission_id, "approval_requested", {
                "approval_id": approval_id, "action_kind": str(action_kind),
                "action_args": action_args, "args_hash": args_hash,
                "origin_channel": origin_channel,
                "origin_thread": origin_thread,
                "origin_sender": origin_sender,
                "nonce": nonce, "expires_at": expires_at,
            })
            if notify_payload is not None:
                self._enqueue_outbox(
                    conn, mission_id, "channel_notify", notify_payload,
                    notify_dedupe_key or f"approval:{approval_id}",
                )
            return {
                "approval_id": approval_id, "nonce": nonce,
                "args_hash": args_hash,
            }
        return self._mutate(fn)

    def decide_approval(self, approval_id: str, verb: str, *, nonce: str,
                        origin_channel: str, origin_thread: str = "",
                        origin_sender: str = "",
                        decided_by: str = "") -> Dict[str, Any]:
        """Atomically consume a pending approval.

        Rejections (all raise :class:`ApprovalError`, no state change except
        expiry): unknown id, already decided (nonce replay), expired, wrong
        origin, wrong nonce. A ``local`` decision origin — the operator at
        the shell — may decide channel-bound approvals; the decision origin
        is recorded either way.
        """
        if verb not in ("approve", "deny"):
            raise KernelError(f"unknown approval verb {verb!r}")

        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, action_kind, action_args, args_hash,"
                " origin_channel, origin_thread, origin_sender, nonce,"
                " status, expires_at FROM approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalError(f"no approval {approval_id!r}")
            (mission_id, action_kind, action_args, args_hash,
             o_channel, o_thread, o_sender, expected_nonce, status,
             expires_at) = row
            if status != ApprovalStatus.PENDING:
                raise ApprovalError(
                    f"approval {approval_id} already {status} — one-use"
                    " nonce consumed"
                )
            now = float(self.clock())
            if now > float(expires_at):
                # Mark expiry as a committed fact, then reject: raising here
                # would roll the expiry event back with the transaction.
                self._append(conn, mission_id, "approval_expired", {
                    "approval_id": approval_id,
                })
                return {"__expired__": approval_id}
            decision_origin = (origin_channel, origin_thread, origin_sender)
            bound_origin = (o_channel, o_thread, o_sender)
            if decision_origin != bound_origin and origin_channel != "local":
                raise ApprovalError(
                    f"approval {approval_id} is bound to a different origin"
                )
            if nonce != expected_nonce:
                raise ApprovalError(
                    f"approval {approval_id} nonce mismatch"
                )
            final = (
                ApprovalStatus.APPROVED if verb == "approve"
                else ApprovalStatus.DENIED
            )
            self._append(conn, mission_id, "approval_decided", {
                "approval_id": approval_id, "status": final,
                "decided_by": decided_by,
                "decision_origin": ":".join(decision_origin),
            })
            return {
                "approval_id": approval_id, "mission_id": mission_id,
                "status": final, "action_kind": action_kind,
                "action_args": action_args, "args_hash": args_hash,
            }
        result = self._mutate(fn)
        if "__expired__" in result:
            raise ApprovalError(f"approval {result['__expired__']} expired")
        return result

    def expire_approvals(self, now: Optional[float] = None) -> int:
        def fn(conn):
            current = float(now if now is not None else self.clock())
            rows = conn.execute(
                "SELECT approval_id, mission_id FROM approvals WHERE"
                " status=? AND expires_at < ? ORDER BY approval_id",
                (ApprovalStatus.PENDING, current),
            ).fetchall()
            for approval_id, mission_id in rows:
                self._append(conn, mission_id, "approval_expired", {
                    "approval_id": approval_id,
                })
            return len(rows)
        return self._mutate(fn)

    def pending_approvals(self) -> List[Dict[str, Any]]:
        rows = self._read_conn().execute(
            "SELECT * FROM approvals WHERE status=? ORDER BY created_at",
            (ApprovalStatus.PENDING,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def create_timer(self, mission_id: str, logical_key: str, due_at: float,
                     interval_seconds: int = 0,
                     misfire_policy: str = MisfirePolicy.COALESCE,
                     catch_up_limit: int = 5,
                     payload: Optional[Dict[str, Any]] = None) -> str:
        if misfire_policy not in MisfirePolicy.ALL:
            raise KernelError(f"unknown misfire policy {misfire_policy!r}")
        if interval_seconds < 0:
            raise KernelError("interval_seconds must not be negative")
        catch_up_limit = min(max(1, int(catch_up_limit)), CATCH_UP_HARD_CAP)
        timer_id = kernel_id("tmr")

        def fn(conn):
            self._mission_row(conn, mission_id)
            existing = conn.execute(
                "SELECT timer_id FROM timers WHERE mission_id=? AND"
                " logical_key=?",
                (mission_id, logical_key),
            ).fetchone()
            if existing is not None:
                raise KernelError(
                    f"timer with logical key {logical_key!r} already exists"
                    f" for mission {mission_id}"
                )
            self._append(conn, mission_id, "timer_created", {
                "timer_id": timer_id, "logical_key": logical_key,
                "generation": 1, "due_at": float(due_at),
                "interval_seconds": int(interval_seconds),
                "misfire_policy": misfire_policy,
                "catch_up_limit": catch_up_limit,
                "payload": payload or {"effect": "mission_wake"},
            })
            return timer_id
        return self._mutate(fn)

    def reschedule_timer(self, timer_id: str, due_at: float,
                         expected_generation: Optional[int] = None,
                         interval_seconds: Optional[int] = None) -> int:
        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, generation, interval_seconds, status"
                " FROM timers WHERE timer_id=?",
                (timer_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown timer {timer_id!r}")
            mission_id, generation, interval, status = row
            if status == "cancelled":
                raise KernelError(f"timer {timer_id} is cancelled")
            if (expected_generation is not None
                    and int(generation) != expected_generation):
                raise StaleGenerationError(
                    f"timer {timer_id} generation {generation} !="
                    f" expected {expected_generation}"
                )
            new_generation = int(generation) + 1
            self._append(conn, mission_id, "timer_rescheduled", {
                "timer_id": timer_id, "generation": new_generation,
                "due_at": float(due_at),
                "interval_seconds": int(
                    interval if interval_seconds is None else interval_seconds
                ),
            })
            return new_generation
        return self._mutate(fn)

    def cancel_timer(self, timer_id: str, reason: str = "") -> None:
        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, status FROM timers WHERE timer_id=?",
                (timer_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown timer {timer_id!r}")
            if row[1] == "cancelled":
                return
            self._append(conn, row[0], "timer_cancelled", {
                "timer_id": timer_id, "reason": reason,
            })
        self._mutate(fn)

    def claim_due_timers(self, holder: str, now: Optional[float] = None,
                         lease_seconds: float = 120.0,
                         limit: int = 16) -> List[Dict[str, Any]]:
        """Operationally claim due timers (no events; claims are leases, not
        truth). A claim on a timer whose previous claim expired is a normal
        takeover — the generation check at fire time fences stale holders."""
        def fn(conn):
            current = float(now if now is not None else self.clock())
            rows = conn.execute(
                "SELECT timer_id, mission_id, logical_key, generation,"
                " due_at, interval_seconds, misfire_policy, catch_up_limit,"
                " payload FROM timers WHERE status='active' AND due_at <= ?"
                " AND (claimed_by IS NULL OR claim_expires_at <= ?)"
                " ORDER BY due_at LIMIT ?",
                (current, current, int(limit)),
            ).fetchall()
            claims = []
            for row in rows:
                conn.execute(
                    "UPDATE timers SET claimed_by=?, claim_expires_at=?"
                    " WHERE timer_id=?",
                    (holder, current + float(lease_seconds), row[0]),
                )
                claims.append({
                    "timer_id": row[0], "mission_id": row[1],
                    "logical_key": row[2], "generation": int(row[3]),
                    "due_at": float(row[4]),
                    "interval_seconds": int(row[5]),
                    "misfire_policy": row[6],
                    "catch_up_limit": int(row[7]),
                    "payload": row[8],
                })
            return claims
        return self._mutate(fn)

    @staticmethod
    def _next_future_due(due_at: float, interval: int, now: float) -> float:
        if interval <= 0:
            return due_at
        elapsed = now - due_at
        steps = int(elapsed // interval) + 1
        return due_at + steps * interval

    def fire_timer(self, timer_id: str, expected_generation: int,
                   holder: str = "",
                   now: Optional[float] = None) -> Dict[str, Any]:
        """The exactly-once timer-fire transaction.

        Verifies generation (fencing stale claims), applies the misfire
        policy, appends one ``timer_fired`` event per fired occurrence,
        applies the timer's declared effect (``mission_wake``), and advances
        or completes the timer — all in one transaction. A crash before
        commit leaves the timer claimable again; a crash after commit leaves
        it advanced. Either way no occurrence fires twice.
        """
        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, logical_key, generation, due_at,"
                " interval_seconds, misfire_policy, catch_up_limit, payload,"
                " status, claimed_by, claim_expires_at FROM timers WHERE"
                " timer_id=?",
                (timer_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown timer {timer_id!r}")
            (mission_id, logical_key, generation, due_at, interval,
             misfire_policy, catch_up_limit, payload_json, status,
             claimed_by, claim_expires_at) = row
            if status != "active":
                raise StaleGenerationError(
                    f"timer {timer_id} is {status}, not active"
                )
            if int(generation) != int(expected_generation):
                raise StaleGenerationError(
                    f"timer {timer_id} generation {generation} !="
                    f" expected {expected_generation} — stale claim"
                )
            current = float(now if now is not None else self.clock())
            if holder and claimed_by and claimed_by != holder:
                if claim_expires_at is not None and (
                    float(claim_expires_at) > current
                ):
                    raise StaleGenerationError(
                        f"timer {timer_id} is claimed by {claimed_by!r}"
                    )
            due = float(due_at)
            interval = int(interval)
            if current < due:
                raise KernelError(f"timer {timer_id} is not due")
            late = (current - due) > MISFIRE_GRACE_SECONDS
            missed = 1
            if interval > 0:
                missed = int((current - due) // interval) + 1
            # Which scheduled occurrences actually fire:
            if misfire_policy == MisfirePolicy.SKIP and late:
                fire_times: List[float] = []
            elif misfire_policy == MisfirePolicy.CATCH_UP and interval > 0:
                count = min(missed, int(catch_up_limit), CATCH_UP_HARD_CAP)
                fire_times = [due + i * interval for i in range(count)]
            else:  # coalesce (and on-time skip / one-shots)
                fire_times = [due]
            payload = _json.loads(payload_json or "{}")
            final = interval <= 0
            fires = []
            for index, scheduled_for in enumerate(fire_times):
                is_last = index == len(fire_times) - 1
                self._append(conn, mission_id, "timer_fired", {
                    "timer_id": timer_id, "logical_key": logical_key,
                    "generation": int(generation),
                    "scheduled_for": scheduled_for,
                    "fired_at": current,
                    "coalesced": (
                        missed if misfire_policy == MisfirePolicy.COALESCE
                        else 1
                    ),
                    "final": bool(final and is_last),
                })
                fires.append(scheduled_for)
                self._apply_timer_effect(conn, mission_id, payload)
            mission_status = self._mission_row(conn, mission_id)[2]
            next_due = None
            if mission_status in MissionState.TERMINAL:
                # The mission can never run again; its timers die with it.
                self._append(conn, mission_id, "timer_cancelled", {
                    "timer_id": timer_id, "reason": "mission terminal",
                })
            elif interval > 0:
                next_due = self._next_future_due(due, interval, current)
                self._append(conn, mission_id, "timer_rescheduled", {
                    "timer_id": timer_id, "generation": int(generation) + 1,
                    "due_at": next_due, "interval_seconds": interval,
                })
            elif not fire_times:
                # one-shot skipped by misfire policy: it will never fire
                self._append(conn, mission_id, "timer_cancelled", {
                    "timer_id": timer_id, "reason": "misfire_skipped",
                })
            return {
                "timer_id": timer_id, "mission_id": mission_id,
                "fires": fires, "next_due_at": next_due,
                "skipped": not fires,
            }
        return self._mutate(fn)

    def _apply_timer_effect(self, conn: sqlite3.Connection, mission_id: str,
                            payload: Dict[str, Any]) -> None:
        effect = str(payload.get("effect") or "mission_wake")
        if effect == "mission_wake":
            row = self._mission_row(conn, mission_id)
            status = row[2]
            if status == MissionState.WAITING_TIMER:
                self._transition(
                    conn, mission_id, MissionState.READY, None,
                    reason="timer fired",
                )
            # ready/active/paused/waiting_*/terminal: waking is a no-op —
            # the mission is already runnable, running, or deliberately not.
        elif effect == "outbox":
            self._enqueue_outbox(
                conn, mission_id, str(payload.get("kind") or "channel_notify"),
                dict(payload.get("payload") or {}),
                str(payload.get("dedupe_key") or kernel_id("obx")),
            )
        else:
            raise KernelError(f"unknown timer effect {effect!r}")

    def get_timer(self, timer_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM timers WHERE timer_id=?", (timer_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_timer(self, mission_id: str,
                   logical_key: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM timers WHERE mission_id=? AND logical_key=?",
            (mission_id, logical_key),
        ).fetchone()
        return dict(row) if row else None

    def list_timers(self, mission_id: str = "",
                    status: str = "") -> List[Dict[str, Any]]:
        query = "SELECT * FROM timers"
        clauses, params = [], []
        if mission_id:
            clauses.append("mission_id=?")
            params.append(mission_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY due_at"
        rows = self._read_conn().execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Leases (operational coordination, never mission truth)
    # ------------------------------------------------------------------

    def acquire_lease(self, kind: str, resource: str, holder: str,
                      seconds: float,
                      now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        def fn(conn):
            current = float(now if now is not None else self.clock())
            row = conn.execute(
                "SELECT lease_id, holder, expires_at, fencing_token FROM"
                " leases WHERE kind=? AND resource=?",
                (kind, resource),
            ).fetchone()
            if row is not None and float(row[2]) > current and (
                row[1] != holder
            ):
                return None
            fencing = (int(row[3]) if row is not None else 0) + 1
            lease_id = row[0] if row is not None else kernel_id("lse")
            epoch = self._epoch if self._epoch is not None else 0
            if row is None:
                conn.execute(
                    "INSERT INTO leases(lease_id, kind, resource, holder,"
                    " epoch, fencing_token, granted_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (lease_id, kind, resource, holder, epoch, fencing,
                     current, current + float(seconds)),
                )
            else:
                conn.execute(
                    "UPDATE leases SET holder=?, epoch=?, fencing_token=?,"
                    " granted_at=?, expires_at=? WHERE lease_id=?",
                    (holder, epoch, fencing, current,
                     current + float(seconds), lease_id),
                )
            return {
                "lease_id": lease_id, "kind": kind, "resource": resource,
                "holder": holder, "fencing_token": fencing,
                "expires_at": current + float(seconds),
            }
        return self._mutate(fn)

    def release_lease(self, kind: str, resource: str, holder: str) -> bool:
        def fn(conn):
            cursor = conn.execute(
                "DELETE FROM leases WHERE kind=? AND resource=? AND"
                " holder=?",
                (kind, resource, holder),
            )
            return cursor.rowcount > 0
        return self._mutate(fn)

    def get_lease(self, kind: str, resource: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM leases WHERE kind=? AND resource=?",
            (kind, resource),
        ).fetchone()
        return dict(row) if row else None

    def expired_leases(self,
                       now: Optional[float] = None) -> List[Dict[str, Any]]:
        current = float(now if now is not None else self.clock())
        rows = self._read_conn().execute(
            "SELECT * FROM leases WHERE expires_at <= ?", (current,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # External actions (idempotency + query-before-retry)
    # ------------------------------------------------------------------

    def record_action(self, mission_id: str, action_class: str,
                      idempotency_key: str, task_id: str = "",
                      detail: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
        """Record an intended external action. Idempotent on the key: a
        repeat returns the existing row (``duplicate=True``) with no event —
        the caller must not perform the side effect again."""
        if action_class not in ActionClass.ALL:
            raise KernelError(f"unknown action class {action_class!r}")
        if not str(idempotency_key).strip():
            raise KernelError("idempotency_key is required")
        action_id = kernel_id("act")

        def fn(conn):
            existing = conn.execute(
                "SELECT action_id, status FROM external_actions WHERE"
                " idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return {
                    "action_id": existing[0], "status": existing[1],
                    "duplicate": True,
                }
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "action_recorded", {
                "action_id": action_id, "task_id": task_id,
                "action_class": action_class,
                "idempotency_key": idempotency_key,
                "detail": detail or {},
            })
            return {
                "action_id": action_id, "status": ActionStatus.PENDING,
                "duplicate": False,
            }
        return self._mutate(fn)

    def resolve_action(self, action_id: str, status: str,
                       detail: Optional[Dict[str, Any]] = None) -> None:
        """Resolve an action outcome. ``unknown`` may later be re-resolved
        after querying the external system (query-before-retry); committed
        and failed are final."""
        if status not in (ActionStatus.COMMITTED, ActionStatus.FAILED,
                          ActionStatus.UNKNOWN):
            raise KernelError(f"invalid action resolution {status!r}")

        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, status FROM external_actions WHERE"
                " action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown action {action_id!r}")
            mission_id, current = row
            if current in (ActionStatus.COMMITTED, ActionStatus.FAILED):
                raise KernelError(
                    f"action {action_id} already resolved as {current} —"
                    " reconcile by querying, never blind-retry"
                )
            self._append(conn, mission_id, "action_resolved", {
                "action_id": action_id, "status": status,
                "detail": detail or {},
            })
        self._mutate(fn)

    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM external_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_action(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM external_actions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Inbox / outbox
    # ------------------------------------------------------------------

    def receive_inbox(self, source: str, idempotency_key: str,
                      payload: Dict[str, Any],
                      mission_id: str = "") -> Dict[str, Any]:
        def fn(conn):
            existing = conn.execute(
                "SELECT inbox_id, status FROM inbox WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return {
                    "inbox_id": existing[0], "status": existing[1],
                    "duplicate": True,
                }
            self._append(conn, mission_id, "inbox_received", {
                "source": source, "idempotency_key": idempotency_key,
                "payload": payload,
            })
            row = conn.execute(
                "SELECT inbox_id FROM inbox WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return {
                "inbox_id": row[0], "status": InboxStatus.PENDING,
                "duplicate": False,
            }
        return self._mutate(fn)

    def mark_inbox_processed(self, idempotency_key: str, ok: bool = True,
                             detail: str = "") -> None:
        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, status FROM inbox WHERE"
                " idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown inbox item {idempotency_key!r}")
            if row[1] != InboxStatus.PENDING:
                raise KernelError(
                    f"inbox item {idempotency_key!r} already {row[1]}"
                )
            self._append(conn, row[0], "inbox_processed", {
                "idempotency_key": idempotency_key,
                "status": (
                    InboxStatus.PROCESSED if ok else InboxStatus.FAILED
                ),
                "detail": str(detail),
            })
        self._mutate(fn)

    def enqueue_outbox(self, kind: str, payload: Dict[str, Any],
                       dedupe_key: str, mission_id: str = "") -> bool:
        """Insert an outbox message (idempotent on dedupe_key). Returns
        False when the key already exists — the effect was already queued."""
        return self._mutate(
            lambda conn: self._enqueue_outbox(
                conn, mission_id, kind, payload, dedupe_key
            )
        )

    def claim_deliverable_outbox(self, now: Optional[float] = None,
                                 limit: int = 16) -> List[Dict[str, Any]]:
        def fn(conn):
            current = float(now if now is not None else self.clock())
            rows = conn.execute(
                "SELECT outbox_id, kind, mission_id, payload, dedupe_key,"
                " attempts FROM outbox WHERE status='pending' AND"
                " next_attempt_at <= ? ORDER BY outbox_id LIMIT ?",
                (current, int(limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                backoff = min(
                    OUTBOX_BASE_BACKOFF * (2 ** int(row[5])),
                    OUTBOX_MAX_BACKOFF,
                )
                conn.execute(
                    "UPDATE outbox SET next_attempt_at=?, attempts=attempts+1"
                    " WHERE outbox_id=?",
                    (current + backoff, row[0]),
                )
                claimed.append({
                    "outbox_id": row[0], "kind": row[1],
                    "mission_id": row[2], "payload": row[3],
                    "dedupe_key": row[4], "attempts": int(row[5]) + 1,
                })
            return claimed
        return self._mutate(fn)

    def mark_outbox_delivered(self, outbox_id: int, transport: str,
                              now: Optional[float] = None) -> bool:
        """Mark delivery (operational). Returns False if it was already
        delivered — the redelivery race loses and must not re-effect."""
        def fn(conn):
            current = float(now if now is not None else self.clock())
            cursor = conn.execute(
                "UPDATE outbox SET status='delivered', transport=?,"
                " delivered_at=?, last_error='' WHERE outbox_id=? AND"
                " status='pending'",
                (transport, current, int(outbox_id)),
            )
            return cursor.rowcount > 0
        return self._mutate(fn)

    def mark_outbox_failed(self, outbox_id: int, error: str,
                           permanent: bool = False) -> None:
        def fn(conn):
            if permanent:
                conn.execute(
                    "UPDATE outbox SET status='failed', last_error=? WHERE"
                    " outbox_id=? AND status='pending'",
                    (str(error)[:500], int(outbox_id)),
                )
            else:
                conn.execute(
                    "UPDATE outbox SET last_error=? WHERE outbox_id=? AND"
                    " status='pending'",
                    (str(error)[:500], int(outbox_id)),
                )
        self._mutate(fn)

    def list_outbox(self, status: str = "",
                    mission_id: str = "") -> List[Dict[str, Any]]:
        query = "SELECT * FROM outbox"
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if mission_id:
            clauses.append("mission_id=?")
            params.append(mission_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY outbox_id"
        rows = self._read_conn().execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Artifacts / resource bindings
    # ------------------------------------------------------------------

    def record_artifact(self, mission_id: str, name: str,
                        content: Optional[str] = None,
                        digest: str = "", size: int = -1,
                        task_id: str = "") -> str:
        if content is not None:
            raw = content.encode("utf-8")
            if len(raw) > ARTIFACT_INLINE_MAX_BYTES:
                raise KernelError(
                    "artifact too large to inline — store it externally and"
                    " record its digest"
                )
            digest = hashlib.sha256(raw).hexdigest()
            size = len(raw)
        if not digest or size < 0:
            raise KernelError(
                "artifact requires content, or an explicit digest and size"
            )
        artifact_id = kernel_id("art")

        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "artifact_recorded", {
                "artifact_id": artifact_id, "task_id": task_id,
                "name": str(name), "digest": digest, "size": int(size),
                "content": content,
            })
            return artifact_id
        return self._mutate(fn)

    def record_binding(self, mission_id: str, kind: str,
                       resource: Dict[str, Any], *,
                       task_id: str = "",
                       status: str = BindingStatus.ACTIVE,
                       cursor: int = 0,
                       detail: Optional[Dict[str, Any]] = None) -> str:
        """Bind an external resource to a mission (event-sourced).

        ``resource`` carries the external identifiers — for a Capitol run
        binding: org/agent/workflow/version ids, context_id, run_id,
        session_id, idempotency key. ``cursor`` is the resumable
        event-sequence high-water mark the supervisor advances through
        :meth:`update_binding`; ``detail`` holds mutable supervision
        state (backoff, last error, HITL linkage).
        """
        if status not in BindingStatus.ALL:
            raise KernelError(f"unknown binding status {status!r}")
        if int(cursor) < 0:
            raise KernelError("binding cursor must not be negative")
        binding_id = kernel_id("bnd")

        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "binding_recorded", {
                "binding_id": binding_id, "binding_kind": str(kind),
                "resource": resource,
                "task_id": str(task_id),
                "status": status,
                "cursor": int(cursor),
                "detail": detail or {},
            })
            return binding_id
        return self._mutate(fn)

    def update_binding(self, binding_id: str, *,
                       status: Optional[str] = None,
                       cursor: Optional[int] = None,
                       detail: Optional[Dict[str, Any]] = None) -> None:
        """Advance a binding's lifecycle (event-sourced, single event).

        The cursor is monotonic — moving it backwards is refused, so a
        crashed/replayed supervisor pass can never lose progress. A
        terminal binding accepts no further updates.
        """
        if status is not None and status not in BindingStatus.ALL:
            raise KernelError(f"unknown binding status {status!r}")

        def fn(conn):
            row = conn.execute(
                'SELECT mission_id, status, "cursor", detail FROM'
                " resource_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown binding {binding_id!r}")
            mission_id, current_status, current_cursor, current_detail = row
            if current_status in BindingStatus.TERMINAL:
                raise KernelError(
                    f"binding {binding_id} is terminal ({current_status})"
                    " — no further updates"
                )
            fields: Dict[str, Any] = {}
            if status is not None and status != current_status:
                fields["status"] = status
            if cursor is not None:
                if int(cursor) < int(current_cursor):
                    raise KernelError(
                        f"binding {binding_id} cursor is monotonic:"
                        f" {cursor} < {current_cursor}"
                    )
                if int(cursor) != int(current_cursor):
                    fields["cursor"] = int(cursor)
            if detail is not None and _canonical(detail) != current_detail:
                fields["detail"] = detail
            if not fields:
                return  # no-op: nothing changed, no event appended
            self._append(conn, mission_id, "binding_updated", {
                "binding_id": binding_id, "fields": fields,
            })
        self._mutate(fn)

    @staticmethod
    def _binding_dict(row) -> Dict[str, Any]:
        binding = dict(row)
        for key in ("resource", "detail"):
            try:
                binding[key] = _json.loads(binding.get(key) or "{}")
            except ValueError:
                binding[key] = {}
        return binding

    def get_binding(self, binding_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM resource_bindings WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        return self._binding_dict(row) if row else None

    def find_bindings(self, *, kind: str = "", mission_id: str = "",
                      status: str = "",
                      statuses: Optional[Any] = None
                      ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM resource_bindings"
        clauses: List[str] = []
        params: List[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if mission_id:
            clauses.append("mission_id=?")
            params.append(mission_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if statuses:
            wanted = sorted(str(item) for item in statuses)
            clauses.append(
                "status IN (%s)" % ",".join("?" for _ in wanted)
            )
            params.extend(wanted)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, binding_id"
        rows = self._read_conn().execute(query, params).fetchall()
        return [self._binding_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Fleet: worker registry (Swarm Phase 2)
    #
    # Worker truth (identity, admin trust/data labels, observed
    # capabilities, states, digests, protocol range) is event-sourced
    # under the "" mission chain and fully replayed. Heartbeats are
    # operational coordination, never events.
    # ------------------------------------------------------------------

    def enroll_worker(self, name: str, host: str, *, ssh_user: str = "",
                      ssh_port: Optional[int] = None,
                      trust_level: int = 0,
                      data_ceiling: str = DataClassification.INTERNAL,
                      labels: Optional[Dict[str, Any]] = None,
                      capabilities: Optional[Dict[str, Any]] = None,
                      runtime_profile: str = "",
                      profiles: Optional[List[str]] = None,
                      resource_group: str = "",
                      max_concurrency: int = 1,
                      protocol_min: int = 1, protocol_max: int = 1,
                      autonomy_capable: bool = False,
                      worker_id: Optional[str] = None) -> str:
        name = str(name or "").strip()
        if not name:
            raise KernelError("worker name is required")
        if data_ceiling not in DataClassification.ALL:
            raise KernelError(f"unknown data ceiling {data_ceiling!r}")
        if int(max_concurrency) < 1:
            raise KernelError("max_concurrency must be at least 1")
        wid = worker_id or kernel_id("wrk")

        def fn(conn):
            existing = conn.execute(
                "SELECT worker_id FROM workers WHERE name=?", (name,)
            ).fetchone()
            if existing is not None:
                raise KernelError(
                    f"worker name {name!r} is already enrolled"
                    f" ({existing[0]})"
                )
            self._append(conn, "", "worker_enrolled", {
                "worker_id": wid, "name": name, "host": str(host),
                "ssh_user": str(ssh_user or ""),
                "ssh_port": int(ssh_port) if ssh_port else None,
                "state": WorkerState.PENDING,
                "trust_level": int(trust_level),
                "data_ceiling": data_ceiling,
                "labels": labels or {},
                "capabilities": capabilities or {},
                "runtime_profile": str(runtime_profile),
                "profiles": list(profiles or []),
                "resource_group": str(resource_group),
                "max_concurrency": int(max_concurrency),
                "artifact_digest": "", "config_digest": "",
                "protocol_min": int(protocol_min),
                "protocol_max": int(protocol_max),
                "incarnation": 0,
                "autonomy_capable": bool(autonomy_capable),
                "version": 1,
            })
            return wid
        return self._mutate(fn)

    def _worker_row(self, conn: sqlite3.Connection, worker_id: str):
        row = conn.execute(
            "SELECT worker_id, state, version FROM workers WHERE"
            " worker_id=?",
            (worker_id,),
        ).fetchone()
        if row is None:
            raise KernelError(f"unknown worker {worker_id!r}")
        return row

    def update_worker(self, worker_id: str, fields: Dict[str, Any],
                      expected_version: Optional[int] = None) -> int:
        unknown = set(fields) - WORKER_UPDATABLE_FIELDS
        if unknown:
            raise KernelError(
                f"worker fields {sorted(unknown)} are not updatable —"
                " failing closed"
            )
        if "data_ceiling" in fields and (
            fields["data_ceiling"] not in DataClassification.ALL
        ):
            raise KernelError(
                f"unknown data ceiling {fields['data_ceiling']!r}"
            )
        if not fields:
            raise KernelError("update_worker needs at least one field")

        def fn(conn):
            row = self._worker_row(conn, worker_id)
            version = int(row[2])
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"worker {worker_id} version {version} !="
                    f" expected {expected_version}"
                )
            new_version = version + 1
            self._append(conn, "", "worker_updated", {
                "worker_id": worker_id, "fields": fields,
                "version": new_version,
            })
            return new_version
        return self._mutate(fn)

    def transition_worker(self, worker_id: str, target: str,
                          reason: str = "",
                          expected_version: Optional[int] = None) -> int:
        def fn(conn):
            row = self._worker_row(conn, worker_id)
            current, version = row[1], int(row[2])
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"worker {worker_id} version {version} !="
                    f" expected {expected_version}"
                )
            check_worker_transition(current, target)
            new_version = version + 1
            self._append(conn, "", "worker_transitioned", {
                "worker_id": worker_id, "from": current, "to": target,
                "reason": str(reason), "version": new_version,
            })
            return new_version
        return self._mutate(fn)

    def record_worker_heartbeat(self, worker_id: str, seq: int,
                                now: Optional[float] = None) -> bool:
        """Operational heartbeat record (no event). Returns True when the
        sequence advanced; a lower sequence still stamps the time (it
        signals a restarted worker — the plane handles incarnations)."""
        def fn(conn):
            self._worker_row(conn, worker_id)
            current = float(now if now is not None else self.clock())
            row = conn.execute(
                "SELECT heartbeat_seq FROM workers WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
            advanced = int(seq) > int(row[0])
            conn.execute(
                "UPDATE workers SET heartbeat_seq=?, last_heartbeat_at=?"
                " WHERE worker_id=?",
                (max(int(seq), int(row[0])), current, worker_id),
            )
            return advanced
        return self._mutate(fn)

    @staticmethod
    def _worker_dict(row) -> Dict[str, Any]:
        data = dict(row)
        data["labels"] = _json.loads(data["labels"])
        data["capabilities"] = _json.loads(data["capabilities"])
        data["profiles"] = _json.loads(data["profiles"]).get("list", [])
        data["autonomy_capable"] = bool(data["autonomy_capable"])
        return data

    def get_worker(self, worker_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
        ).fetchone()
        return self._worker_dict(row) if row else None

    def find_worker(self, name: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM workers WHERE name=?", (name,)
        ).fetchone()
        return self._worker_dict(row) if row else None

    def list_workers(self, state: str = "") -> List[Dict[str, Any]]:
        if state:
            rows = self._read_conn().execute(
                "SELECT * FROM workers WHERE state=? ORDER BY name",
                (state,),
            ).fetchall()
        else:
            rows = self._read_conn().execute(
                "SELECT * FROM workers ORDER BY name"
            ).fetchall()
        return [self._worker_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Fleet: dispatches (distributed task plane truth)
    # ------------------------------------------------------------------

    def create_dispatch(self, envelope: Dict[str, Any],
                        max_attempts: int = 3) -> str:
        """Persist a new dispatch from a validated TaskEnvelope dict."""
        try:
            validated = TaskEnvelope.from_dict(dict(envelope))
        except ProtocolError as exc:
            raise KernelError(f"invalid task envelope: {exc}")
        if int(max_attempts) < 1:
            raise KernelError("max_attempts must be at least 1")
        payload = validated.to_dict()

        def fn(conn):
            self._mission_row(conn, validated.mission_id)
            existing = conn.execute(
                "SELECT task_id FROM dispatches WHERE task_id=?",
                (validated.task_id,),
            ).fetchone()
            if existing is not None:
                raise KernelError(
                    f"dispatch {validated.task_id} already exists"
                )
            self._append(conn, validated.mission_id, "dispatch_created", {
                "envelope": payload, "state": DispatchState.QUEUED,
                "max_attempts": int(max_attempts), "version": 1,
            })
            return validated.task_id
        return self._mutate(fn)

    def transition_dispatch(self, task_id: str, target: str, *,
                            expected_version: Optional[int] = None,
                            attempt: Optional[int] = None,
                            worker_id: Optional[str] = None,
                            fence: Optional[int] = None,
                            not_before: Optional[float] = None,
                            failure_class: str = "",
                            result: Optional[Dict[str, Any]] = None,
                            error: str = "", reason: str = "") -> int:
        """One journaled dispatch state change. Unspecified attempt/worker/
        fence fields carry forward; a terminal state is final forever."""
        def fn(conn):
            row = conn.execute(
                "SELECT mission_id, state, version, attempt, worker_id,"
                " fence, not_before, result FROM dispatches WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KernelError(f"unknown dispatch {task_id!r}")
            (mission_id, current, version, cur_attempt, cur_worker,
             cur_fence, cur_not_before, cur_result) = row
            if expected_version is not None and (
                int(version) != expected_version
            ):
                raise ConflictError(
                    f"dispatch {task_id} version {version} !="
                    f" expected {expected_version}"
                )
            check_dispatch_transition(current, target)
            new_version = int(version) + 1
            self._append(conn, mission_id, "dispatch_transitioned", {
                "task_id": task_id, "from": current, "to": target,
                "version": new_version,
                "attempt": int(
                    attempt if attempt is not None else cur_attempt
                ),
                "worker_id": (
                    worker_id if worker_id is not None else cur_worker
                ),
                "fence": int(fence if fence is not None else cur_fence),
                "not_before": float(
                    not_before if not_before is not None else cur_not_before
                ),
                "failure_class": failure_class,
                "result": (
                    result if result is not None
                    else _json.loads(cur_result or "{}")
                ),
                "error": str(error), "reason": str(reason),
            })
            return new_version
        return self._mutate(fn)

    def get_dispatch(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM dispatches WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["envelope"] = _json.loads(data["envelope"])
        data["result"] = _json.loads(data["result"] or "{}")
        return data

    def list_dispatches(self, state: str = "", mission_id: str = "",
                        worker_id: str = "",
                        parent_task_id: str = "") -> List[Dict[str, Any]]:
        query = "SELECT * FROM dispatches"
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if mission_id:
            clauses.append("mission_id=?")
            params.append(mission_id)
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if parent_task_id:
            clauses.append("parent_task_id=?")
            params.append(parent_task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        rows = self._read_conn().execute(query, params).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["envelope"] = _json.loads(data["envelope"])
            data["result"] = _json.loads(data["result"] or "{}")
            result.append(data)
        return result

    def count_worker_dispatches(self, worker_id: str) -> int:
        """In-flight dispatches assigned to one worker (capacity checks)."""
        row = self._read_conn().execute(
            "SELECT COUNT(*) FROM dispatches WHERE worker_id=? AND state"
            " IN (?,?,?)",
            (worker_id, DispatchState.OFFERING, DispatchState.RUNNING,
             DispatchState.WAITING_CHILD),
        ).fetchone()
        return int(row[0])

    def record_dispatch_events(self, events: List[Dict[str, Any]]) -> int:
        """Persist worker-observed task events. Idempotent on
        (task_id, attempt, seq): redelivered batches insert nothing new —
        this is what makes at-least-once event delivery safe."""
        def fn(conn):
            inserted = 0
            now = float(self.clock())
            for event in events:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO dispatch_events(task_id,"
                    " attempt, seq, kind, payload, failure_class,"
                    " received_at) VALUES (?,?,?,?,?,?,?)",
                    (str(event["task_id"]), int(event["attempt"]),
                     int(event["sequence"]), str(event["kind"]),
                     _canonical(event.get("payload", {})),
                     str(event.get("failure_class", "")), now),
                )
                inserted += cursor.rowcount
            return inserted
        return self._mutate(fn)

    def list_dispatch_events(self, task_id: str,
                             attempt: Optional[int] = None,
                             since_seq: int = -1) -> List[Dict[str, Any]]:
        query = ("SELECT * FROM dispatch_events WHERE task_id=? AND seq>?")
        params: List[Any] = [task_id, int(since_seq)]
        if attempt is not None:
            query += " AND attempt=?"
            params.append(int(attempt))
        query += " ORDER BY attempt, seq"
        rows = self._read_conn().execute(query, params).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["payload"] = _json.loads(data["payload"] or "{}")
            result.append(data)
        return result

    # ------------------------------------------------------------------
    # Personal items (personal-items plan P1)
    # ------------------------------------------------------------------
    #
    # Durable user records — todos, recipes, paper ideas — in named
    # spaces, under the exact mission discipline: every mutation is one
    # immutable event on the item's own chain applied to the projection
    # in the same transaction. Item content is personal and local-only:
    # it never feeds memory consolidation, never reaches Capitol or any
    # org surface, and is stored text — never executed or parsed as
    # instructions. The credential write-guard applies to every item
    # write (whole-entry rejection, same as memory).

    @staticmethod
    def _guard_item_content(title: Any, body: Any, tags: Any) -> None:
        findings: List[str] = []
        for chunk in (title, body, " ".join(tags or [])):
            for label in credential_findings(str(chunk or "")):
                if label not in findings:
                    findings.append(label)
        if findings:
            raise CredentialRejected(findings)

    def add_item(self, title: str, *, space: str = "", body: str = "",
                 due_at: Optional[float] = None, priority: Any = None,
                 tags: Any = None, source: str = "chat",
                 actor: str = "user",
                 item_id: Optional[str] = None) -> Dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise KernelError("item title is required")
        space = normalize_space(space)
        priority = normalize_item_priority(priority)
        tags = normalize_item_tags(tags)
        if due_at is not None:
            due_at = float(due_at)
        self._guard_item_content(title, body, tags)
        iid = item_id or kernel_id("item")

        def fn(conn):
            self._append(conn, iid, "item_added", {
                "item_id": iid, "space": space, "title": title,
                "body": str(body or ""), "status": ItemStatus.OPEN,
                "due_at": due_at, "priority": priority, "tags": tags,
                "source": str(source or "chat"), "mission_id": "",
                "actor": str(actor or "user"), "version": 1,
            })
            return iid
        self._mutate(fn)
        return self.get_item(iid)  # committed above; never None

    def _item_row(self, conn: sqlite3.Connection, item_id: str):
        row = conn.execute(
            "SELECT item_id, status, version, mission_id FROM items"
            " WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KernelError(f"unknown item {item_id!r}")
        return row

    def update_item(self, item_id: str, fields: Dict[str, Any], *,
                    actor: str = "user", source: str = "chat",
                    expected_version: Optional[int] = None) -> int:
        unknown = set(fields) - ITEM_UPDATABLE_FIELDS
        if unknown:
            raise KernelError(
                f"item fields {sorted(unknown)} are not updatable —"
                " failing closed"
            )
        if not fields:
            raise KernelError("update_item needs at least one field")
        clean: Dict[str, Any] = {}
        for key, value in fields.items():
            if key == "title":
                value = str(value or "").strip()
                if not value:
                    raise KernelError("item title cannot be empty")
            elif key == "body":
                value = str(value or "")
            elif key == "due_at":
                value = float(value) if value is not None else None
            elif key == "priority":
                value = normalize_item_priority(value)
            elif key == "tags":
                value = normalize_item_tags(value)
            elif key == "space":
                value = normalize_space(value)
            elif key == "status" and value != ItemStatus.OPEN:
                # done/archived ride their own events with their own
                # provenance; an update may only reopen.
                raise KernelError(
                    "update_item may only set status='open' (reopen) —"
                    " use complete_item / archive_item"
                )
            clean[key] = value
        self._guard_item_content(
            clean.get("title", ""), clean.get("body", ""),
            clean.get("tags", []),
        )

        def fn(conn):
            row = self._item_row(conn, item_id)
            status, version = row[1], int(row[2])
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"item {item_id} version {version} !="
                    f" expected {expected_version}"
                )
            if "status" in clean:
                check_item_transition(status, clean["status"])
            new_version = version + 1
            self._append(conn, item_id, "item_updated", {
                "item_id": item_id, "fields": clean,
                "version": new_version, "actor": str(actor or "user"),
                "source": str(source or "chat"),
            })
            return new_version
        return self._mutate(fn)

    def _finish_item(self, item_id: str, kind: str, target: str,
                     actor: str, source: str,
                     expected_version: Optional[int]) -> int:
        def fn(conn):
            row = self._item_row(conn, item_id)
            status, version = row[1], int(row[2])
            if expected_version is not None and version != expected_version:
                raise ConflictError(
                    f"item {item_id} version {version} !="
                    f" expected {expected_version}"
                )
            check_item_transition(status, target)
            new_version = version + 1
            self._append(conn, item_id, kind, {
                "item_id": item_id, "from": status,
                "version": new_version, "actor": str(actor or "user"),
                "source": str(source or "chat"),
            })
            return new_version
        return self._mutate(fn)

    def complete_item(self, item_id: str, *, actor: str = "user",
                      source: str = "chat",
                      expected_version: Optional[int] = None) -> int:
        return self._finish_item(
            item_id, "item_completed", ItemStatus.DONE, actor, source,
            expected_version,
        )

    def archive_item(self, item_id: str, *, actor: str = "user",
                     source: str = "chat",
                     expected_version: Optional[int] = None) -> int:
        return self._finish_item(
            item_id, "item_archived", ItemStatus.ARCHIVED, actor, source,
            expected_version,
        )

    def escalate_item(self, item_id: str, mission_id: str, *,
                      actor: str = "user") -> int:
        """Bind an item to the mission its escalation created. One live
        link per item; the mission must exist in this kernel."""
        def fn(conn):
            row = self._item_row(conn, item_id)
            status, version, linked = row[1], int(row[2]), row[3]
            if status != ItemStatus.OPEN:
                raise KernelError(
                    f"item {item_id} is {status}; only open items escalate"
                )
            if linked:
                raise KernelError(
                    f"item {item_id} is already escalated to {linked}"
                )
            self._mission_row(conn, mission_id)
            new_version = version + 1
            self._append(conn, item_id, "item_escalated", {
                "item_id": item_id, "mission_id": mission_id,
                "version": new_version, "actor": str(actor or "user"),
            })
            return new_version
        return self._mutate(fn)

    # -- item queries (deterministic; ties always break on item_id) --------

    @staticmethod
    def _item_dict(row) -> Dict[str, Any]:
        data = dict(row)
        data["tags"] = _json.loads(data["tags"]).get("list", [])
        return data

    _ITEM_SELECT = (
        "SELECT i.*, (SELECT MIN(seq) FROM mission_events e WHERE"
        " e.mission_id = i.item_id) AS item_seq FROM items i"
    )

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            self._ITEM_SELECT + " WHERE i.item_id=?", (item_id,)
        ).fetchone()
        return self._item_dict(row) if row else None

    def resolve_item(self, ref: str) -> Optional[Dict[str, Any]]:
        """Item from a full id, unique id prefix, or #<seq> alias (the
        seq of the item's first event — same scheme as mission task ids).
        None when nothing (or more than one thing) matches."""
        ref = str(ref or "").strip().lstrip("#")
        if not ref:
            return None
        if ref.isdigit():
            row = self._read_conn().execute(
                "SELECT mission_id FROM mission_events WHERE seq=?",
                (int(ref),),
            ).fetchone()
            if row is None:
                return None
            return self.get_item(row[0])
        exact = self.get_item(ref)
        if exact is not None:
            return exact
        rows = self._read_conn().execute(
            "SELECT item_id FROM items WHERE item_id LIKE ? ESCAPE '\\'"
            " LIMIT 2",
            (ref.replace("\\", "\\\\").replace("%", r"\%")
             .replace("_", r"\_") + "%",),
        ).fetchall()
        if len(rows) != 1:
            return None
        return self.get_item(rows[0][0])

    def list_items(self, space: str = "", status: str = ItemStatus.OPEN,
                   tag: str = "",
                   limit: int = 500) -> List[Dict[str, Any]]:
        """Items ordered by creation (created_at, then item_id). ``status``
        may be one status, "" or "all" for every status."""
        query = self._ITEM_SELECT
        conditions: List[str] = []
        params: List[Any] = []
        if space:
            conditions.append("i.space=?")
            params.append(normalize_space(space))
        if status and status != "all":
            if status not in ItemStatus.ALL:
                raise KernelError(f"unknown item status {status!r}")
            conditions.append("i.status=?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY i.created_at, i.item_id LIMIT ?"
        params.append(int(limit))
        rows = self._read_conn().execute(query, params).fetchall()
        items = [self._item_dict(row) for row in rows]
        if tag:
            wanted = normalize_item_tags([tag])
            items = [
                item for item in items
                if all(t in item["tags"] for t in wanted)
            ]
        return items

    def search_items(self, text: str, space: str = "",
                     limit: int = 50) -> List[Dict[str, Any]]:
        """Substring search over title and body (case-insensitive via
        LIKE), all statuses, creation order."""
        needle = str(text or "").strip()
        if not needle:
            return []
        pattern = "%" + (
            needle.replace("\\", "\\\\").replace("%", r"\%")
            .replace("_", r"\_")
        ) + "%"
        query = self._ITEM_SELECT + (
            " WHERE (i.title LIKE ? ESCAPE '\\' OR i.body LIKE ?"
            " ESCAPE '\\')"
        )
        params: List[Any] = [pattern, pattern]
        if space:
            query += " AND i.space=?"
            params.append(normalize_space(space))
        query += " ORDER BY i.created_at, i.item_id LIMIT ?"
        params.append(int(limit))
        rows = self._read_conn().execute(query, params).fetchall()
        return [self._item_dict(row) for row in rows]

    def find_items_by_mission(self,
                              mission_id: str) -> List[Dict[str, Any]]:
        rows = self._read_conn().execute(
            self._ITEM_SELECT + " WHERE i.mission_id=?"
            " ORDER BY i.created_at, i.item_id",
            (mission_id,),
        ).fetchall()
        return [self._item_dict(row) for row in rows]

    def list_item_spaces(self) -> List[Dict[str, Any]]:
        rows = self._read_conn().execute(
            "SELECT space, COUNT(*) AS total,"
            " SUM(CASE WHEN status=? THEN 1 ELSE 0 END) AS open"
            " FROM items GROUP BY space ORDER BY space",
            (ItemStatus.OPEN,),
        ).fetchall()
        return [
            {"space": row["space"], "total": int(row["total"]),
             "open": int(row["open"] or 0)}
            for row in rows
        ]

    def item_events(self, item_id: str,
                    limit: int = 100) -> List[Dict[str, Any]]:
        """The item's full event history (its chain id is the item id)."""
        return self.event_tail(item_id, limit=limit)

    # ------------------------------------------------------------------
    # Process compilations (process-compiler plan C1/C2). The compilation
    # aggregate is event-sourced under its own chain id; the store owns
    # structural invariants (status machine, approval origin binding,
    # secret guard); semantic card validation is the compiler's job
    # (conch.capitol.compiler.card) and happens before anything lands
    # here.
    # ------------------------------------------------------------------

    @staticmethod
    def _guard_card(card: Dict[str, Any]) -> str:
        """Minimal structural check + credential guard; returns the
        card's canonical digest. Cards are data about infrastructure —
        secret bytes never belong in one (whole-card rejection)."""
        if not isinstance(card, dict):
            raise KernelError("an architecture card must be a dict")
        goal = str(card.get("goal") or "").strip()
        if not goal:
            raise KernelError("an architecture card requires a goal")
        text = _canonical(card)
        findings = credential_findings(text)
        if findings:
            raise CredentialRejected(sorted(set(findings)))
        return "sha256:" + hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

    def create_compilation(self, card: Dict[str, Any], *,
                           actor: str = "user",
                           compilation_id: Optional[str] = None,
                           capture: Optional[Dict[str, Any]] = None
                           ) -> Dict[str, Any]:
        digest = self._guard_card(card)
        cid = compilation_id or kernel_id("cmp")
        goal = str(card.get("goal") or "").strip()
        # Capture provenance (Capture→Card): a small ids-and-ranges dict
        # recorded in the journal event only — the projection ignores it
        # (replay == live is unaffected), and status surfaces read it
        # back from the compilation's first event. Guarded like every
        # other journal write.
        if capture is not None:
            findings = credential_findings(_canonical(capture))
            if findings:
                raise CredentialRejected(sorted(set(findings)))

        def fn(conn):
            data = {
                "compilation_id": cid,
                "status": CompilationStatus.COMPILED,
                "goal": goal, "card": card, "digest": digest,
                "author": str(actor or "user"), "version": 1,
            }
            if capture is not None:
                data["capture"] = capture
            self._append(conn, cid, "compilation_created", data)
            return cid
        self._mutate(fn)
        return self.get_compilation(cid)  # committed above; never None

    def compilation_capture(self, compilation_id: str
                            ) -> Optional[Dict[str, Any]]:
        """The capture provenance recorded at creation (None when the
        compilation was goal-compiled, not capture-sourced)."""
        events = self.events_since(
            compilation_id, 0, kinds=("compilation_created",), limit=1,
        )
        if not events:
            return None
        capture = events[0]["data"].get("capture")
        return capture if isinstance(capture, dict) else None

    def _compilation_row(self, conn: sqlite3.Connection,
                         compilation_id: str):
        row = conn.execute(
            "SELECT compilation_id, status, card_version, version,"
            " approved_version, approved_digest, materialization"
            " FROM compilations WHERE compilation_id=?",
            (compilation_id,),
        ).fetchone()
        if row is None:
            raise KernelError(f"unknown compilation {compilation_id!r}")
        return row

    def record_compilation_card(self, compilation_id: str,
                                card: Dict[str, Any], *,
                                actor: str = "user",
                                guidance: str = "") -> int:
        """Record a new card version (recompilation). Allowed only while
        the compilation is still a design (compiled/rejected/approved —
        revising an approved card invalidates its approval); materialized
        and later compilations are infrastructure, never mutated."""
        digest = self._guard_card(card)

        def fn(conn):
            row = self._compilation_row(conn, compilation_id)
            status, card_version, version = row[1], int(row[2]), int(row[3])
            if status not in (CompilationStatus.COMPILED,
                              CompilationStatus.REJECTED,
                              CompilationStatus.APPROVED):
                raise KernelError(
                    f"compilation {compilation_id} is {status}; new card"
                    " versions are only recorded before materialization"
                )
            if status != CompilationStatus.COMPILED:
                check_compilation_transition(
                    status, CompilationStatus.COMPILED
                )
            new_version = card_version + 1
            self._append(conn, compilation_id,
                         "compilation_card_recorded", {
                             "compilation_id": compilation_id,
                             "card_version": new_version, "card": card,
                             "digest": digest,
                             "status": CompilationStatus.COMPILED,
                             "author": str(actor or "user"),
                             "guidance": str(guidance or ""),
                             "version": version + 1,
                         })
            return new_version
        return self._mutate(fn)

    def decide_compilation(self, compilation_id: str, verb: str, *,
                           decided_by: str = "",
                           origin_channel: str = "local",
                           origin_thread: str = "",
                           origin_sender: str = "",
                           reason: str = "") -> Dict[str, Any]:
        """Approve or reject the compilation's CURRENT card version.

        The authorization moment: approval pins the exact card version
        and digest that materialization will honor. v1 is local-only —
        any non-local decision origin is refused, which also blocks a
        session or channel surface from ever approving a card (no
        self-approval; approval is a user act at the shell).
        """
        if verb not in ("approve", "reject"):
            raise KernelError(f"unknown compilation verb {verb!r}")
        if str(origin_channel or "") != "local":
            raise ApprovalError(
                "compilation approvals are origin-bound and local-only "
                f"in v1; refusing a decision from origin "
                f"{origin_channel!r}"
            )
        decision_origin = ":".join(
            (origin_channel, origin_thread, origin_sender)
        )

        def fn(conn):
            row = self._compilation_row(conn, compilation_id)
            status, card_version, version = row[1], int(row[2]), int(row[3])
            target = (
                CompilationStatus.APPROVED if verb == "approve"
                else CompilationStatus.REJECTED
            )
            check_compilation_transition(status, target)
            digest_row = conn.execute(
                "SELECT digest FROM compilation_cards WHERE"
                " compilation_id=? AND card_version=?",
                (compilation_id, card_version),
            ).fetchone()
            self._append(conn, compilation_id, "compilation_decided", {
                "compilation_id": compilation_id, "status": target,
                "card_version": card_version,
                "digest": str(digest_row[0]) if digest_row else "",
                "decided_by": str(decided_by or ""),
                "decision_origin": decision_origin,
                "reason": str(reason or ""),
                "version": version + 1,
            })
            return {
                "compilation_id": compilation_id, "status": target,
                "card_version": card_version,
                "digest": str(digest_row[0]) if digest_row else "",
            }
        return self._mutate(fn)

    def transition_compilation(self, compilation_id: str, target: str, *,
                               reason: str = "",
                               mission_id: str = "") -> None:
        def fn(conn):
            row = self._compilation_row(conn, compilation_id)
            status, version = row[1], int(row[3])
            check_compilation_transition(status, target)
            self._append(conn, compilation_id,
                         "compilation_transitioned", {
                             "compilation_id": compilation_id,
                             "from": status, "to": target,
                             "reason": str(reason or ""),
                             "mission_id": str(mission_id or ""),
                             "version": version + 1,
                         })
        self._mutate(fn)

    def record_compilation_materialization(
        self, compilation_id: str, materialization: Dict[str, Any], *,
        complete: bool = False, error: str = "",
    ) -> None:
        """Record the full cumulative materialization state (receipts +
        rollback refs). ``complete=True`` additionally advances
        approved → materialized; re-recording a complete state on an
        already-materialized compilation is a no-op transition (replay).
        """
        if not isinstance(materialization, dict):
            raise KernelError("materialization must be a dict")
        payload = dict(materialization)
        if error:
            payload["error"] = str(error)[:1000]

        def fn(conn):
            row = self._compilation_row(conn, compilation_id)
            status, version = row[1], int(row[3])
            if status not in (CompilationStatus.APPROVED,
                              CompilationStatus.MATERIALIZED,
                              CompilationStatus.VERIFIED,
                              CompilationStatus.OPERATING):
                raise KernelError(
                    f"compilation {compilation_id} is {status}; "
                    "materialization records need an approved card"
                )
            self._append(conn, compilation_id,
                         "compilation_materialization_recorded", {
                             "compilation_id": compilation_id,
                             "materialization": payload,
                             "complete": bool(complete),
                             "version": version + 1,
                         })
            if complete and status == CompilationStatus.APPROVED:
                self._append(conn, compilation_id,
                             "compilation_transitioned", {
                                 "compilation_id": compilation_id,
                                 "from": status,
                                 "to": CompilationStatus.MATERIALIZED,
                                 "reason": "materialization complete",
                                 "mission_id": "",
                                 "version": version + 2,
                             })
        self._mutate(fn)

    def record_compilation_drill(self, compilation_id: str,
                                 result: Dict[str, Any], *,
                                 passed: bool) -> None:
        """Attach a drill result. A pass advances materialized →
        verified; a failure leaves status=materialized with the failure
        attached (rollback stays on offer)."""
        if not isinstance(result, dict):
            raise KernelError("drill result must be a dict")
        payload = dict(result, passed=bool(passed))

        def fn(conn):
            row = self._compilation_row(conn, compilation_id)
            status, version = row[1], int(row[3])
            if status not in (CompilationStatus.MATERIALIZED,
                              CompilationStatus.VERIFIED,
                              CompilationStatus.OPERATING):
                raise KernelError(
                    f"compilation {compilation_id} is {status}; drills"
                    " run against materialized compilations"
                )
            self._append(conn, compilation_id,
                         "compilation_drill_recorded", {
                             "compilation_id": compilation_id,
                             "drill": payload,
                             "version": version + 1,
                         })
            if passed and status == CompilationStatus.MATERIALIZED:
                self._append(conn, compilation_id,
                             "compilation_transitioned", {
                                 "compilation_id": compilation_id,
                                 "from": status,
                                 "to": CompilationStatus.VERIFIED,
                                 "reason": "acceptance drill passed",
                                 "mission_id": "",
                                 "version": version + 2,
                             })
        self._mutate(fn)

    # -- compilation queries (deterministic ordering) ---------------------

    @staticmethod
    def _compilation_dict(row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("materialization", "drill"):
            try:
                data[key] = _json.loads(data[key] or "{}")
            except ValueError:
                data[key] = {}
        return data

    _COMPILATION_SELECT = (
        "SELECT c.*, (SELECT MIN(seq) FROM mission_events e WHERE"
        " e.mission_id = c.compilation_id) AS compilation_seq"
        " FROM compilations c"
    )

    def get_compilation(self,
                        compilation_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            self._COMPILATION_SELECT + " WHERE c.compilation_id=?",
            (compilation_id,),
        ).fetchone()
        return self._compilation_dict(row) if row else None

    def resolve_compilation(self, ref: str) -> Optional[Dict[str, Any]]:
        """Compilation from a full id, unique id prefix, or #<seq> alias
        (the seq of its first event — the items scheme)."""
        ref = str(ref or "").strip().lstrip("#")
        if not ref:
            return None
        if ref.isdigit():
            row = self._read_conn().execute(
                "SELECT mission_id FROM mission_events WHERE seq=?",
                (int(ref),),
            ).fetchone()
            if row is None:
                return None
            return self.get_compilation(row[0])
        exact = self.get_compilation(ref)
        if exact is not None:
            return exact
        rows = self._read_conn().execute(
            "SELECT compilation_id FROM compilations WHERE"
            " compilation_id LIKE ? ESCAPE '\\' LIMIT 2",
            (ref.replace("\\", "\\\\").replace("%", r"\%")
             .replace("_", r"\_") + "%",),
        ).fetchall()
        if len(rows) != 1:
            return None
        return self.get_compilation(rows[0][0])

    def list_compilations(self, status: str = ""
                          ) -> List[Dict[str, Any]]:
        query = self._COMPILATION_SELECT
        params: List[Any] = []
        if status:
            if status not in CompilationStatus.ALL:
                raise KernelError(
                    f"unknown compilation status {status!r}"
                )
            query += " WHERE c.status=?"
            params.append(status)
        query += " ORDER BY c.created_at, c.compilation_id"
        rows = self._read_conn().execute(query, params).fetchall()
        return [self._compilation_dict(row) for row in rows]

    def compilation_card(self, compilation_id: str,
                         card_version: Optional[int] = None
                         ) -> Optional[Dict[str, Any]]:
        """One card version (default: the latest) with parsed content."""
        conn = self._read_conn()
        if card_version is None:
            row = conn.execute(
                "SELECT * FROM compilation_cards WHERE compilation_id=?"
                " ORDER BY card_version DESC LIMIT 1",
                (compilation_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM compilation_cards WHERE compilation_id=?"
                " AND card_version=?",
                (compilation_id, int(card_version)),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["card"] = _json.loads(data["card"])
        return data

    def compilation_card_versions(self, compilation_id: str
                                  ) -> List[Dict[str, Any]]:
        rows = self._read_conn().execute(
            "SELECT compilation_id, card_version, digest, author,"
            " guidance, created_at FROM compilation_cards WHERE"
            " compilation_id=? ORDER BY card_version",
            (compilation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def compilation_events(self, compilation_id: str,
                           limit: int = 100) -> List[Dict[str, Any]]:
        """The compilation's full event history (its own chain id)."""
        return self.event_tail(compilation_id, limit=limit)

    # ------------------------------------------------------------------
    # Composite session flows (single-transaction guarantees)
    # ------------------------------------------------------------------

    def start_session(self, mission_id: str, session_id: str, holder: str,
                      reserves: Dict[str, int],
                      lease_seconds: float = 900.0,
                      expected_version: Optional[int] = None
                      ) -> Dict[str, Any]:
        """ready→active + session lease + budget reserve + event, atomically.

        Raises if the mission is not ready, the lease is held, or budgets
        are insufficient — in which case nothing changed at all.
        """
        def fn(conn):
            row = self._mission_row(conn, mission_id)
            status = row[2]
            scope_id = row[5]
            if bool(row[6]):
                raise KernelError(
                    f"mission {mission_id} has STOP requested"
                )
            if status != MissionState.READY:
                raise KernelError(
                    f"mission {mission_id} is {status}, not ready"
                )
            current = float(self.clock())
            lease = conn.execute(
                "SELECT holder, expires_at FROM leases WHERE kind=? AND"
                " resource=?",
                ("mission_session", mission_id),
            ).fetchone()
            if lease is not None and float(lease[1]) > current and (
                lease[0] != holder
            ):
                raise KernelError(
                    f"mission {mission_id} session lease held by {lease[0]!r}"
                )
            fencing = 1
            if lease is not None:
                lease_row = conn.execute(
                    "SELECT fencing_token FROM leases WHERE kind=? AND"
                    " resource=?",
                    ("mission_session", mission_id),
                ).fetchone()
                fencing = int(lease_row[0]) + 1
                conn.execute(
                    "UPDATE leases SET holder=?, fencing_token=?,"
                    " granted_at=?, expires_at=? WHERE kind=? AND resource=?",
                    (holder, fencing, current, current + lease_seconds,
                     "mission_session", mission_id),
                )
            else:
                conn.execute(
                    "INSERT INTO leases(lease_id, kind, resource, holder,"
                    " epoch, fencing_token, granted_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (kernel_id("lse"), "mission_session", mission_id, holder,
                     self._epoch or 0, fencing, current,
                     current + lease_seconds),
                )
            if reserves:
                self._reserve(
                    conn, mission_id, scope_id, reserves, session_id,
                    note="work session",
                )
            new_version = self._transition(
                conn, mission_id, MissionState.ACTIVE, expected_version,
                reason=f"session {session_id} started",
            )
            self._append(conn, mission_id, "session_started", {
                "session_id": session_id, "holder": holder,
                "reservation_id": session_id,
            })
            return {
                "mission_id": mission_id, "session_id": session_id,
                "version": new_version, "scope_id": scope_id,
                "fencing_token": fencing,
            }
        return self._mutate(fn)

    def checkpoint_session(self, mission_id: str, session_id: str,
                           holder: str, summary: str,
                           outcome: str,
                           state: Optional[Dict[str, Any]] = None,
                           actuals: Optional[Dict[str, int]] = None,
                           next_wake_at: Optional[float] = None,
                           notify_payload: Optional[Dict[str, Any]] = None,
                           notify_dedupe_key: str = "",
                           error: str = "") -> Dict[str, Any]:
        """Session completion: checkpoint + budget commit + transition +
        next-wake timer + outbox notification — one transaction.

        ``outcome`` is the target state: waiting_timer, waiting_input,
        waiting_approval, succeeded, failed, or cancelled.
        """
        if outcome not in (
            MissionState.WAITING_TIMER, MissionState.WAITING_INPUT,
            MissionState.WAITING_APPROVAL, MissionState.SUCCEEDED,
            MissionState.FAILED, MissionState.CANCELLED,
        ):
            raise KernelError(f"invalid session outcome {outcome!r}")
        checkpoint_id = kernel_id("ckpt")

        def fn(conn):
            row = self._mission_row(conn, mission_id)
            status, scope_id = row[2], row[5]
            if status != MissionState.ACTIVE:
                raise KernelError(
                    f"mission {mission_id} is {status}, not active"
                )
            current = float(self.clock())
            lease = conn.execute(
                "SELECT holder, expires_at FROM leases WHERE kind=? AND"
                " resource=?",
                ("mission_session", mission_id),
            ).fetchone()
            if lease is None or lease[0] != holder:
                raise KernelError(
                    f"session lease for {mission_id} is not held by"
                    f" {holder!r} — refusing to checkpoint"
                )
            if float(lease[1]) <= current:
                raise KernelError(
                    f"session lease for {mission_id} expired — the session"
                    " was abandoned; refusing a late checkpoint"
                )
            self._append(conn, mission_id, "checkpoint_recorded", {
                "checkpoint_id": checkpoint_id, "session_id": session_id,
                "summary": str(summary), "state": state or {},
            })
            reservation = conn.execute(
                "SELECT 1 FROM budget_reservations WHERE reservation_id=?"
                " AND scope_id=? AND status='active' LIMIT 1",
                (session_id, scope_id),
            ).fetchone()
            if reservation is not None:
                self._commit_budget(
                    conn, mission_id, scope_id, session_id, actuals or {},
                    note="session checkpoint",
                )
            self._append(conn, mission_id, "session_checkpointed", {
                "session_id": session_id, "outcome": outcome,
                "checkpoint_id": checkpoint_id,
            })
            self._transition(
                conn, mission_id, outcome, None,
                reason=f"session {session_id} checkpoint", error=error,
            )
            if outcome == MissionState.WAITING_TIMER and (
                next_wake_at is not None
            ):
                wake = conn.execute(
                    "SELECT timer_id, generation, interval_seconds FROM"
                    " timers WHERE mission_id=? AND logical_key='wake'",
                    (mission_id,),
                ).fetchone()
                if wake is not None:
                    self._append(conn, mission_id, "timer_rescheduled", {
                        "timer_id": wake[0],
                        "generation": int(wake[1]) + 1,
                        "due_at": float(next_wake_at),
                        "interval_seconds": int(wake[2]),
                    })
            if notify_payload is not None:
                self._enqueue_outbox(
                    conn, mission_id, "channel_notify", notify_payload,
                    notify_dedupe_key or f"session:{session_id}",
                )
            conn.execute(
                "DELETE FROM leases WHERE kind=? AND resource=? AND"
                " holder=?",
                ("mission_session", mission_id, holder),
            )
            if outcome in MissionState.TERMINAL:
                timers = conn.execute(
                    "SELECT timer_id FROM timers WHERE mission_id=? AND"
                    " status='active'",
                    (mission_id,),
                ).fetchall()
                for (timer_id,) in timers:
                    self._append(conn, mission_id, "timer_cancelled", {
                        "timer_id": timer_id, "reason": "mission terminal",
                    })
            return {"checkpoint_id": checkpoint_id, "outcome": outcome}
        return self._mutate(fn)

    def record_review(self, mission_id: str, review_id: str, holder: str,
                      *, action: str, content: Dict[str, Any],
                      plan_content: Optional[Dict[str, Any]] = None,
                      plan_rationale: str = "",
                      notify_payload: Optional[Dict[str, Any]] = None,
                      notify_dedupe_key: str = "",
                      actuals: Optional[Dict[str, int]] = None
                      ) -> Dict[str, Any]:
        """Review-session completion: verdict + budget commit + optional
        numbered plan revision + optional escalation notification + the
        active→ready transition — one transaction.

        The review session was started through :meth:`start_session` (same
        lease and reservation discipline as a work session; the review_id is
        the session/reservation id). Unlike a work session it records no
        checkpoint and never increments ``runs`` — reviews judge work, they
        are not work. A re-plan writes the next numbered plan version through
        the same plans machinery ``update_plan`` uses, journaled with an
        explicit ``plan_revised`` event carrying the rationale.
        """
        if action not in ("continue", "re-plan", "escalate"):
            raise KernelError(f"invalid review action {action!r}")
        if action == "re-plan" and plan_content is None:
            raise KernelError("re-plan reviews require plan content")
        plan_id = kernel_id("pln") if plan_content is not None else ""

        def fn(conn):
            row = self._mission_row(conn, mission_id)
            status, scope_id = row[2], row[5]
            if status != MissionState.ACTIVE:
                raise KernelError(
                    f"mission {mission_id} is {status}, not active"
                )
            current = float(self.clock())
            lease = conn.execute(
                "SELECT holder, expires_at FROM leases WHERE kind=? AND"
                " resource=?",
                ("mission_session", mission_id),
            ).fetchone()
            if lease is None or lease[0] != holder:
                raise KernelError(
                    f"session lease for {mission_id} is not held by"
                    f" {holder!r} — refusing to record the review"
                )
            if float(lease[1]) <= current:
                raise KernelError(
                    f"session lease for {mission_id} expired — the review"
                    " was abandoned; refusing a late verdict"
                )
            self._append(conn, mission_id, "review_recorded", {
                "review_id": review_id, "session_id": review_id,
                "action": action, "content": content,
            })
            reservation = conn.execute(
                "SELECT 1 FROM budget_reservations WHERE reservation_id=?"
                " AND scope_id=? AND status='active' LIMIT 1",
                (review_id, scope_id),
            ).fetchone()
            if reservation is not None:
                self._commit_budget(
                    conn, mission_id, scope_id, review_id, actuals or {},
                    note="review session",
                )
            plan_version = None
            if plan_content is not None:
                version_row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM plans WHERE"
                    " mission_id=?",
                    (mission_id,),
                ).fetchone()
                plan_version = int(version_row[0]) + 1
                self._append(conn, mission_id, "plan_recorded", {
                    "plan_id": plan_id, "plan_version": plan_version,
                    "content": plan_content,
                })
                self._append(conn, mission_id, "plan_revised", {
                    "plan_id": plan_id, "review_id": review_id,
                    "from_version": plan_version - 1,
                    "to_version": plan_version,
                    "rationale": str(plan_rationale),
                })
            if notify_payload is not None:
                self._enqueue_outbox(
                    conn, mission_id, "channel_notify", notify_payload,
                    notify_dedupe_key or f"review:{review_id}",
                )
            self._transition(
                conn, mission_id, MissionState.READY, None,
                reason=f"review {review_id}: {action}",
            )
            conn.execute(
                "DELETE FROM leases WHERE kind=? AND resource=? AND"
                " holder=?",
                ("mission_session", mission_id, holder),
            )
            return {
                "review_id": review_id, "action": action,
                "plan_id": plan_id, "plan_version": plan_version,
            }
        return self._mutate(fn)

    def record_review_skip(self, mission_id: str, reason: str,
                           review_id: str = "") -> None:
        """Journal a skipped review (budget exhausted, model unusable, …).

        Journal-only, but it advances the review cadence marker so a
        persistently skipping mission journals one skip per cadence period,
        never a skip per tick.
        """
        def fn(conn):
            self._mission_row(conn, mission_id)
            self._append(conn, mission_id, "review_skipped", {
                "reason": str(reason), "review_id": str(review_id),
            })
        self._mutate(fn)

    def abandon_session(self, mission_id: str, session_id: str,
                        reason: str = "lease expired") -> bool:
        """Recovery path: an active mission whose session lease is gone or
        expired returns to ready with its reservation released. Duplicate
        effects are impossible: side effects happened (or not) inside past
        transactions; this only repairs coordination state."""
        def fn(conn):
            row = self._mission_row(conn, mission_id)
            status, scope_id = row[2], row[5]
            if status != MissionState.ACTIVE:
                return False
            current = float(self.clock())
            lease = conn.execute(
                "SELECT holder, expires_at FROM leases WHERE kind=? AND"
                " resource=?",
                ("mission_session", mission_id),
            ).fetchone()
            if lease is not None and float(lease[1]) > current:
                return False  # a live session still owns this mission
            reservation = conn.execute(
                "SELECT 1 FROM budget_reservations WHERE reservation_id=?"
                " AND scope_id=? AND status='active' LIMIT 1",
                (session_id, scope_id),
            ).fetchone()
            if reservation is not None:
                self._release_budget(
                    conn, mission_id, scope_id, session_id,
                    note="session abandoned",
                )
            self._append(conn, mission_id, "session_abandoned", {
                "session_id": session_id, "reason": str(reason),
            })
            self._transition(
                conn, mission_id, MissionState.READY, None,
                reason=f"session {session_id} abandoned: {reason}",
            )
            conn.execute(
                "DELETE FROM leases WHERE kind=? AND resource=?",
                ("mission_session", mission_id),
            )
            return True
        return self._mutate(fn)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM missions WHERE mission_id=?", (mission_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["spec"] = _json.loads(data["spec"])
        return data

    def list_missions(self, status: str = "") -> List[Dict[str, Any]]:
        if status:
            rows = self._read_conn().execute(
                "SELECT * FROM missions WHERE status=? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = self._read_conn().execute(
                "SELECT * FROM missions ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["spec"] = _json.loads(data["spec"])
            result.append(data)
        return result

    def event_count(self, mission_id: str = "") -> int:
        if mission_id:
            row = self._read_conn().execute(
                "SELECT COUNT(*) FROM mission_events WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
        else:
            row = self._read_conn().execute(
                "SELECT COUNT(*) FROM mission_events"
            ).fetchone()
        return int(row[0])

    def event_tail(self, mission_id: str, limit: int = 20,
                   ) -> List[Dict[str, Any]]:
        rows = self._read_conn().execute(
            "SELECT seq, mission_id, kind, data, schema_version, created_at"
            " FROM mission_events WHERE mission_id=? ORDER BY seq DESC"
            " LIMIT ?",
            (mission_id, int(limit)),
        ).fetchall()
        events = []
        for row in reversed(rows):
            if int(row["schema_version"]) != EVENT_SCHEMA_VERSION:
                raise KernelError(
                    f"event seq={row['seq']} has unsupported schema version"
                    f" {row['schema_version']} — failing closed"
                )
            events.append({
                "seq": row["seq"], "mission_id": row["mission_id"],
                "kind": row["kind"], "data": _json.loads(row["data"]),
                "created_at": row["created_at"],
            })
        return events

    def open_tasks(self, mission_id: str) -> List[Dict[str, Any]]:
        rows = self._read_conn().execute(
            "SELECT * FROM tasks WHERE mission_id=? AND state IN (?, ?)"
            " ORDER BY created_at",
            (mission_id, TaskState.OPEN, TaskState.IN_PROGRESS),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_plan(self, mission_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM plans WHERE mission_id=? ORDER BY version DESC"
            " LIMIT 1",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["content"] = _json.loads(data["content"])
        return data

    def latest_checkpoint(self, mission_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM checkpoints WHERE mission_id=? ORDER BY"
            " created_at DESC, checkpoint_id DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["state"] = _json.loads(data["state"])
        return data

    def latest_review(self, mission_id: str) -> Optional[Dict[str, Any]]:
        row = self._read_conn().execute(
            "SELECT * FROM reviews WHERE mission_id=? ORDER BY"
            " created_at DESC, review_id DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["content"] = _json.loads(data["content"])
        return data

    def last_event(self, mission_id: str,
                   kinds: Any) -> Optional[Dict[str, Any]]:
        """Most recent event of the given kind(s) for one mission."""
        wanted = sorted(str(kind) for kind in kinds)
        if not wanted:
            return None
        row = self._read_conn().execute(
            "SELECT seq, kind, data, created_at FROM mission_events WHERE"
            " mission_id=? AND kind IN (%s) ORDER BY seq DESC LIMIT 1"
            % ",".join("?" for _ in wanted),
            [mission_id, *wanted],
        ).fetchone()
        if row is None:
            return None
        return {
            "seq": int(row["seq"]), "kind": row["kind"],
            "data": _json.loads(row["data"]),
            "created_at": float(row["created_at"]),
        }

    def count_events_since(self, mission_id: str, kinds: Any,
                           since_seq: int = 0) -> Dict[str, int]:
        """Per-kind event counts after ``since_seq`` for one mission."""
        wanted = sorted(str(kind) for kind in kinds)
        if not wanted:
            return {}
        rows = self._read_conn().execute(
            "SELECT kind, COUNT(*) FROM mission_events WHERE mission_id=?"
            " AND seq>? AND kind IN (%s) GROUP BY kind"
            % ",".join("?" for _ in wanted),
            [mission_id, int(since_seq), *wanted],
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def events_since(self, mission_id: str, since_seq: int = 0,
                     kinds: Any = (),
                     limit: int = 200) -> List[Dict[str, Any]]:
        """Events after ``since_seq`` in order, optionally kind-filtered."""
        query = (
            "SELECT seq, kind, data, created_at FROM mission_events WHERE"
            " mission_id=? AND seq>?"
        )
        params: List[Any] = [mission_id, int(since_seq)]
        wanted = sorted(str(kind) for kind in kinds or ())
        if wanted:
            query += " AND kind IN (%s)" % ",".join("?" for _ in wanted)
            params.extend(wanted)
        query += " ORDER BY seq LIMIT ?"
        params.append(int(limit))
        rows = self._read_conn().execute(query, params).fetchall()
        return [
            {
                "seq": int(row["seq"]), "kind": row["kind"],
                "data": _json.loads(row["data"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Chain verification, replay, backup
    # ------------------------------------------------------------------

    def verify_chain(self, mission_id: str = "") -> int:
        """Recompute the per-mission hash chains; returns events verified.
        Any mismatch, gap, or unknown schema version fails closed."""
        conn = self._read_conn()
        if mission_id:
            mission_ids = [mission_id]
        else:
            mission_ids = [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT mission_id FROM mission_events"
                ).fetchall()
            ]
        verified = 0
        for mid in mission_ids:
            prev_hash = GENESIS_HASH
            for row in conn.execute(
                "SELECT seq, kind, data, schema_version, created_at,"
                " prev_hash, hash FROM mission_events WHERE mission_id=?"
                " ORDER BY seq",
                (mid,),
            ):
                if int(row["schema_version"]) != EVENT_SCHEMA_VERSION:
                    raise KernelError(
                        f"event seq={row['seq']} has unsupported schema"
                        f" version {row['schema_version']} — failing closed"
                    )
                if row["prev_hash"] != prev_hash:
                    raise KernelError(
                        f"event chain broken at seq={row['seq']} for"
                        f" mission {mid}: prev_hash mismatch"
                    )
                expected = event_hash(
                    mid, row["kind"], _json.loads(row["data"]),
                    row["created_at"], prev_hash,
                )
                if expected != row["hash"]:
                    raise KernelError(
                        f"event chain broken at seq={row['seq']} for"
                        f" mission {mid}: hash mismatch"
                    )
                prev_hash = row["hash"]
                verified += 1
        return verified

    def replay_projections(self) -> sqlite3.Connection:
        """Rebuild every replayed projection into a fresh in-memory database
        by applying the event journal in order. Fails closed on unknown
        event kinds or schema versions."""
        dest = sqlite3.connect(":memory:")
        dest.executescript(_SCHEMA)
        src = self._read_conn()
        for row in src.execute(
            "SELECT mission_id, kind, data, schema_version, created_at FROM"
            " mission_events ORDER BY seq"
        ):
            if int(row["schema_version"]) != EVENT_SCHEMA_VERSION:
                dest.close()
                raise KernelError(
                    f"unsupported event schema version"
                    f" {row['schema_version']} — failing closed"
                )
            _apply_event(
                dest, row["mission_id"], row["kind"],
                _json.loads(row["data"]), row["created_at"],
            )
        dest.commit()
        return dest

    def _dump_replayed(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        dump: Dict[str, Any] = {}
        for table, columns in REPLAYED_TABLES.items():
            column_sql = ", ".join(columns)
            order = columns[0]
            rows = conn.execute(
                f"SELECT {column_sql} FROM {table} ORDER BY {order}"
            ).fetchall()
            dump[table] = [tuple(row) for row in rows]
        return dump

    def replay_matches_live(self) -> Tuple[bool, str]:
        """Rebuild from events and compare every replayed projection."""
        replayed = self.replay_projections()
        try:
            live_dump = self._dump_replayed(self._read_conn())
            replay_dump = self._dump_replayed(replayed)
        finally:
            replayed.close()
        for table in REPLAYED_TABLES:
            if live_dump[table] != replay_dump[table]:
                live_rows = {tuple(r) for r in live_dump[table]}
                replay_rows = {tuple(r) for r in replay_dump[table]}
                extra = live_rows - replay_rows
                missing = replay_rows - live_rows
                return False, (
                    f"projection {table} diverges from replay —"
                    f" {len(extra)} live-only, {len(missing)} replay-only"
                )
        return True, ""

    def verify_integrity(self) -> Dict[str, Any]:
        """Full drill: SQLite integrity, hash chains, replay equivalence."""
        row = self._read_conn().execute("PRAGMA integrity_check").fetchone()
        if row[0] != "ok":
            raise KernelError(f"sqlite integrity check failed: {row[0]}")
        events = self.verify_chain()
        ok, detail = self.replay_matches_live()
        if not ok:
            raise KernelError(f"replay mismatch: {detail}")
        return {"events_verified": events, "replay": "match"}

    def backup(self, dest_path: Any) -> str:
        """Consistent online snapshot via the SQLite backup API (runs on the
        writer thread, so it serializes with mutations)."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        def fn(conn):
            dest = sqlite3.connect(str(dest_path))
            try:
                conn.backup(dest)
            finally:
                dest.close()
            return str(dest_path)
        # raw: the backup API takes its own locks and would deadlock inside
        # an explicit transaction on the same connection.
        result = self._mutate(fn, raw=True)
        try:
            os.chmod(dest_path, 0o600)
        except OSError:
            pass
        return result

    @staticmethod
    def restore(snapshot_path: Any, db_path: Any) -> None:
        """Offline restore: replace the kernel database file with a
        snapshot. The store must not be open on ``db_path``."""
        snapshot_path = Path(snapshot_path)
        db_path = Path(db_path)
        if not snapshot_path.exists():
            raise KernelError(f"snapshot {snapshot_path} does not exist")
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(db_path) + suffix)
            if stale.exists():
                stale.unlink()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(snapshot_path), str(db_path))
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    def reconcile(self, holder: str = "",
                  now: Optional[float] = None) -> Dict[str, int]:
        """Startup/periodic repair: abandon sessions whose lease expired and
        drop expired leases. Never touches missions with live leases."""
        current = float(now if now is not None else self.clock())
        abandoned = 0
        for mission in self.list_missions(status=MissionState.ACTIVE):
            lease = self.get_lease("mission_session", mission["mission_id"])
            if lease is None or float(lease["expires_at"]) <= current:
                session_id = ""
                for event in reversed(
                    self.event_tail(mission["mission_id"], limit=50)
                ):
                    if event["kind"] == "session_started":
                        session_id = event["data"].get("session_id", "")
                        break
                if self.abandon_session(
                    mission["mission_id"], session_id or "unknown",
                    reason="reconcile: lease missing or expired",
                ):
                    abandoned += 1
        dropped = 0
        def fn(conn):
            cursor = conn.execute(
                "DELETE FROM leases WHERE expires_at <= ?", (current,)
            )
            return cursor.rowcount
        dropped = self._mutate(fn)
        return {"sessions_abandoned": abandoned, "leases_dropped": dropped}
