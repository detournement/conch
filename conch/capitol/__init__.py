"""Capitol A2A integration (Swarm roadmap Phase 3).

``CapitolRuntime`` is the policy-bearing adapter Conch uses to drive
governed Capitol workflows over the A2A JSON-RPC gateway — AgentCard
discovery with fail-closed capability gating, handshake/context threading,
the org agent directory, workflow catalog/describe/suggest/versions/stats,
typed invocation with caller-supplied idempotency keys, the full run
lifecycle (status, batch events, resumable SSE watch with polling
fallback, pause/resume/stop, JSON-RPC cancel), HITL responses, artifact
upload/download, outputs, and eval roll-ups.

``CapitolAdmin`` (separately gated, default off) is the bounded
builder/provisioning profile; ``conch.capitol.supervisor`` binds runs to
kernel missions and supervises them from the daemon tick.

Stdlib only. Model access is bounded: the ``capitol_control`` session
tool (:mod:`conch.capitol.tool`) exposes the RUNTIME surface with
required idempotency keys and required-policy gates; ``CapitolAdmin``
is never model-callable, and drivers/mission tools construct exact
requests (deterministic policy authorizes). The ``A2Actrl`` reference
client is the normative wire reference.
"""

from .errors import (  # noqa: F401
    CapitolAuthError,
    CapitolCapabilityError,
    CapitolError,
    CapitolProtocolError,
)
from .client import CapitolRuntime  # noqa: F401
