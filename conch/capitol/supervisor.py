"""Mission↔Capitol supervision (Swarm Phase 3).

Two pieces share this module because they share one contract — the
``capitol_run`` resource binding and its idempotency conventions:

- :class:`CapitolSupervisor` runs inside the ``conch-edge`` daemon tick.
  Each pass advances the persisted event cursor for every supervised run
  binding (batch ``get_workflow_events`` polling — SSE belongs to
  interactive sessions), translates run events into mission inbox events
  (waking missions on HITL/terminal/failure), maps Capitol HITL requests
  into origin-bound expiring kernel approvals whose decision flows back
  as the HITL reply, maps mission abort onto ``stop_workflow``/
  ``CancelTask``, and degrades cleanly when Capitol is unreachable —
  bindings back off with exponential retry and resume from the persisted
  cursor when the stack returns. No cloud fallback exists on this path.

- :class:`CapitolControlClient` is the bounded ``capitol_control`` tool a
  mission session receives **only** when its spec's ``capitol`` envelope
  grants authority. The spec — never the prompt — decides which workflows
  may start, whether clarifications may be answered, and how many runs a
  mission may bind. Every effect passes the required-policy registry and
  the kernel external-action ledger first.

Exactly-once discipline: run starts, HITL replies, and cancels are
ledgered external actions with deterministic idempotency keys
(``capitol:{mission}:{workflow}:rN`` / ``capitol:hitl:{run}:{request}`` /
``capitol:cancel:{run}``); the same keys ride the wire as Capitol
idempotency keys, so a crash between ledger and wire replays into the
same run instead of a duplicate.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from ..policy import evaluate_required_policy
from ..swarm.protocol import ActionClass
from .client import (
    TERMINAL_RUN_STATUSES,
    CapitolRuntime,
)
from .errors import CapitolAuthError, CapitolError

#: Binding kind for supervised Capitol runs.
RUN_BINDING_KIND = "capitol_run"

#: Approval action kinds for Capitol HITL checkpoints.
APPROVAL_INTERVENTION = "capitol.hitl.intervention"
APPROVAL_CLARIFICATION = "capitol.hitl.clarification"

DEFAULT_POLL_SECONDS = 10.0
DEFAULT_HITL_TTL_SECONDS = 1800.0
BACKOFF_BASE_SECONDS = 15.0
BACKOFF_CAP_SECONDS = 900.0

#: Fallback clarification reply when an operator approves the checkpoint
#: without supplying text (the workflow regains autonomy instead of
#: waiting out its server-side timeout).
DEFAULT_CLARIFICATION_REPLY = (
    "No additional information available; proceed with your best judgment."
)

_TERMINAL_BINDING_FOR_RUN = {
    "success": "completed",
    "failed": "failed",
    "stopped": "cancelled",
    "cancelled": "cancelled",
}


def run_start_idempotency_key(mission_id: str, workflow_id: str,
                              attempt: int) -> str:
    return f"capitol:{mission_id}:{workflow_id}:r{int(attempt)}"


def hitl_idempotency_key(run_id: str, request_id: str) -> str:
    return f"capitol:hitl:{run_id}:{request_id}"


def cancel_idempotency_key(run_id: str) -> str:
    return f"capitol:cancel:{run_id}"


def _clip(text: Any, cap: int = 500) -> str:
    text = str(text if text is not None else "")
    return text if len(text) <= cap else text[:cap] + "…"


def _hitl_fields(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract request_id/node_id/prompt/kind from a ``node.input_required``
    event (both the flat and ``extra``-nested layouts)."""
    data = event.get("data") or {}
    extra = data.get("extra") or {}
    node = event.get("node") or {}
    return {
        "request_id": str(data.get("request_id")
                          or extra.get("request_id") or ""),
        "node_id": str(node.get("node_id") or data.get("node_id") or ""),
        "prompt": _clip(data.get("prompt") or extra.get("prompt")
                        or "The workflow needs input to continue.", 2000),
        "kind": (
            "clarification"
            if str(data.get("input_kind") or extra.get("input_kind") or "")
            == "clarification" else "intervention"
        ),
    }


