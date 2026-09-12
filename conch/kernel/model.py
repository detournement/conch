"""Kernel domain model: states, transitions, event taxonomy, mission spec.

Everything here is deterministic data + validation — no I/O. The store
enforces these rules inside its transactions; the engine and daemon consume
them. Unknown states, transitions, event kinds, and event schema versions
all fail closed (:class:`KernelError`), matching conch.swarm.protocol
conventions.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, Dict, Optional

#: Version stamped on every kernel event row. Reading or replaying an event
#: with any other version fails closed — an older kernel must never
#: half-interpret rows written by a newer one.
EVENT_SCHEMA_VERSION = 1


class KernelError(Exception):
    """Base class: a kernel invariant was violated. Always fail closed."""


class ConflictError(KernelError):
    """Optimistic version check failed — the caller's copy is stale."""


class BudgetExceededError(KernelError):
    """A reserve would exceed the scope's cap. The transaction aborted."""


class StaleGenerationError(KernelError):
    """A timer fire/reschedule carried an outdated generation."""


class ApprovalError(KernelError):
    """An approval could not be consumed (missing/expired/origin/nonce)."""


# ---------------------------------------------------------------------------
# Canonical kernel IDs (same shape as conch.swarm.protocol IDs)
# ---------------------------------------------------------------------------

#: Kernel-local ID kinds. ``msn``/``task``/``wrk`` intentionally match the
#: swarm protocol so mission/task/worker IDs are valid on the wire.
KERNEL_ID_KINDS = frozenset({
    "msn", "task", "pln", "ses", "ckpt", "apr", "tmr", "act", "att",
    "art", "bnd", "scp", "lse", "obx", "wrk",
})

_ID_RE = re.compile(r"^([a-z]{2,8})-([0-9a-f]{13})-([0-9a-f]{16})$")


def kernel_id(kind: str) -> str:
    """``{kind}-{unix_ms:013x}-{random 8 bytes hex}`` — time-sortable."""
    if kind not in KERNEL_ID_KINDS:
        raise KernelError(f"unknown kernel ID kind: {kind!r}")
    return f"{kind}-{int(time.time() * 1000):013x}-{secrets.token_hex(8)}"


def parse_kernel_id(value: str) -> str:
    match = _ID_RE.match(value or "")
    if not match or match.group(1) not in KERNEL_ID_KINDS:
        raise KernelError(f"malformed kernel ID: {value!r}")
    return match.group(1)


# ---------------------------------------------------------------------------
# Mission state machine
# ---------------------------------------------------------------------------

class MissionState:
    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    WAITING_TIMER = "waiting_timer"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = frozenset({
        DRAFT, READY, ACTIVE, WAITING_TIMER, WAITING_INPUT,
        WAITING_APPROVAL, PAUSED, SUCCEEDED, FAILED, CANCELLED,
    })
    TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})
    WAITING = frozenset({WAITING_TIMER, WAITING_INPUT, WAITING_APPROVAL})


#: Legal transitions. Anything not listed is rejected in the store.
MISSION_TRANSITIONS = {
    MissionState.DRAFT: frozenset({
        MissionState.READY, MissionState.CANCELLED,
    }),
    MissionState.READY: frozenset({
        MissionState.ACTIVE, MissionState.WAITING_TIMER,
        MissionState.PAUSED, MissionState.CANCELLED,
        MissionState.FAILED,  # e.g. budget exhausted before a session
    }),
    MissionState.ACTIVE: frozenset({
        MissionState.WAITING_TIMER, MissionState.WAITING_INPUT,
        MissionState.WAITING_APPROVAL, MissionState.PAUSED,
        MissionState.READY,  # session abandoned (crash) → retry
        MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED,
    }),
    MissionState.WAITING_TIMER: frozenset({
        MissionState.READY, MissionState.PAUSED, MissionState.CANCELLED,
        MissionState.FAILED,
    }),
    MissionState.WAITING_INPUT: frozenset({
        MissionState.READY, MissionState.PAUSED, MissionState.CANCELLED,
        MissionState.FAILED,
    }),
    MissionState.WAITING_APPROVAL: frozenset({
        MissionState.READY, MissionState.PAUSED, MissionState.CANCELLED,
        MissionState.FAILED,
    }),
    MissionState.PAUSED: frozenset({
        MissionState.READY, MissionState.CANCELLED,
    }),
    MissionState.SUCCEEDED: frozenset(),
    MissionState.FAILED: frozenset(),
    MissionState.CANCELLED: frozenset(),
}


