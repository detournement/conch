"""Personal items: deterministic urgency, due windows, and queries.

The ``items`` aggregate itself lives in :mod:`conch.kernel.store` (same
event→projection discipline as missions). This module holds everything
that is *computed, not stored*: due-date parsing, day boundaries, and the
urgency ordering the plan fixes as ``overdue > due-today > explicit
priority > age`` — pure functions of ``(item, now)`` so "what's most
urgent?" is deterministic and explainable, never model-ranked.

All day math is local time: personal todos follow the user's clock.
"""

from __future__ import annotations

import re
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from .model import ItemStatus, KernelError

#: Urgency tiers (lower sorts first). Items due later than today rank by
#: priority/age exactly like undated ones — a due date next month must not
#: outrank an old undated item, per the plan's fixed ordering.
TIER_OVERDUE = 0
TIER_DUE_TODAY = 1
TIER_PRIORITY = 2
TIER_AGE = 3

_RELATIVE_DUE_RE = re.compile(r"^\+(\d+)([mhdw])$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})$"
)
_CLEAR_DUE_WORDS = ("none", "clear", "-")

_RELATIVE_UNITS = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


def day_bounds(now: float) -> Tuple[float, float]:
    """(local midnight, next local midnight) around *now*. mktime
    normalizes the +1 day, so month/year rollover and DST are its
    problem, not ours."""
    lt = _time.localtime(now)
    start = _time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)
    )
    end = _time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)
    )
    return start, end


def end_of_day(now: float, days_ahead: int = 0) -> float:
    """23:59:59 local, *days_ahead* days from *now*'s date — the due
    instant for date-only dues ("due Sept 20" means by end of Sept 20)."""
    lt = _time.localtime(now)
    return _time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday + days_ahead, 23, 59, 59,
         0, 0, -1)
    )