class CapitolSupervisor:
    """Daemon-tick supervisor for ``capitol_run`` bindings."""

    def __init__(self, store, config: dict, *,
                 runtime_factory: Optional[Callable[[], CapitolRuntime]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.config = config or {}
        self._runtime_factory = runtime_factory
        self._runtime: Optional[CapitolRuntime] = None
        self._log = log or (lambda line: None)
        self.clock = clock

    # -- runtime -----------------------------------------------------------

    def runtime(self) -> CapitolRuntime:
        if self._runtime is None:
            factory = self._runtime_factory or (
                lambda: CapitolRuntime.from_config(self.config)
            )
            self._runtime = factory()
        return self._runtime

    def _invalidate_runtime(self) -> None:
        """Drop the cached runtime so the next pass re-resolves the bearer
        (rotation-friendly; never cached across auth failures)."""
        self._runtime = None

    # -- the pass ----------------------------------------------------------

    def tick(self) -> Dict[str, int]:
        """One supervision pass over every supervised run binding.

        Per-binding failures never abort the pass; unreachable-Capitol
        errors degrade the binding with backoff and the pass moves on.
        """
        from ..kernel.model import BindingStatus

        stats = {"polled": 0, "events": 0, "hitl": 0, "woken": 0,
                 "terminal": 0, "degraded": 0, "cancelled": 0}
        now = float(self.clock())
        bindings = self.store.find_bindings(
            kind=RUN_BINDING_KIND, statuses=BindingStatus.SUPERVISED
        )
        for binding in bindings:
            detail = binding.get("detail") or {}
            if float(detail.get("next_poll_at") or 0) > now:
                continue
            try:
                self._supervise(binding, stats)
            except CapitolAuthError as exc:
                self._invalidate_runtime()
                self._degrade(binding, f"auth: {exc}", stats,
                              auth_needed=True)
            except CapitolError as exc:
                if exc.retryable is False:
                    self._log(
                        f"capitol binding {binding['binding_id']}: "
                        f"non-retryable error: {_clip(exc, 200)}"
                    )
                    # a status probe next pass decides what became of the
                    # run; never blind-retry a refused operation
                    self._reschedule(binding, BACKOFF_BASE_SECONDS)
                else:
                    self._degrade(binding, str(exc), stats)
            except Exception as exc:  # never break the daemon tick
                self._log(
                    f"capitol binding {binding['binding_id']}: "
                    f"{type(exc).__name__}: {_clip(exc, 200)}"
                )
                self._reschedule(binding, BACKOFF_BASE_SECONDS)
        return stats

    # -- per-binding supervision ---------------------------------------------

    def _supervise(self, binding: Dict[str, Any],
                   stats: Dict[str, int]) -> None:
        from ..kernel.model import MissionState

        mission = self.store.get_mission(binding["mission_id"])
        if mission is None:
            self._finish(binding, "cancelled", "mission row missing")
            return
        run_id = str((binding.get("resource") or {}).get("run_id") or "")
        if not run_id:
            self._finish(binding, "failed", "binding has no run_id")
            return
        if mission["stop_requested"] or (
            mission["status"] in MissionState.TERMINAL
        ):
            self._cancel_run(binding, mission, run_id, stats)
            return
        stats["polled"] += 1
        self._poll(binding, mission, run_id, stats)

    def _poll(self, binding: Dict[str, Any], mission: Dict[str, Any],
              run_id: str, stats: Dict[str, int]) -> None:
        from ..kernel.model import BindingStatus

        runtime = self.runtime()
        cursor = int(binding.get("cursor") or 0)
        payload = runtime.run_events(run_id, since_sequence=cursor + 1)
        events = (payload or {}).get("events")
        events = events if isinstance(events, list) else []
        detail = dict(binding.get("detail") or {})
        was_degraded = binding.get("status") == BindingStatus.DEGRADED
        pending_hitl = None
        new_cursor = cursor
        for event in events:
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, (int, float)):
                if int(sequence) <= cursor:
                    continue  # server re-sent below our cursor
                new_cursor = max(new_cursor, int(sequence))
            kind = str(event.get("event_type") or "")
            if kind == "node.input_required":
                pending_hitl = dict(_hitl_fields(event),
                                    sequence=int(new_cursor))
                stats["events"] += 1
            elif kind in ("workflow.run_failed", "node.node_failed"):
                self._inbox(
                    binding, mission,
                    f"failure:{new_cursor}",
                    f"[capitol] run {run_id} reported {kind}: "
                    + _clip((event.get('data') or {}).get('error')
                            or (event.get('data') or {}).get('message')
                            or "", 300),
                )
                stats["events"] += 1

        status_payload = runtime.run_status(run_id)
        run_state = str((status_payload or {}).get("status") or "").lower()

        # Reaching here means Capitol answered: end any outage episode.
        if was_degraded or detail.get("outage_started"):
            self._inbox(
                binding, mission,
                f"recovered:{detail.get('outage_started', 0)}",
                f"[capitol] reachable again; resuming run {run_id} "
                f"supervision from cursor {new_cursor}",
            )
            detail.pop("outage_started", None)
        detail.pop("backoff_seconds", None)
        detail.pop("next_poll_at", None)
        detail.pop("last_error", None)

        if run_state in TERMINAL_RUN_STATUSES:
            final = _TERMINAL_BINDING_FOR_RUN.get(run_state, "failed")
            detail.pop("pending_hitl", None)
            detail["final_state"] = run_state
            error_message = _clip(
                (status_payload or {}).get("error_message") or "", 300
            )
            if error_message:
                detail["final_error"] = error_message
            self.store.update_binding(
                binding["binding_id"], status=final,
                cursor=new_cursor, detail=detail,
            )
            self._inbox(
                binding, mission, "terminal",
                f"[capitol] run {run_id} ended {run_state}"
                + (f": {error_message}" if error_message else ""),
            )
            if self._wake(mission):
                stats["woken"] += 1
            stats["terminal"] += 1
            return

        if pending_hitl and not (
            (detail.get("pending_hitl") or {}).get("request_id")
            == pending_hitl["request_id"]
        ):
            # a new HITL checkpoint: park it behind a kernel approval
            approval = self._open_approval(
                binding, mission, run_id, pending_hitl
            )
            pending_hitl["approval_id"] = approval["approval_id"]
            detail["pending_hitl"] = pending_hitl
            self.store.update_binding(
                binding["binding_id"], status=BindingStatus.WAITING_HITL,
                cursor=new_cursor, detail=detail,
            )
            self._inbox(
                binding, mission,
                f"hitl:{pending_hitl['request_id']}",
                f"[capitol] run {run_id} needs input "
                f"({pending_hitl['kind']}): {pending_hitl['prompt'][:200]} "
                f"— parked as approval {approval['approval_id']} "
                "(decide via /approvals)",
            )
            if self._wake(mission):
                stats["woken"] += 1
            stats["hitl"] += 1
            return

        if detail.get("pending_hitl"):
            # an open checkpoint: relay a decided approval as the reply
            flushed = self._flush_hitl(binding, mission, run_id, detail)
            if flushed:
                detail.pop("pending_hitl", None)
                self.store.update_binding(
                    binding["binding_id"], status=BindingStatus.ACTIVE,
                    cursor=new_cursor, detail=detail,
                )
                return
            self.store.update_binding(
                binding["binding_id"], cursor=new_cursor, detail=detail,
            )
            return

        self.store.update_binding(
            binding["binding_id"], status=BindingStatus.ACTIVE,
            cursor=new_cursor, detail=detail,
        )

    # -- HITL ↔ approvals -----------------------------------------------------

    def _open_approval(self, binding: Dict[str, Any],
                       mission: Dict[str, Any], run_id: str,
                       hitl: Dict[str, Any]) -> Dict[str, str]:
        spec = mission.get("spec") or {}
        channel = str(spec.get("channel") or "")
        ttl = float(
            self.config.get("capitol_hitl_ttl_seconds")
            or DEFAULT_HITL_TTL_SECONDS
        )
        action_kind = (
            APPROVAL_CLARIFICATION if hitl["kind"] == "clarification"
            else APPROVAL_INTERVENTION
        )
        prompt = hitl["prompt"]
        args = {
            "binding_id": binding["binding_id"],
            "run_id": run_id,
            "request_id": hitl["request_id"],
            "node_id": hitl["node_id"],
            "kind": hitl["kind"],
            "prompt": prompt,
        }
        notify = {
            "text": (
                f"[conch mission {mission['mission_id']}] Capitol run "
                f"{run_id} is waiting on a {hitl['kind']}:\n\n{prompt}\n\n"
                "Decide with /approvals (approve = continue / proceed, "
                "deny = stop / decline)."
            ),
            "channel": channel,
        }
        return self.store.request_approval(
            mission["mission_id"], action_kind, args,
            origin_channel=channel or "local",
            ttl_seconds=ttl,
            notify_payload=notify,
            notify_dedupe_key=(
                f"capitol-hitl:{run_id}:{hitl['request_id']}"
            ),
        )

    def _flush_hitl(self, binding: Dict[str, Any], mission: Dict[str, Any],
                    run_id: str, detail: Dict[str, Any]) -> bool:
        """Relay a decided approval to Capitol. True when the checkpoint is
        settled (reply delivered, already answered, or expired)."""
        pending = detail.get("pending_hitl") or {}
        approval_id = str(pending.get("approval_id") or "")
        request_id = str(pending.get("request_id") or "")
        approval = (
            self.store.get_approval(approval_id) if approval_id else None
        )
        if approval is None:
            return True  # nothing to relay; poll continues
        if approval["status"] == "pending":
            return False
        if approval["status"] == "expired":
            self._inbox(
                binding, mission, f"hitl-expired:{request_id}",
                f"[capitol] approval {approval_id} for run {run_id} "
                "expired unanswered; the workflow's own HITL timeout "
                "now governs",
            )
            return True
        approved = approval["status"] == "approved"
        key = hitl_idempotency_key(run_id, request_id)
        action = self.store.record_action(
            mission["mission_id"], ActionClass.COMMUNICATE, key,
            task_id=str(binding.get("task_id") or ""),
            detail={"run_id": run_id, "request_id": request_id,
                    "verb": approval["status"]},
        )
        if action.get("duplicate") and action.get("status") == "committed":
            return True
        runtime = self.runtime()
        try:
            if pending.get("kind") == "clarification":
                runtime.submit_clarification(
                    run_id, request_id,
                    DEFAULT_CLARIFICATION_REPLY if approved else "",
                    declined=not approved,
                )
            else:
                runtime.submit_intervention(
                    run_id, str(pending.get("node_id") or ""), request_id,
                    "continue" if approved else "stop",
                )
        except CapitolError as exc:
            if exc.retryable is True:
                return False  # transport blip: retry next pass
            # refused (commonly: already answered / no longer pending) —
            # the checkpoint is settled either way; record the outcome
            self._resolve_quietly(action["action_id"], "committed", {
                "note": f"treated as settled: {_clip(exc, 200)}",
            })
            self._inbox(
                binding, mission, f"hitl-settled:{request_id}",
                f"[capitol] HITL reply for run {run_id} was not "
                f"deliverable ({_clip(exc, 150)}); continuing supervision",
            )
            return True
        self._resolve_quietly(action["action_id"], "committed", {
            "delivered": True,
        })
        self._inbox(
            binding, mission, f"hitl-answered:{request_id}",
            f"[capitol] {approval['status']} reply delivered to run "
            f"{run_id} ({pending.get('kind')})",
        )
        return True

    def _resolve_quietly(self, action_id: str, status: str,
                         detail: Dict[str, Any]) -> None:
        from ..kernel.model import KernelError

        try:
            self.store.resolve_action(action_id, status, detail)
        except KernelError:
            pass  # already resolved by an earlier pass

    # -- abort → cancel ---------------------------------------------------------

    def _cancel_run(self, binding: Dict[str, Any], mission: Dict[str, Any],
                    run_id: str, stats: Dict[str, int]) -> None:
        key = cancel_idempotency_key(run_id)
        action = self.store.record_action(
            mission["mission_id"], ActionClass.COMMUNICATE, key,
            task_id=str(binding.get("task_id") or ""),
            detail={"run_id": run_id, "reason": "mission aborted"},
        )
        if not (action.get("duplicate")
                and action.get("status") == "committed"):
            runtime = self.runtime()
            try:
                runtime.stop_run(run_id, reason="conch mission aborted")
            except CapitolError as exc:
                if isinstance(exc, CapitolAuthError) or exc.retryable is True:
                    raise  # degrade path retries the cancel
                # refused stop (already terminal, or unsupported): try the
                # spec-level hard cancel, then reconcile from status
                try:
                    runtime.cancel_task(run_id)
                except CapitolError:
                    pass
                try:
                    status = runtime.run_status(run_id)
                    state = str((status or {}).get("status") or "").lower()
                except CapitolError:
                    state = ""
                if state and state not in TERMINAL_RUN_STATUSES:
                    self._resolve_quietly(
                        action["action_id"], "unknown",
                        {"note": f"stop refused; run still {state}"},
                    )
                    self._reschedule(binding, BACKOFF_BASE_SECONDS)
                    return
            self._resolve_quietly(action["action_id"], "committed", {
                "stopped": True,
            })
        self._finish(binding, "cancelled", "mission aborted")
        self._inbox(
            binding, mission, "cancelled",
            f"[capitol] run {run_id} stopped: mission aborted",
        )
        stats["cancelled"] += 1

    # -- degradation --------------------------------------------------------------

    def _degrade(self, binding: Dict[str, Any], error: str,
                 stats: Dict[str, int], *, auth_needed: bool = False) -> None:
        """Capitol unreachable (or credential refused): back off with the
        cursor intact. The binding is the retry state — no cloud fallback,
        and recovery resumes exactly from ``cursor``."""
        from ..kernel.model import BindingStatus

        detail = dict(binding.get("detail") or {})
        backoff = float(detail.get("backoff_seconds") or 0)
        backoff = min(
            max(BACKOFF_BASE_SECONDS, backoff * 2), BACKOFF_CAP_SECONDS
        )
        now = float(self.clock())
        first_failure = "outage_started" not in detail
        detail["backoff_seconds"] = backoff
        detail["next_poll_at"] = now + backoff
        detail["last_error"] = _clip(error, 300)
        detail.setdefault("outage_started", now)
        if auth_needed:
            detail["auth_needed"] = True
        self.store.update_binding(
            binding["binding_id"], status=BindingStatus.DEGRADED,
            detail=detail,
        )
        stats["degraded"] += 1
        if first_failure:
            mission = self.store.get_mission(binding["mission_id"])
            if mission is not None:
                self._inbox(
                    binding, mission, f"degraded:{detail['outage_started']}",
                    "[capitol] endpoint unreachable"
                    + (" (credential refused — operator action needed)"
                       if auth_needed else "")
                    + f"; run supervision parked with backoff "
                      f"(cursor {binding.get('cursor', 0)} preserved): "
                    + _clip(error, 200),
                )
            self._log(
                f"capitol degraded: binding {binding['binding_id']} "
                f"backing off {backoff:.0f}s ({_clip(error, 160)})"
            )

    def _reschedule(self, binding: Dict[str, Any], delay: float) -> None:
        detail = dict(binding.get("detail") or {})
        detail["next_poll_at"] = float(self.clock()) + float(delay)
        self.store.update_binding(binding["binding_id"], detail=detail)

    # -- kernel plumbing -------------------------------------------------------------

    def _finish(self, binding: Dict[str, Any], status: str,
                note: str) -> None:
        detail = dict(binding.get("detail") or {})
        detail.pop("pending_hitl", None)
        detail.pop("next_poll_at", None)
        detail.pop("backoff_seconds", None)
        detail["finished_note"] = note
        self.store.update_binding(
            binding["binding_id"], status=status, detail=detail,
        )

    def _inbox(self, binding: Dict[str, Any], mission: Dict[str, Any],
               marker: str, text: str) -> None:
        run_id = str((binding.get("resource") or {}).get("run_id") or "")
        self.store.receive_inbox(
            "capitol",
            f"capitol:{run_id or binding['binding_id']}:{marker}",
            {"text": text},
            mission["mission_id"],
        )

    def _wake(self, mission: Dict[str, Any]) -> bool:
        """Wake a parked mission so its next session sees the new state."""
        from ..kernel.model import KernelError, MissionState

        if mission["status"] not in (MissionState.WAITING_TIMER,
                                     MissionState.WAITING_INPUT):
            return False
        try:
            self.store.transition_mission(
                mission["mission_id"], MissionState.READY,
                reason="capitol event",
            )
            return True
        except KernelError:
            return False  # racing transition; the event tail explains


# ---------------------------------------------------------------------------
# The bounded mission tool
# ---------------------------------------------------------------------------

CAPITOL_CONTROL_TOOL = {
    "type": "function",
    "function": {
        "name": "capitol_control",
        "description": (
            "Drive governed Capitol workflows within this mission's "
            "envelope: start an allowlisted workflow run "
            "(start_capitol_run), check a bound run (check_run), or "
            "answer a pending clarification (respond_hitl) when the "
            "envelope allows it. Authority comes from the mission spec; "
            "runs are supervised durably by the daemon and survive this "
            "session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["start_capitol_run", "check_run",
                             "respond_hitl"],
                    "description": "The Capitol operation to perform.",
                },
                "workflow_id": {
                    "type": "string",
                    "description": (
                        "start_capitol_run: the workflow to start (must "
                        "be on the mission's allowlist)."
                    ),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "start_capitol_run: the workflow inputs object."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "description": "check_run / respond_hitl: the run.",
                },
                "response": {
                    "type": "string",
                    "description": (
                        "respond_hitl: the clarification answer text."
                    ),
                },
            },
            "required": ["op"],
        },
    },
}