def check_transition(current: str, target: str) -> None:
    if current not in MissionState.ALL:
        raise KernelError(f"unknown mission state {current!r}")
    if target not in MissionState.ALL:
        raise KernelError(f"unknown mission state {target!r}")
    if target not in MISSION_TRANSITIONS[current]:
        raise KernelError(
            f"illegal mission transition {current!r} -> {target!r}"
        )


class TaskState:
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"

    ALL = frozenset({OPEN, IN_PROGRESS, DONE, CANCELLED, FAILED})
    TERMINAL = frozenset({DONE, CANCELLED, FAILED})


TASK_TRANSITIONS = {
    TaskState.OPEN: frozenset({
        TaskState.IN_PROGRESS, TaskState.DONE, TaskState.CANCELLED,
        TaskState.FAILED,
    }),
    TaskState.IN_PROGRESS: frozenset({
        TaskState.OPEN, TaskState.DONE, TaskState.CANCELLED,
        TaskState.FAILED,
    }),
    TaskState.DONE: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.FAILED: frozenset(),
}


def check_task_transition(current: str, target: str) -> None:
    if current not in TaskState.ALL or target not in TaskState.ALL:
        raise KernelError(
            f"unknown task state in transition {current!r} -> {target!r}"
        )
    if target not in TASK_TRANSITIONS[current]:
        raise KernelError(
            f"illegal task transition {current!r} -> {target!r}"
        )


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------

class MisfirePolicy:
    """What a timer does about occurrences missed while nothing was firing.

    - ``skip``: missed occurrences never fire; jump to the next future due.
    - ``coalesce``: all missed occurrences collapse into exactly one fire.
    - ``catch_up``: one fire per missed occurrence, bounded by
      ``catch_up_limit``; the remainder coalesces.
    """

    SKIP = "skip"
    COALESCE = "coalesce"
    CATCH_UP = "catch_up"

    ALL = frozenset({SKIP, COALESCE, CATCH_UP})


#: Lateness beyond which an occurrence counts as a misfire rather than
#: ordinary scheduling jitter.
MISFIRE_GRACE_SECONDS = 60.0

#: Hard bound on catch_up fires in one processing pass, whatever the
#: configured limit says (bounded catch-up is a plan requirement).
CATCH_UP_HARD_CAP = 32


# ---------------------------------------------------------------------------
# Approvals / actions / outbox
# ---------------------------------------------------------------------------

class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"

    ALL = frozenset({PENDING, APPROVED, DENIED, EXPIRED})


class ActionStatus:
    """External-action ledger status. ``unknown`` outcomes are reconciled by
    querying, never blind-retried (non-negotiable invariant)."""

    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"
    UNKNOWN = "unknown"

    ALL = frozenset({PENDING, COMMITTED, FAILED, UNKNOWN})


class OutboxStatus:
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"

    ALL = frozenset({PENDING, DELIVERED, FAILED})


class InboxStatus:
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"

    ALL = frozenset({PENDING, PROCESSED, FAILED})


class BindingStatus:
    """Resource-binding lifecycle (Swarm Phase 3).

    A binding ties a mission (and optionally one of its tasks) to an
    external resource — a Capitol run, session, artifact, schedule, or
    provisioned asset — and carries the supervision cursor. ``DEGRADED``
    means the external system is unreachable and the supervisor is backing
    off; it is never terminal, and a recovered system resumes from the
    persisted cursor.
    """

    ACTIVE = "active"
    WAITING_HITL = "waiting_hitl"
    DEGRADED = "degraded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = frozenset({
        ACTIVE, WAITING_HITL, DEGRADED, COMPLETED, FAILED, CANCELLED,
    })
    TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
    SUPERVISED = frozenset({ACTIVE, WAITING_HITL, DEGRADED})


# ---------------------------------------------------------------------------
# Fleet: worker registry and dispatch state machines (Swarm Phase 2)
# ---------------------------------------------------------------------------

