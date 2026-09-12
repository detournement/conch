"""The conch fleet worker supervisor (Swarm Phase 2).

One supervisor process owns one worker home directory on a trusted host.
It is replaceable compute, never a state authority: mission truth stays on
the controller; the supervisor holds only bounded task envelopes, locally
spooled events, and per-task workspaces.

Guarantees implemented here:

- **Offer/start receipt handshake**: an offer persists
  ``(task, attempt, fence, epoch)`` and its receipt in one SQLite
  transaction *before* acknowledging; duplicate offers/starts return the
  recorded receipt. Start is a separate idempotent step so a controller
  crash between offer and start recovers by re-sending either.
- **Fencing**: offers/starts/resumes carrying an older
  ``(controller_epoch, fence)`` than the persisted one are rejected; a
  newer fence supersedes the old attempt (killing its process group), so
  a stale controller or worker can never commit.
- **Bounded queue**: past capacity, offers are rejected with
  ``retry_after`` — the controller backs off instead of piling work on.
- **Event spool**: task subprocesses append events to a per-attempt JSONL
  spool; the controller polls ``task.events`` and acknowledges a
  watermark. Events survive supervisor restarts and redelivery is safe
  (the controller dedupes on ``(task, attempt, seq)``).
- **Cancellation** is a state transition plus a process-group kill.
- **Wall-clock enforcement**: a task subprocess that outlives its
  envelope's wall budget (plus grace) is killed and failed as
  ``resource``.

The task subprocess runs :mod:`conch.fleet.taskexec` (an ``AgentSession``
with the envelope's exact tool intersection) in its own session/process
group, so kills are total and a supervisor restart can adopt survivors.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..swarm.protocol import (
    FailureClass,
    MAX_WIRE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    RpcRequest,
    RpcResponse,
    TaskEnvelope,
    new_id,
)

#: Worker-side task states (controller truth is the dispatch table).
OFFERED = "offered"
RUNNING = "running"
WAITING_CHILD = "waiting_child"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})

#: Exit code a task subprocess uses to signal "parked on a delegation".
CHILD_EXIT_WAITING = 75

#: Grace beyond the envelope wall clock before the supervisor kills a task.
WALL_GRACE_SECONDS = 30.0

#: Max artifact chunk (raw bytes) per RPC — the b64 form stays well under
#: the wire bound.
ARTIFACT_CHUNK_BYTES = 256 * 1024

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    attempt INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    controller_epoch INTEGER NOT NULL DEFAULT 0,
    envelope TEXT NOT NULL,
    state TEXT NOT NULL,
    offer_receipt TEXT NOT NULL DEFAULT '{}',
    receipt TEXT NOT NULL DEFAULT '{}',
    pid INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    acked_seq INTEGER NOT NULL DEFAULT -1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class WorkerError(Exception):
    pass


def _now() -> float:
    return time.time()


class WorkerSupervisor:
    """Owns one worker home; serves the RPC socket; reaps task children."""

    def __init__(self, home, config: Optional[Dict[str, Any]] = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:
            pass
        for sub in ("run", "state", "workspaces", "artifacts/sha256",
                    "logs"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.config = dict(config or self._load_config())
        self.worker_id = str(self.config.get("worker_id") or "")
        self.name = str(self.config.get("name") or self.home.name)
        self.max_concurrency = max(
            1, int(self.config.get("max_concurrency", 1))
        )
        self.max_queue = max(1, int(self.config.get("max_queue", 8)))
        self._lock = threading.RLock()
        self._children: Dict[str, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._reaper: Optional[threading.Thread] = None
        self._heartbeat_seq = 0
        self._started_at = _now()
        self._db = sqlite3.connect(
            str(self.home / "state" / "worker.db"), check_same_thread=False
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_DB_SCHEMA)
        self._db.commit()
        self.incarnation = self._bump_incarnation()

    # -- setup ---------------------------------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        path = self.home / "config.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _bump_incarnation(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM meta WHERE key='incarnation'"
            ).fetchone()
            value = (int(row[0]) if row else 0) + 1
            self._db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES"
                " ('incarnation', ?)",
                (str(value),),
            )
            self._db.commit()
            return value

    def log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.home / "logs" / "supervisor.log", "a") as handle:
                handle.write(f"[{stamp}] {line}\n")
        except OSError:
            pass

    # -- workspace/spool paths -------------------------------------------------

    def workspace(self, task_id: str) -> Path:
        return self.home / "workspaces" / task_id

    def _events_path(self, task_id: str, attempt: int) -> Path:
        return self.workspace(task_id) / f"events-{attempt}.jsonl"

    def _result_path(self, task_id: str, attempt: int) -> Path:
        return self.workspace(task_id) / f"result-{attempt}.json"

    def _resume_path(self, task_id: str, attempt: int) -> Path:
        return self.workspace(task_id) / f"resume-{attempt}.json"

    def _task_file(self, task_id: str) -> Path:
        return self.workspace(task_id) / "task.json"

    # -- db helpers --------------------------------------------------------------

    def _row(self, task_id: str) -> Optional[Dict[str, Any]]:
        cursor = self._db.execute(
            "SELECT task_id, attempt, fence, controller_epoch, envelope,"
            " state, offer_receipt, receipt, pid, started_at, acked_seq"
            " FROM tasks WHERE task_id=?",
            (task_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "task_id": row[0], "attempt": int(row[1]), "fence": int(row[2]),
            "controller_epoch": int(row[3]),
            "envelope": json.loads(row[4]), "state": row[5],
            "offer_receipt": json.loads(row[6]),
            "receipt": json.loads(row[7]), "pid": int(row[8]),
            "started_at": float(row[9]), "acked_seq": int(row[10]),
        }

    def _set_state(self, task_id: str, state: str, *, pid: int = -1,
                   receipt: Optional[Dict[str, Any]] = None,
                   started_at: Optional[float] = None) -> None:
        sets = ["state=?", "updated_at=?"]
        params: List[Any] = [state, _now()]
        if pid >= 0:
            sets.append("pid=?")
            params.append(pid)
        if receipt is not None:
            sets.append("receipt=?")
            params.append(json.dumps(receipt, sort_keys=True))
        if started_at is not None:
            sets.append("started_at=?")
            params.append(started_at)
        params.append(task_id)
        self._db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", params
        )
        self._db.commit()

    def _in_flight_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN (?,?,?)",
            (OFFERED, RUNNING, WAITING_CHILD),
        ).fetchone()
        return int(row[0])

    # -- RPC dispatch ---------------------------------------------------------

    def handle_request(self, request: RpcRequest) -> RpcResponse:
        handlers = {
            "worker.status": self._op_status,
            "task.offer": self._op_offer,
            "task.start": self._op_start,
            "task.cancel": self._op_cancel,
            "task.status": self._op_task_status,
            "task.events": self._op_events,
            "task.events_ack": self._op_events_ack,
            "task.resume": self._op_resume,
            "artifact.put": self._op_artifact_put,
            "artifact.get": self._op_artifact_get,
        }
        handler = handlers.get(request.op)
        if handler is None:  # pragma: no cover — RpcRequest validates ops
            return self._fail(request, "unknown op", FailureClass.BUG)
        try:
            with self._lock:
                result = handler(request.args)
        except _RpcFailure as exc:
            return RpcResponse(
                rpc_id=request.rpc_id, ok=False, error=exc.message,
                error_class=exc.error_class, retry_after=exc.retry_after,
            )
        except (WorkerError, ProtocolError, KeyError, ValueError,
                TypeError) as exc:
            return self._fail(
                request, f"{type(exc).__name__}: {exc}", FailureClass.BUG
            )
        return RpcResponse(rpc_id=request.rpc_id, ok=True, result=result)

    @staticmethod
    def _fail(request: RpcRequest, message: str,
              error_class: str) -> RpcResponse:
        return RpcResponse(
            rpc_id=request.rpc_id, ok=False, error=message,
            error_class=error_class,
        )

    # -- ops --------------------------------------------------------------------

    def _op_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        tasks = {}
        for row in self._db.execute(
            "SELECT task_id, attempt, fence, state, acked_seq FROM tasks"
        ).fetchall():
            events = self._read_events(row[0], int(row[1]))
            tasks[row[0]] = {
                "attempt": int(row[1]), "fence": int(row[2]),
                "state": row[3], "acked_seq": int(row[4]),
                "event_count": len(events),
            }
        return {
            "worker_id": self.worker_id, "name": self.name,
            "incarnation": self.incarnation,
            "heartbeat_seq": self._heartbeat_seq,
            "protocol_version": PROTOCOL_VERSION,
            "uptime_seconds": max(0.0, _now() - self._started_at),
            "queue": {
                "in_flight": self._in_flight_count(),
                "max_queue": self.max_queue,
                "max_concurrency": self.max_concurrency,
            },
            "tasks": tasks,
        }

    def _offer_receipt(self, row: Dict[str, Any],
                       duplicate: bool) -> Dict[str, Any]:
        receipt = dict(row["offer_receipt"])
        receipt["duplicate"] = duplicate
        receipt["state"] = row["state"]
        return receipt

    def _op_offer(self, args: Dict[str, Any]) -> Dict[str, Any]:
        envelope = TaskEnvelope.from_dict(dict(args["envelope"]))
        attempt = int(args["attempt"])
        fence = int(args["fence"])
        epoch = int(args.get("controller_epoch", 0))
        if attempt < 1 or fence < 0:
            raise WorkerError("attempt/fence out of range")
        row = self._row(envelope.task_id)
        if row is not None:
            incoming = (epoch, fence)
            stored = (row["controller_epoch"], row["fence"])
            if incoming == stored and attempt == row["attempt"]:
                # The duplicate contract: the recorded receipt, unchanged.
                return self._offer_receipt(row, duplicate=True)
            if incoming < stored:
                raise _RpcFailure(
                    f"stale fence {incoming} < {stored} for"
                    f" {envelope.task_id} — refusing",
                    FailureClass.POLICY,
                )
            if incoming == stored and attempt != row["attempt"]:
                raise _RpcFailure(
                    f"offer for {envelope.task_id} reuses fence {fence}"
                    f" with a different attempt — refusing",
                    FailureClass.POLICY,
                )
            # Newer fence: the controller superseded the old attempt.
            self._kill_task(envelope.task_id, row, reason="superseded")
        else:
            if self._in_flight_count() >= self.max_queue:
                raise _RpcFailure(
                    f"worker queue is full ({self.max_queue}) — retry later",
                    FailureClass.RESOURCE, retry_after=5.0,
                )
        workspace = self.workspace(envelope.task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        receipt = {
            "receipt_id": new_id("rcpt"),
            "task_id": envelope.task_id,
            "attempt": attempt,
            "fence": fence,
            "controller_epoch": epoch,
            "accepted_at": _now(),
        }
        # Persist (task, attempt, fence) + receipt BEFORE acknowledging.
        self._db.execute(
            "INSERT OR REPLACE INTO tasks(task_id, attempt, fence,"
            " controller_epoch, envelope, state, offer_receipt, receipt,"
            " pid, started_at, acked_seq, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?, '{}', 0, 0, -1, ?, ?)",
            (envelope.task_id, attempt, fence, epoch, envelope.to_json(),
             OFFERED, json.dumps(receipt, sort_keys=True), _now(), _now()),
        )
        self._db.commit()
        self._task_file(envelope.task_id).write_text(json.dumps({
            "envelope": envelope.to_dict(), "attempt": attempt,
            "fence": fence,
        }, sort_keys=True), encoding="utf-8")
        row = self._row(envelope.task_id)
        return self._offer_receipt(row, duplicate=False)

    def _check_fence(self, row: Dict[str, Any],
                     args: Dict[str, Any]) -> None:
        attempt = int(args["attempt"])
        fence = int(args["fence"])
        if (fence, attempt) != (row["fence"], row["attempt"]):
            raise _RpcFailure(
                f"fence/attempt mismatch for {row['task_id']}:"
                f" got (fence={fence}, attempt={attempt}), current is"
                f" (fence={row['fence']}, attempt={row['attempt']})"
                " — refusing",
                FailureClass.POLICY,
            )

    def _op_start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args["task_id"])
        row = self._row(task_id)
        if row is None:
            raise _RpcFailure(
                f"no offered task {task_id!r}", FailureClass.POLICY
            )
        self._check_fence(row, args)
        if row["state"] in TERMINAL_STATES:
            return {"state": row["state"], "duplicate": True,
                    "receipt": row["receipt"]}
        if row["state"] in (RUNNING, WAITING_CHILD):
            return {"state": row["state"], "duplicate": True}
        if self._running_count() >= self.max_concurrency:
            raise _RpcFailure(
                "worker is at max concurrency — retry later",
                FailureClass.RESOURCE, retry_after=3.0,
            )
        self._spawn(task_id, row)
        return {"state": RUNNING, "duplicate": False}

    def _op_cancel(self, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args["task_id"])
        row = self._row(task_id)
        if row is None:
            raise _RpcFailure(
                f"no task {task_id!r}", FailureClass.POLICY
            )
        if row["state"] in TERMINAL_STATES:
            return {"state": row["state"], "duplicate": True}
        self._kill_task(task_id, row, reason=str(
            args.get("reason") or "controller cancel"
        ))
        self._set_state(task_id, CANCELLED, receipt={
            "outcome": "cancelled",
            "reason": str(args.get("reason") or "controller cancel"),
        })
        self._append_event(task_id, row["attempt"], "cancelled", {})
        return {"state": CANCELLED, "duplicate": False}

    def _op_task_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args["task_id"])
        row = self._row(task_id)
        if row is None:
            raise _RpcFailure(
                f"no task {task_id!r}", FailureClass.POLICY
            )
        events = self._read_events(task_id, row["attempt"])
        return {
            "task_id": task_id, "state": row["state"],
            "attempt": row["attempt"], "fence": row["fence"],
            "receipt": row["receipt"], "event_count": len(events),
            "acked_seq": row["acked_seq"],
        }

    def _op_events(self, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args["task_id"])
        row = self._row(task_id)
        if row is None:
            raise _RpcFailure(
                f"no task {task_id!r}", FailureClass.POLICY
            )
        attempt = int(args.get("attempt") or row["attempt"])
        since = int(args.get("since_seq", -1))
        events = self._read_events(task_id, attempt)
        batch = []
        for seq, event in enumerate(events):
            if seq <= since:
                continue
            batch.append({
                "task_id": task_id, "attempt": attempt, "sequence": seq,
                "kind": event.get("kind", "log"),
                "failure_class": event.get("failure_class", ""),
                "payload": event.get("payload", {}),
                "created_at": event.get("created_at", 0.0),
            })
            if len(batch) >= 64:
                break
        return {
            "events": batch, "watermark": len(events) - 1,
            "attempt": attempt, "state": row["state"],
        }

    def _op_events_ack(self, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args["task_id"])
        row = self._row(task_id)
        if row is None:
            raise _RpcFailure(
                f"no task {task_id!r}", FailureClass.POLICY
            )
        upto = int(args["upto_seq"])
        if upto > row["acked_seq"]:
            self._db.execute(
                "UPDATE tasks SET acked_seq=?, updated_at=? WHERE"
                " task_id=?",
                (upto, _now(), task_id),
            )
            self._db.commit()
        return {"acked_seq": max(upto, row["acked_seq"])}

    def _op_resume(self, args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(args["task_id"])
        row = self._row(task_id)
        if row is None:
            raise _RpcFailure(
                f"no task {task_id!r}", FailureClass.POLICY
            )
        self._check_fence(row, args)
        if row["state"] in TERMINAL_STATES:
            return {"state": row["state"], "duplicate": True,
                    "receipt": row["receipt"]}
        if row["state"] == RUNNING:
            return {"state": RUNNING, "duplicate": True}
        if row["state"] != WAITING_CHILD:
            raise _RpcFailure(
                f"task {task_id} is {row['state']}, not waiting_child",
                FailureClass.POLICY,
            )
        resume_path = self._resume_path(task_id, row["attempt"])
        try:
            resume = json.loads(resume_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise _RpcFailure(
                f"task {task_id} has no readable resume state",
                FailureClass.BUG,
            )
        resume["child_result"] = {
            "tool_call_id": str(args.get("tool_call_id", "")),
            "result_text": str(args.get("result_text", "")),
        }
        resume_path.write_text(
            json.dumps(resume, sort_keys=True), encoding="utf-8"
        )
        self._spawn(task_id, row, resume=True)
        return {"state": RUNNING, "duplicate": False}

    # -- artifact transfer (content-addressed, digest-verified) ----------------

    def _artifact_path(self, digest: str) -> Path:
        digest = str(digest).lower()
        if len(digest) != 64 or any(
            c not in "0123456789abcdef" for c in digest
        ):
            raise WorkerError(f"invalid artifact digest {digest!r}")
        return self.home / "artifacts" / "sha256" / digest

    def _op_artifact_put(self, args: Dict[str, Any]) -> Dict[str, Any]:
        digest = str(args["digest"])
        path = self._artifact_path(digest)
        if path.is_file():
            return {"complete": True, "duplicate": True,
                    "size": path.stat().st_size}
        offset = int(args["offset"])
        total = int(args["total_size"])
        data = base64.b64decode(str(args["data_b64"]), validate=True)
        if len(data) > ARTIFACT_CHUNK_BYTES:
            raise WorkerError("artifact chunk exceeds the bound")
        part = path.with_name(path.name + ".part")
        current = part.stat().st_size if part.exists() else 0
        if offset != current:
            raise _RpcFailure(
                f"artifact offset {offset} != spooled {current} —"
                " transfer must be sequential",
                FailureClass.TRANSIENT,
            )
        with open(part, "ab") as handle:
            handle.write(data)
        size = part.stat().st_size
        if size < total:
            return {"complete": False, "received": size}
        if size > total:
            part.unlink()
            raise WorkerError("artifact overshot its declared size")
        actual = hashlib.sha256(part.read_bytes()).hexdigest()
        if actual != digest:
            part.unlink()
            raise _RpcFailure(
                f"artifact digest mismatch after transfer (got {actual})"
                " — nothing stored",
                FailureClass.TRANSIENT,
            )
        os.replace(part, path)
        return {"complete": True, "duplicate": False, "size": size}

    def _op_artifact_get(self, args: Dict[str, Any]) -> Dict[str, Any]:
        digest = str(args["digest"])
        path = self._artifact_path(digest)
        if not path.is_file():
            raise _RpcFailure(
                f"artifact {digest} not present", FailureClass.TRANSIENT
            )
        offset = int(args.get("offset", 0))
        length = min(
            int(args.get("length", ARTIFACT_CHUNK_BYTES)),
            ARTIFACT_CHUNK_BYTES,
        )
        total = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        return {
            "data_b64": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) >= total,
            "total_size": total,
        }

    # -- task subprocess lifecycle ------------------------------------------------

    def _running_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE state=?", (RUNNING,)
        ).fetchone()
        return int(row[0])

    def _spawn(self, task_id: str, row: Dict[str, Any],
               resume: bool = False) -> None:
        workspace = self.workspace(task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        import conch

        package_root = str(Path(conch.__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            package_root + (os.pathsep + existing if existing else "")
        )
        env["CONCH_FLEET_TASK_HOME"] = str(self.home)
        env["CONCH_FLEET_TASK_ID"] = task_id
        env["CONCH_FLEET_TASK_ATTEMPT"] = str(row["attempt"])
        if resume:
            env["CONCH_FLEET_TASK_RESUME"] = "1"
        else:
            # A fresh (re)start invalidates any previous spool for this
            # attempt: the attempt starts clean.
            for path in (self._events_path(task_id, row["attempt"]),
                         self._result_path(task_id, row["attempt"]),
                         self._resume_path(task_id, row["attempt"])):
                try:
                    path.unlink()
                except OSError:
                    pass
        log_path = workspace / f"child-{row['attempt']}.log"
        with open(log_path, "ab") as log_handle:
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "from conch.fleet.taskexec import child_main;"
                 " raise SystemExit(child_main())"],
                stdin=subprocess.DEVNULL, stdout=log_handle,
                stderr=log_handle, cwd=str(workspace), env=env,
                start_new_session=True,
            )
        self._children[task_id] = child
        self._set_state(
            task_id, RUNNING, pid=child.pid, started_at=_now()
        )
        self.log(
            f"task {task_id} attempt {row['attempt']} started"
            f" (pid {child.pid}{', resumed' if resume else ''})"
        )

    def _kill_task(self, task_id: str, row: Dict[str, Any],
                   reason: str) -> None:
        pid = row.get("pid") or 0
        child = self._children.pop(task_id, None)
        target = child.pid if child is not None else pid
        if target and _pid_alive(target):
            _kill_process_group(target)
        if child is not None:
            try:
                child.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass
        if target:
            self.log(f"task {task_id} killed (pgid {target}): {reason}")

    def _append_event(self, task_id: str, attempt: int, kind: str,
                      payload: Dict[str, Any],
                      failure_class: str = "") -> None:
        """Supervisor-side spool append (cancellations, supervisor kills)."""
        path = self._events_path(task_id, attempt)
        line = json.dumps({
            "kind": kind, "payload": payload,
            "failure_class": failure_class, "created_at": _now(),
        }, sort_keys=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _read_events(self, task_id: str,
                     attempt: int) -> List[Dict[str, Any]]:
        path = self._events_path(task_id, attempt)
        if not path.is_file():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # torn trailing write — ignore the partial line
        return events

    # -- reaper -------------------------------------------------------------------

    def reap_once(self) -> None:
        """One supervision pass: finalize exited children, enforce wall
        clocks, adopt survivors after a restart."""
        with self._lock:
            self._heartbeat_seq += 1
            rows = [
                self._row(row[0]) for row in self._db.execute(
                    "SELECT task_id FROM tasks WHERE state=?", (RUNNING,)
                ).fetchall()
            ]
        for row in rows:
            if row is None:
                continue
            task_id = row["task_id"]
            child = self._children.get(task_id)
            alive = (
                child.poll() is None if child is not None
                else _pid_alive(row["pid"])
            )
            envelope = row["envelope"]
            wall = float(envelope.get("wall_clock_seconds", 600))
            if alive:
                if row["started_at"] and (
                    _now() - row["started_at"] > wall + WALL_GRACE_SECONDS
                ):
                    with self._lock:
                        self._kill_task(task_id, row, reason="wall clock")
                        self._finalize(
                            row, outcome="failure",
                            failure_class=FailureClass.RESOURCE,
                            error="wall clock budget exceeded",
                        )
                continue
            self._children.pop(task_id, None)
            with self._lock:
                fresh = self._row(task_id)
                if fresh is None or fresh["state"] != RUNNING:
                    continue
                self._finalize_exited(fresh, child)

    def _finalize_exited(self, row: Dict[str, Any],
                         child: Optional[subprocess.Popen]) -> None:
        task_id = row["task_id"]
        attempt = row["attempt"]
        result_path = self._result_path(task_id, attempt)
        resume_path = self._resume_path(task_id, attempt)
        rc = child.returncode if child is not None else None
        if resume_path.is_file() and (rc in (CHILD_EXIT_WAITING, None)):
            self._set_state(task_id, WAITING_CHILD, pid=0)
            self.log(f"task {task_id} parked waiting_child")
            return
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except ValueError:
                result = None
            if isinstance(result, dict) and result.get("outcome") in (
                "success", "failure"
            ):
                self._finalize(
                    row, outcome=result["outcome"],
                    failure_class=str(result.get("failure_class", "")),
                    error=str(result.get("error", "")),
                    payload=result.get("payload") or {},
                )
                return
        self._finalize(
            row, outcome="failure", failure_class=FailureClass.TRANSIENT,
            error=f"task subprocess died (rc={rc})",
        )

    def _finalize(self, row: Dict[str, Any], *, outcome: str,
                  failure_class: str = "", error: str = "",
                  payload: Optional[Dict[str, Any]] = None) -> None:
        task_id = row["task_id"]
        receipt = {
            "receipt_id": new_id("rcpt"),
            "task_id": task_id,
            "attempt": row["attempt"],
            "fence": row["fence"],
            "outcome": outcome,
            "failure_class": failure_class,
            "error": error,
            "payload": payload or {},
            "finished_at": _now(),
        }
        state = COMPLETED if outcome == "success" else FAILED
        self._set_state(task_id, state, pid=0, receipt=receipt)
        self.log(
            f"task {task_id} attempt {row['attempt']} finalized:"
            f" {outcome}{' (' + failure_class + ')' if failure_class else ''}"
        )

    # -- socket server -----------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        return self.home / "run" / "supervisor.sock"

    def serve_forever(self) -> int:
        path = self.socket_path
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        # The 0700 run directory is the authorization boundary; some
        # filesystems (e.g. Docker Desktop bind mounts) reject chmod on a
        # socket, so tightening the socket itself is best-effort.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        (self.home / "run" / "supervisor.pid").write_text(
            f"{os.getpid()}\n"
        )
        self._reaper = threading.Thread(
            target=self._reaper_loop, name="conch-worker-reaper",
            daemon=True,
        )
        self._reaper.start()
        self.log(
            f"supervisor started: incarnation {self.incarnation},"
            f" pid {os.getpid()}, socket {path}"
        )
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.settimeout(30.0)
                    self._serve_one(conn)
                except Exception as exc:
                    self.log(
                        f"rpc error: {type(exc).__name__}: {exc}"
                    )
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            self.shutdown()
        return 0

    def _serve_one(self, conn: socket.socket) -> None:
        chunks = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_WIRE_BYTES:
                self._respond_error(conn, "request exceeds size bound")
                return
            if chunk.endswith(b"\n"):
                break
        raw = b"".join(chunks).strip()
        if not raw:
            return
        try:
            request = RpcRequest.from_json(raw)
        except ProtocolError as exc:
            self._respond_error(conn, f"protocol error: {exc}")
            return
        response = self.handle_request(request)
        try:
            conn.sendall(response.to_json().encode("ascii") + b"\n")
        except OSError:
            pass

    @staticmethod
    def _respond_error(conn: socket.socket, message: str) -> None:
        # A malformed request has no rpc_id to echo; reply with a fresh
        # one so the line is still a valid RpcResponse.
        response = RpcResponse(
            rpc_id=new_id("rpc"), ok=False, error=message,
            error_class=FailureClass.BUG,
        )
        try:
            conn.sendall(response.to_json().encode("ascii") + b"\n")
        except OSError:
            pass

    def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reap_once()
            except Exception as exc:
                self.log(f"reaper error: {type(exc).__name__}: {exc}")
            self._stop.wait(0.2)

    def request_stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass
        self.log("supervisor stopped")

    def close(self) -> None:
        self.shutdown()
        try:
            self._db.close()
        except sqlite3.Error:
            pass


class _RpcFailure(Exception):
    """Internal: a handler rejection with a failure class + retry hint."""

    def __init__(self, message: str, error_class: str,
                 retry_after: float = 0.0):
        super().__init__(message)
        self.message = message
        self.error_class = error_class
        self.retry_after = retry_after


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_process_group(pid: int) -> None:
    """SIGTERM the process group, escalate to SIGKILL after a grace."""
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except OSError:
                return
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Relay mode (docker profile): stdin line → in-container socket → stdout
# ---------------------------------------------------------------------------

def relay_once(home) -> int:
    home = Path(home)
    socket_path = home / "run" / "supervisor.sock"
    line = sys.stdin.buffer.readline(MAX_WIRE_BYTES + 2)
    if not line.strip():
        print("conch-worker --relay expects one JSON line", file=sys.stderr)
        return 2
    if len(line) > MAX_WIRE_BYTES:
        print("request exceeds size bound", file=sys.stderr)
        return 1
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(30.0)
    try:
        client.connect(str(socket_path))
        client.sendall(line)
        chunks = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_WIRE_BYTES:
                print("response exceeds size bound", file=sys.stderr)
                return 1
            if chunk.endswith(b"\n"):
                break
    except OSError as exc:
        print(f"relay failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    sys.stdout.buffer.write(b"".join(chunks))
    sys.stdout.buffer.flush()
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conch-worker",
        description=(
            "Bounded Conch fleet worker supervisor: executes versioned"
            " task envelopes under offer/start receipts, fencing, event"
            " spooling, and process-group cancellation. Deployed as a"
            " signed single-file artifact (or the docker profile) by"
            " conch-hostctl; the controller drives it over SSH stdio."
        ),
    )
    try:
        from conch import __version__ as version
    except ImportError:  # pragma: no cover
        version = "standalone"
    parser.add_argument(
        "--version", action="version",
        version=f"conch-worker {version}",
    )
    parser.add_argument(
        "--home", default=os.environ.get("CONCH_FLEET_WORKER_HOME", ""),
        metavar="DIR",
        help="Worker home directory (or $CONCH_FLEET_WORKER_HOME).",
    )
    parser.add_argument(
        "--relay", action="store_true",
        help="Relay one RPC line from stdin to the supervisor socket"
             " (used by hostctl for the docker profile).",
    )
    args = parser.parse_args(argv)
    if not args.home:
        parser.error("--home is required (or set CONCH_FLEET_WORKER_HOME)")
    if args.relay:
        return relay_once(args.home)
    supervisor = WorkerSupervisor(args.home)

    def _handle(signum, frame):
        supervisor.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return supervisor.serve_forever()


__all__ = [
    "WorkerSupervisor", "main", "relay_once",
    "OFFERED", "RUNNING", "WAITING_CHILD", "COMPLETED", "FAILED",
    "CANCELLED",
]


if __name__ == "__main__":
    raise SystemExit(main())
