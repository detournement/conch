"""Browser capture source (capture plan, browser satellite).

Events the satellite extension recorded — journaled in the kernel inbox
as ``source="browser"`` by the native messaging host — become a capture
context for the normal ``/compile`` review pipeline, exactly like the
mission/session/email/history sources. Everything here is read-only
over rows the host already validated and secretguard-scrubbed; the
assembled block is guarded whole again as the second net (browser
capture is curated input: a credential here is an anomaly, so the
reject-whole rule applies, not the history drop-and-count rule).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..errors import CapitolError
from .capture import CAPTURE_BLOCK_CAP, guard_capture_text

#: Bounds on one capture read (newest events win).
MAX_BROWSER_EVENTS = 400
DEFAULT_WINDOW_DAYS = 30


def _host(origin: str) -> str:
    return str(origin or "").split("://", 1)[-1].split("/", 1)[0]


def browser_events(store, origin: str = "", *,
                   window_days: int = DEFAULT_WINDOW_DAYS,
                   limit: int = MAX_BROWSER_EVENTS,
                   now: float = 0.0) -> List[Dict[str, Any]]:
    """Journaled browser event payloads, chronological, optionally
    filtered to one origin (a full origin or a bare host both match)."""
    horizon = (now or time.time()) - max(1, int(window_days)) * 86400
    rows = store.list_inbox(
        source="browser", limit=max(1, int(limit)), since=horizon,
    )
    events = []
    wanted = _host(origin).lower()
    for row in rows:
        payload = row.get("payload") or {}
        if not isinstance(payload, dict) or not payload.get("kind"):
            continue
        if wanted and _host(payload.get("origin", "")).lower() != wanted:
            continue
        events.append(payload)
    return events


def render_browser_event(payload: Dict[str, Any]) -> str:
    """One human trace line for a browser event payload."""
    kind = str(payload.get("kind") or "")
    host = _host(payload.get("origin", ""))
    detail = payload.get("detail") or {}
    stamp = ""
    try:
        stamp = time.strftime(
            "%m-%d %H:%M", time.localtime(float(payload.get("ts") or 0))
        )
    except (ValueError, OverflowError, OSError):
        pass
    if kind == "nav":
        body = f"open {host}{detail.get('path') or '/'}"
    elif kind == "click":
        label = str(detail.get("label") or "").strip()
        role = str(detail.get("role") or "element")
        body = f"click {role}" + (f" \u201c{label}\u201d" if label else "")
        body += f" on {host}"
    elif kind == "submit":
        fields = detail.get("fields") or []
        form = str(detail.get("form") or "form")
        body = (f"submit {form} on {host}"
                f" (fields: {', '.join(fields) if fields else 'none'})")
    elif kind == "copy":
        body = f"copy from {host} (content never captured)"
    else:
        body = f"{kind} on {host}"
    return f"{stamp} {body}".strip()


def capture_from_browser(store, origin: str = "", *,
                         window_days: int = DEFAULT_WINDOW_DAYS,
                         ) -> Dict[str, Any]:
    """Capture context from journaled browser events.

    With an origin the goal can default to automating that origin's
    procedure; without one the mix is heterogeneous, so — like shell
    history — the goal must be explicit (compile_from_capture enforces
    it via the empty ``default_goal``).
    """
    events = browser_events(
        store, origin, window_days=window_days,
    )
    if not events:
        where = f" for {origin}" if origin else ""
        raise CapitolError(
            f"browser capture: no journaled browser events{where} in the"
            f" last {window_days} day(s) — is the extension on and the"
            " origin allowlisted? (/install capture browser)"
        )
    origins = sorted({_host(event.get("origin", "")) for event in events})
    label_origin = _host(origin) or (
        origins[0] if len(origins) == 1 else f"{len(origins)} origins"
    )
    lines = [
        (f"Browser capture — {len(events)} event(s) across "
         f"{len(origins)} origin(s) ({', '.join(origins[:6])}), last "
         f"{window_days} day(s). Clicks carry semantic targets, submits"
         " carry field names only, copy events never carry content:"),
    ] + [f"  {render_browser_event(event)}" for event in events]
    block = "\n".join(lines)
    if len(block) > CAPTURE_BLOCK_CAP:
        block = block[: CAPTURE_BLOCK_CAP - 15] + "\n... [clipped]"
    block = guard_capture_text(block)
    return {
        "kind": "browser",
        "source_id": label_origin,
        "label": f"browser events ({label_origin}, "
                 f"{len(events)} event(s))",
        "default_goal": (
            f"Automate the recurring browser procedure on "
            f"{_host(origin)}" if origin else ""
        ),
        "block": block,
        "provenance": {
            "kind": "browser",
            "source": _host(origin) or "all origins",
            "events": len(events),
            "origins": origins,
        },
    }
