"""Mission critic sessions: scheduled, cheap self-review with deterministic
stall detection (roadmap: mission judgment and shared memory).

A review session scores mission progress against the spec's success
criteria and lands one of three actions as kernel events:

- ``continue`` — progress is real; nothing changes.
- ``re-plan`` — a new numbered plan version through the existing plans
  machinery, journaled with an explicit ``plan_revised`` event + rationale.
- ``escalate`` — a channel notification through the existing outbox path
  (never silent) when the criteria themselves look wrong or the mission is
  unrecoverable.

Discipline split, matching the roadmap's "models propose; deterministic
policy authorizes":

- **Stall detection is deterministic and happens BEFORE the model sees
  anything** (:func:`stall_signals`): material state change is defined via
  kernel events, repeated failure via task attempts and checkpoint errors.
- The model proposes the verdict; :func:`resolve_outcome` enforces the
  rules (a deterministically stalled mission may never "continue"; an
  unusable model on a stalled mission escalates rather than staying
  silent).

Review sessions run on the weak model when configured (``weak_model``),
falling back to the main model; they make exactly one model call with no
tools and a small bounded context — spec + criteria + plan + stall
metrics + journal tail + budgets.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from .model import MissionKind
from .store import MissionStore

#: Kernel event kinds that count as material mission-state change for
#: stall detection. Checkpoints, notes, budget bookkeeping, and timers are
#: deliberately absent: every session produces those mechanically, so they
#: can never distinguish progress from spinning.
MATERIAL_EVENT_KINDS = (
    "plan_recorded",
    "task_created",
    "task_transitioned",
    "action_recorded",
    "action_resolved",
    "artifact_recorded",
    "approval_requested",
    "binding_recorded",
    "binding_updated",
)

#: Events that mark a completed review pass (recorded or skipped); the
#: cadence clock restarts from the latest one.
REVIEW_MARKER_KINDS = ("review_recorded", "review_skipped")

#: Same failure repeated this many times within the window = a stall.
REPEAT_FAILURE_MIN = 2

#: Hard cap on how many recent work sessions one review inspects.
STALL_WINDOW_CAP = 10

#: Hard ceiling on the review context, well under the work-session bound.
REVIEW_CONTEXT_CHARS = 7000

REVIEW_DEFAULTS = {
    "enabled": True,
    "every_sessions": 5,       # review after this many work sessions ...
    "every_seconds": 86400,    # ... or daily, whichever comes first
    "stall_sessions": 3,       # N: no-material-change window
    "token_budget": 12000,
    "wall_seconds": 180,
}

VERDICTS = ("met", "on-track", "stalled", "at-risk")
ACTIONS = ("continue", "re-plan", "escalate")

REVIEW_SYSTEM_PROMPT = (
    "You are the mission critic for a durable, headless mission. You are "
    "NOT the worker: do no mission work and call no tools. Judge progress "
    "strictly from the mission context provided.\n\n"
    "Score EVERY success criterion as one of: met, on-track, stalled, "
    "at-risk — each with one line of evidence naming a concrete fact from "
    "the journal tail or checkpoint. Then choose exactly one action:\n"
    "- continue: progress is real and the plan is sound.\n"
    "- re-plan: progress stalled or the plan no longer fits; provide a "
    "full replacement plan as a list of concrete steps.\n"
    "- escalate: the success criteria themselves look wrong or "
    "unachievable, or the mission cannot recover without a human; say "
    "why.\n\n"
    "The deterministic stall detector's verdict is included in the "
    "context. If it says STALLED, 'continue' is not allowed: choose "
    "re-plan (preferred when a better approach exists) or escalate.\n\n"
    "Respond with ONLY one JSON object, no prose, exactly this shape:\n"
    '{"criteria": [{"criterion": "...", "verdict": '
    '"met|on-track|stalled|at-risk", "evidence": "..."}], '
    '"action": "continue|re-plan|escalate", "rationale": "...", '
    '"plan": ["step", "..."]}\n'
    "The plan array is required for re-plan and must be omitted or empty "
    "otherwise."
)


def _clip(text: str, cap: int) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    return text[: cap - 15] + "\n... [clipped]"


# ---------------------------------------------------------------------------
# Policy resolution (spec overrides config overrides defaults)
# ---------------------------------------------------------------------------

def resolve_policy(spec: Dict[str, Any],
                   config: Optional[dict]) -> Dict[str, Any]:
    """Effective review policy for one mission.

    Spec ``review`` keys win over config ``mission_review_*`` keys over
    :data:`REVIEW_DEFAULTS`. Scheduled prompts are never reviewed — they
    carry no plan or success criteria to judge.
    """
    from ..config import get_bool, get_int

    config = config or {}
    review = spec.get("review") or {}
    policy = dict(REVIEW_DEFAULTS)
    policy["enabled"] = get_bool(config, "mission_reviews", True)
    for key in ("every_sessions", "every_seconds", "stall_sessions",
                "token_budget", "wall_seconds"):
        policy[key] = get_int(config, f"mission_review_{key}", policy[key])
    for key, value in review.items():
        policy[key] = value
    if spec.get("kind") == MissionKind.SCHEDULED_PROMPT:
        policy["enabled"] = False
    policy["every_sessions"] = max(1, int(policy["every_sessions"]))
    policy["every_seconds"] = max(0, int(policy["every_seconds"]))
    policy["stall_sessions"] = max(2, int(policy["stall_sessions"]))
    return policy


# ---------------------------------------------------------------------------
# Deterministic cadence + stall detection (no model involved)
# ---------------------------------------------------------------------------

def review_marker(store: MissionStore,
                  mission_id: str) -> Optional[Dict[str, Any]]:
    return store.last_event(mission_id, REVIEW_MARKER_KINDS)

def review_due(store: MissionStore, mission: Dict[str, Any],
               policy: Dict[str, Any], now: float) -> bool:
    """True when this mission owes a review: at least one work session has
    checkpointed since the last review marker AND (``every_sessions`` work
    sessions have accumulated OR ``every_seconds`` has elapsed)."""
    if not policy.get("enabled"):
        return False
    mission_id = mission["mission_id"]
    marker = review_marker(store, mission_id)
    since_seq = marker["seq"] if marker else 0
    since_time = (
        marker["created_at"] if marker else float(mission["created_at"])
    )
    sessions = store.count_events_since(
        mission_id, ("session_checkpointed",), since_seq
    ).get("session_checkpointed", 0)
    if sessions < 1:
        return False
    if sessions >= int(policy["every_sessions"]):
        return True
    every_seconds = int(policy["every_seconds"])
    return every_seconds > 0 and (now - since_time) >= every_seconds


def stall_signals(store: MissionStore, mission_id: str,
                  policy: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic stall detection over kernel events — computed BEFORE
    the model sees anything, and journaled with the verdict.

    Definition implemented here — a mission is **stalled** when either:

    1. **No material change**: at least ``stall_sessions`` (N) work
       sessions have checkpointed since the last review marker, and the
       journal across that window contains zero events from
       :data:`MATERIAL_EVENT_KINDS` (plan versions, task deltas, external
       actions, artifacts, approval requests, resource bindings) — every
       session ran, none of them moved durable mission state; or
    2. **Repeated failure of the same step**: within the window, one task
       accumulated ≥ ``REPEAT_FAILURE_MIN`` failed attempts, or the last
       ≥ ``REPEAT_FAILURE_MIN`` consecutive session checkpoints carry the
       same non-empty error.
    """
    marker = review_marker(store, mission_id)
    marker_seq = marker["seq"] if marker else 0
    checkpoint_seqs = [
        event["seq"] for event in store.events_since(
            mission_id, marker_seq, ("session_checkpointed",),
            limit=STALL_WINDOW_CAP + 1,
        )
    ]
    window = len(checkpoint_seqs)
    boundary_seq = marker_seq
    material = store.count_events_since(
        mission_id, MATERIAL_EVENT_KINDS, boundary_seq
    )
    material_total = sum(material.values())
    stall_sessions = int(policy["stall_sessions"])
    no_material_change = window >= stall_sessions and material_total == 0

    # Repeated failure of the same step, window-scoped: failed
    # attempt_finished events since the boundary, grouped per task through
    # the task_attempts projection (the event carries the attempt id).
    failed_attempts = [
        str(event["data"].get("attempt_id") or "")
        for event in store.events_since(
            mission_id, boundary_seq, ("attempt_finished",), limit=200
        )
        if event["data"].get("state") == "failed"
    ]
    failed_by_task: Dict[str, int] = {}
    if failed_attempts:
        rows = store._read_conn().execute(
            "SELECT task_id FROM task_attempts WHERE attempt_id IN (%s)"
            % ",".join("?" for _ in failed_attempts),
            failed_attempts,
        ).fetchall()
        for (task_id,) in rows:
            failed_by_task[str(task_id)] = (
                failed_by_task.get(str(task_id), 0) + 1
            )
    repeat_detail = ""
    repeated_task = ""
    for task_id, count in sorted(failed_by_task.items()):
        if count >= REPEAT_FAILURE_MIN:
            repeated_task = task_id
            repeat_detail = (
                f"task {task_id} failed {count} attempts"
            )
            break

    if not repeat_detail and window >= REPEAT_FAILURE_MIN:
        errors: List[str] = []
        for row in store._read_conn().execute(
            "SELECT state FROM checkpoints WHERE mission_id=? ORDER BY"
            " created_at DESC, checkpoint_id DESC LIMIT ?",
            (mission_id, REPEAT_FAILURE_MIN),
        ).fetchall():
            try:
                state = json.loads(row[0] or "{}")
            except ValueError:
                state = {}
            errors.append(str(state.get("error") or ""))
        if (
            len(errors) >= REPEAT_FAILURE_MIN
            and errors[0]
            and all(error == errors[0] for error in errors)
        ):
            repeat_detail = (
                f"the last {len(errors)} sessions failed with the same"
                f" error: {errors[0][:120]}"
            )

    repeated_failure = bool(repeat_detail)
    return {
        "window_sessions": window,
        "stall_sessions_required": stall_sessions,
        "material_events": {k: material[k] for k in sorted(material)},
        "material_total": material_total,
        "no_material_change": no_material_change,
        "repeated_failure": repeated_failure,
        "repeat_detail": repeat_detail,
        "repeated_task_id": repeated_task,
        "stalled": no_material_change or repeated_failure,
        "since_seq": boundary_seq,
    }


