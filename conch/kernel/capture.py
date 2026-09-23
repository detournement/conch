"""Bounded capture reader over a mission's kernel journal.

The Capture→Card pipeline (capture plan, feature 1) turns work that
*already happened* into a draft Architecture Card for ``/compile``
review. This module is the edge-side half: it reads one mission's
event chain out of the kernel and reduces it to a small, deterministic,
structured trace — no model involvement, no network, kernel-only
imports (the import-direction gates rely on that).

Boundaries and privacy:

- Only material, procedure-bearing event kinds are read (plans, tasks,
  external actions, artifacts, checkpoints, notes, reviews). Budget,
  timer, inbox/outbox, and session bookkeeping never enter a capture.
- Personal-item events are excluded defensively (they chain under item
  ids, not mission ids, so they should never appear — the exclusion is
  belt-and-braces, mirroring ``consolidate.py``'s privacy fence).
- Everything is size-capped: an arbitrarily long journal produces a
  head+tail trace with an explicit elision marker, never an unbounded
  block.
- The reader returns *data*; credential scanning happens where the
  trace crosses into synthesis (``conch.capitol.compiler.capture``),
  so the guard lives on the one path that leaves the kernel.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .model import ITEM_EVENT_KINDS, KernelError

#: Event kinds that carry procedure (what was decided, done, produced).
CAPTURE_EVENT_KINDS = (
    "mission_created",
    "mission_transitioned",
    "mission_note",
    "plan_recorded",
    "plan_revised",
    "task_created",
    "task_transitioned",
    "action_recorded",
    "action_resolved",
    "artifact_recorded",
    "checkpoint_recorded",
    "review_recorded",
)

#: Hard bounds — a capture is a digest, not an export.
MAX_CAPTURE_EVENTS = 240
_EVENT_TEXT_CAP = 280
_ELISION = {"kind": "…", "seq": 0, "text": ""}


def resolve_mission_ref(store, ref: str) -> Dict[str, Any]:
    """Resolve a mission by exact id or unique prefix (fail closed on
    ambiguity, naming the candidates)."""
    ref = str(ref or "").strip()
    if not ref:
        raise KernelError("capture needs a mission id")
    mission = store.get_mission(ref)
    if mission is not None:
        return mission
    matches = [
        row for row in store.list_missions()
        if str(row.get("mission_id", "")).startswith(ref)
    ]
    if len(matches) == 1:
        return store.get_mission(matches[0]["mission_id"])
    if not matches:
        raise KernelError(f"no mission matching {ref!r}")
    raise KernelError(
        f"mission ref {ref!r} is ambiguous: "
        + ", ".join(sorted(row["mission_id"] for row in matches)[:6])
    )


def _clip(value: Any, cap: int = _EVENT_TEXT_CAP) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _event_line(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One journal event → one small trace entry (or None to drop)."""
    kind = event.get("kind", "")
    if kind in ITEM_EVENT_KINDS:  # privacy fence, defensive
        return None
    data = event.get("data") or {}
    text = ""
    if kind == "mission_created":
        spec = data.get("spec") or {}
        text = _clip(spec.get("goal") or "")
    elif kind == "mission_transitioned":
        text = _clip(f"{data.get('status', '')} {data.get('reason', '')}")
    elif kind == "mission_note":
        text = _clip(data.get("text") or "")
    elif kind in ("plan_recorded", "plan_revised"):
        content = data.get("content")
        if isinstance(content, dict):
            steps = content.get("steps") or []
            text = _clip(
                f"plan v{data.get('plan_version', '?')}: "
                + " | ".join(str(step) for step in steps[:8])
            )
        else:
            text = _clip(content)
    elif kind in ("task_created", "task_transitioned"):
        text = _clip(
            f"{data.get('title', data.get('task_id', ''))}"
            f" {data.get('status', '')}"
        )
    elif kind in ("action_recorded", "action_resolved"):
        detail = data.get("detail") or {}
        text = _clip(
            f"[{data.get('action_class', '')}] "
            + (detail.get("op") or detail.get("kind")
               or detail.get("summary") or str(detail)[:120])
            + (f" → {data.get('outcome', '')}" if "outcome" in data else "")
        )
    elif kind == "artifact_recorded":
        text = _clip(f"{data.get('name', '')} ({data.get('size', 0)}b)")
    elif kind == "checkpoint_recorded":
        text = _clip(data.get("summary") or "")
    elif kind == "review_recorded":
        text = _clip(
            f"action={data.get('action', '')} "
            + str(data.get('scores') or '')[:160]
        )
    if not text:
        return None
    return {"kind": kind, "seq": int(event.get("seq", 0)), "text": text}


def read_mission_capture(store, ref: str, *,
                         max_events: int = MAX_CAPTURE_EVENTS
                         ) -> Dict[str, Any]:
    """Read one mission's journal into a bounded capture structure.

    Returns ``{source, mission_id, goal, status, event_range,
    total_events, elided, trace}`` where ``trace`` is at most
    ``max_events`` small entries (head + tail around an elision marker
    when the journal is longer).
    """
    mission = resolve_mission_ref(store, ref)
    mission_id = mission["mission_id"]
    events: List[Dict[str, Any]] = []
    seq = 0
    while True:
        page = store.events_since(
            mission_id, seq, kinds=CAPTURE_EVENT_KINDS, limit=500,
        )
        if not page:
            break
        events.extend(page)
        seq = page[-1]["seq"]
        if len(page) < 500:
            break
    trace = [line for line in map(_event_line, events) if line]
    total = len(trace)
    elided = 0
    if total > max_events:
        head = max_events * 2 // 3
        tail = max_events - head
        elided = total - head - tail
        trace = trace[:head] + [dict(_ELISION)] + trace[-tail:]
    first_seq = events[0]["seq"] if events else 0
    last_seq = events[-1]["seq"] if events else 0
    return {
        "source": "mission",
        "mission_id": mission_id,
        "goal": str(mission.get("goal") or ""),
        "status": str(mission.get("status") or ""),
        "event_range": [first_seq, last_seq],
        "total_events": total,
        "elided": elided,
        "trace": trace,
    }


def render_mission_capture(capture: Dict[str, Any], *,
                           cap_chars: int = 9000) -> str:
    """The bounded text block synthesis injects into the compile
    session (deterministic; discovery-digest style)."""
    lines = [
        f"Mission {capture['mission_id']} ({capture['status']})",
        f"Goal: {capture['goal']}",
        f"Journal events {capture['event_range'][0]}–"
        f"{capture['event_range'][1]}"
        f" ({capture['total_events']} material"
        + (f", {capture['elided']} elided" if capture["elided"] else "")
        + "):",
    ]
    for entry in capture["trace"]:
        if entry["kind"] == "…":
            lines.append(f"  … [{capture['elided']} events elided] …")
        else:
            lines.append(f"  {entry['seq']:>6} {entry['kind']}: "
                         f"{entry['text']}")
    text = "\n".join(lines)
    if len(text) > cap_chars:
        text = text[: cap_chars - 15] + "\n... [clipped]"
    return text
