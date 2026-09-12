"""The distributed task plane (Swarm Phase 2): controller-side scheduling,
leases, fencing, heartbeats, retries, and brokered delegation.

The plane is the controller's view of dispatched work. It owns no worker
process; it drives workers through a :class:`WorkerTransport`-shaped client
(``call(op, args) -> RpcResponse``) supplied by a factory, and it records
every state change in the kernel's dispatch tables so the whole plane is
crash-recoverable and replayable.

Invariants enforced here:

- **Offer/start handshake**: a dispatch is offered (worker persists its
  receipt) then started; duplicates are safe because the worker returns
  the recorded receipt.
- **Controller epoch + fencing**: every offer carries the controller epoch
  and a per-dispatch fence that increases each attempt. The kernel's epoch
  guard stops a superseded controller from writing at all; a worker result
  is committed only when its ``(attempt, fence)`` matches the dispatch's
  current values — a stale fence can never commit.
- **Heartbeats with monotonic deadlines**: a worker that misses its
  heartbeat deadline is marked UNREACHABLE and its in-flight dispatches are
  requeued (fence bumped), so a lost worker never strands work.
- **Retries by failure class** with exponential backoff + jitter and an
  attempt cap; ``unknown_external_outcome`` parks in NEEDS_RECONCILE and is
  never blind-retried.
- **Bounded scheduling**: filter → score over protocol/state/labels/
  capabilities/model residency/capacity, plus shared-endpoint resource
  groups so one Ollama box is never oversubscribed.
- **Brokered delegation**: a worker's ``delegation_requested`` event is
  validated (depth, fan-out, budget, authority-subset, trust placement)
  into a child dispatch; the parent parks WAITING_CHILD and its child's
  result is returned as a complete tool-result group on resume.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Dict, List, Optional

from ..kernel.model import DispatchState, KernelError, WorkerState
from ..kernel.store import MissionStore
from ..swarm.protocol import (
    ActionClass,
    FailureClass,
    TaskEnvelope,
    classification_rank,
    new_id,
)
from .registry import FleetRegistry

#: Failure classes that a retry may clear.
RETRYABLE = frozenset({FailureClass.TRANSIENT, FailureClass.RESOURCE})

DEFAULTS = {
    "max_attempts": 3,
    "backoff_base_seconds": 2.0,
    "backoff_cap_seconds": 300.0,
    "heartbeat_deadline_seconds": 90.0,
    "max_delegation_depth": 3,
    "max_fan_out": 8,
    "child_token_budget": 100000,
    "default_required_trust": 0,
}


class PlaneError(KernelError):
    pass


TransportFactory = Callable[[Dict[str, Any]], Any]


class TaskPlane:
    def __init__(self, store: MissionStore, registry: FleetRegistry,
                 transport_factory: TransportFactory, *,
                 config: Optional[Dict[str, Any]] = None,
                 clock: Callable[[], float] = time.time,
                 log: Optional[Callable[[str], None]] = None):
        self.store = store
        self.registry = registry
        self._factory = transport_factory
        self.config = dict(DEFAULTS)
        self.config.update(config or {})
        self.clock = clock
        self._log = log or (lambda line: None)
        self.resource_group_caps: Dict[str, int] = dict(
            self.config.get("resource_group_caps", {}) or {}
        )
        #: Per-task required-trust overrides (default from config).
        self._required_trust: Dict[str, int] = {}

    @property
    def epoch(self) -> int:
        return self.store.current_epoch()

    # -- submission -----------------------------------------------------------

    def submit(self, envelope: Any, max_attempts: Optional[int] = None,
               required_trust: Optional[int] = None) -> str:
        """Queue a task envelope for scheduling. Accepts a TaskEnvelope or
        an envelope dict."""
        if isinstance(envelope, TaskEnvelope):
            payload = envelope.to_dict()
        else:
            payload = dict(envelope)
        task_id = self.store.create_dispatch(
            payload,
            max_attempts=int(
                max_attempts
                if max_attempts is not None
                else self.config["max_attempts"]
            ),
        )
        if required_trust is not None:
            self._required_trust[task_id] = int(required_trust)
        return task_id

    def _trust_for(self, task_id: str) -> int:
        return self._required_trust.get(
            task_id, int(self.config["default_required_trust"])
        )

    # -- scheduling -----------------------------------------------------------

    def _group_cap(self, group: str, members: List[Dict[str, Any]]) -> int:
        if group in self.resource_group_caps:
            return int(self.resource_group_caps[group])
        # Default: the box's declared capacity = the largest member's
        # max_concurrency (workers pointing at one endpoint share it).
        return max((int(m["max_concurrency"]) for m in members), default=1)

    def _group_in_flight(self, group: str) -> int:
        total = 0
        for worker in self.registry.list():
            if worker.get("resource_group") == group:
                total += self.store.count_worker_dispatches(
                    worker["worker_id"]
                )
        return total

    def _has_group_capacity(self, worker: Dict[str, Any]) -> bool:
        group = worker.get("resource_group") or ""
        if not group:
            return True
        members = [
            w for w in self.registry.list()
            if w.get("resource_group") == group
        ]
        return self._group_in_flight(group) < self._group_cap(group, members)

    def _candidates(self, envelope: TaskEnvelope,
                    required_trust: int) -> List[Dict[str, Any]]:
        out = []
        for worker in self.registry.list(state=WorkerState.ACTIVE):
            if not self.registry.eligible(
                worker, envelope, required_trust=required_trust
            ):
                continue
            load = self.store.count_worker_dispatches(worker["worker_id"])
            if load >= int(worker["max_concurrency"]):
                continue
            if not self._has_group_capacity(worker):
                continue
            out.append((worker, load))
        return out

    def _score(self, worker: Dict[str, Any], load: int,
               envelope: TaskEnvelope) -> tuple:
        model_resident = 1 if (
            envelope.model
            and self.registry.worker_has_model(worker, envelope.model)
        ) else 0
        # Prefer resident model, then lowest load, then higher trust.
        return (model_resident, -load, int(worker["trust_level"]))

    def pick_worker(self, envelope: TaskEnvelope,
                    required_trust: int) -> Optional[Dict[str, Any]]:
        candidates = self._candidates(envelope, required_trust)
        if not candidates:
            return None
        best = max(
            candidates,
            key=lambda pair: self._score(pair[0], pair[1], envelope),
        )
        return best[0]

    def schedule_once(self, limit: int = 16) -> List[Dict[str, Any]]:
        now = float(self.clock())
        results = []
        queued = self.store.list_dispatches(state=DispatchState.QUEUED)
        for dispatch in queued:
            if len(results) >= limit:
                break
            if float(dispatch["not_before"]) > now:
                continue
            envelope = TaskEnvelope.from_dict(dispatch["envelope"])
            worker = self.pick_worker(
                envelope, self._trust_for(dispatch["task_id"])
            )
            if worker is None:
                continue
            results.append(self._offer_and_start(dispatch, worker))
        return results

    def _offer_and_start(self, dispatch: Dict[str, Any],
                         worker: Dict[str, Any]) -> Dict[str, Any]:
        task_id = dispatch["task_id"]
        attempt = int(dispatch["attempt"]) + 1
        fence = int(dispatch["fence"]) + 1
        worker_id = worker["worker_id"]
        envelope = dispatch["envelope"]
        # Record the intent (QUEUED→OFFERING) BEFORE contacting the worker,
        # so a crash mid-offer recovers as an offer we can safely repeat.
        self.store.transition_dispatch(
            task_id, DispatchState.OFFERING, attempt=attempt,
            worker_id=worker_id, fence=fence,
            reason=f"offer to {worker_id}",
        )
        transport = self._factory(worker)
        try:
            offer = transport.call("task.offer", {
                "envelope": envelope, "attempt": attempt, "fence": fence,
                "controller_epoch": self.epoch,
            })
        except Exception as exc:
            return self._offer_failed(
                dispatch, worker, f"offer transport error: {exc}",
                mark_unreachable=True,
            )
        if not offer.ok:
            return self._offer_failed(
                dispatch, worker,
                f"offer refused: {offer.error}",
                retry_after=offer.retry_after,
                error_class=offer.error_class,
            )
        try:
            start = transport.call("task.start", {
                "task_id": task_id, "attempt": attempt, "fence": fence,
            })
        except Exception as exc:
            return self._offer_failed(
                dispatch, worker, f"start transport error: {exc}",
                mark_unreachable=True,
            )
        if not start.ok:
            return self._offer_failed(
                dispatch, worker, f"start refused: {start.error}",
                retry_after=start.retry_after, error_class=start.error_class,
            )
        self.store.transition_dispatch(
            task_id, DispatchState.RUNNING, reason="worker started",
            result={"offer_receipt": offer.result.get("receipt_id", "")},
        )
        self._log(f"dispatch {task_id} running on {worker_id} (fence {fence})")
        return {"task_id": task_id, "worker_id": worker_id,
                "state": DispatchState.RUNNING, "attempt": attempt,
                "fence": fence}

    def _offer_failed(self, dispatch: Dict[str, Any], worker: Dict[str, Any],
                      reason: str, *, retry_after: float = 0.0,
                      error_class: str = "", mark_unreachable: bool = False
                      ) -> Dict[str, Any]:
        task_id = dispatch["task_id"]
        if mark_unreachable:
            try:
                self.registry.mark_unreachable(
                    worker["worker_id"], reason="offer transport error"
                )
            except KernelError:
                pass
        now = float(self.clock())
        attempt = int(dispatch["attempt"]) + 1
        # A refused/failed offer returns to the queue (no worker) with
        # backoff — unless the attempt cap is spent.
        if attempt >= int(dispatch["max_attempts"]) and error_class not in (
            FailureClass.RESOURCE, "",
        ):
            self.store.transition_dispatch(
                task_id, DispatchState.FAILED, worker_id="",
                failure_class=error_class or FailureClass.TRANSIENT,
                error=reason, reason="offer failed, attempts exhausted",
            )
            state = DispatchState.FAILED
        else:
            backoff = retry_after or self._backoff(attempt)
            self.store.transition_dispatch(
                task_id, DispatchState.QUEUED, worker_id="",
                not_before=now + backoff,
                reason=f"offer failed, requeued: {reason}",
            )
            state = DispatchState.QUEUED
        self._log(f"dispatch {task_id} offer failed: {reason}")
        return {"task_id": task_id, "state": state, "error": reason}

    def _backoff(self, attempt: int) -> float:
        base = float(self.config["backoff_base_seconds"])
        cap = float(self.config["backoff_cap_seconds"])
        raw = min(base * (2 ** max(0, attempt - 1)), cap)
        return raw * (0.5 + random.random() * 0.5)  # full jitter (half..full)

    # -- polling / completion -------------------------------------------------

    def poll_once(self) -> Dict[str, int]:
        stats = {"events": 0, "completed": 0, "failed": 0, "requeued": 0,
                 "delegated": 0, "resumed": 0}
        for dispatch in self.store.list_dispatches():
            if dispatch["state"] not in DispatchState.IN_FLIGHT:
                continue
            try:
                self._poll_dispatch(dispatch, stats)
            except Exception as exc:  # a single bad worker can't stall others
                self._log(
                    f"poll error on {dispatch['task_id']}:"
                    f" {type(exc).__name__}: {exc}"
                )
        return stats

    def _poll_dispatch(self, dispatch: Dict[str, Any],
                       stats: Dict[str, int]) -> None:
        task_id = dispatch["task_id"]
        worker = self.registry.get(dispatch["worker_id"])
        if worker is None:
            return
        transport = self._factory(worker)
        attempt = int(dispatch["attempt"])
        # Ingest events (idempotent) and advance the ack watermark.
        existing = self.store.list_dispatch_events(task_id, attempt)
        since = max((e["seq"] for e in existing), default=-1)
        try:
            events_resp = transport.call("task.events", {
                "task_id": task_id, "attempt": attempt, "since_seq": since,
            })
        except Exception as exc:
            self._log(f"events poll failed for {task_id}: {exc}")
            return
        if events_resp.ok and events_resp.result.get("events"):
            batch = events_resp.result["events"]
            stats["events"] += self.store.record_dispatch_events(batch)
            watermark = events_resp.result.get("watermark", since)
            try:
                transport.call("task.events_ack", {
                    "task_id": task_id, "upto_seq": watermark,
                })
            except Exception:
                pass
        if dispatch["state"] == DispatchState.RUNNING:
            # Broker any delegation the worker parked on (may move the
            # dispatch to WAITING_CHILD).
            self._handle_delegations(dispatch, transport, stats)
        dispatch = self.store.get_dispatch(task_id)
        if dispatch["state"] == DispatchState.WAITING_CHILD:
            # A parked parent drives its own resume from its children's
            # outcomes — deterministic, no cross-dispatch callback.
            self._try_resume(dispatch, transport, stats)
            return
        if dispatch["state"] not in (
            DispatchState.OFFERING, DispatchState.RUNNING
        ):
            return
        try:
            status = transport.call("task.status", {"task_id": task_id})
        except Exception as exc:
            self._log(f"status poll failed for {task_id}: {exc}")
            return
        if not status.ok:
            return
        worker_state = status.result.get("state")
        if worker_state in ("completed", "failed", "cancelled"):
            self._finalize_from_receipt(
                dispatch, status.result.get("receipt") or {}, stats
            )

    def _handle_delegations(self, dispatch: Dict[str, Any], transport,
                            stats: Dict[str, int]) -> None:
        task_id = dispatch["task_id"]
        attempt = int(dispatch["attempt"])
        events = self.store.list_dispatch_events(task_id, attempt)
        requested = [
            e for e in events if e["kind"] == "delegation_requested"
        ]
        if not requested:
            return
        current = self.store.get_dispatch(task_id)
        mapping = dict((current.get("result") or {}).get("delegations", {}))
        changed = False
        for event in requested:
            delegation_id = event["payload"].get("delegation_id", "")
            if not delegation_id or delegation_id in mapping:
                continue
            entry = self._broker_one(dispatch, event["payload"])
            mapping[delegation_id] = entry
            changed = True
            if entry["status"] == "pending":
                stats["delegated"] += 1
        if changed and current["state"] == DispatchState.RUNNING:
            result = dict(current.get("result") or {})
            result["delegations"] = mapping
            self.store.transition_dispatch(
                task_id, DispatchState.WAITING_CHILD, result=result,
                reason="parked on brokered delegation",
            )

    def _broker_one(self, parent: Dict[str, Any],
                    payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate one delegation into a child dispatch. Returns a mapping
        entry: pending(child_id), or rejected(reason). Never raises past a
        subset violation (which is a fail-closed rejection)."""
        parent_env = TaskEnvelope.from_dict(parent["envelope"])
        depth = self._depth(parent["task_id"])
        if depth >= int(self.config["max_delegation_depth"]):
            self._log(
                f"delegation from {parent['task_id']} rejected: max depth"
            )
            return {"status": "rejected", "reason": "max delegation depth",
                    "resumed": False}
        siblings = self.store.list_dispatches(
            parent_task_id=parent["task_id"]
        )
        if len(siblings) >= int(self.config["max_fan_out"]):
            return {"status": "rejected", "reason": "max fan-out",
                    "resumed": False}
        child_env = self._child_envelope(parent_env, payload)
        try:
            self._assert_subset(child_env, parent_env)
        except PlaneError as exc:
            return {"status": "rejected", "reason": str(exc),
                    "resumed": False}
        child_id = self.store.create_dispatch(
            child_env.to_dict(),
            max_attempts=int(self.config["max_attempts"]),
        )
        self._required_trust[child_id] = self._trust_for(parent["task_id"])
        self._log(
            f"brokered child {child_id} for {parent['task_id']}"
            f" (depth {depth + 1})"
        )
        return {"status": "pending", "child": child_id, "resumed": False}

    def _child_envelope(self, parent: TaskEnvelope,
                        payload: Dict[str, Any]) -> TaskEnvelope:
        task = str(payload.get("task") or "").strip() or "(delegated task)"
        context = str(payload.get("context") or "")
        parent_budget = int(parent.token_budget)
        child_budget = (
            min(parent_budget, int(self.config["child_token_budget"]))
            if parent_budget else int(self.config["child_token_budget"])
        )
        return TaskEnvelope(
            task_id=new_id("task"),
            mission_id=parent.mission_id,
            principal=parent.principal,
            task=task,
            idempotency_key=new_id("task"),
            issued_at=float(self.clock()),
            parent_task_id=parent.task_id,
            delegator=parent.principal,
            role=parent.role,
            context=context,
            model=parent.model,
            skills=parent.skills,
            tools=parent.tools,                        # ⊆ parent (equal)
            action_classes=parent.action_classes,      # ⊆ parent (equal)
            data_classification=parent.data_classification,  # ≤ parent
            max_tool_rounds=parent.max_tool_rounds,
            token_budget=child_budget,
            wall_clock_seconds=parent.wall_clock_seconds,
        )

    @staticmethod
    def _assert_subset(child: TaskEnvelope, parent: TaskEnvelope) -> None:
        if not set(child.tools).issubset(set(parent.tools)):
            raise PlaneError("child tools exceed the parent's — refusing")
        if not set(child.action_classes).issubset(
            set(parent.action_classes) | {ActionClass.READ}
        ):
            raise PlaneError(
                "child action classes exceed the parent's — refusing"
            )
        if classification_rank(child.data_classification) > (
            classification_rank(parent.data_classification)
        ):
            raise PlaneError(
                "child data classification exceeds the parent's — refusing"
            )
        if child.token_budget and parent.token_budget and (
            child.token_budget > parent.token_budget
        ):
            raise PlaneError("child token budget exceeds the parent's")

    def _try_resume(self, parent: Dict[str, Any], transport,
                    stats: Dict[str, int]) -> None:
        """A WAITING_CHILD parent resumes as soon as its outstanding
        delegation has an outcome (child terminal, or an immediate
        rejection). The child's result returns as a tool-result group."""
        result = dict(parent.get("result") or {})
        mapping = dict(result.get("delegations") or {})
        for delegation_id, entry in mapping.items():
            if not isinstance(entry, dict) or entry.get("resumed"):
                continue
            result_text = None
            if entry["status"] == "rejected":
                result_text = f"delegation refused: {entry.get('reason')}"
            elif entry["status"] == "pending":
                child = self.store.get_dispatch(entry.get("child", ""))
                if child is None:
                    result_text = "delegated subtask vanished"
                elif child["state"] == DispatchState.SUCCEEDED:
                    payload = child.get("result") or {}
                    result_text = str(payload.get("summary") or "(no result)")
                elif child["state"] == DispatchState.FAILED:
                    result_text = (
                        "delegated subtask failed: "
                        + str(child.get("error") or "unknown error")
                    )
                elif child["state"] == DispatchState.CANCELLED:
                    result_text = "delegated subtask was cancelled"
                else:
                    continue  # child still in flight — keep waiting
            if result_text is None:
                continue
            try:
                resume = transport.call("task.resume", {
                    "task_id": parent["task_id"],
                    "attempt": int(parent["attempt"]),
                    "fence": int(parent["fence"]),
                    "tool_call_id": delegation_id,
                    "result_text": result_text,
                })
            except Exception as exc:
                self._log(f"resume of {parent['task_id']} failed: {exc}")
                return
            if not resume.ok:
                self._log(
                    f"worker refused resume of {parent['task_id']}:"
                    f" {resume.error}"
                )
                return
            entry["resumed"] = True
            result["delegations"] = mapping
            self.store.transition_dispatch(
                parent["task_id"], DispatchState.RUNNING, result=result,
                reason="resumed with child result",
            )
            stats["resumed"] += 1
            return  # one resume per pass; re-poll continues the parent

    def _depth(self, task_id: str) -> int:
        depth = 0
        seen = set()
        current = self.store.get_dispatch(task_id)
        while current and current.get("parent_task_id"):
            if current["task_id"] in seen:
                break
            seen.add(current["task_id"])
            depth += 1
            current = self.store.get_dispatch(current["parent_task_id"])
        return depth

    def _finalize_from_receipt(self, dispatch: Dict[str, Any],
                               receipt: Dict[str, Any],
                               stats: Dict[str, int]) -> None:
        task_id = dispatch["task_id"]
        # Fencing: only a receipt matching the dispatch's current
        # (attempt, fence) may commit. A stale worker/attempt can't.
        if not receipt:
            return
        if int(receipt.get("attempt", -1)) != int(dispatch["attempt"]) or (
            int(receipt.get("fence", -1)) != int(dispatch["fence"])
        ):
            self._log(
                f"ignoring stale receipt for {task_id}"
                f" (receipt fence {receipt.get('fence')} !="
                f" {dispatch['fence']})"
            )
            return
        outcome = receipt.get("outcome")
        if outcome == "success":
            self.store.transition_dispatch(
                task_id, DispatchState.SUCCEEDED,
                result=receipt.get("payload") or {},
                reason="worker success",
            )
            stats["completed"] += 1
        elif outcome == "cancelled":
            self.store.transition_dispatch(
                task_id, DispatchState.CANCELLED, reason="worker cancelled",
            )
        else:
            self._handle_failure(dispatch, receipt, stats)

    def _handle_failure(self, dispatch: Dict[str, Any],
                        receipt: Dict[str, Any],
                        stats: Dict[str, int]) -> None:
        task_id = dispatch["task_id"]
        failure_class = receipt.get("failure_class") or FailureClass.TRANSIENT
        error = receipt.get("error", "")
        attempt = int(dispatch["attempt"])
        if failure_class == FailureClass.UNKNOWN_EXTERNAL_OUTCOME:
            self.store.transition_dispatch(
                task_id, DispatchState.NEEDS_RECONCILE,
                failure_class=failure_class, error=error,
                reason="unknown external outcome — reconcile, never retry",
            )
            return
        if failure_class in RETRYABLE and attempt < int(
            dispatch["max_attempts"]
        ):
            self.store.transition_dispatch(
                task_id, DispatchState.QUEUED, worker_id="",
                not_before=float(self.clock()) + self._backoff(attempt),
                failure_class=failure_class, error=error,
                reason=f"retry after {failure_class} failure",
            )
            stats["requeued"] += 1
            return
        self.store.transition_dispatch(
            task_id, DispatchState.FAILED, failure_class=failure_class,
            error=error, reason="terminal failure",
        )
        stats["failed"] += 1

    # -- heartbeats / reaping -------------------------------------------------

    def heartbeat_sweep(self) -> Dict[str, int]:
        """Poll each ACTIVE/DRAINING worker's status, record heartbeats, and
        mark unreachable workers whose deadline has passed."""
        stats = {"heartbeats": 0, "unreachable": 0}
        now = float(self.clock())
        deadline = float(self.config["heartbeat_deadline_seconds"])
        for worker in self.registry.list():
            if worker["state"] not in (
                WorkerState.ACTIVE, WorkerState.DRAINING,
                WorkerState.UPDATING,
            ):
                continue
            transport = self._factory(worker)
            try:
                status = transport.call("worker.status", {})
            except Exception:
                status = None
            if status is not None and status.ok:
                self.registry.heartbeat(
                    worker["worker_id"], status.result.get("heartbeat_seq", 0)
                )
                stats["heartbeats"] += 1
                continue
            last = worker.get("last_heartbeat_at")
            reference = float(last) if last else float(
                worker.get("updated_at") or now
            )
            if now - reference >= deadline:
                self._mark_unreachable(worker)
                stats["unreachable"] += 1
        return stats

    def _mark_unreachable(self, worker: Dict[str, Any]) -> None:
        try:
            self.registry.mark_unreachable(
                worker["worker_id"], reason="heartbeat deadline exceeded"
            )
        except KernelError:
            return
        # Requeue its in-flight work (fence bumped on the next offer).
        for state in (DispatchState.OFFERING, DispatchState.RUNNING,
                      DispatchState.WAITING_CHILD):
            for dispatch in self.store.list_dispatches(
                state=state, worker_id=worker["worker_id"]
            ):
                self.store.transition_dispatch(
                    dispatch["task_id"], DispatchState.QUEUED, worker_id="",
                    not_before=float(self.clock()),
                    reason="worker unreachable — requeued",
                )
        self._log(f"worker {worker['worker_id']} marked unreachable")

    # -- cancellation ---------------------------------------------------------

    def cancel(self, task_id: str, reason: str = "operator cancel") -> bool:
        dispatch = self.store.get_dispatch(task_id)
        if dispatch is None:
            raise PlaneError(f"unknown dispatch {task_id!r}")
        if dispatch["state"] in DispatchState.TERMINAL:
            return False
        worker = self.registry.get(dispatch["worker_id"])
        if worker is not None and dispatch["state"] in (
            DispatchState.RUNNING, DispatchState.OFFERING,
            DispatchState.WAITING_CHILD,
        ):
            try:
                self._factory(worker).call("task.cancel", {
                    "task_id": task_id, "reason": reason,
                })
            except Exception as exc:
                self._log(f"worker cancel of {task_id} failed: {exc}")
        self.store.transition_dispatch(
            task_id, DispatchState.CANCELLED, reason=reason,
        )
        return True

    # -- artifact transfer (content-addressed, digest-verified both ways) -----

    def push_artifact(self, worker: Dict[str, Any], digest: str,
                      data: bytes) -> Dict[str, Any]:
        import base64
        import hashlib

        if hashlib.sha256(data).hexdigest() != digest:
            raise PlaneError("refusing to push data that mismatches digest")
        transport = self._factory(worker)
        chunk = 256 * 1024
        offset = 0
        result = {}
        while offset < len(data) or not data:
            piece = data[offset:offset + chunk]
            resp = transport.call("artifact.put", {
                "digest": digest, "offset": offset,
                "total_size": len(data),
                "data_b64": base64.b64encode(piece).decode("ascii"),
            })
            if not resp.ok:
                raise PlaneError(f"artifact push failed: {resp.error}")
            result = resp.result
            if result.get("complete"):
                break
            offset += len(piece)
            if not piece:
                break
        return result

    def pull_artifact(self, worker: Dict[str, Any],
                      digest: str) -> bytes:
        import base64
        import hashlib

        transport = self._factory(worker)
        chunk = 256 * 1024
        offset = 0
        buffer = bytearray()
        while True:
            resp = transport.call("artifact.get", {
                "digest": digest, "offset": offset, "length": chunk,
            })
            if not resp.ok:
                raise PlaneError(f"artifact pull failed: {resp.error}")
            piece = base64.b64decode(resp.result["data_b64"])
            buffer.extend(piece)
            offset += len(piece)
            if resp.result.get("eof"):
                break
            if not piece:
                break
        if hashlib.sha256(bytes(buffer)).hexdigest() != digest:
            raise PlaneError(
                "pulled artifact digest mismatch — refusing the bytes"
            )
        return bytes(buffer)

    # -- one full pass --------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        scheduled = self.schedule_once()
        polled = self.poll_once()
        return {"scheduled": len(scheduled), **polled}