# ---------------------------------------------------------------------------
# Bounded review context
# ---------------------------------------------------------------------------

def build_review_context(store: MissionStore, mission: Dict[str, Any],
                         signals: Dict[str, Any]) -> str:
    mission_id = mission["mission_id"]
    spec = mission["spec"]
    parts: List[str] = []
    head = [
        f"Mission review — {mission_id} (work sessions completed:"
        f" {mission['runs']}, {signals['window_sessions']} since the last"
        " review)",
        f"Goal: {spec.get('goal', '')}",
    ]
    criteria = spec.get("success_criteria") or []
    if criteria:
        head.append("Success criteria:")
        head.extend(
            f"  {i + 1}. {criterion}"
            for i, criterion in enumerate(criteria)
        )
    else:
        head.append(
            "Success criteria: (none declared — judge against the goal)"
        )
    if spec.get("constraints"):
        head.append("Constraints: " + "; ".join(spec["constraints"]))
    parts.append(_clip("\n".join(head), 1800))

    plan = store.latest_plan(mission_id)
    if plan:
        steps = plan["content"].get("steps", [])
        parts.append(_clip(
            "Current plan (v%s):\n%s" % (
                plan["version"],
                "\n".join(
                    f"  {i + 1}. {step}" for i, step in enumerate(steps)
                ),
            ),
            1500,
        ))
    else:
        parts.append("Current plan: (none recorded)")

    checkpoint = store.latest_checkpoint(mission_id)
    if checkpoint:
        parts.append(_clip(
            "Latest checkpoint:\n" + checkpoint["summary"], 1400
        ))

    material = signals["material_events"]
    stall_lines = [
        "Deterministic stall detector (computed from kernel events,"
        " before this review):",
        "  material events since last review: "
        + (
            ", ".join(f"{kind}={count}" for kind, count in material.items())
            or "none"
        ),
        f"  window: {signals['window_sessions']} work session(s)"
        f" (stall threshold: {signals['stall_sessions_required']})",
    ]
    if signals["repeat_detail"]:
        stall_lines.append(f"  repeated failure: {signals['repeat_detail']}")
    stall_lines.append(
        "  verdict: %s" % ("STALLED" if signals["stalled"] else "not stalled")
    )
    parts.append("\n".join(stall_lines))

    events = store.event_tail(mission_id, limit=12)
    event_lines = []
    for event in events:
        data = event["data"]
        brief = {
            key: data[key] for key in ("to", "reason", "summary", "text",
                                       "status", "error", "action")
            if data.get(key)
        }
        event_lines.append(f"  - {event['kind']} {brief}")
    if event_lines:
        parts.append(_clip(
            "Journal tail (oldest first):\n" + "\n".join(event_lines), 2200
        ))

    budgets = store.budget_status(mission["root_scope_id"])
    if budgets:
        parts.append(_clip(
            "Remaining budgets:\n" + "\n".join(
                f"  - {line}: {values['available']} of {values['cap']} left"
                for line, values in budgets.items()
            ),
            600,
        ))
    return _clip("\n\n".join(parts), REVIEW_CONTEXT_CHARS)


