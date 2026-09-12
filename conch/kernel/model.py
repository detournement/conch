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

#: Kernel-local ID kinds. ``msn``/``task`` intentionally match the swarm
#: protocol so mission/task IDs are valid on the wire in later phases.
KERNEL_ID_KINDS = frozenset({
    "msn", "task", "pln", "ses", "ckpt", "apr", "tmr", "act", "att",
    "art", "bnd", "scp", "lse", "obx",
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
    "outbox_enqueued",
    "inbox_received",
    "inbox_processed",
    "session_started",
    "session_checkpointed",
    "session_abandoned",
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
        "session_token_budget", "principal",
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
    try:
        catch_up_limit = int(spec.get("catch_up_limit", 5))
    except (TypeError, ValueError):
        raise KernelError("catch_up_limit must be an integer")
    if catch_up_limit < 1:
        raise KernelError("catch_up_limit must be at least 1")

    normalized: Dict[str, Any] = {
        "goal": goal,
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
    }
    for key, default in SESSION_DEFAULTS.items():
        raw = spec.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise KernelError(f"mission spec {key} must be an integer")
        if raw < 1:
            raise KernelError(f"mission spec {key} must be positive")
        normalized[key] = min(raw, SESSION_CEILINGS[key])
    return normalized
