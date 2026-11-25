"""Durable pack state (engine feature E15): the ``PilotState`` shape,
generalized.

One tiny versioned JSON file per pack under the conch state dir —
``{session → runs/revisions/contract linkage}`` plus the
``{channel}:{thread_id} → session`` thread bindings that double as the
intake dedupe. Atomic writes, 0600, corrupt files degrade to empty.
Kernel-native pack state is refactor stage R4 (design gap G2); when
``edge_daemon=true`` runs are additionally supervised through
``capitol_run`` bindings, so nothing here is the only copy of a
supervised run.

Every mutation is a read-merge-write under the lock: the file is
re-read, the change applied, and the result written through a unique
temp file and an atomic rename, so concurrent writers never clobber
each other's sessions. Two things keep that cheap on a long-lived
daemon:

- **Deferred run cursors.** ``advance_cursor`` keeps a run's
  ``last_sequence`` in memory and writes it only every
  ``cursor_flush_events`` events / ``cursor_flush_seconds`` seconds, on
  ``flush_cursors()`` (park, finish, error), or along with any other
  write. The persisted run cursor is informational: a parked run resumes
  from the exact ``hitl.last_sequence`` written at park time, and the
  watch loops skip any replayed ``sequence <= last_seen``, so a cursor a
  few events stale is safe.
- **Retention.** Each write drops terminal sessions (phase ``done`` /
  ``failed``, or phaseless shell sessions) whose last update is older
  than ``retention_seconds`` (default 30 days) together with their thread
  bindings, drops finished runs older than that from live sessions, and
  caps every per-session history list at ``history_cap`` entries.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_STATE_LOCK = threading.RLock()

STATE_VERSION = 1

#: Default retention for terminal sessions and finished runs.
DEFAULT_RETENTION_SECONDS = 30 * 86400
#: Default cap on each per-session list (runs, revisions, history).
DEFAULT_HISTORY_CAP = 50
#: Deferred cursor flush cadence (whichever comes first).
DEFAULT_CURSOR_FLUSH_EVENTS = 100
DEFAULT_CURSOR_FLUSH_SECONDS = 10.0

#: Session phases after which nothing more happens to a session.
TERMINAL_PHASES = frozenset({"done", "failed"})


def state_dir() -> Path:
    root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return root / "conch"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PackState:
    """Durable {session → runs/revisions/linkage} state, atomic writes."""

    def __init__(self, path: Optional[Path] = None, *,
                 filename: str = "pack_state.json",
                 retention_seconds: Optional[float] = None,
                 history_cap: Optional[int] = None,
                 cursor_flush_events: int = DEFAULT_CURSOR_FLUSH_EVENTS,
                 cursor_flush_seconds: float = DEFAULT_CURSOR_FLUSH_SECONDS,
                 clock=time.time):
        self._path = Path(path) if path else state_dir() / filename
        #: ``<= 0`` disables age-based pruning.
        self.retention_seconds = float(
            DEFAULT_RETENTION_SECONDS if retention_seconds is None
            else retention_seconds
        )
        #: ``<= 0`` disables the per-session list cap.
        self.history_cap = int(
            DEFAULT_HISTORY_CAP if history_cap is None else history_cap
        )
        self.cursor_flush_events = max(1, int(cursor_flush_events))
        self.cursor_flush_seconds = float(cursor_flush_seconds)
        self._clock = clock
        #: Per-session append-only lists subject to ``history_cap``:
        #: ``runs``, ``revisions`` and every collection appended through
        #: :meth:`append`. Other list fields (``photos``, ``media`` …)
        #: are session data, not history, and are never trimmed.
        self.history_lists = {"runs", "revisions"}
        #: (session_id, run_id) -> highest unflushed sequence.
        self._pending: Dict[Tuple[str, str], int] = {}
        self._pending_events = 0
        self._last_flush = float(clock())

    @property
    def path(self) -> Path:
        return self._path

    def configure_retention(self, config: Optional[dict]) -> "PackState":
        """Apply ``capitol_pack_retention_days`` /
        ``capitol_pack_history_cap`` from a conch config, when set."""
        config = config or {}
        days = config.get("capitol_pack_retention_days")
        if days not in (None, ""):
            self.retention_seconds = _float(days, 30.0) * 86400
        cap = config.get("capitol_pack_history_cap")
        if cap not in (None, ""):
            try:
                self.history_cap = int(cap)
            except (TypeError, ValueError):
                pass
        return self

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": STATE_VERSION, "sessions": {}}
        if not isinstance(data, dict) or "sessions" not in data:
            return {"version": STATE_VERSION, "sessions": {}}
        return data

    def _save(self, data: Dict[str, Any]):
        self._merge_pending(data)
        self._prune(data)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temp file per write (mkstemp creates it 0600), so two
        # processes saving at once never interleave into one temp file.
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, sort_keys=True,
                          separators=(",", ":"))
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    # -- deferred run cursors ---------------------------------------------

    def _merge_pending(self, data: Dict[str, Any]):
        if not self._pending:
            return
        sessions = data.get("sessions") or {}
        for (session_id, run_id), sequence in self._pending.items():
            session = sessions.get(session_id) or {}
            for run in session.get("runs") or []:
                if run.get("run_id") != run_id:
                    continue
                # Never move a cursor backwards (another writer, or a
                # status update, may already hold a newer one).
                current = run.get("last_sequence")
                if not isinstance(current, (int, float)) or \
                        int(current) < sequence:
                    run["last_sequence"] = sequence
        self._pending.clear()
        self._pending_events = 0
        self._last_flush = float(self._clock())

    def advance_cursor(self, session_id: str, run_id: str, sequence: int,
                       *, flush: bool = False):
        """Record a run's ``last_sequence`` in memory; write it when the
        flush cadence is due or *flush* is true."""
        with _STATE_LOCK:
            key = (str(session_id), str(run_id))
            sequence = int(sequence)
            if sequence > self._pending.get(key, -1):
                self._pending[key] = sequence
            self._pending_events += 1
            due = (
                flush
                or self._pending_events >= self.cursor_flush_events
                or float(self._clock()) - self._last_flush
                >= self.cursor_flush_seconds
            )
            if due:
                self.flush_cursors()

    def flush_cursors(self):
        """Write any unflushed run cursors now (no-op when none)."""
        with _STATE_LOCK:
            if not self._pending:
                return
            self._save(self.load())

    # -- retention ----------------------------------------------------------

    def _prune(self, data: Dict[str, Any]):
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            return
        now = float(self._clock())
        cutoff = (now - self.retention_seconds
                  if self.retention_seconds > 0 else None)
        dropped = set()
        for session_id, session in list(sessions.items()):
            if not isinstance(session, dict):
                continue
            phase = session.get("phase")
            touched = _float(
                session.get("updated_at") or session.get("created_at"), now
            )
            if (cutoff is not None and touched < cutoff
                    and (phase is None or phase in TERMINAL_PHASES)
                    and not session.get("approval_id")):
                del sessions[session_id]
                dropped.add(session_id)
                continue
            self._prune_session(session, cutoff)
        threads = data.get("threads")
        if dropped and isinstance(threads, dict):
            # The binding goes with its session (a later message in that
            # thread recovers exactly like any dangling binding does).
            for key, session_id in list(threads.items()):
                if session_id in dropped:
                    del threads[key]

    def _prune_session(self, session: Dict[str, Any],
                       cutoff: Optional[float]):
        hitl_run = str((session.get("hitl") or {}).get("run_id") or "")
        runs = session.get("runs")
        if isinstance(runs, list) and cutoff is not None:
            session["runs"] = [
                run for run in runs
                if not (
                    isinstance(run, dict)
                    and run.get("status")
                    and run.get("run_id") != hitl_run
                    and _float(run.get("started_at"), cutoff) < cutoff
                )
            ]
        if self.history_cap > 0:
            for key, value in list(session.items()):
                if isinstance(value, list) and len(value) > self.history_cap \
                        and key in self.history_lists:
                    session[key] = value[-self.history_cap:]

    # -- mutations ----------------------------------------------------------

    def update_session(self, session_id: str, **fields) -> Dict[str, Any]:
        with _STATE_LOCK:
            data = self.load()
            session = data["sessions"].setdefault(
                session_id, {"created_at": time.time()}
            )
            for key, value in fields.items():
                session[key] = value
            session["updated_at"] = time.time()
            self._save(data)
            return session

    def append(self, session_id: str, key: str, entry: Dict[str, Any]):
        with _STATE_LOCK:
            data = self.load()
            session = data["sessions"].setdefault(
                session_id, {"created_at": time.time()}
            )
            session.setdefault(key, []).append(entry)
            self.history_lists.add(key)
            session["updated_at"] = time.time()
            self._save(data)

    def record_run(
        self, session_id: str, kind: str, run_id: str, idempotency_key: str
    ):
        self.append(session_id, "runs", {
            "kind": kind,
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "started_at": time.time(),
            "last_sequence": 0,
        })

    def update_run(self, session_id: str, run_id: str, **fields):
        with _STATE_LOCK:
            data = self.load()
            session = data["sessions"].get(session_id) or {}
            for run in session.get("runs", []):
                if run.get("run_id") == run_id:
                    run.update(fields)
            if "last_sequence" in fields:
                # An explicit cursor write supersedes the deferred one.
                self._pending.pop((session_id, run_id), None)
            self._save(data)

    def sessions(self) -> Dict[str, Any]:
        return dict(self.load().get("sessions", {}))

    def session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return (self.load().get("sessions") or {}).get(session_id)

    # -- channel-thread bindings (thread ↔ pack session) ---------------------

    def bind_thread(self, key: str, session_id: str):
        """Bind ``{channel}:{thread_id}`` to a session, durably — the
        binding doubles as the intake dedupe (one session per thread,
        recorded before any run starts)."""
        with _STATE_LOCK:
            data = self.load()
            data.setdefault("threads", {})[key] = session_id
            self._save(data)

    def thread_session(self, key: str) -> Optional[str]:
        return (self.load().get("threads") or {}).get(key)