# ---------------------------------------------------------------------------
# The default runner: one weak-model call, no tools
# ---------------------------------------------------------------------------

def default_runner(config: dict) -> Callable:
    """One bounded model call on the weak model (fallback: main model).

    Returns ``fn(mission, context, signals) -> (reply_text, usage, error)``.
    Never raises; errors come back as the third element.
    """

    def run(mission: Dict[str, Any], context: str,
            signals: Dict[str, Any]):
        from ..providers import RAW_FNS
        from ..runtime import is_error_response, weak_model_config

        cfg = weak_model_config(config) or dict(config or {})
        provider = str(cfg.get("provider") or "").lower()
        raw_fn = RAW_FNS.get(provider)
        if raw_fn is None:
            return "", {}, f"no provider available for reviews ({provider!r})"
        messages = [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": (
                context + "\n\nReturn the JSON verdict now."
            )},
        ]
        try:
            response = raw_fn(cfg, messages, None)
        except Exception as exc:
            return "", {}, f"{type(exc).__name__}: {exc}"
        if is_error_response(response):
            return "", {}, str(response.get("_error") or "provider error")
        usage = response.get("_usage") or {}
        if isinstance(usage, dict):
            usage = dict(usage)
            usage.setdefault("model", response.get("_model", ""))
        return str(response.get("content") or ""), usage, ""

    return run


