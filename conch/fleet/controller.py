"""The supervised ``conch-controller`` daemon: the fleet comes alive.

One controller owns the fleet kernel — its own SQLite scope at
``<state>/conch/fleet/`` (registry, dispatches, dispatch events, the
grant ledger), deliberately SEPARATE from the edge daemon's mission
kernel. Both daemons adopt fencing epochs on their store, so they can
never share one database; splitting the scope lets ``conch-edge`` (the
personal mission brain) and ``conch-controller`` (the fleet dispatcher)
coexist on one machine with independent locks, epochs, and crash
recovery. Shell surfaces (`/fleet`, ``fleet_delegate``) attach through
the controller's socket when it runs and drive the fleet kernel directly
when it does not — the same two-transport client abstraction the mission
commands use.

Responsibilities per tick (all crash-safe — every state change is a
journaled dispatch/worker event, and the offer/start handshake plus
epoch+fence guards make a killed-and-restarted controller recover
without double execution):

- schedule queued dispatches onto eligible workers over WorkerTransport
  (SSH stdio RPC; no worker network port),
- poll running dispatches: ingest events, broker delegations, finalize
  fenced receipts, retry by failure class with backoff,
- heartbeat-sweep the fleet on its own cadence (silent worker →
  UNREACHABLE + requeue),
- queue completed tasks' content-addressed artifacts for the artifact
  thread, which streams them into the local store off the tick,
- record worker-reported skill inventories (capability, never authority)
  from the heartbeat sweep's own ``worker.status`` results.

Per-tick cost is bounded: every worker RPC rides one shared SSH
ControlMaster per host (no handshake per call), a worker that fails a
transport call is skipped for the rest of the tick and backed off, and
artifact bytes never move on the tick thread.

The daemon reuses the edge daemon's machinery exactly as Phase 1
promised: the same ``_KernelLock`` flock + adopted-epoch ownership, the
same versioned JSON control socket protocol, the same
launchd/systemd-supervised install commands.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..kernel.control import (
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_LINE_BYTES,
    daemon_alive,
    ensure_runtime_dir,
    runtime_dir,
)
from ..kernel.daemon import DaemonAlreadyRunning, _KernelLock
from ..kernel.model import DispatchState, KernelError, WorkerState
from ..kernel.store import MissionStore, default_state_dir
from ..swarm.protocol import TaskEnvelope
from . import authority
from .plane import ARTIFACT_PULL_MAX_BYTES, TaskPlane
from .registry import FleetRegistry

CONTROLLER_LOG_MAX_BYTES = 1024 * 1024

#: Goal string of the draft mission every ad-hoc fleet dispatch chains
#: under (dispatch events need a mission; /fleet runs are not missions).
ADHOC_MISSION_GOAL = "fleet ad-hoc dispatch host"


def fleet_kernel_dir() -> Path:
    """The controller's own kernel scope (never the edge daemon's)."""
    return default_state_dir() / "fleet"


def fleet_socket_path() -> Path:
    return runtime_dir() / "fleet.sock"


def controller_alive(socket_path: Optional[Path] = None) -> bool:
    return daemon_alive(socket_path or fleet_socket_path())


def local_artifact_dir(kernel_dir: Optional[Path] = None) -> Path:
    return (Path(kernel_dir) if kernel_dir else fleet_kernel_dir()) \
        / "artifacts" / "sha256"


def ensure_adhoc_mission(store: MissionStore) -> str:
    """The draft mission ad-hoc dispatches chain under (find or create)."""
    for mission in store.list_missions():
        if (mission.get("spec") or {}).get("goal") == ADHOC_MISSION_GOAL:
            return mission["mission_id"]
    return store.create_mission({
        "goal": ADHOC_MISSION_GOAL, "budgets": {}, "cadence_seconds": 0,
    })


class PooledTransportFactory:
    """WorkerTransport factory over each worker's recorded SSH identity.

    All transports share one :class:`SSHConnectionPool`, so each worker
    host gets one ControlMaster (started lazily, re-established on
    failure) instead of a fresh SSH connection per RPC. ``close()`` stops
    the masters; the controller calls it on shutdown.

    The remote relay command is per-worker: enrollment records where
    hostctl was bootstrapped (``labels.hostctl``); hosts with a full conch
    install fall back to the ``conch-hostctl`` console script.
    """

    def __init__(self, config: Optional[dict] = None, *, pool=None):
        from .transport import SSHConnectionPool

        self.pool = pool or SSHConnectionPool()
        try:
            self.rpc_timeout = float(
                (config or {}).get("fleet_rpc_timeout", 60.0)
            )
        except (TypeError, ValueError):
            self.rpc_timeout = 60.0

    def __call__(self, worker: Dict[str, Any]):
        from ..ssh_control import SSHTarget
        from .transport import WorkerTransport

        target = SSHTarget(
            host=str(worker["host"]),
            user=str(worker.get("ssh_user") or ""),
            port=worker.get("ssh_port"),
        )
        labels = worker.get("labels") or {}
        hostctl = str(labels.get("hostctl") or "conch-hostctl")
        return WorkerTransport(
            target, worker["name"], pool=self.pool, hostctl=hostctl,
            rpc_timeout=self.rpc_timeout,
        )

    def close(self) -> None:
        self.pool.close()


def default_transport_factory(config: Optional[dict] = None):
    """The production factory: pooled SSH transports (see above)."""
    return PooledTransportFactory(config)


def close_transport_factory(factory) -> None:
    close = getattr(factory, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class ArtifactPuller:
    """Pulls completed tasks' artifacts on its own thread.

    The tick thread only *queues* digests (from the plane's success hook
    and one startup scan), so it never moves artifact bytes and never
    re-lists already-pulled dispatches. Each pull is one streamed hostctl
    session into a ``.part`` file, digest-verified, then renamed. A failed
    pull backs off per digest instead of retrying every tick. The puller
    touches no SQLite state: each job carries the worker row it needs.
    """

    def __init__(self, plane: TaskPlane, store_dir: Path,
                 log: Callable[[str], None], *,
                 backoff_base: float = 5.0, backoff_cap: float = 600.0,
                 max_attempts: int = 10,
                 clock: Callable[[], float] = time.monotonic):
        self.plane = plane
        self.store_dir = Path(store_dir)
        self._log = log
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.max_attempts = int(max_attempts)
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pulled_unreported = 0
        self.pulled_total = 0

    # -- queueing (tick thread) ------------------------------------------------

    def add_dispatch(self, dispatch: Dict[str, Any],
                     worker: Optional[Dict[str, Any]]) -> int:
        """Queue a SUCCEEDED dispatch's artifacts not yet stored locally."""
        if worker is None:
            return 0
        references = (dispatch.get("result") or {}).get("artifacts") or []
        added = 0
        for ref in references:
            if not isinstance(ref, dict):
                continue
            digest = str(ref.get("digest") or "").strip().lower()
            if len(digest) != 64 or any(
                c not in "0123456789abcdef" for c in digest
            ):
                continue
            if (self.store_dir / digest).is_file():
                continue
            try:
                size = int(ref.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size > ARTIFACT_PULL_MAX_BYTES:
                self._log(
                    f"artifact {digest[:12]} for {dispatch['task_id']}"
                    f" declares {size} bytes — over the pull bound,"
                    " not pulled"
                )
                continue
            with self._lock:
                if digest in self._jobs:
                    continue
                self._jobs[digest] = {
                    "digest": digest, "worker": dict(worker),
                    "task_id": dispatch["task_id"],
                    "name": ref.get("name"),
                    "max_bytes": size or ARTIFACT_PULL_MAX_BYTES,
                    "attempts": 0, "next_at": 0.0,
                }
            added += 1
        if added:
            self._wake.set()
        return added

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)

    def take_pulled(self) -> int:
        with self._lock:
            count, self._pulled_unreported = self._pulled_unreported, 0
            return count

    # -- pulling (artifact thread) ---------------------------------------------

    def _next_job(self) -> Optional[Dict[str, Any]]:
        now = float(self._clock())
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job["next_at"] > now:
                continue
            if not self.plane.worker_available(job["worker"]["worker_id"]):
                continue
            return job
        return None

    def process_one(self) -> bool:
        """Pull one ready artifact; False when nothing is ready."""
        job = self._next_job()
        if job is None:
            return False
        digest = job["digest"]
        target = self.store_dir / digest
        try:
            size = self.plane.pull_artifact_to(
                job["worker"], digest, target,
                max_bytes=job["max_bytes"], cancel=self._stop,
            )
        except Exception as exc:
            with self._lock:
                job["attempts"] += 1
                gave_up = job["attempts"] >= self.max_attempts
                if gave_up:
                    self._jobs.pop(digest, None)
                else:
                    delay = min(
                        self.backoff_base * (2 ** (job["attempts"] - 1)),
                        self.backoff_cap,
                    )
                    job["next_at"] = float(self._clock()) + delay
            self._log(
                f"artifact pull {digest[:12]} for {job['task_id']} failed"
                f" (attempt {job['attempts']}): {exc}"
                + (" — giving up; pull on demand with /fleet" if gave_up
                   else "")
            )
            return True
        with self._lock:
            self._jobs.pop(digest, None)
            self._pulled_unreported += 1
            self.pulled_total += 1
        self._log(
            f"artifact {digest[:12]} ({job.get('name')}, {size} bytes)"
            f" pulled for {job['task_id']}"
        )
        return True

    def drain(self) -> int:
        """Synchronously pull every artifact ready now (single-tick mode).
        Each job is tried at most once; failures back off as usual."""
        count = 0
        while not self._stop.is_set() and self.process_one():
            count += 1
        return count

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.process_one()
            except Exception as exc:  # never let the thread die silently
                self._log(f"artifact thread error: {exc}")
                worked = False
            if not worked:
                self._wake.wait(1.0)
                self._wake.clear()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="conch-controller-artifacts",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def _plane_config(config: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    mapping = {
        "fleet_max_attempts": ("max_attempts", int),
        "fleet_backoff_base_seconds": ("backoff_base_seconds", float),
        "fleet_backoff_cap_seconds": ("backoff_cap_seconds", float),
        "fleet_heartbeat_deadline_seconds": (
            "heartbeat_deadline_seconds", float),
        "fleet_max_delegation_depth": ("max_delegation_depth", int),
        "fleet_max_fan_out": ("max_fan_out", int),
        "fleet_child_token_budget": ("child_token_budget", int),
    }
    for key, (name, cast) in mapping.items():
        raw = (config or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            out[name] = cast(raw)
        except (TypeError, ValueError):
            continue
    groups = str((config or {}).get("fleet_resource_group_caps") or "")
    caps: Dict[str, int] = {}
    for part in groups.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        group, _, cap = part.rpartition(":")
        try:
            caps[group.strip()] = int(cap)
        except ValueError:
            continue
    if caps:
        out["resource_group_caps"] = caps
    return out


class ControllerDaemon:
    """Headless supervisor owning one fleet kernel and driving TaskPlane."""

    def __init__(self, config: dict, *,
                 kernel_dir: Optional[Path] = None,
                 socket_path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time,
                 tick_seconds: float = 1.0,
                 transport_factory: Optional[Callable] = None,
                 artifact_thread: bool = True):
        self.config = config or {}
        self.kernel_dir = Path(kernel_dir) if kernel_dir else (
            fleet_kernel_dir()
        )
        self._socket_path = Path(socket_path) if socket_path else None
        self.clock = clock
        self.tick_seconds = float(tick_seconds)
        self.holder = f"controller-{socket.gethostname()}-{os.getpid()}"
        self._factory = transport_factory or default_transport_factory(
            self.config
        )
        self._artifact_thread = bool(artifact_thread)
        self.artifacts: Optional[ArtifactPuller] = None
        self._lock = _KernelLock(self.kernel_dir)
        self.store: Optional[MissionStore] = None
        self.registry: Optional[FleetRegistry] = None
        self.plane: Optional[TaskPlane] = None
        self.epoch = 0
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._started_at = 0.0
        self._tick_count = 0
        self._last_sweep = 0.0
        self._log_lock = threading.Lock()
        try:
            self._sweep_seconds = float(
                self.config.get("fleet_heartbeat_sweep_seconds", 30.0)
            )
        except (TypeError, ValueError):
            self._sweep_seconds = 30.0

    # -- logging (never secrets) ----------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.kernel_dir / "controller.log"

    def log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{stamp}] {line}\n"
        with self._log_lock:
            try:
                self.kernel_dir.mkdir(parents=True, exist_ok=True)
                if (self.log_path.exists() and
                        self.log_path.stat().st_size >
                        CONTROLLER_LOG_MAX_BYTES):
                    tail = self.log_path.read_bytes()[-65536:]
                    self.log_path.write_bytes(b"[log rotated]\n" + tail)
                with open(self.log_path, "a") as handle:
                    handle.write(text)
            except OSError:
                pass

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Acquire the fleet lock, open the fleet kernel, adopt the
        controller epoch, requeue orphans, and bring up the socket."""
        try:
            self._lock.acquire(self.holder)
        except DaemonAlreadyRunning:
            raise DaemonAlreadyRunning(
                f"another conch-controller holds {self._lock.path} — two"
                " controllers can never share one fleet kernel"
            )
        try:
            self.store = MissionStore(
                self.kernel_dir / "kernel.db", clock=self.clock
            )
            self.epoch = self.store.adopt_epoch()
            self.registry = FleetRegistry(self.store)
            self.plane = TaskPlane(
                self.store, self.registry, self._factory,
                config=_plane_config(self.config), clock=self.clock,
                log=self.log,
            )
            self.artifacts = ArtifactPuller(
                self.plane, local_artifact_dir(self.kernel_dir), self.log,
            )
            self.plane.on_success = self._queue_artifacts
            self._seed_artifact_queue()
            if self._artifact_thread:
                self.artifacts.start()
            recovered = self._recover()
            if recovered:
                self.log(
                    f"recovered {recovered} in-flight dispatch(es) from a"
                    " previous controller (epoch fencing guards commits)"
                )
            self._start_socket_server()
            self._started_at = float(self.clock())
            self.log(
                f"controller started: holder={self.holder}"
                f" epoch={self.epoch}"
                f" kernel={self.kernel_dir / 'kernel.db'}"
            )
        except BaseException:
            self.shutdown()
            raise

    def _recover(self) -> int:
        """Adoption pass after a crash/restart: in-flight dispatches stay
        in flight (the next poll ingests their real state; fenced receipts
        from superseded attempts can never commit). OFFERING dispatches
        whose offer may or may not have landed are safe to re-poll too —
        the worker's offer receipt is idempotent. Nothing is blindly
        requeued here; requeueing is the heartbeat sweep's evidence-based
        decision."""
        count = 0
        for dispatch in self.store.list_dispatches():
            if dispatch["state"] in DispatchState.IN_FLIGHT:
                count += 1
        return count

    def shutdown(self) -> None:
        self._stop.set()
        if self.artifacts is not None:
            self.artifacts.stop()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)
            self._server_thread = None
        socket_path = self.socket_path
        try:
            if socket_path.exists():
                socket_path.unlink()
        except OSError:
            pass
        # Stop the per-worker SSH ControlMasters this controller started.
        close_transport_factory(self._factory)
        if self.store is not None:
            self.store.close()
            self.store = None
        self._lock.release()
        self.log("controller stopped")

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        def handle(signum, frame):
            self.log(f"signal {signum} received — shutting down gracefully")
            self._stop.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.tick()
            except Exception as exc:
                self.log(f"tick error: {type(exc).__name__}: {exc}")
            elapsed = time.time() - started
            self._stop.wait(max(0.05, self.tick_seconds - elapsed))
        self.shutdown()

    # -- the tick ----------------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """One supervision pass: schedule, poll, sweep (on cadence), pull
        artifacts. Crash-safe at every boundary — the plane records intent
        before contacting workers, receipts are fence-checked, and the
        heartbeat sweep requeues only on evidence."""
        if self.plane is None:
            raise KernelError("controller is not started")
        stats: Dict[str, Any] = self.plane.tick()
        now = float(self.clock())
        if now - self._last_sweep >= self._sweep_seconds:
            self._last_sweep = now
            sweep = self.plane.heartbeat_sweep()
            stats.update(sweep)
            self._record_skill_inventories()
        stats["artifacts_pulled"] = (
            self.artifacts.take_pulled() if self.artifacts else 0
        )
        stats["artifacts_pending"] = (
            self.artifacts.pending() if self.artifacts else 0
        )
        self._tick_count += 1
        return stats

    def _record_skill_inventories(self) -> None:
        """Fold worker-reported skills into capabilities, from the
        heartbeat sweep's own ``worker.status`` results (no second call)."""
        statuses = self.plane.last_sweep_statuses
        for worker in self.registry.list(state=WorkerState.ACTIVE):
            status = statuses.get(worker["worker_id"])
            if not status:
                continue
            skills = status.get("skills")
            if isinstance(skills, list):
                try:
                    self.registry.record_skills(
                        worker["worker_id"], skills
                    )
                except KernelError:
                    continue

    def _queue_artifacts(self, dispatch: Optional[Dict[str, Any]]) -> None:
        """Plane success hook: queue the dispatch's artifacts for pulling."""
        if not dispatch or self.artifacts is None:
            return
        if not (dispatch.get("result") or {}).get("artifacts"):
            return
        worker = self.registry.get(dispatch.get("worker_id") or "")
        self.artifacts.add_dispatch(dispatch, worker)

    def _seed_artifact_queue(self) -> int:
        """One startup scan for SUCCEEDED dispatches whose artifacts are
        not stored locally yet (later completions arrive via the hook)."""
        queued = 0
        for dispatch in self.store.list_dispatches(
            state=DispatchState.SUCCEEDED
        ):
            if not (dispatch.get("result") or {}).get("artifacts"):
                continue
            worker = self.registry.get(dispatch.get("worker_id") or "")
            queued += self.artifacts.add_dispatch(dispatch, worker)
        if queued:
            self.log(f"queued {queued} artifact(s) left unpulled")
        return queued

    # -- control socket ------------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        if self._socket_path is not None:
            return self._socket_path
        return fleet_socket_path()

    def _start_socket_server(self) -> None:
        path = self.socket_path
        if self._socket_path is None:
            ensure_runtime_dir()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self._server_thread = threading.Thread(
            target=self._serve_loop, name="conch-controller-control",
            daemon=True,
        )
        self._server_thread.start()

    def _serve_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(30.0)
                self._serve_one(conn)
            except Exception as exc:
                self.log(f"control error: {type(exc).__name__}: {exc}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve_one(self, conn: socket.socket) -> None:
        chunks = []
        total = 0
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONTROL_LINE_BYTES:
                self._respond(conn, ok=False, error="request too large")
                return
            if chunk.endswith(b"\n"):
                break
        raw = b"".join(chunks)
        if not raw.strip():
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond(conn, ok=False, error="malformed request")
            return
        if not isinstance(request, dict) or request.get("v") != (
            CONTROL_PROTOCOL_VERSION
        ):
            self._respond(conn, ok=False, error=(
                "unsupported control protocol version"
                f" (supported: {CONTROL_PROTOCOL_VERSION})"
            ))
            return
        op = str(request.get("op") or "")
        args = request.get("args") or {}
        if not isinstance(args, dict):
            self._respond(conn, ok=False, error="args must be an object")
            return
        try:
            result = self._dispatch(op, args)
        except (KernelError, authority.AuthorityError) as exc:
            self._respond(conn, ok=False, error=str(exc))
            return
        except Exception as exc:
            self._respond(
                conn, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
            return
        self._respond(conn, ok=True, result=result)

    @staticmethod
    def _respond(conn: socket.socket, *, ok: bool, result: Any = None,
                 error: str = "") -> None:
        body: Dict[str, Any] = {"v": CONTROL_PROTOCOL_VERSION, "ok": ok}
        if ok:
            body["result"] = result
        else:
            body["error"] = error
        try:
            conn.sendall((json.dumps(body) + "\n").encode("utf-8"))
        except OSError:
            pass

    # -- control operations ---------------------------------------------------------

    def _worker_by_ref(self, ref: str) -> Dict[str, Any]:
        ref = str(ref or "").strip()
        worker = self.registry.find(ref) or self.registry.get(ref)
        if worker is None:
            raise KernelError(f"unknown worker {ref!r}")
        return worker

    def _dispatch(self, op: str, args: Dict[str, Any]) -> Any:
        if op == "status":
            states: Dict[str, int] = {}
            for dispatch in self.store.list_dispatches():
                states[dispatch["state"]] = (
                    states.get(dispatch["state"], 0) + 1
                )
            return {
                "protocol": CONTROL_PROTOCOL_VERSION,
                "holder": self.holder,
                "epoch": self.epoch,
                "uptime_seconds": max(
                    0.0, float(self.clock()) - self._started_at
                ),
                "workers": {
                    worker["name"]: worker["state"]
                    for worker in self.registry.list()
                },
                "dispatches": states,
                "kernel": str(self.kernel_dir / "kernel.db"),
                "events": self.store.event_count(),
            }
        if op == "workers.list":
            return worker_entries(self.registry)
        if op == "worker.probe":
            worker = self._worker_by_ref(args.get("worker"))
            return probe_worker(
                self.registry, worker, self._factory(worker)
            )
        if op == "worker.drain":
            worker = self._worker_by_ref(args.get("worker"))
            self.registry.drain(
                worker["worker_id"],
                reason=str(args.get("reason") or "operator drain"),
            )
            return {"ok": True, "state": WorkerState.DRAINING}
        if op == "worker.enable":
            worker = self._worker_by_ref(args.get("worker"))
            self.registry.activate(
                worker["worker_id"],
                reason=str(args.get("reason") or "operator enable"),
            )
            return {"ok": True, "state": WorkerState.ACTIVE}
        if op == "worker.grant":
            worker = self._worker_by_ref(args.get("worker"))
            grant = authority.validate_grant(
                args.get("actions"), args.get("tools"),
                str(args.get("data") or ""),
                caller_actions=args.get("caller_actions"),
                caller_data=str(args.get("caller_data") or ""),
            )
            ceiling = authority.apply_grant(
                self.registry, worker["worker_id"], grant,
                granted_by=str(args.get("granted_by") or "owner"),
            )
            self.log(
                f"grant applied to {worker['name']}: {grant}"
            )
            return {"ok": True, "ceiling": _ceiling_json(ceiling)}
        if op == "worker.revoke_grants":
            worker = self._worker_by_ref(args.get("worker"))
            ceiling = authority.revoke_grants(
                self.registry, worker["worker_id"]
            )
            return {"ok": True, "ceiling": _ceiling_json(ceiling)}
        if op == "mission.adhoc":
            return {"mission_id": ensure_adhoc_mission(self.store)}
        if op == "task.submit":
            envelope = args.get("envelope")
            if not isinstance(envelope, dict):
                raise KernelError("task.submit needs an envelope object")
            # The envelope arrives pre-clamped by the client; re-clamp on
            # a targeted worker here so a hand-rolled socket caller can't
            # skip the authority intersection.
            validated = TaskEnvelope.from_dict(dict(envelope))
            worker_ref = str(args.get("worker") or "")
            if worker_ref and worker_ref != "auto":
                worker = self._worker_by_ref(worker_ref)
                clamped = authority.clamp_envelope(
                    worker, tools=validated.tools,
                    actions=validated.action_classes,
                    data=validated.data_classification,
                    token_budget=validated.token_budget,
                    caller=args.get("caller") and {
                        **args["caller"],
                    } or None,
                )
                check_skill_availability(
                    self.registry, worker, validated.skills,
                    transport=self._factory(worker),
                )
                payload = validated.to_dict()
                payload.update({
                    "tools": list(clamped["tools"]),
                    "action_classes": list(clamped["actions"]),
                    "data_classification": clamped["data"],
                    "token_budget": clamped["token_budget"],
                })
                validated = TaskEnvelope.from_dict(payload)
            task_id = self.plane.submit(
                validated,
                required_trust=args.get("required_trust"),
            )
            return {"task_id": task_id}
        if op == "task.get":
            dispatch = self.store.get_dispatch(
                str(args.get("task_id") or "")
            )
            if dispatch is None:
                raise KernelError(
                    f"unknown task {args.get('task_id')!r}"
                )
            return dispatch
        if op == "tasks.list":
            return self.store.list_dispatches(
                state=str(args.get("state") or "")
            )
        if op == "task.cancel":
            return {
                "cancelled": self.plane.cancel(
                    str(args.get("task_id") or ""),
                    reason=str(args.get("reason") or "operator cancel"),
                )
            }
        if op == "task.events":
            return self.store.list_dispatch_events(
                str(args.get("task_id") or "")
            )
        if op == "artifact.pull":
            dispatch = self.store.get_dispatch(
                str(args.get("task_id") or "")
            )
            if dispatch is None:
                raise KernelError(
                    f"unknown task {args.get('task_id')!r}"
                )
            worker = self.registry.get(dispatch.get("worker_id") or "")
            if worker is None:
                raise KernelError("task has no assigned worker to pull from")
            digest = str(args.get("digest") or "").strip().lower()
            target = local_artifact_dir(self.kernel_dir) / digest
            size = self.plane.pull_artifact_to(worker, digest, target)
            return {"path": str(target), "size": size}
        if op == "shutdown":
            self._stop.set()
            return {"ok": True}
        raise KernelError(f"unknown control op {op!r}")


# ---------------------------------------------------------------------------
# Shared helpers (used by the daemon AND the direct-drive client, so both
# transports behave identically)
# ---------------------------------------------------------------------------

def _ceiling_json(ceiling: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tools": sorted(ceiling["tools"]),
        "actions": sorted(ceiling["actions"]),
        "data": ceiling["data"],
    }


def worker_entries(registry: FleetRegistry) -> List[Dict[str, Any]]:
    entries = []
    for worker in registry.list():
        entries.append({
            "worker_id": worker["worker_id"],
            "name": worker["name"],
            "host": worker["host"],
            "ssh_user": worker.get("ssh_user") or "",
            "state": worker["state"],
            "trust_level": worker["trust_level"],
            "data_ceiling": worker["data_ceiling"],
            "runtime_profile": worker.get("runtime_profile") or "",
            "max_concurrency": worker["max_concurrency"],
            "autonomy_capable": bool(worker.get("autonomy_capable")),
            "last_heartbeat_at": worker.get("last_heartbeat_at"),
            "skills": registry.worker_skills(worker),
            "ceiling": _ceiling_json(authority.worker_ceiling(worker)),
        })
    return entries


def probe_worker(registry: FleetRegistry, worker: Dict[str, Any],
                 transport) -> Dict[str, Any]:
    """Live ``worker.status`` probe; records heartbeat + skill inventory."""
    try:
        status = transport.call("worker.status", {})
    except Exception as exc:
        return {
            "name": worker["name"], "reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not status.ok:
        return {
            "name": worker["name"], "reachable": False,
            "error": status.error,
        }
    result = status.result
    try:
        registry.heartbeat(
            worker["worker_id"], int(result.get("heartbeat_seq") or 0)
        )
        if isinstance(result.get("skills"), list):
            registry.record_skills(worker["worker_id"], result["skills"])
    except KernelError:
        pass
    return {
        "name": worker["name"], "reachable": True,
        "status": result,
    }


def check_skill_availability(registry: FleetRegistry,
                             worker: Dict[str, Any],
                             skills, *, transport=None) -> None:
    """Refuse-with-clear-error when a targeted worker lacks a named skill.

    v1 deliberately does NOT ship skills to workers: built-in skills ride
    the signed artifact and user skills are installed on the host by its
    operator. Checks the recorded inventory first, falls back to a live
    probe when the registry has never seen one.
    """
    if not skills:
        return
    installed = set(registry.worker_skills(worker))
    if not installed and transport is not None:
        probe = probe_worker(registry, worker, transport)
        if probe.get("reachable"):
            installed = set(
                (probe.get("status") or {}).get("skills") or []
            )
    missing = set(skills) - installed
    if missing:
        raise authority.AuthorityError(
            f"worker {worker['name']!r} does not report skill(s) "
            f"{sorted(missing)} installed — refusing dispatch. Install "
            "the skill on the worker host (built-ins ship with the "
            "artifact; user skills go in ~/.config/conch/skills/)."
        )


# ---------------------------------------------------------------------------
# Entrypoint + supervised install (same machinery as conch-edge)
# ---------------------------------------------------------------------------

def run_controller_daemon(config: dict, *, once: bool = False) -> int:
    """Entry for ``conch-controller run``: start, supervise, exit cleanly."""
    daemon = ControllerDaemon(config, artifact_thread=not once)
    try:
        daemon.start()
    except DaemonAlreadyRunning as exc:
        print(f"conch-controller: {exc}", flush=True)
        return 75  # EX_TEMPFAIL: already running
    print(
        f"conch-controller: daemon running (epoch {daemon.epoch}, socket"
        f" {daemon.socket_path}, log {daemon.log_path})",
        flush=True,
    )
    if once:
        try:
            stats = daemon.tick()
            if daemon.artifacts is not None:
                stats["artifacts_pulled"] += daemon.artifacts.drain()
            print(
                f"conch-controller: single tick complete: {stats}",
                flush=True,
            )
        finally:
            daemon.shutdown()
        return 0
    daemon.install_signal_handlers()
    daemon.run_forever()
    return 0


CONTROLLER_LAUNCHD_LABEL = "com.conch.controller"
CONTROLLER_UNIT_NAME = "conch-controller"

_CONTROLLER_MAIN_SNIPPET = (
    "from conch.entrypoints import controller_main; "
    "import sys; sys.exit(controller_main())"
)


def _resolve_controller_program():
    import shutil
    import sys

    environment = {
        "PATH": os.environ.get("PATH", "").strip()
        or "/usr/local/bin:/usr/bin:/bin",
    }
    script = shutil.which("conch-controller")
    if script:
        return [script], environment
    package_parent = Path(__file__).resolve().parent.parent.parent
    environment["PYTHONPATH"] = str(package_parent)
    return [sys.executable, "-c", _CONTROLLER_MAIN_SNIPPET], environment


def controller_install_cmd(config: dict, *, platform_name: str = "",
                           runner=None, alive=None,
                           sleep=time.sleep, verify_seconds: float = 30.0,
                           out=print) -> int:
    """Render + load + verify the OS-supervised controller (idempotent)."""
    import subprocess
    import sys

    from ..kernel.install import (
        _wait_for,
        render_launchd_plist,
        render_systemd_unit,
    )

    platform_name = platform_name or sys.platform
    runner = runner or (
        lambda cmd: subprocess.run(cmd, capture_output=True, text=True)
    )
    alive = alive or controller_alive
    program_args, environment = _resolve_controller_program()
    if platform_name == "darwin":
        uid = os.getuid()
        plist_path = (
            Path.home() / "Library" / "LaunchAgents"
            / f"{CONTROLLER_LAUNCHD_LABEL}.plist"
        )
        kernel_dir = fleet_kernel_dir()
        kernel_dir.mkdir(parents=True, exist_ok=True)
        rendered = render_launchd_plist(
            program_args, environment,
            str(kernel_dir / "launchd.out"),
            str(kernel_dir / "launchd.err"),
            working_directory=str(Path.home()),
        ).replace("com.conch.edge", CONTROLLER_LAUNCHD_LABEL)
        runner(["launchctl", "bootout",
                f"gui/{uid}/{CONTROLLER_LAUNCHD_LABEL}"])
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(rendered)
        out(f"conch-controller install: wrote {plist_path}")
        result = runner(["launchctl", "bootstrap", f"gui/{uid}",
                         str(plist_path)])
        if result.returncode != 0:
            legacy = runner(["launchctl", "load", "-w", str(plist_path)])
            if legacy.returncode != 0:
                out("conch-controller install: launchctl could not load"
                    " the agent:")
                out(f"  bootstrap: "
                    f"{result.stderr.strip() or result.stdout.strip()}")
                out(f"  load -w:   "
                    f"{legacy.stderr.strip() or legacy.stdout.strip()}")
                return 1
        runner(["launchctl", "enable",
                f"gui/{uid}/{CONTROLLER_LAUNCHD_LABEL}"])
        if not _wait_for(alive, verify_seconds, sleep):
            out(
                "conch-controller install: agent loaded but the daemon did"
                f" not answer its socket within {int(verify_seconds)}s —"
                f" check {kernel_dir / 'launchd.err'} and"
                f" {kernel_dir / 'controller.log'}."
            )
            return 1
        out(
            f"conch-controller install: launchd agent"
            f" {CONTROLLER_LAUNCHD_LABEL} running"
        )
        return 0
    if platform_name.startswith("linux"):
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", "").strip()
            or (Path.home() / ".config")
        )
        unit_path = (
            config_home / "systemd" / "user"
            / f"{CONTROLLER_UNIT_NAME}.service"
        )
        rendered = render_systemd_unit(program_args, environment).replace(
            "Conch edge daemon (durable mission kernel)",
            "Conch fleet controller (task plane dispatcher)",
        )
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(rendered)
        out(f"conch-controller install: wrote {unit_path}")
        reload_result = runner(["systemctl", "--user", "daemon-reload"])
        if reload_result.returncode != 0:
            out("conch-controller install: daemon-reload failed:"
                f" {reload_result.stderr.strip()}")
            return 1
        enable = runner(["systemctl", "--user", "enable", "--now",
                         CONTROLLER_UNIT_NAME])
        if enable.returncode != 0:
            out("conch-controller install: enable --now failed:"
                f" {enable.stderr.strip()}")
            return 1
        if not _wait_for(alive, verify_seconds, sleep):
            out(
                "conch-controller install: unit enabled but the daemon did"
                f" not answer its socket within {int(verify_seconds)}s —"
                f" see journalctl --user -u {CONTROLLER_UNIT_NAME}."
            )
            return 1
        out("conch-controller install: systemd user unit running")
        return 0
    out(
        f"conch-controller install: unsupported platform {platform_name!r}"
        " — launchd (macOS) and systemd user units (Linux) are supported."
    )
    return 2


def controller_uninstall_cmd(config: dict, *, platform_name: str = "",
                             runner=None, alive=None,
                             sleep=time.sleep, out=print) -> int:
    import subprocess
    import sys

    from ..kernel.install import _wait_for

    platform_name = platform_name or sys.platform
    runner = runner or (
        lambda cmd: subprocess.run(cmd, capture_output=True, text=True)
    )
    alive = alive or controller_alive
    if platform_name == "darwin":
        uid = os.getuid()
        runner(["launchctl", "bootout",
                f"gui/{uid}/{CONTROLLER_LAUNCHD_LABEL}"])
        plist_path = (
            Path.home() / "Library" / "LaunchAgents"
            / f"{CONTROLLER_LAUNCHD_LABEL}.plist"
        )
        removed = False
        if plist_path.exists():
            plist_path.unlink()
            removed = True
        stopped = _wait_for(lambda: not alive(), 10.0, sleep)
        out(
            "conch-controller uninstall: "
            + (f"removed {plist_path}" if removed
               else "no launchd agent was installed")
            + ("; daemon stopped" if stopped
               else "; a controller still answers the socket")
        )
        return 0
    if platform_name.startswith("linux"):
        runner(["systemctl", "--user", "disable", "--now",
                CONTROLLER_UNIT_NAME])
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", "").strip()
            or (Path.home() / ".config")
        )
        unit_path = (
            config_home / "systemd" / "user"
            / f"{CONTROLLER_UNIT_NAME}.service"
        )
        removed = False
        if unit_path.exists():
            unit_path.unlink()
            removed = True
        runner(["systemctl", "--user", "daemon-reload"])
        stopped = _wait_for(lambda: not alive(), 10.0, sleep)
        out(
            "conch-controller uninstall: "
            + (f"removed {unit_path}" if removed
               else "no systemd user unit was installed")
            + ("; daemon stopped" if stopped
               else "; a controller still answers the socket")
        )
        return 0
    out(f"conch-controller uninstall: unsupported platform"
        f" {platform_name!r}")
    return 2


def controller_status_cmd(config: dict, *, platform_name: str = "",
                          runner=None, alive=None, out=print) -> int:
    import subprocess
    import sys

    platform_name = platform_name or sys.platform
    runner = runner or (
        lambda cmd: subprocess.run(cmd, capture_output=True, text=True)
    )
    alive = alive or controller_alive
    if platform_name == "darwin":
        plist_path = (
            Path.home() / "Library" / "LaunchAgents"
            / f"{CONTROLLER_LAUNCHD_LABEL}.plist"
        )
        out(f"  agent file: {plist_path}"
            + ("" if plist_path.exists() else " (not installed)"))
        listed = runner(["launchctl", "list", CONTROLLER_LAUNCHD_LABEL])
        out("  launchctl list: "
            + ("loaded" if listed.returncode == 0 else "not loaded"))
    elif platform_name.startswith("linux"):
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", "").strip()
            or (Path.home() / ".config")
        )
        unit_path = (
            config_home / "systemd" / "user"
            / f"{CONTROLLER_UNIT_NAME}.service"
        )
        out(f"  unit file: {unit_path}"
            + ("" if unit_path.exists() else " (not installed)"))
        active = runner(["systemctl", "--user", "is-active",
                         CONTROLLER_UNIT_NAME])
        out(f"  systemctl --user is-active: {active.stdout.strip() or '?'}")
    else:
        out(f"  platform {platform_name!r}: no supervisor integration")
    if not alive():
        out("  controller: not answering the control socket")
        return 1
    from ..kernel.control import ControlError, request

    try:
        status = request("status", socket_path=fleet_socket_path())
    except ControlError as exc:
        out(f"  control socket: no answer ({exc})")
        return 1
    workers = ", ".join(
        f"{name}={state}"
        for name, state in sorted((status.get("workers") or {}).items())
    ) or "none enrolled"
    dispatches = ", ".join(
        f"{count} {name}" for name, count in
        sorted((status.get("dispatches") or {}).items())
    ) or "none"
    out(f"  controller: healthy — holder {status.get('holder')},"
        f" epoch {status.get('epoch')},"
        f" uptime {int(status.get('uptime_seconds') or 0)}s")
    out(f"  workers: {workers}")
    out(f"  dispatches: {dispatches}")
    return 0
