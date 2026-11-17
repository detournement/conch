"""Fleet attach: one client API, two transports (the mission-command
pattern applied to the fleet).

``attach_fleet(config)`` returns a :class:`SocketFleetClient` when the
``conch-controller`` daemon answers on its control socket, else a
:class:`DirectFleetClient` that opens the fleet kernel itself and drives
the TaskPlane one-shot (schedule → poll loop) for synchronous runs. Both
expose the same operations, so ``/fleet`` and ``fleet_delegate`` behave
identically with or without a running controller — the only difference is
*who* ticks the plane (the daemon continuously, the direct client only
while a call is in flight).

Every dispatch flows through :func:`build_task_envelope`, which applies
the authority clamp (requested ∩ worker ceiling ∩ caller authority) and
the skill-availability check before anything reaches a worker.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..kernel import control
from ..kernel.model import DispatchState, KernelError
from ..kernel.store import MissionStore
from ..swarm.protocol import TaskEnvelope, new_id
from . import authority
from .controller import (
    check_skill_availability,
    controller_alive,
    default_transport_factory,
    ensure_adhoc_mission,
    fleet_kernel_dir,
    fleet_socket_path,
    local_artifact_dir,
    probe_worker,
    worker_entries,
    _plane_config,
)
from .plane import TaskPlane
from .registry import FleetRegistry


class FleetUnavailable(KernelError):
    """No controller socket and the fleet kernel cannot be opened."""


#: Default synchronous-run budget knobs.
DEFAULT_WALL_SECONDS = 300
DEFAULT_TOOL_ROUNDS = 10


def build_task_envelope(*, task: str, mission_id: str,
                        worker: Optional[Dict[str, Any]] = None,
                        skill: str = "",
                        tools: Optional[List[str]] = None,
                        actions: Optional[List[str]] = None,
                        model: str = "", data: str = "",
                        token_budget: int = 0,
                        wall_clock_seconds: int = 0,
                        max_tool_rounds: int = 0,
                        context: str = "", principal: str = "user",
                        caller: Optional[Dict[str, Any]] = None
                        ) -> TaskEnvelope:
    """One clamped, validated TaskEnvelope for a fleet dispatch.

    - ``skill`` names a local skill; its declared tool scope becomes the
      requested tool set when ``--tools`` is not given (the remote agent
      "acts as" the skill — the worker loads the same skill's prompt and
      re-intersects its tools at execution).
    - With a targeted ``worker`` the clamp runs here against its ceiling;
      auto dispatch clamps against the caller only and relies on the
      registry's ceiling-aware eligibility filter for placement.
    """
    task = str(task or "").strip()
    if not task:
        raise KernelError("a fleet task needs a non-empty prompt")
    skills: tuple = ()
    if skill:
        from ..skills import get_skill

        record = get_skill(skill)
        if record is None:
            raise KernelError(
                f"unknown skill {skill!r} — /skills lists what exists"
                " locally; the worker must have it installed too"
            )
        skills = (record["name"],)
        if tools is None and record.get("tools") is not None:
            tools = list(record["tools"])
        if not model:
            model = record.get("model") or ""
        if not max_tool_rounds and record.get("rounds"):
            max_tool_rounds = int(record["rounds"])
    if worker is not None:
        clamped = authority.clamp_envelope(
            worker, tools=tools, actions=actions, data=data,
            token_budget=token_budget, caller=caller,
        )
    else:
        # Auto placement: clamp against the caller's authority only; the
        # scheduler refuses workers whose ceiling the envelope exceeds.
        caller_auth = caller or authority.OWNER_AUTHORITY
        pseudo_worker = {
            "labels": {"grants": {"tools": "full",
                                  "actions": sorted(
                                      authority.GRANTABLE_ACTIONS)}},
            "data_ceiling": str(
                caller_auth.get("data") or "restricted"
            ),
        }
        clamped = authority.clamp_envelope(
            pseudo_worker, tools=tools, actions=actions, data=data,
            token_budget=token_budget, caller=caller_auth,
        )
    return TaskEnvelope(
        task_id=new_id("task"),
        mission_id=mission_id,
        principal=str(principal or "user"),
        task=task,
        idempotency_key=new_id("task"),
        issued_at=time.time(),
        context=str(context or ""),
        model=str(model or ""),
        skills=skills,
        tools=clamped["tools"],
        action_classes=clamped["actions"],
        data_classification=clamped["data"],
        max_tool_rounds=int(max_tool_rounds or DEFAULT_TOOL_ROUNDS),
        token_budget=int(clamped["token_budget"]),
        wall_clock_seconds=int(wall_clock_seconds or DEFAULT_WALL_SECONDS),
    )


class SocketFleetClient:
    """Talks to the running controller over its control socket."""

    mode = "socket"

    def __init__(self, socket_path: Optional[Path] = None):
        self._socket_path = socket_path or fleet_socket_path()

    def _call(self, op: str, args: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return control.request(
                op, args, socket_path=self._socket_path
            )
        except control.ControlError as exc:
            raise KernelError(str(exc))

    def status(self) -> Dict[str, Any]:
        return self._call("status")

    def list_workers(self) -> List[Dict[str, Any]]:
        return self._call("workers.list")

    def probe_worker(self, worker: str) -> Dict[str, Any]:
        return self._call("worker.probe", {"worker": worker})

    def drain(self, worker: str, reason: str = "") -> Dict[str, Any]:
        return self._call("worker.drain",
                          {"worker": worker, "reason": reason})

    def enable(self, worker: str, reason: str = "") -> Dict[str, Any]:
        return self._call("worker.enable",
                          {"worker": worker, "reason": reason})

    def grant(self, worker: str, *, actions=None, tools=None,
              data: str = "", granted_by: str = "owner",
              caller_actions=None, caller_data: str = "") -> Dict[str, Any]:
        return self._call("worker.grant", {
            "worker": worker,
            "actions": list(actions) if actions is not None else None,
            "tools": tools if tools in (None, "full") else list(tools),
            "data": data, "granted_by": granted_by,
            "caller_actions": (
                list(caller_actions) if caller_actions is not None else None
            ),
            "caller_data": caller_data,
        })

    def revoke_grants(self, worker: str) -> Dict[str, Any]:
        return self._call("worker.revoke_grants", {"worker": worker})

    def submit(self, envelope: TaskEnvelope, *, worker: str = "auto",
               required_trust: Optional[int] = None,
               caller: Optional[Dict[str, Any]] = None) -> str:
        args: Dict[str, Any] = {
            "envelope": envelope.to_dict(), "worker": worker,
        }
        if required_trust is not None:
            args["required_trust"] = int(required_trust)
        if caller is not None:
            args["caller"] = _caller_json(caller)
        return self._call("task.submit", args)["task_id"]

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._call("task.get", {"task_id": task_id})

    def list_tasks(self, state: str = "") -> List[Dict[str, Any]]:
        return self._call("tasks.list", {"state": state})

    def cancel(self, task_id: str, reason: str = "") -> bool:
        return bool(self._call("task.cancel", {
            "task_id": task_id, "reason": reason or "operator cancel",
        }).get("cancelled"))

    def task_events(self, task_id: str) -> List[Dict[str, Any]]:
        return self._call("task.events", {"task_id": task_id})

    def pull_artifact(self, task_id: str, digest: str) -> Dict[str, Any]:
        return self._call("artifact.pull", {
            "task_id": task_id, "digest": digest,
        })

    def adhoc_mission_id(self) -> str:
        return self._call("mission.adhoc")["mission_id"]

    def wait(self, task_id: str, timeout: float = 330.0,
             poll_seconds: float = 1.0) -> Dict[str, Any]:
        """Block until the dispatch is terminal (the daemon does the
        driving); returns the final dispatch row."""
        deadline = time.time() + max(timeout, 1.0)
        while time.time() < deadline:
            dispatch = self.get_task(task_id)
            if dispatch["state"] in DispatchState.TERMINAL:
                return dispatch
            time.sleep(poll_seconds)
        return self.get_task(task_id)

    def close(self) -> None:
        pass


class DirectFleetClient:
    """Opens the fleet kernel directly — the no-controller path.

    Registry/task reads and admin writes work exactly like the socket
    client; a synchronous ``run`` drives the plane itself (one-shot
    schedule + poll loop over the real WorkerTransport) since no daemon is
    ticking. attach_fleet() only builds this when no controller answers,
    so a direct drive never races a live daemon's scheduler.
    """

    mode = "direct"

    def __init__(self, config: dict, *,
                 kernel_dir: Optional[Path] = None,
                 transport_factory=None):
        self.config = config or {}
        self.kernel_dir = Path(kernel_dir) if kernel_dir else (
            fleet_kernel_dir()
        )
        try:
            self.store = MissionStore(self.kernel_dir / "kernel.db")
        except KernelError:
            raise
        except Exception as exc:
            raise FleetUnavailable(f"cannot open fleet kernel: {exc}")
        self.registry = FleetRegistry(self.store)
        self._factory = transport_factory or default_transport_factory(
            self.config
        )
        self.plane = TaskPlane(
            self.store, self.registry, self._factory,
            config=_plane_config(self.config),
        )

    # -- registry / admin -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        states: Dict[str, int] = {}
        for dispatch in self.store.list_dispatches():
            states[dispatch["state"]] = states.get(dispatch["state"], 0) + 1
        return {
            "holder": "(no controller — direct fleet access)",
            "epoch": self.store.current_epoch(),
            "workers": {
                worker["name"]: worker["state"]
                for worker in self.registry.list()
            },
            "dispatches": states,
            "kernel": str(self.kernel_dir / "kernel.db"),
            "events": self.store.event_count(),
        }

    def list_workers(self) -> List[Dict[str, Any]]:
        return worker_entries(self.registry)

    def _worker_by_ref(self, ref: str) -> Dict[str, Any]:
        worker = self.registry.find(str(ref or "").strip()) or (
            self.registry.get(str(ref or "").strip())
        )
        if worker is None:
            raise KernelError(f"unknown worker {ref!r}")
        return worker

    def probe_worker(self, worker: str) -> Dict[str, Any]:
        record = self._worker_by_ref(worker)
        return probe_worker(self.registry, record, self._factory(record))

    def drain(self, worker: str, reason: str = "") -> Dict[str, Any]:
        record = self._worker_by_ref(worker)
        self.registry.drain(
            record["worker_id"], reason=reason or "operator drain"
        )
        return {"ok": True, "state": "draining"}

    def enable(self, worker: str, reason: str = "") -> Dict[str, Any]:
        record = self._worker_by_ref(worker)
        self.registry.activate(
            record["worker_id"], reason=reason or "operator enable"
        )
        return {"ok": True, "state": "active"}

    def grant(self, worker: str, *, actions=None, tools=None,
              data: str = "", granted_by: str = "owner",
              caller_actions=None, caller_data: str = "") -> Dict[str, Any]:
        record = self._worker_by_ref(worker)
        grant = authority.validate_grant(
            actions, tools, data,
            caller_actions=caller_actions, caller_data=caller_data,
        )
        ceiling = authority.apply_grant(
            self.registry, record["worker_id"], grant,
            granted_by=granted_by,
        )
        return {"ok": True, "ceiling": {
            "tools": sorted(ceiling["tools"]),
            "actions": sorted(ceiling["actions"]),
            "data": ceiling["data"],
        }}

    def revoke_grants(self, worker: str) -> Dict[str, Any]:
        record = self._worker_by_ref(worker)
        ceiling = authority.revoke_grants(
            self.registry, record["worker_id"]
        )
        return {"ok": True, "ceiling": {
            "tools": sorted(ceiling["tools"]),
            "actions": sorted(ceiling["actions"]),
            "data": ceiling["data"],
        }}

    # -- dispatch ---------------------------------------------------------------

    def adhoc_mission_id(self) -> str:
        return ensure_adhoc_mission(self.store)

    def submit(self, envelope: TaskEnvelope, *, worker: str = "auto",
               required_trust: Optional[int] = None,
               caller: Optional[Dict[str, Any]] = None) -> str:
        if worker and worker != "auto":
            record = self._worker_by_ref(worker)
            clamped = authority.clamp_envelope(
                record, tools=envelope.tools,
                actions=envelope.action_classes,
                data=envelope.data_classification,
                token_budget=envelope.token_budget,
                caller=caller,
            )
            check_skill_availability(
                self.registry, record, envelope.skills,
                transport=self._factory(record),
            )
            payload = envelope.to_dict()
            payload.update({
                "tools": list(clamped["tools"]),
                "action_classes": list(clamped["actions"]),
                "data_classification": clamped["data"],
                "token_budget": clamped["token_budget"],
            })
            envelope = TaskEnvelope.from_dict(payload)
        return self.plane.submit(envelope, required_trust=required_trust)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        dispatch = self.store.get_dispatch(task_id)
        if dispatch is None:
            raise KernelError(f"unknown task {task_id!r}")
        return dispatch

    def list_tasks(self, state: str = "") -> List[Dict[str, Any]]:
        return self.store.list_dispatches(state=state)

    def cancel(self, task_id: str, reason: str = "") -> bool:
        return self.plane.cancel(task_id, reason or "operator cancel")

    def task_events(self, task_id: str) -> List[Dict[str, Any]]:
        return self.store.list_dispatch_events(task_id)

    def pull_artifact(self, task_id: str, digest: str) -> Dict[str, Any]:
        dispatch = self.get_task(task_id)
        worker = self.registry.get(dispatch.get("worker_id") or "")
        if worker is None:
            raise KernelError("task has no assigned worker to pull from")
        data = self.plane.pull_artifact(worker, digest)
        store_dir = local_artifact_dir(self.kernel_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        target = store_dir / digest
        if not target.is_file():
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(data)
            import os as _os

            _os.replace(tmp, target)
        return {"path": str(target), "size": len(data)}

    def wait(self, task_id: str, timeout: float = 330.0,
             poll_seconds: float = 0.3) -> Dict[str, Any]:
        """Direct drive: no daemon is ticking, so THIS client schedules
        and polls until the dispatch is terminal or the timeout passes."""
        deadline = time.time() + max(timeout, 1.0)
        while time.time() < deadline:
            self.plane.schedule_once()
            self.plane.poll_once()
            dispatch = self.get_task(task_id)
            if dispatch["state"] in DispatchState.TERMINAL:
                return dispatch
            time.sleep(poll_seconds)
        return self.get_task(task_id)

    def close(self) -> None:
        self.store.close()


def _caller_json(caller: Dict[str, Any]) -> Dict[str, Any]:
    """Caller authority in JSON-safe form for the control socket."""
    tools = caller.get("tools")
    actions = caller.get("actions")
    return {
        "tools": sorted(tools) if tools is not None else None,
        "actions": sorted(actions) if actions is not None else None,
        "data": caller.get("data"),
        "token_budget": caller.get("token_budget"),
    }


def attach_fleet(config: dict, *, socket_path: Optional[Path] = None,
                 kernel_dir: Optional[Path] = None,
                 transport_factory=None):
    """Socket client when the controller answers, else direct access."""
    if controller_alive(socket_path):
        return SocketFleetClient(socket_path)
    return DirectFleetClient(
        config, kernel_dir=kernel_dir, transport_factory=transport_factory,
    )


def run_fleet_task(client, *, task: str, worker: str = "auto",
                   skill: str = "", tools: Optional[List[str]] = None,
                   actions: Optional[List[str]] = None, model: str = "",
                   data: str = "", token_budget: int = 0,
                   wall_clock_seconds: int = 0, context: str = "",
                   principal: str = "user",
                   caller: Optional[Dict[str, Any]] = None,
                   required_trust: Optional[int] = None,
                   timeout: float = 0.0) -> Dict[str, Any]:
    """Submit one clamped task and wait for its outcome — the single code
    path /fleet run, fleet_delegate, and mission fleet calls all share."""
    record = None
    if worker and worker != "auto":
        entries = {
            entry["name"]: entry for entry in client.list_workers()
        }
        if worker not in entries:
            raise KernelError(
                f"unknown worker {worker!r} — /fleet workers lists the"
                " registry"
            )
        # The client-side clamp needs the full record; both client kinds
        # re-clamp at submit against the authoritative row.
        if isinstance(client, DirectFleetClient):
            record = client._worker_by_ref(worker)
        else:
            record = None  # socket path: the daemon clamps
    envelope = build_task_envelope(
        task=task, mission_id=client.adhoc_mission_id(), worker=record,
        skill=skill, tools=tools, actions=actions, model=model, data=data,
        token_budget=token_budget, wall_clock_seconds=wall_clock_seconds,
        context=context, principal=principal, caller=caller,
    )
    task_id = client.submit(
        envelope, worker=worker or "auto", required_trust=required_trust,
        caller=caller,
    )
    wall = int(wall_clock_seconds or DEFAULT_WALL_SECONDS)
    return client.wait(task_id, timeout=timeout or (wall + 30.0))