def parse_due(text: Any, now: float) -> Optional[float]:
    """Deterministic due parsing → epoch seconds (or None to clear).

    Accepted: ``none``/``clear``/``-`` (clear), ``today``, ``tomorrow``,
    ``+N[mhdw]`` (relative to *now*), ``YYYY-MM-DD`` (end of that day),
    ``YYYY-MM-DD HH:MM`` / ``YYYY-MM-DDTHH:MM`` (exact, local time).
    Anything else fails closed.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    word = str(text).strip().lower()
    if not word or word in _CLEAR_DUE_WORDS:
        return None
    if word in ("today", "tod"):
        return end_of_day(now)
    if word in ("tomorrow", "tom"):
        return end_of_day(now, days_ahead=1)
    match = _RELATIVE_DUE_RE.match(word)
    if match:
        return float(now) + int(match.group(1)) * _RELATIVE_UNITS[
            match.group(2)
        ]
    match = _DATE_RE.match(word)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _time.mktime((year, month, day, 23, 59, 59, 0, 0, -1))
    match = _DATETIME_RE.match(str(text).strip())
    if match:
        year, month, day, hour, minute = (int(g) for g in match.groups())
        return _time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
    raise KernelError(
        f"cannot parse due {text!r} — use today, tomorrow, +N[mhdw],"
        " YYYY-MM-DD, or 'YYYY-MM-DD HH:MM'"
    )


def is_overdue(item: Dict[str, Any], now: float) -> bool:
    due = item.get("due_at")
    return due is not None and float(due) < float(now)


def is_due_today(item: Dict[str, Any], now: float) -> bool:
    """Due between *now* and the next local midnight (not yet overdue)."""
    due = item.get("due_at")
    if due is None:
        return False
    _, next_midnight = day_bounds(now)
    return float(now) <= float(due) < next_midnight


def urgency_key(item: Dict[str, Any], now: float) -> Tuple:
    """Total order implementing overdue > due-today > priority > age.
    Ties break on due time, then age (older first), then item id — every
    element deterministic, so two calls always agree."""
    created = float(item.get("created_at") or 0.0)
    item_id = str(item.get("item_id") or "")
    due = item.get("due_at")
    if is_overdue(item, now):
        return (TIER_OVERDUE, float(due), created, item_id)
    if is_due_today(item, now):
        return (TIER_DUE_TODAY, float(due), created, item_id)
    priority = item.get("priority")
    if priority is not None:
        return (TIER_PRIORITY, float(int(priority)), created, item_id)
    return (TIER_AGE, created, 0.0, item_id)


def _format_span(seconds: float) -> str:
    seconds = max(float(seconds), 0.0)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def urgency_reason(item: Dict[str, Any], now: float) -> str:
    """The human-readable why behind :func:`urgency_key` — same tier
    logic, so the explanation can never disagree with the ordering."""
    due = item.get("due_at")
    if is_overdue(item, now):
        return f"overdue {_format_span(now - float(due))}"
    if is_due_today(item, now):
        lt = _time.localtime(float(due))
        if (lt.tm_hour, lt.tm_min, lt.tm_sec) == (23, 59, 59):
            return "due today"
        return f"due today {lt.tm_hour:02d}:{lt.tm_min:02d}"
    priority = item.get("priority")
    if priority is not None:
        return f"priority p{int(priority)}"
    return f"open {_format_span(now - float(item.get('created_at') or now))}"


def sort_by_urgency(items: List[Dict[str, Any]],
                    now: float) -> List[Dict[str, Any]]:
    return sorted(items, key=lambda item: urgency_key(item, now))


# ---------------------------------------------------------------------------
# Deterministic queries over the store's open items
# ---------------------------------------------------------------------------

def due_today(store, now: float, space: str = "") -> List[Dict[str, Any]]:
    """Open items due between now and the next local midnight, soonest
    first."""
    rows = [
        item for item in store.list_items(space=space,
                                          status=ItemStatus.OPEN)
        if is_due_today(item, now)
    ]
    rows.sort(key=lambda item: (float(item["due_at"]), item["item_id"]))
    return rows


def overdue(store, now: float, space: str = "") -> List[Dict[str, Any]]:
    """Open items whose due time has passed, most overdue first."""
    rows = [
        item for item in store.list_items(space=space,
                                          status=ItemStatus.OPEN)
        if is_overdue(item, now)
    ]
    rows.sort(key=lambda item: (float(item["due_at"]), item["item_id"]))
    return rows


def most_urgent(store, now: float, limit: int = 5,
                space: str = "") -> List[Dict[str, Any]]:
    """Top-N open items by the fixed urgency order."""
    rows = sort_by_urgency(
        store.list_items(space=space, status=ItemStatus.OPEN), now
    )
    return rows[: max(int(limit), 0)]


# ---------------------------------------------------------------------------
# Shared rendering (tool results and command output both use these, so the
# two surfaces can never describe the same item differently)
# ---------------------------------------------------------------------------

def format_due(due_at: Optional[float]) -> str:
    if due_at is None:
        return ""
    lt = _time.localtime(float(due_at))
    if (lt.tm_hour, lt.tm_min, lt.tm_sec) == (23, 59, 59):
        return _time.strftime("%Y-%m-%d", lt)
    return _time.strftime("%Y-%m-%d %H:%M", lt)


def item_line(item: Dict[str, Any], now: float,
              with_space: bool = False) -> str:
    """One deterministic plain-text line for an item."""
    mark = {"open": " ", "done": "x", "archived": "~"}.get(
        item["status"], "?"
    )
    parts = [f"[{mark}] #{item['item_seq']}"]
    if with_space:
        parts.append(f"[{item['space']}]")
    parts.append(item["title"])
    due = format_due(item.get("due_at"))
    if due:
        parts.append(f"(due {due})")
    if item.get("priority") is not None:
        parts.append(f"(p{int(item['priority'])})")
    if item.get("tags"):
        parts.append(" ".join(f"#{tag}" for tag in item["tags"]))
    if item["status"] == ItemStatus.OPEN:
        parts.append(f"<{urgency_reason(item, now)}>")
    if item.get("mission_id"):
        parts.append(f"[mission {item['mission_id'][:20]}]")
    parts.append(f"({item['item_id']})")
    return " ".join(parts)


def item_detail(store, item: Dict[str, Any], now: float,
                history_limit: int = 20) -> str:
    """Full plain-text record: fields, body, and the event history."""
    lines = [item_line(item, now, with_space=True)]
    if item.get("body"):
        lines.append("")
        lines.extend("  " + line for line in item["body"].splitlines())
        lines.append("")
    lines.append(
        f"  created {format_stamp(item['created_at'])}"
        f"  updated {format_stamp(item['updated_at'])}"
        f"  source {item.get('source') or 'chat'}"
    )
    events = store.item_events(item["item_id"], limit=history_limit)
    if events:
        lines.append("  history:")
        for event in events:
            brief = _event_brief(event)
            lines.append(
                f"    {format_stamp(event['created_at'])} {event['kind']}"
                + (f" {brief}" if brief else "")
            )
    return "\n".join(lines)


def format_stamp(timestamp: float) -> str:
    return _time.strftime(
        "%Y-%m-%d %H:%M", _time.localtime(float(timestamp))
    )


def _event_brief(event: Dict[str, Any]) -> str:
    data = event.get("data") or {}
    if event["kind"] == "item_updated":
        fields = data.get("fields") or {}
        return "fields: " + ", ".join(sorted(fields))
    if event["kind"] == "item_escalated":
        return f"mission {data.get('mission_id', '')}"
    if event["kind"] == "item_mission_synced":
        return (
            f"mission {data.get('mission_id', '')} {data.get('outcome', '')}"
            f" — proposes {data.get('proposal', '')}"
        )
    actor = data.get("actor") or ""
    return f"by {actor}" if actor else ""