class WorkerState:
    """FleetRegistry worker lifecycle. REVOKED is terminal; QUARANTINED
    and REVOKED are operator decisions, never automatic."""

    PENDING = "pending"          # enrolled, not yet admitted for work
    ACTIVE = "active"            # schedulable
    DRAINING = "draining"        # finishing in-flight work, no new tasks
    OFFLINE = "offline"          # deliberately stopped (still trusted)
    UNREACHABLE = "unreachable"  # missed heartbeats; observed, not chosen
    UPDATING = "updating"        # deploy/activate in progress
    QUARANTINED = "quarantined"  # operator hold: no work, trust suspended
    REVOKED = "revoked"          # terminal: never schedulable again

    ALL = frozenset({
        PENDING, ACTIVE, DRAINING, OFFLINE, UNREACHABLE, UPDATING,
        QUARANTINED, REVOKED,
    })
    SCHEDULABLE = frozenset({ACTIVE})
    TERMINAL = frozenset({REVOKED})


WORKER_TRANSITIONS = {
    WorkerState.PENDING: frozenset({
        WorkerState.ACTIVE, WorkerState.UPDATING, WorkerState.QUARANTINED,
        WorkerState.REVOKED,
    }),
    WorkerState.ACTIVE: frozenset({
        WorkerState.DRAINING, WorkerState.OFFLINE, WorkerState.UNREACHABLE,
        WorkerState.UPDATING, WorkerState.QUARANTINED, WorkerState.REVOKED,
    }),
    WorkerState.DRAINING: frozenset({
        WorkerState.ACTIVE, WorkerState.OFFLINE, WorkerState.UNREACHABLE,
        WorkerState.QUARANTINED, WorkerState.REVOKED,
    }),
    WorkerState.OFFLINE: frozenset({
        WorkerState.ACTIVE, WorkerState.UPDATING, WorkerState.QUARANTINED,
        WorkerState.REVOKED,
    }),
    WorkerState.UNREACHABLE: frozenset({
        WorkerState.ACTIVE, WorkerState.OFFLINE, WorkerState.QUARANTINED,
        WorkerState.REVOKED,
    }),
    WorkerState.UPDATING: frozenset({
        WorkerState.ACTIVE, WorkerState.OFFLINE, WorkerState.UNREACHABLE,
        WorkerState.QUARANTINED, WorkerState.REVOKED,
    }),
    WorkerState.QUARANTINED: frozenset({
        WorkerState.ACTIVE, WorkerState.OFFLINE, WorkerState.REVOKED,
    }),
    WorkerState.REVOKED: frozenset(),
}


def check_worker_transition(current: str, target: str) -> None:
    if current not in WorkerState.ALL or target not in WorkerState.ALL:
        raise KernelError(
            f"unknown worker state in transition {current!r} -> {target!r}"
        )
    if target not in WORKER_TRANSITIONS[current]:
        raise KernelError(
            f"illegal worker transition {current!r} -> {target!r}"
        )


class DispatchState:
    """Distributed task dispatch lifecycle (controller truth).

    ``NEEDS_RECONCILE`` exists for unknown external outcomes: the dispatch
    parks until something queries the real outcome — never blind-retried.
    """

    QUEUED = "queued"                  # awaiting scheduling
    OFFERING = "offering"              # offered to a worker, awaiting start
    RUNNING = "running"                # started under a live lease
    WAITING_CHILD = "waiting_child"    # parked on a brokered delegation
    NEEDS_RECONCILE = "needs_reconcile"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL = frozenset({
        QUEUED, OFFERING, RUNNING, WAITING_CHILD, NEEDS_RECONCILE,
        SUCCEEDED, FAILED, CANCELLED,
    })
    TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})
    IN_FLIGHT = frozenset({OFFERING, RUNNING, WAITING_CHILD})


DISPATCH_TRANSITIONS = {
    DispatchState.QUEUED: frozenset({
        DispatchState.OFFERING, DispatchState.FAILED,
        DispatchState.CANCELLED,
    }),
    DispatchState.OFFERING: frozenset({
        DispatchState.RUNNING, DispatchState.QUEUED, DispatchState.FAILED,
        DispatchState.CANCELLED, DispatchState.NEEDS_RECONCILE,
    }),
    DispatchState.RUNNING: frozenset({
        DispatchState.WAITING_CHILD, DispatchState.SUCCEEDED,
        DispatchState.FAILED, DispatchState.CANCELLED,
        DispatchState.QUEUED, DispatchState.NEEDS_RECONCILE,
    }),
    DispatchState.WAITING_CHILD: frozenset({
        DispatchState.RUNNING, DispatchState.QUEUED, DispatchState.FAILED,
        DispatchState.CANCELLED,
    }),
    DispatchState.NEEDS_RECONCILE: frozenset({
        DispatchState.QUEUED, DispatchState.SUCCEEDED,
        DispatchState.FAILED, DispatchState.CANCELLED,
    }),
    DispatchState.SUCCEEDED: frozenset(),
    DispatchState.FAILED: frozenset(),
    DispatchState.CANCELLED: frozenset(),
}


