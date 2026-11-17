"""Read-model shaping shared by the daemon's control ops and the direct
(shell-attached, no-daemon) client, so both paths return identical shapes.

Nothing here mutates; everything is secret-free by construction (kernel
rows never contain secret bytes)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .model import MissionKind, MissionState
from .store import MissionStore


def legacy_task_id(store: MissionStore, mission_id: str) -> int:
    """Stable small integer alias for a mission: the seq of its first
    event. Monotonic in creation order, never reused — the `/tasks` and
    `/cancel <id>` UX keeps its integer ids on top of the kernel."""
    row = store._read_conn().execute(
        "SELECT MIN(seq) FROM mission_events WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    return int(row[0] or 0)


def mission_by_task_id(store: MissionStore, task_id: int) -> Optional[str]:
    row = store._read_conn().execute(
        "SELECT mission_id FROM mission_events WHERE seq=?",
        (int(task_id),),
    ).fetchone()
    return row[0] if row else None


def mission_summary(store: MissionStore,
                    mission: Dict[str, Any]) -> Dict[str, Any]:
    spec = mission["spec"]
    timer = store.find_timer(mission["mission_id"], "wake")
    return {
        "mission_id": mission["mission_id"],
        "task_seq": legacy_task_id(store, mission["mission_id"]),
        "kind": mission["kind"],
        "status": mission["status"],
        "goal": spec.get("goal", ""),
        "runs": mission["runs"],
        "last_session_at": mission["last_session_at"],
        "stop_requested": bool(mission["stop_requested"]),
        "next_wake_at": (
            timer["due_at"] if timer and timer["status"] == "active"
            else None
        ),
        "last_error": mission["last_error"],
        "created_at": mission["created_at"],
        "updated_at": mission["updated_at"],
    }


def mission_detail(store: MissionStore,
                   mission: Dict[str, Any]) -> Dict[str, Any]:
    mission_id = mission["mission_id"]
    detail = mission_summary(store, mission)
    detail["spec"] = mission["spec"]
    detail["events"] = store.event_tail(mission_id, limit=15)
    checkpoint = store.latest_checkpoint(mission_id)
    detail["checkpoint"] = (
        {"summary": checkpoint["summary"],
         "created_at": checkpoint["created_at"]}
        if checkpoint else None
    )
    detail["budgets"] = store.budget_status(mission["root_scope_id"])
    detail["open_tasks"] = [
        {"task_id": task["task_id"], "title": task["title"],
         "state": task["state"]}
        for task in store.open_tasks(mission_id)
    ]
    plan = store.latest_plan(mission_id)
    detail["plan"] = plan["content"] if plan else None
    review = store.latest_review(mission_id)
    if review:
        content = review["content"]
        stall = content.get("stall") or {}
        detail["review"] = {
            "review_id": review["review_id"],
            "action": review["action"],
            "created_at": review["created_at"],
            "criteria": content.get("criteria", []),
            "rationale": content.get("rationale", ""),
            "stalled": bool(stall.get("stalled")),
            "stall_detail": (
                stall.get("repeat_detail")
                or (
                    "no material change across"
                    f" {stall.get('window_sessions', '?')} sessions"
                    if stall.get("no_material_change") else ""
                )
            ),
        }
    else:
        detail["review"] = None
    return detail


def schedule_entries(store: MissionStore) -> List[Dict[str, Any]]:
    entries = []
    for mission in store.list_missions():
        if mission["kind"] != MissionKind.SCHEDULED_PROMPT:
            continue
        summary = mission_summary(store, mission)
        summary["prompt"] = mission["spec"].get("prompt", "")
        summary["interval"] = mission["spec"].get("cadence_seconds", 0)
        summary["run_once"] = bool(mission["spec"].get("run_once"))
        summary["active"] = (
            mission["status"] not in MissionState.TERMINAL
            and mission["status"] != MissionState.PAUSED
        )
        entries.append(summary)
    return entries


def approval_entries(store: MissionStore) -> List[Dict[str, Any]]:
    return [
        {
            "approval_id": row["approval_id"],
            "mission_id": row["mission_id"],
            "action_kind": row["action_kind"],
            "action_args": row["action_args"],
            "args_hash": row["args_hash"],
            "nonce": row["nonce"],
            "origin_channel": row["origin_channel"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        for row in store.pending_approvals()
    ]
