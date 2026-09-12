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
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_STATE_LOCK = threading.RLock()

STATE_VERSION = 1


def state_dir() -> Path:
    root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return root / "conch"


class PackState:
    """Durable {session → runs/revisions/linkage} state, atomic writes."""

    def __init__(self, path: Optional[Path] = None, *,
                 filename: str = "pack_state.json"):
        self._path = Path(path) if path else state_dir() / filename

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": STATE_VERSION, "sessions": {}}
        if not isinstance(data, dict) or "sessions" not in data:
            return {"version": STATE_VERSION, "sessions": {}}
        return data

    def _save(self, data: Dict[str, Any]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self._path)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

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
