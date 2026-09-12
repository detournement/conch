"""Legacy ``tasks.json`` → kernel migration (idempotent).

On daemon start, active legacy scheduler tasks become scheduled-prompt
missions with wake timers preserving their next run time and interval; the
original file is preserved as ``tasks.json.bak``. Idempotency: every
migrated task leaves a ``legacy_task`` resource binding keyed by a content
digest, so re-running the migration (or migrating a recreated tasks.json)
never duplicates a mission.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict

from .model import MissionKind
from .store import default_state_dir


def _legacy_digest(prompt: str, interval: int, run_once: bool) -> str:
    body = json.dumps(
        [prompt, int(interval), bool(run_once)], sort_keys=True
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _migrated_digests(store) -> set:
    rows = store._read_conn().execute(
        "SELECT resource FROM resource_bindings WHERE kind='legacy_task'"
    ).fetchall()
    digests = set()
    for (resource,) in rows:
        try:
            digests.add(json.loads(resource).get("digest", ""))
        except ValueError:
            continue
    return digests


def migrate_tasks_json(engine, state_dir: Path = None,
                       log: Callable[[str], None] = None) -> Dict[str, Any]:
    """Migrate legacy scheduler tasks into kernel missions/timers.

    Only active tasks migrate (stopped ones stay archived in the .bak).
    Original run statistics are journaled on the new mission. Safe to call
    on every daemon start.
    """
    log = log or (lambda line: None)
    state_dir = Path(state_dir) if state_dir else default_state_dir()
    tasks_path = state_dir / "tasks.json"
    report = {"found": 0, "migrated": 0, "skipped": 0, "backup": ""}
    if not tasks_path.exists():
        return report
    try:
        entries = json.loads(tasks_path.read_text())
    except (ValueError, OSError) as exc:
        log(f"migration: cannot read {tasks_path}: {exc}")
        return report
    if not isinstance(entries, list):
        log(f"migration: {tasks_path} is not a task list; leaving it alone")
        return report
    store = engine.store
    seen = _migrated_digests(store)
    now = float(store.clock())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        report["found"] += 1
        prompt = str(entry.get("prompt") or "").strip()
        try:
            interval = int(entry.get("interval") or 0)
        except (TypeError, ValueError):
            interval = 0
        run_once = bool(entry.get("run_once"))
        active = bool(entry.get("active", True))
        if not prompt or interval <= 0 or not active:
            report["skipped"] += 1
            continue
        digest = _legacy_digest(prompt, interval, run_once)
        if digest in seen:
            report["skipped"] += 1
            continue
        goal = f"scheduled task: {prompt[:120]}"
        mission_id = engine.create_mission({
            "goal": goal,
            "kind": MissionKind.SCHEDULED_PROMPT,
            "prompt": prompt,
            "cadence_seconds": interval,
            "run_once": run_once,
            "budgets": {},
        }, activate=True)
        store.record_binding(mission_id, "legacy_task", {
            "digest": digest,
            "legacy_id": entry.get("id"),
            "run_count": entry.get("run_count", 0),
            "last_run": entry.get("last_run", ""),
        })
        # Preserve the original next-run time when it is still meaningful.
        next_run_at = entry.get("next_run_at")
        if isinstance(next_run_at, (int, float)) and next_run_at > now:
            timer = store.find_timer(mission_id, "wake")
            if timer is not None:
                store.reschedule_timer(
                    timer["timer_id"], float(next_run_at),
                    expected_generation=timer["generation"],
                )
        seen.add(digest)
        report["migrated"] += 1
        log(f"migration: task #{entry.get('id')} -> {mission_id}")
    backup = tasks_path.with_suffix(".json.bak")
    if backup.exists():
        backup = tasks_path.with_suffix(
            f".json.bak.{int(time.time())}"
        )
    tasks_path.rename(backup)
    report["backup"] = str(backup)
    log(
        f"migration: {report['migrated']} task(s) migrated,"
        f" {report['skipped']} skipped; original preserved at {backup}"
    )
    return report
