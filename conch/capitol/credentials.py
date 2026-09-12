"""Bearer resolution and secret hygiene for the Capitol adapter.

Non-negotiable invariant: secret bytes never enter prompts, logs, state
files, config files, or error messages. Conch therefore never *stores* a
Capitol bearer — it resolves one at call time from, in order:

1. the environment variable named by the ``capitol_bearer_env`` config key
   (default ``CAPITOL_A2A_BEARER``), and
2. the A2Actrl agent registry at ``~/.capitol-a2a/agents.yaml`` (the
   established local credential store for Capitol A2A agents), matched on
   the configured org + agent (and base-url host when present).

Everything that could carry a token through an exception or a log line is
scrubbed with :func:`redact_text` before it leaves this package.

``local_only`` semantics: a local-only session may still drive a Capitol
endpoint on localhost or the LAN (the stack the pilot builds against runs
on ``localhost:8300``); non-local endpoints are refused.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ..config import local_only_enabled
from .errors import CapitolAuthError, CapitolError

DEFAULT_BEARER_ENV = "CAPITOL_A2A_BEARER"
REGISTRY_PATH = Path(os.path.expanduser("~/.capitol-a2a/agents.yaml"))

REDACTED = "[redacted]"


def redact_text(text: str, secrets) -> str:
    """Replace every non-empty secret occurrence in *text* with a marker."""
    result = str(text)
    for secret in secrets or ():
        if secret:
            result = result.replace(secret, REDACTED)
    return result


# ---------------------------------------------------------------------------
# local_only enforcement
# ---------------------------------------------------------------------------

def is_local_endpoint(url: str) -> bool:
    """True when *url* points at localhost or a private/LAN address.

    Hostnames that are not IP literals count as local only for
    ``localhost`` and ``*.local`` (mDNS) names — anything else would need
    a DNS lookup to classify, and a network probe inside a policy check is
    itself a leak, so unknown names fail closed (not local).
    """
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
    )


def ensure_endpoint_allowed(config: dict, base_url: str) -> None:
    """Refuse non-local Capitol endpoints under ``local_only`` sessions."""
    provider = (config or {}).get("provider", "")
    if local_only_enabled(config or {}, provider) and not is_local_endpoint(
        base_url
    ):
        raise CapitolError(
            f"local_only is enabled: Capitol endpoint {base_url!r} is not "
            "a localhost/LAN address, refusing to connect"
        )


# ---------------------------------------------------------------------------
# Registry parsing (stdlib-only subset of the agents.yaml format)
# ---------------------------------------------------------------------------

def parse_agents_yaml(text: str) -> List[Dict[str, str]]:
    """Parse the flat ``agents:`` block-sequence of A2Actrl's registry.

    Handles exactly the shape the registry writes — a top-level ``agents:``
    list whose items are one-level ``key: value`` maps — without a YAML
    dependency. Multi-line strings (wrapped descriptions) are ignored;
    only the identity/credential fields matter here. Unknown structure is
    skipped, never guessed at.
    """
    agents: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    in_agents = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "-")) and line != "agents:":
            in_agents = False
        if line == "agents:":
            in_agents = True
            continue
        if not in_agents:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {}
            agents.append(current)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # A wrapped scalar's continuation lines have no "key:" shape and
        # were skipped above; a key we already saw means a new mapping
        # line, which is all this consumer needs.
        if key and key not in current:
            current[key] = value
    return agents


def _load_registry_entries() -> List[Dict[str, str]]:
    try:
        text = REGISTRY_PATH.read_text()
    except OSError:
        return []
    try:
        return parse_agents_yaml(text)
    except Exception:
        # A malformed registry must not crash resolution — it simply
        # contributes no candidates (the env var path still works).
        return []


def resolve_bearer(
    config: dict,
    org_id: str,
    agent_id: str,
    base_url: str = "",
) -> Tuple[str, str]:
    """Resolve the A2A bearer for (org, agent) without ever storing it.

    Returns ``(bearer, source)`` where ``source`` names where the token
    came from (an env-var name or a registry alias) — safe to log. Raises
    :class:`CapitolAuthError` naming the expected sources when nothing
    resolves; the error never contains token bytes.
    """
    env_name = str(
        (config or {}).get("capitol_bearer_env") or DEFAULT_BEARER_ENV
    ).strip() or DEFAULT_BEARER_ENV
    bearer = os.environ.get(env_name, "").strip()
    if bearer:
        return bearer, f"env:{env_name}"

    host = (urlsplit(base_url).hostname or "").lower()
    fallback: Optional[Tuple[str, str]] = None
    for entry in _load_registry_entries():
        if entry.get("org_id") != org_id or entry.get("agent_id") != agent_id:
            continue
        token = (entry.get("bearer") or "").strip()
        if not token:
            continue
        alias = entry.get("name") or "unnamed"
        entry_host = (urlsplit(entry.get("base_url") or "").hostname or "").lower()
        if not host or not entry_host or entry_host == host:
            return token, f"registry:{alias}"
        if fallback is None:
            fallback = (token, f"registry:{alias}")
    if fallback is not None:
        return fallback

    raise CapitolAuthError(
        f"no Capitol bearer found: set ${env_name} or add an entry for "
        f"org {org_id} / agent {agent_id} to {REGISTRY_PATH} "
        "(tokens are never stored in conch config or state)"
    )