class CapitolControlClient:
    """The ``capitol_control`` builtin for mission sessions.

    Every operation re-derives its authority from the mission spec's
    ``capitol`` envelope and the required-policy registry at call time —
    prompt content can never widen it. Results are plain text; failures
    are reported, never raised into the session loop.
    """

    def __init__(self, store, mission: Dict[str, Any], session_id: str,
                 config: dict,
                 runtime_factory: Optional[Callable[[], CapitolRuntime]] = None):
        self._store = store
        self._mission = mission
        self._session_id = session_id
        self._config = config or {}
        self._runtime_factory = runtime_factory
        self._runtime: Optional[CapitolRuntime] = None

    @staticmethod
    def _text(message: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": message}]}

    def _envelope(self) -> Dict[str, Any]:
        spec = self._mission.get("spec") or {}
        capitol = spec.get("capitol")
        return capitol if isinstance(capitol, dict) else {}

    def runtime(self) -> CapitolRuntime:
        if self._runtime is None:
            factory = self._runtime_factory or (
                lambda: CapitolRuntime.from_config(self._config)
            )
            self._runtime = factory()
        return self._runtime

    def call_tool(self, name: str, arguments: dict) -> Dict[str, Any]:
        try:
            return self._dispatch(arguments or {})
        except CapitolAuthError as exc:
            return self._text(
                f"capitol_control: credential needed — {exc}"
            )
        except CapitolError as exc:
            return self._text(f"capitol_control error: {exc}")
        except Exception as exc:
            from ..kernel.model import KernelError

            if isinstance(exc, KernelError):
                return self._text(f"capitol_control error: {exc}")
            raise

    def _dispatch(self, arguments: dict) -> Dict[str, Any]:
        op = str(arguments.get("op") or "").strip()
        if op == "start_capitol_run":
            return self._start_run(arguments)
        if op == "check_run":
            return self._check_run(arguments)
        if op == "respond_hitl":
            return self._respond_hitl(arguments)
        return self._text(f"Unknown capitol_control op {op!r}")

    # -- ops ------------------------------------------------------------------

    def _start_run(self, arguments: dict) -> Dict[str, Any]:
        envelope = self._envelope()
        mission = self._mission
        mission_id = mission["mission_id"]
        spec = mission.get("spec") or {}
        workflow_id = str(arguments.get("workflow_id") or "").strip()
        inputs = arguments.get("inputs")
        if not workflow_id:
            return self._text("start_capitol_run needs workflow_id")
        if inputs is not None and not isinstance(inputs, dict):
            return self._text("start_capitol_run inputs must be an object")
        if spec.get("dry_run", True):
            return self._text(
                "denied: this mission is dry_run — starting a Capitol "
                "run is an external action"
            )
        if not envelope.get("allow_start"):
            return self._text(
                "denied: the mission envelope does not grant "
                "capitol.allow_start"
            )
        allowed = [str(w) for w in envelope.get("workflows") or []]
        if workflow_id not in allowed:
            return self._text(
                f"denied: workflow {workflow_id!r} is not on the "
                f"mission's allowlist ({', '.join(allowed) or 'empty'})"
            )
        existing = self._store.find_bindings(
            kind=RUN_BINDING_KIND, mission_id=mission_id
        )
        max_runs = int(envelope.get("max_runs", 3))
        if len(existing) >= max_runs:
            return self._text(
                f"denied: this mission already bound {len(existing)} "
                f"run(s) (max_runs={max_runs})"
            )
        policy = evaluate_required_policy("capitol.run.start", {
            "mission_id": mission_id,
            "workflow_id": workflow_id,
            "allowlist": allowed,
        })
        if not policy.allowed:
            return self._text(
                f"denied by required policy: {policy.reason}"
            )
        attempt = len(existing) + 1
        key = run_start_idempotency_key(mission_id, workflow_id, attempt)
        # Reconcile a half-finished earlier attempt with the same key: the
        # gateway replays the same run for the same key, so re-invoking is
        # safe and converges on one binding.
        for binding in existing:
            if (binding.get("resource") or {}).get("idempotency_key") == key:
                return self._text(
                    f"run already bound for this attempt: "
                    f"{binding['resource'].get('run_id')} "
                    f"(binding {binding['binding_id']})"
                )
        action = self._store.record_action(
            mission_id, ActionClass.COMMUNICATE, key,
            detail={"workflow_id": workflow_id},
        )
        runtime = self.runtime()
        runtime.ensure_skill("call_workflow", "start_capitol_run")
        submission = runtime.call_workflow(
            workflow_id, inputs, idempotency_key=key,
        )
        run_id = str(submission.get("run_id") or "")
        binding_id = self._store.record_binding(
            mission_id, RUN_BINDING_KIND,
            {
                "org_id": runtime.org_id,
                "agent_id": runtime.agent_id,
                "workflow_id": workflow_id,
                "workflow_version": str(
                    submission.get("workflow_version_id")
                    or submission.get("version_id") or ""
                ),
                "context_id": runtime.context_id or "",
                "run_id": run_id,
                "session_id": str(submission.get("session_id") or ""),
                "idempotency_key": key,
            },
            status="active",
        )
        from ..kernel.model import KernelError

        try:
            self._store.resolve_action(
                action["action_id"], "committed",
                {"run_id": run_id, "binding_id": binding_id},
            )
        except KernelError:
            pass  # duplicate path: an earlier attempt resolved it
        return self._text(
            f"Capitol run {run_id} started for workflow {workflow_id} "
            f"(binding {binding_id}). The daemon supervises it durably; "
            "check_run reports progress."
        )

    def _check_run(self, arguments: dict) -> Dict[str, Any]:
        run_id = str(arguments.get("run_id") or "").strip()
        mission_id = self._mission["mission_id"]
        bindings = self._store.find_bindings(
            kind=RUN_BINDING_KIND, mission_id=mission_id
        )
        if not bindings:
            return self._text("this mission has no bound Capitol runs")
        if run_id:
            bindings = [
                binding for binding in bindings
                if (binding.get("resource") or {}).get("run_id") == run_id
            ]
            if not bindings:
                return self._text(
                    f"run {run_id!r} is not bound to this mission"
                )
        lines = []
        for binding in bindings:
            resource = binding.get("resource") or {}
            line = (
                f"run {resource.get('run_id')} "
                f"[{binding['status']}] workflow "
                f"{resource.get('workflow_id')} cursor {binding['cursor']}"
            )
            pending = (binding.get("detail") or {}).get("pending_hitl")
            if pending:
                line += (
                    f" — waiting on {pending.get('kind')} "
                    f"(approval {pending.get('approval_id')})"
                )
            lines.append(line)
        return self._text("\n".join(lines))

    def _respond_hitl(self, arguments: dict) -> Dict[str, Any]:
        envelope = self._envelope()
        mission_id = self._mission["mission_id"]
        if not envelope.get("allow_respond"):
            return self._text(
                "denied: the mission envelope does not grant "
                "capitol.allow_respond"
            )
        run_id = str(arguments.get("run_id") or "").strip()
        response = str(arguments.get("response") or "").strip()
        if not run_id or not response:
            return self._text("respond_hitl needs run_id and response")
        binding = None
        for candidate in self._store.find_bindings(
            kind=RUN_BINDING_KIND, mission_id=mission_id
        ):
            if (candidate.get("resource") or {}).get("run_id") == run_id:
                binding = candidate
                break
        if binding is None:
            return self._text(
                f"run {run_id!r} is not bound to this mission"
            )
        pending = (binding.get("detail") or {}).get("pending_hitl") or {}
        request_id = str(pending.get("request_id") or "")
        if not request_id:
            return self._text(
                f"run {run_id} has no pending HITL request"
            )
        if pending.get("kind") != "clarification":
            return self._text(
                "denied: interventions require a human approval "
                f"(pending approval {pending.get('approval_id')}); the "
                "envelope only allows answering clarifications"
            )
        policy = evaluate_required_policy("capitol.hitl.respond", {
            "mission_id": mission_id,
            "run_id": run_id,
            "request_id": request_id,
        })
        if not policy.allowed:
            return self._text(
                f"denied by required policy: {policy.reason}"
            )
        key = hitl_idempotency_key(run_id, request_id)
        action = self._store.record_action(
            mission_id, ActionClass.COMMUNICATE, key,
            detail={"run_id": run_id, "request_id": request_id,
                    "source": "mission"},
        )
        if action.get("duplicate") and action.get("status") == "committed":
            return self._text(
                "a reply for this request was already delivered"
            )
        runtime = self.runtime()
        runtime.submit_clarification(run_id, request_id, response)
        from ..kernel.model import BindingStatus, KernelError

        try:
            self._store.resolve_action(
                action["action_id"], "committed",
                {"delivered": True, "source": "mission"},
            )
        except KernelError:
            pass
        detail = dict(binding.get("detail") or {})
        detail.pop("pending_hitl", None)
        try:
            self._store.update_binding(
                binding["binding_id"], status=BindingStatus.ACTIVE,
                detail=detail,
            )
        except KernelError:
            pass
        return self._text(
            f"clarification answered for run {run_id}; supervision "
            "continues"
        )
