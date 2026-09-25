"""Status exporter — push sanitized eBay sales status to the status page.

An edge-daemon service (plugin seam) that periodically POSTs a compact,
allowlisted JSON snapshot of listing sessions, folder drops, and pending
approvals to the authenticated status page (a private Vercel app). The
exporter is strictly one-way and failure-tolerant: a dead page, a bad
token, or a network hiccup logs once per change and never blocks or
retries aggressively — the sales flow does not depend on it.

Sanitization is structural: the snapshot is built exclusively from an
allowlist of fields (never by copying whole records), and the serialized
payload is refused whole if the credential scanner finds anything
token-shaped. Approvals are display-only on the page — the write token
authorizes ingest, nothing on the page can approve or steer a session.

Config:
- ``status_page_url``      — base URL of the status app (unset = off)
- ``status_page_token_env``— env var holding the WRITE token
                             (default ``CONCH_STATUS_WRITE_TOKEN``)
- ``status_export_interval_seconds`` — min seconds between pushes (30)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..secretguard import credential_findings
from .packs.engine import clean_text

__all__ = ["StatusExporterService", "build_snapshot"]

DEFAULT_TOKEN_ENV = "CONCH_STATUS_WRITE_TOKEN"
_TIMEOUT_SECONDS = 10
_MAX_HISTORY = 8
_MAX_SESSIONS = 50


def _phase(session: Dict[str, Any]) -> str:
    return str(session.get("phase") or "unknown")


def _revision_summary(session: Dict[str, Any]) -> Dict[str, Any]:
    contract = session.get("revision") or session.get("contract") or {}
    if not isinstance(contract, dict):
        return {}
    return {
        "title": clean_text(contract.get("title"), 120),
        "price": clean_text(
            contract.get("price") or contract.get("price_usd"), 32
        ),
        "category": clean_text(
            contract.get("category") or contract.get("category_id"), 80
        ),
        "revision": contract.get("revision"),
    }


def build_snapshot(config: dict, *, now: Optional[float] = None
                   ) -> Dict[str, Any]:
    """Allowlisted snapshot of the sales state. Every field below is
    chosen by hand; nothing copies whole session records."""
    from .ebay import PilotState
    from .folder_intake import drops_summary

    now = now if now is not None else time.time()
    listings: List[Dict[str, Any]] = []
    try:
        sessions = PilotState().sessions()
    except Exception:
        sessions = {}
    for session_id, info in sorted(sessions.items())[:_MAX_SESSIONS]:
        listing = info.get("listing") or {}
        listings.append({
            "session_id": clean_text(session_id, 60),
            "phase": _phase(info),
            "surface": clean_text(
                info.get("channel") or info.get("surface") or "shell", 20
            ),
            "revision": _revision_summary(info),
            "listing_id": clean_text(listing.get("listing_id"), 60),
            "listing_url": clean_text(listing.get("listing_url"), 200),
            "updated_at": info.get("updated_at"),
        })
    drops: List[Dict[str, Any]] = []
    try:
        summaries = drops_summary()
    except Exception:
        summaries = []
    for record in summaries[-_MAX_SESSIONS:]:
        drops.append({
            "drop_id": clean_text(record.get("drop_id"), 40),
            "phase": clean_text(record.get("phase"), 30),
            "photos": len(record.get("photos") or []),
            "notes": bool(record.get("notes")),
            "approval_id": record.get("approval_id"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "history": [
                {
                    "at": item.get("at"),
                    "text": clean_text(item.get("text"), 300),
                }
                for item in (record.get("history") or [])[-_MAX_HISTORY:]
            ],
        })
    approvals: List[Dict[str, Any]] = []
    try:
        from ..remote import ApprovalStore

        for request_id, entry in sorted(ApprovalStore().pending().items()):
            approvals.append({
                "id": int(request_id),
                "kind": clean_text(entry.get("kind") or "command", 30),
                "channel": clean_text(entry.get("channel"), 20),
                "describe": clean_text(entry.get("command"), 200),
                "created_at": entry.get("created_at"),
            })
    except Exception:
        pass
    return {
        "schema": "conch.status.v1",
        "generated_at": now,
        "listings": listings,
        "drops": drops,
        "approvals": approvals,
    }


class StatusExporterService:
    """Daemon service: throttled, change-driven snapshot push."""

    def __init__(self, store, config: dict, log=print, clock=None):
        self.store = store
        self.config = config or {}
        self.log = log
        self.clock = clock or time.time
        self._last_push = 0.0
        self._last_digest = ""
        self._last_error = ""
        # Injectable for tests: (url, headers, body) -> status int
        self._post = self._http_post

    def enabled(self) -> bool:
        return bool(str(self.config.get("status_page_url") or "").strip()
                    and self._token())

    def _token(self) -> str:
        env = str(
            self.config.get("status_page_token_env") or DEFAULT_TOKEN_ENV
        ).strip()
        return os.environ.get(env, "").strip()

    def _interval(self) -> float:
        try:
            value = float(
                self.config.get("status_export_interval_seconds") or 30
            )
        except (TypeError, ValueError):
            value = 30.0
        return min(max(value, 5.0), 3600.0)

    def tick(self, stats: Dict[str, int]) -> None:
        if not self.enabled():
            return
        now = self.clock()
        if now - self._last_push < self._interval():
            return
        self._last_push = now
        try:
            snapshot = build_snapshot(self.config, now=now)
            body = json.dumps(snapshot, separators=(",", ":"))
        except Exception as exc:
            self.log(f"status export snapshot failed: {exc}")
            return
        findings = credential_findings(body)
        if findings:
            kinds = ", ".join(sorted({f[0] for f in findings}))
            self.log(
                "status export REFUSED: snapshot matched credential "
                f"patterns ({kinds}) — nothing was sent"
            )
            return
        import hashlib

        digest = hashlib.sha256(body.encode()).hexdigest()
        if digest == self._last_digest:
            return  # unchanged; save the request
        url = str(self.config.get("status_page_url") or "").rstrip("/")
        try:
            status = self._post(
                f"{url}/api/ingest",
                {
                    "Authorization": f"Bearer {self._token()}",
                    "Content-Type": "application/json",
                },
                body.encode(),
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            message = f"status export push failed: {exc}"
            if message != self._last_error:
                self._last_error = message
                self.log(message)
            return
        if status >= 300:
            message = f"status export push rejected: HTTP {status}"
            if message != self._last_error:
                self._last_error = message
                self.log(message)
            return
        self._last_error = ""
        self._last_digest = digest
        stats["status_exports"] = stats.get("status_exports", 0) + 1

    @staticmethod
    def _http_post(url: str, headers: Dict[str, str], body: bytes) -> int:
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(
            request, timeout=_TIMEOUT_SECONDS
        ) as response:
            return int(response.status)