def check_dispatch_transition(current: str, target: str) -> None:
    if current not in DispatchState.ALL or target not in DispatchState.ALL:
        raise KernelError(
            f"unknown dispatch state in transition {current!r} -> {target!r}"
        )
    if target not in DISPATCH_TRANSITIONS[current]:
        raise KernelError(
            f"illegal dispatch transition {current!r} -> {target!r}"
        )


# ---------------------------------------------------------------------------
# Kernel event taxonomy
# ---------------------------------------------------------------------------

#: Every kernel mutation appends exactly one of these. The store's replay
#: applies them in sequence to rebuild all replayed projections; an event
#: kind outside this set fails closed.
EVENT_KINDS = frozenset({
    "mission_created",
    "mission_transitioned",
    "mission_spec_updated",
    "mission_stop_changed",
    "mission_note",
    "plan_recorded",
    "task_created",
    "task_transitioned",
    "attempt_started",
    "attempt_finished",
    "checkpoint_recorded",
    "budget_scope_created",
    "budget_reserved",
    "budget_committed",
    "budget_released",
    "approval_requested",
    "approval_decided",
    "approval_expired",
    "timer_created",
    "timer_fired",
    "timer_rescheduled",
    "timer_cancelled",
    "action_recorded",
    "action_resolved",
    "artifact_recorded",
    "binding_recorded",
    "binding_updated",
    "outbox_enqueued",
    "inbox_received",
    "inbox_processed",
    "session_started",
    "session_checkpointed",
    "session_abandoned",
    # Fleet (Swarm Phase 2). Worker events chain under mission_id "";
    # dispatch events chain under the envelope's real mission.
    "worker_enrolled",
    "worker_updated",
    "worker_transitioned",
    "dispatch_created",
    "dispatch_transitioned",
})


# ---------------------------------------------------------------------------
# Mission spec
# ---------------------------------------------------------------------------

class MissionKind:
    STANDARD = "standard"
    SCHEDULED_PROMPT = "scheduled_prompt"

    ALL = frozenset({STANDARD, SCHEDULED_PROMPT})


#: Session bounds a spec may override, with hard ceilings the kernel
#: enforces regardless of what the spec asks for.
SESSION_DEFAULTS = {
    "session_wall_seconds": 600,
    "session_max_tool_rounds": 15,
    "session_token_budget": 200000,
}
SESSION_CEILINGS = {
    "session_wall_seconds": 3600,
    "session_max_tool_rounds": 50,
    "session_token_budget": 2000000,
}


