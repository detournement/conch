"""Fleet authority: per-worker owner grants, ceilings, and envelope clamps.

The awakening's authority model in one deterministic module. Three inputs
meet at every dispatch and the effective envelope is always their
intersection — never a union, never prompt-derived:

    effective = requested ∩ worker ceiling ∩ caller authority

- **Worker ceiling** — what the owner has granted THIS worker. Defaults
  are deliberately narrow: action classes ``{read}`` (plus the always-free
  task-local classes ``compute``/``write_local``), the small default tool
  set, and the worker's admin-assigned ``data_ceiling``. ``/fleet grant``
  raises the ceiling; grants are stored in the worker's admin ``labels``
  and therefore ride the ledgered ``worker_updated`` kernel event
  (hash-chained, replayable — replay == live).
- **Caller authority** — what the requesting surface may hand out. The
  owner at the interactive shell is the root authority (unbounded caller);
  ``fleet_delegate`` sessions and missions carry explicit narrower
  authority (the mission spec's ``fleet`` block).
- **Hard exclusions** — tools no worker envelope may ever name, whatever
  the grants say: the local self-management surface, interactive and
  credentialed tools, and recursive fleet delegation (``fleet_delegate``;
  plain ``delegate_task`` stays available because the worker's copy is the
  controller-brokered client, never a local recursive subagent).

The shipped clamp matrix:

    dimension   | requested        | worker ceiling            | caller authority   | effective
    ------------|------------------|---------------------------|--------------------|--------------------------
    tools       | names or default | defaults ∪ grants.tools   | names or unbounded | ∩ of all three − excluded
    actions     | classes or {read}| {read,compute,write_local}| classes or all     | ∩ of all three ∪ {read}
                |                  |   ∪ grants.actions        |                    |
    data class  | level or internal| worker.data_ceiling       | level or restricted| min(rank) of all three
    token budget| n or default     | (not ceilinged per-worker)| caller budget or ∞ | min(requested, caller)

Refusals (never silently widened, and never silently emptied):

- a requested tool outside the intersection fails closed with the exact
  names refused;
- a requested action class outside the intersection fails closed the same
  way (``read`` is always granted);
- a hard-excluded tool is refused even under ``--tools full``;
- a grant naming an action class the granting caller does not itself hold
  is refused (authority-subset rule applies to grants too).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..kernel.model import KernelError
from ..swarm.protocol import (
    ActionClass,
    DataClassification,
    classification_rank,
)

class AuthorityError(KernelError):
    """An envelope or grant exceeded its authority. Always fail closed."""


#: Tools a worker envelope may NEVER name, regardless of grants. These are
#: the local self-management / interactive / credentialed surfaces plus
#: recursive fleet delegation. Mirrors (and extends) the executor's
#: WORKER_TOOL_DENYLIST — enforced here on the controller before dispatch
#: and again on the worker at tool-build time (defense in depth).
HARD_EXCLUDED_TOOLS = frozenset({
    "conch_config", "manage_tools", "skill_manage", "interactive_terminal",
    "ssh_remote", "fleet_delegate",
})

#: The default (ungranted) tool set every ACTIVE worker may receive.
#: Deliberately narrow: enough for read/report tasks and brokered
#: delegation, nothing that mutates the operator's world.
DEFAULT_WORKER_TOOLS = frozenset({
    "local_shell", "public_api", "todo_list", "delegate_task",
})

#: Tool families a ``--tools full`` grant unlocks (everything the worker
#: executor can build, minus the hard exclusions).
WORKER_TOOL_UNIVERSE = frozenset({
    "local_shell", "public_api", "todo_list", "delegate_task",
    "save_memory", "search_conversations", "conch_introspect",
    "personal_items", "api_layer", "capitol_control",
})

#: Action classes every worker holds without any grant. ``compute`` and
#: ``write_local`` are task-local by definition (no external side effect),
#: so the "READ-only" default is about the world, not the scratch space.
DEFAULT_WORKER_ACTIONS = frozenset({
    ActionClass.READ, ActionClass.COMPUTE, ActionClass.WRITE_LOCAL,
})

#: Action classes a grant may add (everything else in the taxonomy).
GRANTABLE_ACTIONS = frozenset(ActionClass.ALL) - DEFAULT_WORKER_ACTIONS

#: The caller-authority shape used by owner surfaces (/fleet at the
#: interactive shell): unbounded tools, every action class, the highest
#: data class. Missions and fleet_delegate carry narrower shapes.
OWNER_AUTHORITY: Dict[str, Any] = {
    "tools": None,  # None = unbounded (clamped by the worker ceiling only)
    "actions": frozenset(ActionClass.ALL),
    "data": DataClassification.RESTRICTED,
    "token_budget": 0,  # 0 = unbounded
}

#: Default authority a local agent session exercises through
#: fleet_delegate when nothing narrower is configured.
DEFAULT_DELEGATE_AUTHORITY: Dict[str, Any] = {
    "tools": None,
    "actions": frozenset(DEFAULT_WORKER_ACTIONS),
    "data": DataClassification.INTERNAL,
    "token_budget": 200000,
}


def _names(value: Optional[Iterable[str]]) -> Optional[frozenset]:
    if value is None:
        return None
    return frozenset(str(item).strip() for item in value if str(item).strip())


# ---------------------------------------------------------------------------
# Grants (owner policy per worker, ledgered through worker_updated)
# ---------------------------------------------------------------------------

def worker_grants(worker: Dict[str, Any]) -> Dict[str, Any]:
    """The stored grant block for one worker record (empty by default)."""
    labels = worker.get("labels") or {}
    grants = labels.get("grants") or {}
    return grants if isinstance(grants, dict) else {}


def worker_ceiling(worker: Dict[str, Any]) -> Dict[str, Any]:
    """Effective ceiling for one worker: defaults raised by its grants.

    Returns ``{"tools": frozenset, "actions": frozenset, "data": str}``.
    The hard exclusions are subtracted last so no grant shape can
    reintroduce them.
    """
    grants = worker_grants(worker)
    tools = set(DEFAULT_WORKER_TOOLS)
    granted_tools = grants.get("tools")
    if granted_tools == "full":
        tools |= WORKER_TOOL_UNIVERSE
    elif isinstance(granted_tools, list):
        tools |= {str(name).strip() for name in granted_tools}
    actions = set(DEFAULT_WORKER_ACTIONS)
    for name in grants.get("actions") or []:
        if name in ActionClass.ALL:
            actions.add(name)
    return {
        "tools": frozenset(tools) - HARD_EXCLUDED_TOOLS,
        "actions": frozenset(actions),
        "data": str(worker.get("data_ceiling")
                    or DataClassification.INTERNAL),
    }


def validate_grant(actions: Optional[Iterable[str]],
                   tools: Any,
                   data: str = "", *,
                   caller_actions: Optional[Iterable[str]] = None,
                   caller_data: str = "") -> Dict[str, Any]:
    """Validate a grant request into the stored grant block shape.

    Fail-closed rules:

    - unknown action classes / data classes are refused;
    - hard-excluded tools are refused by name;
    - the authority-subset rule applies to the GRANTOR: a caller that does
      not itself hold an action class (or data level) cannot grant it.
      Owner surfaces pass ``caller_actions=None`` (root authority).
    """
    grant: Dict[str, Any] = {}
    caller_action_set = _names(caller_actions)
    if actions is not None:
        wanted = [str(a).strip() for a in actions if str(a).strip()]
        unknown = set(wanted) - ActionClass.ALL
        if unknown:
            raise AuthorityError(
                f"unknown action class(es) {sorted(unknown)} — grant refused"
            )
        if caller_action_set is not None:
            beyond = set(wanted) - set(caller_action_set)
            if beyond:
                raise AuthorityError(
                    "grantor does not hold action class(es) "
                    f"{sorted(beyond)} — a caller cannot grant authority "
                    "it does not have"
                )
        grant["actions"] = sorted(set(wanted) - DEFAULT_WORKER_ACTIONS)
    if tools is not None:
        if tools == "full":
            grant["tools"] = "full"
        else:
            names = [str(t).strip() for t in tools if str(t).strip()]
            excluded = set(names) & HARD_EXCLUDED_TOOLS
            if excluded:
                raise AuthorityError(
                    f"tool(s) {sorted(excluded)} are hard-excluded for "
                    "workers and can never be granted"
                )
            unknown = set(names) - WORKER_TOOL_UNIVERSE
            if unknown:
                raise AuthorityError(
                    f"unknown worker tool(s) {sorted(unknown)} — grantable "
                    f"families: {sorted(WORKER_TOOL_UNIVERSE)} or 'full'"
                )
            grant["tools"] = sorted(set(names))
    if data:
        if data not in DataClassification.ALL:
            raise AuthorityError(
                f"unknown data classification {data!r} — grant refused"
            )
        if caller_data and (
            classification_rank(data) > classification_rank(caller_data)
        ):
            raise AuthorityError(
                f"grantor data authority is {caller_data!r}; cannot grant "
                f"{data!r}"
            )
        grant["data"] = data
    if not grant:
        raise AuthorityError(
            "a grant needs at least one of --actions, --tools, --data"
        )
    return grant


def apply_grant(registry, worker_id: str, grant: Dict[str, Any],
                granted_by: str = "owner") -> Dict[str, Any]:
    """Merge a validated grant into the worker's admin labels (one
    ledgered ``worker_updated`` event) and raise the data ceiling when the
    grant names one. Returns the worker's new effective ceiling."""
    worker = registry.require(worker_id)
    labels = dict(worker.get("labels") or {})
    merged = dict(worker_grants(worker))
    if "actions" in grant:
        merged["actions"] = sorted(
            set(merged.get("actions") or []) | set(grant["actions"])
        )
    if "tools" in grant:
        if grant["tools"] == "full" or merged.get("tools") == "full":
            merged["tools"] = "full"
        else:
            merged["tools"] = sorted(
                set(merged.get("tools") or []) | set(grant["tools"])
            )
    if "data" in grant:
        merged["data"] = grant["data"]
    merged["granted_by"] = str(granted_by)
    labels["grants"] = merged
    registry.assign_authority(
        worker_id, labels=labels,
        data_ceiling=grant.get("data"),
    )
    return worker_ceiling(registry.require(worker_id))


