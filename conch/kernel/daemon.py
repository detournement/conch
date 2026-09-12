"""The supervised ``conch-edge`` daemon: one kernel, one owner, no shells.

Responsibilities:

- **Exclusive ownership** of the kernel database: an OS-level ``flock`` on
  ``<kernel>/daemon.lock`` plus a monotonically increasing controller epoch
  adopted at start, so a superseded (zombie) daemon can never write again
  even if it still holds file handles.
- **Scheduler-driven session firing**: each tick claims due timers, fires
  them (exactly-once ledger effects), runs at most one ready mission
  session, delivers the outbox, and periodically reconciles leases and
  expires approvals.
- **Graceful SIGTERM/SIGINT**: stop accepting work, finish the in-flight
  kernel transaction, close the store, remove the socket, release the lock.
- **A permission-protected control socket** (0700 dir, 0600 socket)
  speaking the tiny versioned JSON protocol in :mod:`conch.kernel.control`;
  the shell attaches through it while the daemon runs.

``conch-controller`` (later phases) reuses this exact machinery with a
different mission surface, which is why nothing here is edge-specific
beyond naming and defaults.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .control import (
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_LINE_BYTES,
    control_socket_path,
    ensure_runtime_dir,
)
from .engine import MissionEngine
from .migrate import migrate_tasks_json
from .model import (
    ApprovalError,
    KernelError,
    MissionKind,
    StaleGenerationError,
)
from .store import MissionStore, default_kernel_dir, default_state_dir

DAEMON_LOG_MAX_BYTES = 1024 * 1024


class DaemonAlreadyRunning(KernelError):
    """Another daemon holds the kernel lock."""


class _KernelLock:
    """Exclusive, advisory, crash-released OS lock on the kernel dir."""

    def __init__(self, kernel_dir: Path):
        self.path = Path(kernel_dir) / "daemon.lock"
        self._handle = None

    def acquire(self, holder: str) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DaemonAlreadyRunning(
                    f"another conch-edge daemon holds {self.path} — two"
                    " daemons can never share one kernel database"
                )
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {holder}\n")
        handle.flush()
        self._handle = handle
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def release(self) -> None:
        if self._handle is not None:
            import fcntl

            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


class EdgeDaemon:
    """Headless supervisor owning one mission kernel."""

    def __init__(self, config: dict, *,
                 kernel_dir: Optional[Path] = None,
                 state_dir: Optional[Path] = None,
                 socket_path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time,
                 tick_seconds: float = 1.0,
                 session_factory: Optional[Callable] = None,
                 notifier: Optional[Callable] = None):
        self.config = config or {}
        self.state_dir = Path(state_dir) if state_dir else (
            default_state_dir()
        )
        self.kernel_dir = Path(kernel_dir) if kernel_dir else (
            default_kernel_dir()
        )
        self._socket_path = Path(socket_path) if socket_path else None
        self.clock = clock
        self.tick_seconds = float(tick_seconds)
        self.holder = f"edge-{socket.gethostname()}-{os.getpid()}"
        self._session_factory = session_factory
        self._notifier = notifier
        self._lock = _KernelLock(self.kernel_dir)
        self.store: Optional[MissionStore] = None
        self.engine: Optional[MissionEngine] = None
        self._capitol = None
        self._capitol_last_poll = 0.0
        self._capitol_error = ""
        self.epoch = 0
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._started_at = 0.0
        self._tick_count = 0
        self._log_lock = threading.Lock()

    # -- logging (never secrets) ----------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.kernel_dir / "daemon.log"

    def log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{stamp}] {line}\n"
        with self._log_lock:
            try:
                self.kernel_dir.mkdir(parents=True, exist_ok=True)
                if (self.log_path.exists()
                        and self.log_path.stat().st_size > DAEMON_LOG_MAX_BYTES):
                    tail = self.log_path.read_bytes()[-65536:]
                    self.log_path.write_bytes(
                        b"[log rotated]\n" + tail
                    )
                with open(self.log_path, "a") as handle:
                    handle.write(text)
            except OSError:
                pass

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Acquire the lock, open the kernel, adopt the epoch, migrate,
        reconcile, and bring up the control socket."""
        self._lock.acquire(self.holder)
        try:
            self.store = MissionStore(
                self.kernel_dir / "kernel.db", clock=self.clock
            )
            self.epoch = self.store.adopt_epoch()
            self.engine = MissionEngine(
                self.store, self.config, holder=self.holder,
                session_factory=self._session_factory,
                kernel_dir=self.kernel_dir, log=self.log,
            )
            report = migrate_tasks_json(
                self.engine, state_dir=self.state_dir, log=self.log
            )
            if report["migrated"]:
                self.log(
                    f"migrated {report['migrated']} legacy scheduled task(s)"
                    f" into the kernel (backup: {report['backup']})"
                )
            reconciled = self.store.reconcile(self.holder)
            if reconciled["sessions_abandoned"]:
                self.log(
                    f"recovered {reconciled['sessions_abandoned']}"
                    " abandoned session(s) from a previous run"
                )
            self._start_socket_server()
            self._started_at = float(self.clock())
            self.log(
                f"daemon started: holder={self.holder} epoch={self.epoch}"
                f" kernel={self.kernel_dir / 'kernel.db'}"
            )
        except BaseException:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        self._stop.set()
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
        if self.store is not None:
            self.store.close()
            self.store = None
        self._lock.release()
        self.log("daemon stopped")

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

    def tick(self) -> Dict[str, int]:
        """One supervision pass. Every step is crash-safe: timers are
        exactly-once ledger effects, sessions are lease-guarded, and outbox
        delivery is at-least-once with dedupe keys."""
        if self.store is None or self.engine is None:
            raise KernelError("daemon is not started")
        stats = {"fired": 0, "sessions": 0, "delivered": 0}
        for claim in self.store.claim_due_timers(
            self.holder, lease_seconds=120.0
        ):
            if self._stop.is_set():
                break
            try:
                result = self.store.fire_timer(
                    claim["timer_id"], claim["generation"],
                    holder=self.holder,
                )
                stats["fired"] += 1
                if result["fires"]:
                    self.log(
                        f"timer {claim['mission_id']}:{claim['logical_key']}"
                        f" fired x{len(result['fires'])}"
                    )
            except StaleGenerationError:
                continue  # another pass already advanced it
        if not self._stop.is_set():
            for result in self.engine.run_ready_sessions(limit=1):
                stats["sessions"] += 1
                self.log(
                    f"session {result.get('session_id', '?')} for"
                    f" {result['mission_id']}: {result['outcome']}"
                    + (f" ({result['error']})" if result.get("error") else "")
                )
        stats["delivered"] = self.deliver_outbox()
        self._capitol_tick(stats)
        self._tick_count += 1
        if self._tick_count % 60 == 0:
            self.store.reconcile(self.holder)
            self.store.expire_approvals()
        return stats

    # -- Capitol supervision (Swarm Phase 3) -----------------------------------

    def _capitol_configured(self) -> bool:
        return bool(
            str(self.config.get("capitol_base_url") or "").strip()
            and str(self.config.get("capitol_org") or "").strip()
            and str(self.config.get("capitol_agent") or "").strip()
        )

    def _capitol_tick(self, stats: Dict[str, int]) -> None:
        """Run one Capitol supervision pass on its own cadence.

        Unconfigured installs never import the adapter; a failing pass
        logs once per distinct error and retries on cadence — Capitol
        being down degrades bindings, never the daemon.
        """
        if not self._capitol_configured():
            return
        now = float(self.clock())
        try:
            poll_seconds = float(
                self.config.get("capitol_poll_seconds") or 10.0
            )
        except (TypeError, ValueError):
            poll_seconds = 10.0
        if now - self._capitol_last_poll < max(poll_seconds, 1.0):
            return
        self._capitol_last_poll = now
        try:
            if self._capitol is None:
                from ..capitol.supervisor import CapitolSupervisor

                self._capitol = CapitolSupervisor(
                    self.store, self.config, log=self.log,
                    clock=self.clock,
                )
            capitol_stats = self._capitol.tick()
            self._capitol_error = ""
            for key, value in capitol_stats.items():
                if value:
                    stats[f"capitol_{key}"] = (
                        stats.get(f"capitol_{key}", 0) + value
                    )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self._capitol_error:
                self._capitol_error = message
                self.log(f"capitol supervision error: {message}")

    # -- outbox delivery ---------------------------------------------------------

    def _notify(self, payload: Dict[str, Any]):
        """Deliver one channel notification. Returns (ok, transport, error).

        With no channel configured the notification is delivered to the
        daemon log — a real, queryable delivery record. Configuring Slack
        (or SMS/email) upgrades the same path to a live channel without any
        mission changes.
        """
        if self._notifier is not None:
            return self._notifier(payload)
        text = str(payload.get("text") or "")
        channel = str(payload.get("channel") or "")
        from ..channels import ChannelManager

        manager = ChannelManager(self.config)
        target = channel or (self.config.get("notify_channel") or "")
        if not manager.configured() or not manager.get(target or ""):
            if target and manager.get(target) is None and manager.configured():
                pass  # named channel missing; fall through to log delivery
            self.log(f"notify (no channel configured):\n{text}")
            return True, "log", ""
        ok, detail = manager.notify(text, channel=target)
        if ok:
            return True, target or "default", ""
        return False, "", str(detail)

    def deliver_outbox(self, limit: int = 8) -> int:
        delivered = 0
        for item in self.store.claim_deliverable_outbox(limit=limit):
            if item["kind"] != "channel_notify":
                self.store.mark_outbox_failed(
                    item["outbox_id"],
                    f"unknown outbox kind {item['kind']!r}", permanent=True,
                )
                self.log(
                    f"outbox #{item['outbox_id']}: unknown kind"
                    f" {item['kind']!r} — parked as failed"
                )
                continue
            try:
                payload = json.loads(item["payload"])
            except ValueError:
                self.store.mark_outbox_failed(
                    item["outbox_id"], "malformed payload", permanent=True
                )
                continue
            try:
                ok, transport, error = self._notify(payload)
            except Exception as exc:
                ok, transport, error = False, "", f"{type(exc).__name__}: {exc}"
            if ok:
                if self.store.mark_outbox_delivered(
                    item["outbox_id"], transport
                ):
                    delivered += 1
                    self.log(
                        f"outbox #{item['outbox_id']} delivered via"
                        f" {transport} (dedupe {item['dedupe_key']})"
                    )
            else:
                self.store.mark_outbox_failed(item["outbox_id"], error)
                self.log(
                    f"outbox #{item['outbox_id']} delivery failed: {error}"
                    " (will retry with backoff)"
                )
        return delivered

    # -- control socket ------------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        if self._socket_path is not None:
            return self._socket_path
        return control_socket_path()

    def _start_socket_server(self) -> None:
        path = self.socket_path
        if self._socket_path is None:
            ensure_runtime_dir()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
        # We hold the exclusive kernel lock: any existing socket file is a
        # leftover from a dead daemon.
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
            target=self._serve_loop, name="conch-edge-control", daemon=True
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
                conn.settimeout(10.0)
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
                f"unsupported control protocol version"
                f" {request.get('v') if isinstance(request, dict) else '?'!r}"
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
        except (KernelError, ApprovalError) as exc:
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

    def _dispatch(self, op: str, args: Dict[str, Any]) -> Any:
        from .views import (
            approval_entries,
            mission_detail,
            mission_summary,
            schedule_entries,
        )
        if op == "status":
            missions = self.store.list_missions()
            by_status: Dict[str, int] = {}
            for mission in missions:
                by_status[mission["status"]] = (
                    by_status.get(mission["status"], 0) + 1
                )
            return {
                "protocol": CONTROL_PROTOCOL_VERSION,
                "holder": self.holder,
                "epoch": self.epoch,
                "uptime_seconds": max(
                    0.0, float(self.clock()) - self._started_at
                ),
                "missions": by_status,
                "pending_approvals": len(self.store.pending_approvals()),
                "pending_outbox": len(self.store.list_outbox(status="pending")),
                "kernel": str(self.kernel_dir / "kernel.db"),
                "events": self.store.event_count(),
            }
        if op == "missions.list":
            return [
                mission_summary(self.store, mission)
                for mission in self.store.list_missions()
            ]
        if op == "mission.get":
            mission_id = str(args.get("mission_id") or "")
            mission = self.store.get_mission(mission_id)
            if mission is None:
                raise KernelError(f"unknown mission {mission_id!r}")
            return mission_detail(self.store, mission)
        if op == "mission.new":
            spec = args.get("spec")
            if not isinstance(spec, dict):
                raise KernelError("mission.new needs a spec object")
            mission_id = self.engine.create_mission(
                spec, activate=bool(args.get("activate", True))
            )
            return {"mission_id": mission_id}
        if op == "mission.pause":
            self.engine.pause_mission(str(args.get("mission_id") or ""))
            return {"ok": True}
        if op == "mission.resume":
            self.engine.resume_mission(str(args.get("mission_id") or ""))
            return {"ok": True}
        if op == "mission.abort":
            self.engine.abort_mission(str(args.get("mission_id") or ""))
            return {"ok": True}
        if op == "mission.input":
            self.engine.provide_input(
                str(args.get("mission_id") or ""),
                str(args.get("text") or ""),
            )
            return {"ok": True}
        if op == "approvals.list":
            return approval_entries(self.store)
        if op == "approval.decide":
            return self.engine.decide_approval(
                str(args.get("approval_id") or ""),
                str(args.get("verb") or ""),
                nonce=str(args.get("nonce") or ""),
                origin_channel="local",
                decided_by=str(args.get("decided_by") or "shell"),
            )
        if op == "schedule.add":
            prompt = str(args.get("prompt") or "").strip()
            try:
                interval = int(args.get("interval") or 0)
            except (TypeError, ValueError):
                interval = 0
            if not prompt or interval <= 0:
                raise KernelError(
                    "schedule.add needs a prompt and a positive interval"
                )
            mission_id = self.engine.create_mission({
                "goal": f"scheduled task: {prompt[:120]}",
                "kind": MissionKind.SCHEDULED_PROMPT,
                "prompt": prompt,
                "cadence_seconds": interval,
                "run_once": bool(args.get("run_once")),
                "budgets": {},
            }, activate=True)
            return {"mission_id": mission_id, "interval": interval}
        if op == "schedule.list":
            return schedule_entries(self.store)
        if op == "schedule.cancel":
            mission_id = str(args.get("mission_id") or "")
            mission = self.store.get_mission(mission_id)
            if mission is None or mission["kind"] != (
                MissionKind.SCHEDULED_PROMPT
            ):
                raise KernelError(
                    f"no scheduled task with mission id {mission_id!r}"
                )
            self.engine.abort_mission(mission_id)
            return {"ok": True}
        if op == "shutdown":
            self._stop.set()
            return {"ok": True}
        raise KernelError(f"unknown control op {op!r}")


def run_edge_daemon(config: dict, *, foreground: bool = True,
                    once: bool = False) -> int:
    """Entry for ``conch-edge``: start, supervise, exit cleanly."""
    # Honor agent_mode/permission_mode from config exactly like the
    # interactive shell does at startup — mission sessions execute headless
    # with the same authority scheduled tasks have always had.
    from ..bootstrap import apply_agent_mode_from_config

    apply_agent_mode_from_config(config)
    daemon = EdgeDaemon(config)
    try:
        daemon.start()
    except DaemonAlreadyRunning as exc:
        print(f"conch-edge: {exc}", flush=True)
        return 75  # EX_TEMPFAIL: already running
    print(
        f"conch-edge: daemon running (epoch {daemon.epoch}, socket"
        f" {daemon.socket_path}, log {daemon.log_path})",
        flush=True,
    )
    if once:
        try:
            stats = daemon.tick()
            print(f"conch-edge: single tick complete: {stats}", flush=True)
        finally:
            daemon.shutdown()
        return 0
    daemon.install_signal_handlers()
    daemon.run_forever()
    return 0