def normalize_spec(spec: Dict[str, Any],
                   clock: Optional[Any] = None) -> Dict[str, Any]:
    """Validate and normalize a mission spec dict (fail closed).

    Required: ``goal`` (non-empty string). Everything else defaults:
    ``success_criteria``/``constraints`` (lists of strings), ``budgets``
    (integer units per named line), ``cadence_seconds``, ``channel``,
    ``dry_run`` (default True — going live is an explicit operator act),
    ``kind``, ``prompt`` (scheduled_prompt only), ``run_once``,
    ``misfire_policy``, ``catch_up_limit``, and session bounds.
    """
    if not isinstance(spec, dict):
        raise KernelError("mission spec must be a dict")
    known = {
        "goal", "success_criteria", "constraints", "budgets",
        "cadence_seconds", "channel", "dry_run", "kind", "prompt",
        "run_once", "misfire_policy", "catch_up_limit",
        "session_wall_seconds", "session_max_tool_rounds",
        "session_token_budget", "principal", "notify", "capitol",
    }
    unknown = set(spec) - known
    if unknown:
        raise KernelError(
            f"mission spec has unknown field(s) {sorted(unknown)} — "
            "failing closed"
        )
    goal = str(spec.get("goal") or "").strip()
    if not goal:
        raise KernelError("mission spec requires a non-empty goal")
    kind = str(spec.get("kind") or MissionKind.STANDARD)
    if kind not in MissionKind.ALL:
        raise KernelError(f"unknown mission kind {kind!r}")
    prompt = str(spec.get("prompt") or "")
    if kind == MissionKind.SCHEDULED_PROMPT and not prompt.strip():
        raise KernelError("scheduled_prompt missions require a prompt")

    def _str_list(key):
        value = spec.get(key) or []
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) for item in value
        ):
            raise KernelError(f"mission spec {key} must be a list of strings")
        return [item for item in value if item.strip()]

    budgets_in = spec.get("budgets") or {}
    if not isinstance(budgets_in, dict):
        raise KernelError("mission spec budgets must be a dict")
    budgets: Dict[str, int] = {}
    for line, cap in budgets_in.items():
        if isinstance(cap, bool) or not isinstance(cap, int):
            raise KernelError(
                f"budget line {line!r} must be an integer unit cap"
            )
        if cap < 0:
            raise KernelError(f"budget line {line!r} must not be negative")
        budgets[str(line)] = cap

    try:
        cadence = int(spec.get("cadence_seconds", 86400))
    except (TypeError, ValueError):
        raise KernelError("cadence_seconds must be an integer")
    if cadence < 0:
        raise KernelError("cadence_seconds must not be negative")

    misfire = str(spec.get("misfire_policy") or MisfirePolicy.COALESCE)
    if misfire not in MisfirePolicy.ALL:
        raise KernelError(f"unknown misfire_policy {misfire!r}")
    notify = str(spec.get("notify") or "milestones")
    if notify not in ("sessions", "milestones"):
        raise KernelError(
            f"notify must be 'sessions' or 'milestones', got {notify!r}"
        )
    try:
        catch_up_limit = int(spec.get("catch_up_limit", 5))
    except (TypeError, ValueError):
        raise KernelError("catch_up_limit must be an integer")
    if catch_up_limit < 1:
        raise KernelError("catch_up_limit must be at least 1")

    capitol_raw = spec.get("capitol") or {}
    if not isinstance(capitol_raw, dict):
        raise KernelError("mission spec capitol must be a dict")
    capitol_known = {"workflows", "allow_start", "allow_respond",
                     "max_runs"}
    capitol_unknown = set(capitol_raw) - capitol_known
    if capitol_unknown:
        raise KernelError(
            f"mission spec capitol has unknown field(s) "
            f"{sorted(capitol_unknown)} — failing closed"
        )
    capitol: Dict[str, Any] = {}
    if capitol_raw:
        workflows = capitol_raw.get("workflows") or []
        if not isinstance(workflows, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip()
            for item in workflows
        ):
            raise KernelError(
                "capitol.workflows must be a list of workflow id strings"
            )
        try:
            max_runs = int(capitol_raw.get("max_runs", 3))
        except (TypeError, ValueError):
            raise KernelError("capitol.max_runs must be an integer")
        if max_runs < 0:
            raise KernelError("capitol.max_runs must not be negative")
        capitol = {
            "workflows": list(workflows),
            "allow_start": bool(capitol_raw.get("allow_start", False)),
            "allow_respond": bool(capitol_raw.get("allow_respond", False)),
            "max_runs": max_runs,
        }
        if capitol["allow_start"] and not capitol["workflows"]:
            raise KernelError(
                "capitol.allow_start requires a non-empty "
                "capitol.workflows allowlist"
            )

    normalized: Dict[str, Any] = {
        "goal": goal,
        "capitol": capitol,
        "success_criteria": _str_list("success_criteria"),
        "constraints": _str_list("constraints"),
        "budgets": budgets,
        "cadence_seconds": cadence,
        "channel": str(spec.get("channel") or ""),
        "dry_run": bool(spec.get("dry_run", True)),
        "kind": kind,
        "prompt": prompt,
        "run_once": bool(spec.get("run_once", False)),
        "misfire_policy": misfire,
        "catch_up_limit": min(catch_up_limit, CATCH_UP_HARD_CAP),
        "principal": str(spec.get("principal") or "user"),
        "notify": notify,
    }
    for key, default in SESSION_DEFAULTS.items():
        raw = spec.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise KernelError(f"mission spec {key} must be an integer")
        if raw < 1:
            raise KernelError(f"mission spec {key} must be positive")
        normalized[key] = min(raw, SESSION_CEILINGS[key])
    return normalized