# ---------------------------------------------------------------------------
# Verdict parsing + deterministic outcome resolution
# ---------------------------------------------------------------------------

def parse_verdict(text: str) -> Optional[Dict[str, Any]]:
    """Extract and validate the critic's JSON verdict, or None."""
    text = str(text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action == "replan":
        action = "re-plan"
    if action not in ACTIONS:
        return None
    criteria = []
    for row in (raw.get("criteria") or [])[:12]:
        if not isinstance(row, dict):
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        criteria.append({
            "criterion": str(row.get("criterion") or "")[:200],
            "verdict": verdict,
            "evidence": str(row.get("evidence") or "")[:240],
        })
    plan_steps = [
        str(step).strip()[:300]
        for step in (raw.get("plan") or [])[:20]
        if str(step).strip()
    ]
    return {
        "action": action,
        "criteria": criteria,
        "rationale": str(raw.get("rationale") or "")[:600],
        "plan": plan_steps,
    }


def resolve_outcome(verdict: Optional[Dict[str, Any]],
                    signals: Dict[str, Any],
                    error: str) -> Optional[Dict[str, Any]]:
    """Deterministic authority over the model's proposal.

    Returns the final outcome dict (action/criteria/rationale/plan), or
    None meaning "skip this review" (journaled by the caller). Rules:

    - Unusable model output on a **stalled** mission escalates — a
      detected stall is never silently dropped. On a non-stalled mission
      it is a journaled skip (the next cadence retries).
    - A stalled mission may never "continue": a continue proposal is
      escalated with the stall evidence.
    - "re-plan" without a usable plan escalates when stalled, otherwise
      degrades to continue with the malformation noted.
    """
    stalled = bool(signals.get("stalled"))
    if verdict is None:
        if stalled:
            detail = signals.get("repeat_detail") or (
                "no material state change across"
                f" {signals.get('window_sessions')} sessions"
            )
            return {
                "action": "escalate",
                "criteria": [],
                "rationale": (
                    f"deterministic stall detected ({detail}) and the"
                    " review model was unusable"
                    + (f": {error}" if error else "")
                ),
                "plan": [],
            }
        return None
    outcome = dict(verdict)
    if stalled and outcome["action"] == "continue":
        detail = signals.get("repeat_detail") or (
            "no material state change across"
            f" {signals.get('window_sessions')} sessions"
        )
        outcome["action"] = "escalate"
        outcome["rationale"] = (
            f"deterministic stall detected ({detail}); the critic proposed"
            " no change — escalating instead of staying silent. "
            + outcome.get("rationale", "")
        ).strip()
    if outcome["action"] == "re-plan" and not outcome.get("plan"):
        if stalled:
            outcome["action"] = "escalate"
            outcome["rationale"] = (
                "re-plan chosen but no usable plan was produced; the"
                " mission is deterministically stalled. "
                + outcome.get("rationale", "")
            ).strip()
        else:
            outcome["action"] = "continue"
            outcome["rationale"] = (
                "(re-plan requested but no plan provided — treated as"
                " continue) " + outcome.get("rationale", "")
            ).strip()
    outcome["plan"] = outcome.get("plan") if (
        outcome["action"] == "re-plan"
    ) else []
    return outcome
