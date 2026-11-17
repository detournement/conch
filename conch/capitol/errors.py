"""Typed error taxonomy for the Capitol A2A adapter.

The gateway's JSON-RPC errors carry a ``google.rpc.ErrorInfo`` object in
``error.data[]`` (some older gateways send a flat dict); both shapes are
normalized here. Every error is classified before anything retries:

- :class:`CapitolAuthError` — the credential is missing, expired, or
  rejected (HTTP 401 / ``INVALID_TOKEN`` / ``-32004``). There is no
  automatic re-auth on this path (a documented Capitol gap): callers park
  the work as "credential needed" and surface it to the operator.
- :class:`CapitolProtocolError` — the wire is not something we understand:
  unsupported ``wireSchemaVersion``, malformed envelopes, non-JSON bodies.
  Always fail closed on these; never best-effort parse.
- :class:`CapitolError` — everything else, carrying the gateway's typed
  metadata (``code``, ``reason``, ``retryable``, ``category``,
  ``actionable_hint``). Honor ``retryable`` before any retry.

Secret hygiene: messages are redacted by the client before construction;
these classes never see or store bearer tokens.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class CapitolError(Exception):
    """A Capitol A2A call failed. Carries the gateway's typed metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        reason: str = "",
        retryable: Optional[bool] = None,
        category: str = "",
        hint: str = "",
        http_status: Optional[int] = None,
    ):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = retryable
        self.category = category
        self.hint = hint
        self.http_status = http_status


class CapitolAuthError(CapitolError):
    """Credential missing/expired/rejected — park, never auto re-auth."""


class CapitolProtocolError(CapitolError):
    """Unknown wire version or malformed envelope — always fail closed."""


class CapitolCapabilityError(CapitolError):
    """The AgentCard does not advertise the capability a feature needs.

    Raised *before* any wire call is attempted: Phase 3 surfaces are gated
    on the card's self-described skill catalog and capability flags, and an
    unknown or missing capability fails closed with the skill named — a
    refused feature must never be mistaken for a gateway error.
    """


def parse_error_info(error: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a JSON-RPC ``error`` object's ErrorInfo metadata.

    Accepts both the v1.0 wrapped-array shape (``error.data[0].metadata``)
    and the legacy flat-dict shape (``error.data.retryable``). Returns
    ``{code, message, reason, retryable, category, hint}`` with ``None``
    for anything absent — callers must treat unknown ``retryable`` as
    not retryable (fail closed).
    """
    data = error.get("data")
    info = data[0] if isinstance(data, list) and data else data
    info = info if isinstance(info, dict) else {}
    metadata = info.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else info
    retryable = metadata.get("retryable")
    return {
        "code": error.get("code"),
        "message": str(error.get("message") or ""),
        "reason": str(info.get("reason") or ""),
        "retryable": retryable if isinstance(retryable, bool) else None,
        "category": str(metadata.get("category") or ""),
        "hint": str(metadata.get("actionable_hint") or ""),
    }