def revoke_grants(registry, worker_id: str) -> Dict[str, Any]:
    """Drop every grant, returning the worker to its narrow defaults.
    (The data ceiling stays where the admin last set it — lowering data
    authority is an explicit ``assign_authority`` decision.)"""
    worker = registry.require(worker_id)
    labels = dict(worker.get("labels") or {})
    labels.pop("grants", None)
    registry.assign_authority(worker_id, labels=labels)
    return worker_ceiling(registry.require(worker_id))


# ---------------------------------------------------------------------------
# Envelope clamping
# ---------------------------------------------------------------------------

def clamp_envelope(worker: Dict[str, Any], *,
                   tools: Optional[Iterable[str]] = None,
                   actions: Optional[Iterable[str]] = None,
                   data: str = "",
                   token_budget: int = 0,
                   caller: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """Clamp one dispatch request to ``min(requested, ceiling, caller)``.

    Returns ``{"tools": tuple, "actions": tuple, "data": str,
    "token_budget": int}`` ready for a TaskEnvelope. Explicitly requested
    names that fall outside the intersection are refused with the exact
    excess (fail closed, never silently narrowed); omitted dimensions
    default to the narrow end and clamp silently.
    """
    caller = caller or OWNER_AUTHORITY
    ceiling = worker_ceiling(worker)
    caller_tools = _names(caller.get("tools"))
    caller_actions = _names(caller.get("actions")) or frozenset(
        ActionClass.ALL
    )
    caller_data = str(caller.get("data") or DataClassification.RESTRICTED)

    allowed_tools = set(ceiling["tools"])
    if caller_tools is not None:
        allowed_tools &= set(caller_tools)
    allowed_tools -= HARD_EXCLUDED_TOOLS
    requested_tools = _names(tools)
    if requested_tools is None:
        effective_tools = allowed_tools & DEFAULT_WORKER_TOOLS
    else:
        hard = requested_tools & HARD_EXCLUDED_TOOLS
        if hard:
            raise AuthorityError(
                f"tool(s) {sorted(hard)} are hard-excluded for workers"
            )
        beyond = requested_tools - allowed_tools
        if beyond:
            raise AuthorityError(
                f"requested tool(s) {sorted(beyond)} exceed "
                "min(worker ceiling, caller authority) — refused. "
                f"Available: {sorted(allowed_tools)}"
            )
        effective_tools = set(requested_tools)

    allowed_actions = (
        set(ceiling["actions"]) & set(caller_actions)
    ) | {ActionClass.READ}
    requested_actions = _names(actions)
    if requested_actions is None:
        effective_actions = {ActionClass.READ}
    else:
        unknown = requested_actions - ActionClass.ALL
        if unknown:
            raise AuthorityError(
                f"unknown action class(es) {sorted(unknown)}"
            )
        beyond = requested_actions - allowed_actions
        if beyond:
            raise AuthorityError(
                f"requested action class(es) {sorted(beyond)} exceed "
                "min(worker ceiling, caller authority) — refused. "
                f"Available: {sorted(allowed_actions)}"
            )
        effective_actions = set(requested_actions) | {ActionClass.READ}

    requested_data = str(data or DataClassification.INTERNAL)
    if requested_data not in DataClassification.ALL:
        raise AuthorityError(
            f"unknown data classification {requested_data!r}"
        )
    effective_data = min(
        (requested_data, ceiling["data"], caller_data),
        key=classification_rank,
    )

    caller_budget = int(caller.get("token_budget") or 0)
    requested_budget = int(token_budget or 0)
    if caller_budget and requested_budget:
        effective_budget = min(requested_budget, caller_budget)
    else:
        effective_budget = requested_budget or caller_budget

    return {
        "tools": tuple(sorted(effective_tools)),
        "actions": tuple(sorted(effective_actions)),
        "data": effective_data,
        "token_budget": effective_budget,
    }


def describe_ceiling(worker: Dict[str, Any]) -> str:
    """One human line for /fleet workers output."""
    ceiling = worker_ceiling(worker)
    grants = worker_grants(worker)
    granted = " (granted)" if grants else ""
    return (
        f"actions={','.join(sorted(ceiling['actions']))} "
        f"tools={','.join(sorted(ceiling['tools']))} "
        f"data≤{ceiling['data']}{granted}"
    )


def mission_fleet_authority(fleet_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Caller authority derived from a mission spec's ``fleet`` block."""
    spec = fleet_spec or {}
    return {
        "tools": (
            list(spec["tools"]) if isinstance(spec.get("tools"), list)
            else None
        ),
        "actions": frozenset(
            spec.get("actions") or DEFAULT_WORKER_ACTIONS
        ),
        "data": str(spec.get("data") or DataClassification.INTERNAL),
        "token_budget": int(spec.get("token_budget") or 200000),
    }


__all__: List[str] = [
    "AuthorityError", "HARD_EXCLUDED_TOOLS", "DEFAULT_WORKER_TOOLS",
    "WORKER_TOOL_UNIVERSE", "DEFAULT_WORKER_ACTIONS", "GRANTABLE_ACTIONS",
    "OWNER_AUTHORITY", "DEFAULT_DELEGATE_AUTHORITY", "worker_grants",
    "worker_ceiling", "validate_grant", "apply_grant", "revoke_grants",
    "clamp_envelope", "describe_ceiling", "mission_fleet_authority",
]
