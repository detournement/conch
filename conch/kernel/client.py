"""Shell attach: one client API, two transports.

``attach_kernel(config)`` returns a :class:`SocketKernelClient` when the
edge daemon answers on its control socket, else a
:class:`DirectKernelClient` that opens the kernel database itself. Both
expose the same operations, so `/missions`, `/mission`, `/approvals`, and
the kernel-backed `/schedule` UX behave identically with or without a
running daemon — the only difference is *when* sessions fire (only the
daemon executes work).

:class:`KernelSchedulerAdapter` presents the legacy ``Scheduler`` surface
(``add``/``cancel``/``list_tasks`` with integer task ids) on top of either
client, which is what keeps the historical `/schedule`, `/tasks`, and
`/cancel` commands byte-compatible.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import control
from .engine import MissionEngine
from .model import KernelError, MissionKind
from .store import MissionStore, default_kernel_dir
from .views import (
    approval_entries,
    legacy_task_id,
    mission_by_task_id,
    mission_detail,
    mission_summary,
    schedule_entries,
)


class KernelUnavailable(KernelError):
    """No daemon socket and the kernel database cannot be opened."""


class SocketKernelClient:
    """Talks to the running daemon over the control socket."""

    mode = "socket"

    def __init__(self, socket_path: Optional[Path] = None):
        self._socket_path = socket_path

    def _call(self, op: str, args: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return control.request(
                op, args, socket_path=self._socket_path
            )
        except control.ControlError as exc:
            raise KernelError(str(exc))

    def status(self) -> Dict[str, Any]:
        return self._call("status")

    def list_missions(self) -> List[Dict[str, Any]]:
        return self._call("missions.list")

    def get_mission(self, mission_id: str) -> Dict[str, Any]:
        return self._call("mission.get", {"mission_id": mission_id})

    def new_mission(self, spec: Dict[str, Any],
                    activate: bool = True) -> str:
        result = self._call(
            "mission.new", {"spec": spec, "activate": activate}
        )
        return result["mission_id"]

    def pause(self, mission_id: str) -> None:
        self._call("mission.pause", {"mission_id": mission_id})

    def resume(self, mission_id: str) -> None:
        self._call("mission.resume", {"mission_id": mission_id})

    def abort(self, mission_id: str) -> None:
        self._call("mission.abort", {"mission_id": mission_id})

    def provide_input(self, mission_id: str, text: str) -> None:
        self._call("mission.input", {"mission_id": mission_id, "text": text})

    def list_approvals(self) -> List[Dict[str, Any]]:
        return self._call("approvals.list")

    def decide_approval(self, approval_id: str, verb: str, nonce: str,
                        decided_by: str = "shell") -> Dict[str, Any]:
        return self._call("approval.decide", {
            "approval_id": approval_id, "verb": verb, "nonce": nonce,
            "decided_by": decided_by,
        })

    def schedule_add(self, prompt: str, interval: int,
                     run_once: bool = False) -> Dict[str, Any]:
        return self._call("schedule.add", {
            "prompt": prompt, "interval": int(interval),
            "run_once": bool(run_once),
        })

    def schedule_list(self) -> List[Dict[str, Any]]:
        return self._call("schedule.list")

    def schedule_cancel(self, mission_id: str) -> None:
        self._call("schedule.cancel", {"mission_id": mission_id})

    def close(self) -> None:
        pass


class DirectKernelClient:
    """Opens the kernel directly — the no-daemon attach path. Mutations are
    legal (sqlite serializes across processes; missions use optimistic
    versions), but nothing *executes* until a daemon runs."""

    mode = "direct"

    def __init__(self, config: dict,
                 kernel_dir: Optional[Path] = None):
        self.kernel_dir = Path(kernel_dir) if kernel_dir else (
            default_kernel_dir()
        )
        try:
            self.store = MissionStore(self.kernel_dir / "kernel.db")
        except KernelError:
            raise
        except Exception as exc:
            raise KernelUnavailable(
                f"cannot open kernel database: {exc}"
            )
        self.engine = MissionEngine(
            self.store, config, holder="shell-direct",
            kernel_dir=self.kernel_dir,
        )

    def status(self) -> Dict[str, Any]:
        missions = self.store.list_missions()
        by_status: Dict[str, int] = {}
        for mission in missions:
            by_status[mission["status"]] = (
                by_status.get(mission["status"], 0) + 1
            )
        return {
            "protocol": control.CONTROL_PROTOCOL_VERSION,
            "holder": "(no daemon — direct kernel access)",
            "epoch": self.store.current_epoch(),
            "uptime_seconds": 0.0,
            "missions": by_status,
            "pending_approvals": len(self.store.pending_approvals()),
            "pending_outbox": len(self.store.list_outbox(status="pending")),
            "kernel": str(self.kernel_dir / "kernel.db"),
            "events": self.store.event_count(),
        }

    def list_missions(self) -> List[Dict[str, Any]]:
        return [
            mission_summary(self.store, mission)
            for mission in self.store.list_missions()
        ]

    def get_mission(self, mission_id: str) -> Dict[str, Any]:
        mission = self.store.get_mission(mission_id)
        if mission is None:
            raise KernelError(f"unknown mission {mission_id!r}")
        return mission_detail(self.store, mission)

    def new_mission(self, spec: Dict[str, Any],
                    activate: bool = True) -> str:
        return self.engine.create_mission(spec, activate=activate)

    def pause(self, mission_id: str) -> None:
        self.engine.pause_mission(mission_id)

    def resume(self, mission_id: str) -> None:
        self.engine.resume_mission(mission_id)

    def abort(self, mission_id: str) -> None:
        self.engine.abort_mission(mission_id)

    def provide_input(self, mission_id: str, text: str) -> None:
        self.engine.provide_input(mission_id, text)

    def list_approvals(self) -> List[Dict[str, Any]]:
        return approval_entries(self.store)

    def decide_approval(self, approval_id: str, verb: str, nonce: str,
                        decided_by: str = "shell") -> Dict[str, Any]:
        return self.engine.decide_approval(
            approval_id, verb, nonce=nonce, origin_channel="local",
            decided_by=decided_by,
        )

    def schedule_add(self, prompt: str, interval: int,
                     run_once: bool = False) -> Dict[str, Any]:
        prompt = str(prompt).strip()
        if not prompt or int(interval) <= 0:
            raise KernelError(
                "schedule.add needs a prompt and a positive interval"
            )
        mission_id = self.engine.create_mission({
            "goal": f"scheduled task: {prompt[:120]}",
            "kind": MissionKind.SCHEDULED_PROMPT,
            "prompt": prompt,
            "cadence_seconds": int(interval),
            "run_once": bool(run_once),
            "budgets": {},
        }, activate=True)
        return {"mission_id": mission_id, "interval": int(interval)}

    def schedule_list(self) -> List[Dict[str, Any]]:
        return schedule_entries(self.store)

    def schedule_cancel(self, mission_id: str) -> None:
        mission = self.store.get_mission(mission_id)
        if mission is None or mission["kind"] != (
            MissionKind.SCHEDULED_PROMPT
        ):
            raise KernelError(
                f"no scheduled task with mission id {mission_id!r}"
            )
        self.engine.abort_mission(mission_id)

    def close(self) -> None:
        self.store.close()


def attach_kernel(config: dict, *, socket_path: Optional[Path] = None,
                  kernel_dir: Optional[Path] = None):
    """Socket client when the daemon answers, else direct kernel access."""
    if control.daemon_alive(socket_path):
        return SocketKernelClient(socket_path)
    return DirectKernelClient(config, kernel_dir=kernel_dir)


# ---------------------------------------------------------------------------
# Legacy scheduler surface (byte-compatible /schedule /tasks /cancel UX)
# ---------------------------------------------------------------------------

class KernelTask:
    """Duck-type of :class:`conch.scheduler.Task` backed by a mission."""

    def __init__(self, entry: Dict[str, Any]):
        self.id = int(entry.get("task_seq") or 0)
        self.mission_id = str(entry.get("mission_id") or "")
        self.prompt = str(entry.get("prompt") or "")
        self.interval = int(entry.get("interval") or 0)
        self.run_once = bool(entry.get("run_once"))
        self.active = bool(entry.get("active"))
        self.run_count = int(entry.get("runs") or 0)
        last = entry.get("last_session_at") or 0
        self.last_run = (
            datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
            if last else ""
        )
        self.next_run_at = float(entry.get("next_wake_at") or 0.0)
        self.last_error = str(entry.get("last_error") or "")


class KernelSchedulerAdapter:
    """The legacy Scheduler API on top of a kernel client.

    Construction is lazy and failure-tolerant: with no daemon and no
    readable kernel, every method degrades to an empty/False result rather
    than breaking the shell.
    """

    def __init__(self, config: dict, *, socket_path: Optional[Path] = None,
                 kernel_dir: Optional[Path] = None):
        self._config = config
        self._socket_path = socket_path
        self._kernel_dir = kernel_dir
        self._client = None

    def client(self):
        if self._client is None:
            self._client = attach_kernel(
                self._config, socket_path=self._socket_path,
                kernel_dir=self._kernel_dir,
            )
        return self._client

    @property
    def mode(self) -> str:
        try:
            return self.client().mode
        except KernelError:
            return "unavailable"

    def daemon_running(self) -> bool:
        return self.mode == "socket"

    # -- legacy Scheduler surface ---------------------------------------------

    def set_executor(self, executor) -> None:
        pass  # execution belongs to the daemon, never the shell

    def start(self) -> None:
        pass

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def add(self, prompt: str, interval: int,
            run_once: bool = False) -> KernelTask:
        client = self.client()
        result = client.schedule_add(prompt, interval, run_once=run_once)
        for entry in client.schedule_list():
            if entry.get("mission_id") == result.get("mission_id"):
                return KernelTask(entry)
        # schedule_list cannot miss a just-created mission; belt and braces
        return KernelTask({
            "mission_id": result.get("mission_id"), "prompt": prompt,
            "interval": interval, "run_once": run_once, "active": True,
        })

    def cancel(self, task_id: int) -> bool:
        client = self.client()
        for entry in client.schedule_list():
            if int(entry.get("task_seq") or 0) == int(task_id):
                try:
                    client.schedule_cancel(entry["mission_id"])
                    return True
                except KernelError:
                    return False
        return False

    def list_tasks(self) -> List[KernelTask]:
        try:
            return [
                KernelTask(entry)
                for entry in self.client().schedule_list()
            ]
        except KernelError:
            return []


__all__ = [
    "DirectKernelClient",
    "KernelSchedulerAdapter",
    "KernelTask",
    "KernelUnavailable",
    "SocketKernelClient",
    "attach_kernel",
    "legacy_task_id",
    "mission_by_task_id",
]
