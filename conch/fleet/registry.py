"""FleetRegistry: worker identity, trust, capability, and state truth.

A thin domain layer over the mission kernel's worker tables (Swarm
Phase 2). The registry deliberately separates:

- **Administrator-assigned authority** — ``trust_level``, ``data_ceiling``
  and free-form ``labels`` — which only explicit admin calls may change,
  from
- **Observed facts** — ``capabilities`` (probe output), supported runtime
  ``profiles``, heartbeats — which machines report and can never widen
  authority.

All persistence is the kernel's (event-sourced, hash-chained, replayable);
this module adds validation, the scheduling eligibility filter, and
convenience state operations. A compromised worker can lie about its
capabilities; it cannot raise its own trust or data ceiling.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..kernel.model import KernelError, WorkerState
from ..kernel.store import MissionStore
from ..swarm.protocol import (
    PROTOCOL_VERSION,
    TaskEnvelope,
    classification_rank,
)


class FleetRegistry:
    """Worker registry over one kernel store."""

    def __init__(self, store: MissionStore):
        self.store = store

    # -- enrollment / admin ------------------------------------------------

    def enroll(self, name: str, host: str, *, ssh_user: str = "",
               ssh_port: Optional[int] = None, trust_level: int = 0,
               data_ceiling: str = "internal",
               labels: Optional[Dict[str, Any]] = None,
               capabilities: Optional[Dict[str, Any]] = None,
               runtime_profile: str = "",
               profiles: Optional[List[str]] = None,
               resource_group: str = "", max_concurrency: int = 1,
               autonomy_capable: bool = False) -> str:
        return self.store.enroll_worker(
            name, host, ssh_user=ssh_user, ssh_port=ssh_port,
            trust_level=trust_level, data_ceiling=data_ceiling,
            labels=labels, capabilities=capabilities,
            runtime_profile=runtime_profile, profiles=profiles,
            resource_group=resource_group, max_concurrency=max_concurrency,
            protocol_min=PROTOCOL_VERSION, protocol_max=PROTOCOL_VERSION,
            autonomy_capable=autonomy_capable,
        )

    def assign_authority(self, worker_id: str, *,
                         trust_level: Optional[int] = None,
                         data_ceiling: Optional[str] = None,
                         labels: Optional[Dict[str, Any]] = None) -> int:
        """Admin-only: change what this worker is TRUSTED with."""
        fields: Dict[str, Any] = {}
        if trust_level is not None:
            fields["trust_level"] = int(trust_level)
        if data_ceiling is not None:
            fields["data_ceiling"] = data_ceiling
        if labels is not None:
            fields["labels"] = labels
        if not fields:
            raise KernelError("assign_authority needs at least one field")
        return self.store.update_worker(worker_id, fields)

    def record_probe(self, worker_id: str,
                     probe: Dict[str, Any]) -> int:
        """Record observed capabilities from a hostctl probe. Authority
        fields are deliberately untouchable through this path."""
        capabilities = {
            key: probe[key]
            for key in ("os", "os_release", "arch", "python", "systemd",
                        "docker", "disk", "cgroups", "gpu",
                        "model_endpoints", "hostname")
            if key in probe
        }
        fields: Dict[str, Any] = {"capabilities": capabilities}
        if isinstance(probe.get("profiles"), list):
            fields["profiles"] = [str(p) for p in probe["profiles"]]
        return self.store.update_worker(worker_id, fields)

    def record_skills(self, worker_id: str, skills: List[str]) -> int:
        """Record the skills a worker reports as installed (observed fact,
        via ``worker.status``). Like every capability, this can narrow
        scheduling but never widen authority."""
        worker = self.require(worker_id)
        capabilities = dict(worker.get("capabilities") or {})
        clean = sorted({str(s).strip().lower() for s in skills if s})
        if capabilities.get("skills") == clean:
            return int(worker["version"])
        capabilities["skills"] = clean
        return self.store.update_worker(
            worker_id, {"capabilities": capabilities}
        )

    def worker_skills(self, worker: Dict[str, Any]) -> List[str]:
        capabilities = worker.get("capabilities") or {}
        skills = capabilities.get("skills")
        return list(skills) if isinstance(skills, list) else []

    def record_deployment(self, worker_id: str, *, artifact_digest: str,
                          config_digest: str = "",
                          runtime_profile: str = "") -> int:
        fields: Dict[str, Any] = {"artifact_digest": str(artifact_digest)}
        if config_digest:
            fields["config_digest"] = str(config_digest)
        if runtime_profile:
            fields["runtime_profile"] = str(runtime_profile)
        return self.store.update_worker(worker_id, fields)

    def bump_incarnation(self, worker_id: str) -> int:
        worker = self.require(worker_id)
        return self.store.update_worker(
            worker_id, {"incarnation": int(worker["incarnation"]) + 1}
        )

    # -- state ---------------------------------------------------------------

    def transition(self, worker_id: str, target: str,
                   reason: str = "") -> int:
        return self.store.transition_worker(worker_id, target, reason)

    def activate(self, worker_id: str, reason: str = "") -> int:
        return self.transition(worker_id, WorkerState.ACTIVE, reason)

    def drain(self, worker_id: str, reason: str = "") -> int:
        return self.transition(worker_id, WorkerState.DRAINING, reason)

    def quarantine(self, worker_id: str, reason: str) -> int:
        return self.transition(worker_id, WorkerState.QUARANTINED, reason)

    def revoke(self, worker_id: str, reason: str) -> int:
        return self.transition(worker_id, WorkerState.REVOKED, reason)

    def mark_unreachable(self, worker_id: str, reason: str = "") -> int:
        return self.transition(worker_id, WorkerState.UNREACHABLE, reason)

    def heartbeat(self, worker_id: str, seq: int) -> bool:
        return self.store.record_worker_heartbeat(worker_id, seq)

    # -- queries ---------------------------------------------------------------

    def get(self, worker_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_worker(worker_id)

    def require(self, worker_id: str) -> Dict[str, Any]:
        worker = self.store.get_worker(worker_id)
        if worker is None:
            raise KernelError(f"unknown worker {worker_id!r}")
        return worker

    def find(self, name: str) -> Optional[Dict[str, Any]]:
        return self.store.find_worker(name)

    def list(self, state: str = "") -> List[Dict[str, Any]]:
        return self.store.list_workers(state)

    # -- scheduling eligibility -------------------------------------------------

    def eligible(self, worker: Dict[str, Any],
                 envelope: TaskEnvelope, *,
                 required_trust: int = 0) -> bool:
        """Deterministic placement filter: state, protocol, trust, data
        ceiling, model residency. Capacity/queues are the plane's concern.

        Fail closed: anything unknown or missing makes the worker
        ineligible rather than assumed-capable.
        """
        if worker.get("state") not in WorkerState.SCHEDULABLE:
            return False
        if not (
            int(worker.get("protocol_min", 0))
            <= PROTOCOL_VERSION
            <= int(worker.get("protocol_max", 0))
        ):
            return False
        if int(worker.get("trust_level", 0)) < int(required_trust):
            return False
        try:
            worker_ceiling = classification_rank(
                str(worker.get("data_ceiling", ""))
            )
            envelope_class = classification_rank(
                envelope.data_classification
            )
        except Exception:
            return False
        if envelope_class > worker_ceiling:
            return False
        if envelope.model:
            if not self.worker_has_model(worker, envelope.model):
                return False
        if envelope.skills:
            # Skill-addressed dispatch: the worker must have reported the
            # named skill(s) installed. Fail closed on never-probed
            # workers — an unknown skill inventory is not assumed-capable.
            installed = set(self.worker_skills(worker))
            if not set(envelope.skills).issubset(installed):
                return False
        # Owner-grant ceiling: an envelope may not name tools or action
        # classes beyond what this worker has been granted — the scheduler
        # never places work a worker's ceiling would refuse.
        from .authority import worker_ceiling

        ceiling = worker_ceiling(worker)
        if not set(envelope.tools).issubset(ceiling["tools"]):
            return False
        if not set(envelope.action_classes).issubset(ceiling["actions"]):
            return False
        return True

    @staticmethod
    def worker_has_model(worker: Dict[str, Any], model: str) -> bool:
        """Model residency: the worker must have observed the model on one
        of its local endpoints (or advertise it via labels.models)."""
        model = str(model or "").strip()
        if not model:
            return True
        labels = worker.get("labels") or {}
        advertised = labels.get("models")
        if isinstance(advertised, list) and model in advertised:
            return True
        capabilities = worker.get("capabilities") or {}
        for endpoint in capabilities.get("model_endpoints") or []:
            models = endpoint.get("models") or []
            if model in models:
                return True
            # Ollama tags often carry :latest; accept a bare-name match.
            if any(str(m).split(":")[0] == model for m in models):
                return True
        return False
