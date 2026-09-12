"""Capitol A2A integration (Swarm roadmap Phase 3 seed).

``CapitolRuntime`` is the thin, policy-bearing adapter Conch uses to drive
governed Capitol workflows over the A2A JSON-RPC gateway — discovery,
handshake/context threading, typed workflow invocation with caller-supplied
idempotency keys, resumable SSE event streaming, HITL responses, and
artifact upload/download. Stdlib only; models never talk to this surface
directly (drivers construct exact requests, deterministic policy
authorizes).
"""

from .errors import (  # noqa: F401
    CapitolAuthError,
    CapitolError,
    CapitolProtocolError,
)
from .client import CapitolRuntime  # noqa: F401
