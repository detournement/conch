"""Deterministic workflow-payload generation for compiled processes.

The together-funding pattern generalized: an Architecture Card declares a
created workflow as a compact, reviewable **stage graph** (input → agent
→ document/notify chain, tools attached to agents), and this module turns
those stages into the exact ``AdvancedWorkflowPayload`` bytes against the
serving stack's live node catalog. Every node/port/workflow UUID derives
from ``uuid5`` over the workflow identity and a stable role name, so:

- the workflow id is known before the first persist (the card carries it,
  the supervising mission binds it, the drill drives it);
- re-building an unchanged card produces byte-identical payloads, so the
  persist idempotency key (a digest of the payload) replays instead of
  minting a new version;
- what the user approved (stages + prompts + identities) fully determines
  what materializes — the generator is code, not model output.

Stage validation is fail-closed: unknown kinds, unknown fields, unknown
catalog tools, and dangling references are all rejected at card time.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from ..errors import CapitolError

#: Stable namespace for every compiled-workflow UUID.
_NS = uuid.UUID("6c0a5e5e-91c2-4f56-9b1f-3f0ab21c0002")

#: Supported stage kinds → catalog node ids. The v1 generator supports
#: exactly the shapes the launch candidates need; anything else fails
#: closed at validation.
STAGE_KINDS: Dict[str, str] = {
    "text_input": "text_input_node",
    "json_input": "json_input_node",
    "agent": "agent_node",
    "docx": "docx_chat_node",
    "markdown_output": "markdown_output_node",
    "notify": "notify_node",
}
_INPUT_KINDS = ("text_input", "json_input")

#: The output port each kind feeds downstream from, and the param a
#: downstream stage binds its upstream into.
_SOURCE_PORTS = {
    "text_input": "text",
    "json_input": "value",
    "agent": "text",
    "docx": "docx_output",
}
_SINK_PARAMS = {
    "agent": "user_prompt",
    "docx": "request",
    "markdown_output": "markdown_content",
    "notify": "message",
}
_INPUT_FIELDS = {"text_input": "text_input", "json_input": "value"}

_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_COLLECTION_PLACEHOLDER_RE = re.compile(
    r"\$collection:([A-Za-z0-9-]+)"
)

_STAGE_KEYS = {
    "text_input": {"kind", "role", "name", "default", "info"},
    "json_input": {"kind", "role", "name", "value"},
    "agent": {"kind", "role", "name", "system_prompt", "tools", "model",
              "temperature", "timeout", "max_tokens", "from"},
    "docx": {"kind", "role", "name", "from"},
    "markdown_output": {"kind", "role", "name", "from"},
    "notify": {"kind", "role", "name", "title", "event_subtype", "from"},
}


def stable_id(name: str) -> str:
    """Deterministic UUID for a named role in compiled graphs."""
    return str(uuid.uuid5(_NS, name))


def workflow_uuid(identity: str) -> str:
    return stable_id(f"workflow:{identity}")


def node_uuid(identity: str, role: str) -> str:
    return stable_id(f"{identity}:node:{role}")


def payload_digest(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Stage validation (card-time, fail closed)
# ---------------------------------------------------------------------------

def _collection_refs(text: str) -> List[str]:
    return _COLLECTION_PLACEHOLDER_RE.findall(str(text or ""))


def validate_stages(where: str, stages: Any, *,
                    catalog_ids: Optional[List[str]] = None,
                    collection_identities: Optional[Set[str]] = None,
                    discovery_collections: Optional[Set[str]] = None
                    ) -> List[Dict[str, Any]]:
    """Validate a stage list into normalized form (fail closed)."""
    if not isinstance(stages, list) or len(stages) < 2:
        raise CapitolError(
            f"{where} must be a list of at least two stages "
            "(an input stage plus at least one processing stage)"
        )
    catalog = set(catalog_ids or [])
    collections = set(collection_identities or set())
    discovered = set(discovery_collections or set())
    normalized: List[Dict[str, Any]] = []
    roles: Set[str] = set()
    for index, stage in enumerate(stages):
        stage_where = f"{where}[{index}]"
        if not isinstance(stage, dict):
            raise CapitolError(f"{stage_where} must be an object")
        kind = str(stage.get("kind") or "")
        if kind not in STAGE_KINDS:
            raise CapitolError(
                f"{stage_where}.kind {kind!r} is not supported "
                f"({', '.join(sorted(STAGE_KINDS))}) — failing closed"
            )
        unknown = set(stage) - _STAGE_KEYS[kind]
        if unknown:
            raise CapitolError(
                f"{stage_where} has unknown fields (failing closed): "
                + ", ".join(sorted(unknown))
            )
        role = str(stage.get("role") or "").strip()
        if not _ROLE_RE.match(role):
            raise CapitolError(
                f"{stage_where}.role {role!r} is invalid (lowercase "
                "letters, digits, hyphens)"
            )
        if role in roles:
            raise CapitolError(f"{stage_where} duplicates role {role!r}")
        roles.add(role)
        if index == 0 and kind not in _INPUT_KINDS:
            raise CapitolError(
                f"{where}[0] must be an input stage "
                f"({' or '.join(_INPUT_KINDS)})"
            )
        if index > 0 and kind in _INPUT_KINDS:
            raise CapitolError(
                f"{stage_where}: only the first stage may be an input"
            )
        clean: Dict[str, Any] = {
            "kind": kind, "role": role,
            "name": str(stage.get("name") or role),
        }
        if kind == "text_input":
            clean["default"] = str(stage.get("default") or "")
            clean["info"] = str(stage.get("info") or "")
        elif kind == "json_input":
            clean["value"] = stage.get("value")
        elif kind == "agent":
            prompt = str(stage.get("system_prompt") or "").strip()
            if not prompt:
                raise CapitolError(
                    f"{stage_where}.system_prompt must be non-empty — "
                    "the agent's instructions are workflow-fixed"
                )
            clean["system_prompt"] = prompt
            tools = stage.get("tools") or []
            if not isinstance(tools, list) or any(
                not isinstance(tool, str) for tool in tools
            ):
                raise CapitolError(
                    f"{stage_where}.tools must be a list of catalog "
                    "node ids"
                )
            if catalog:
                missing = [tool for tool in tools if tool not in catalog]
                if missing:
                    raise CapitolError(
                        f"{stage_where}.tools not in the discovered node "
                        f"catalog (failing closed): {', '.join(missing)}"
                    )
            clean["tools"] = [str(tool) for tool in tools]
            for key, caster in (("model", str), ("temperature", float),
                                ("timeout", int), ("max_tokens", int)):
                if stage.get(key) is not None:
                    try:
                        clean[key] = caster(stage[key])
                    except (TypeError, ValueError):
                        raise CapitolError(
                            f"{stage_where}.{key} must be a {caster.__name__}"
                        ) from None
            for ref in _collection_refs(prompt):
                if ref not in collections and ref not in discovered:
                    raise CapitolError(
                        f"{stage_where}.system_prompt references unknown "
                        f"collection {ref!r} ($collection:… must name a "
                        "created identity or a discovered collection id)"
                    )
        elif kind == "notify":
            clean["title"] = str(stage.get("title") or clean["name"])
            clean["event_subtype"] = str(
                stage.get("event_subtype") or "conch_compiled_process"
            )
        if index > 0:
            source = str(stage.get("from") or "") or normalized[
                index - 1
            ]["role"]
            if source not in roles or source == role:
                raise CapitolError(
                    f"{stage_where}.from {source!r} does not name an "
                    "earlier stage"
                )
            source_kind = next(
                entry["kind"] for entry in normalized
                if entry["role"] == source
            )
            if source_kind not in _SOURCE_PORTS:
                raise CapitolError(
                    f"{stage_where}.from {source!r}: {source_kind} "
                    "stages produce no downstream output"
                )
            clean["from"] = source
        normalized.append(clean)
    if all(stage["kind"] in _INPUT_KINDS for stage in normalized):
        raise CapitolError(f"{where} needs at least one processing stage")
    return normalized


def stage_input_override_key(identity: str,
                             stages: List[Dict[str, Any]]) -> str:
    """The runs-API override key (``<node_id>.<field_id>``) for the
    workflow's input stage — the drill's steering wheel."""
    first = stages[0]
    return (
        f"{node_uuid(identity, first['role'])}."
        f"{_INPUT_FIELDS[first['kind']]}"
    )


# ---------------------------------------------------------------------------
# Payload assembly (materialization-time, against the live catalog)
# ---------------------------------------------------------------------------

def _find_param(struct: Dict[str, Any], field_id: str) -> Dict[str, Any]:
    for param in struct.get("params", []):
        if param.get("field_id") == field_id:
            return param
    raise CapitolError(
        f"catalog struct {struct.get('node_id')!r} has no param "
        f"{field_id!r}"
    )


def _base_struct(catalog: Dict[str, Dict[str, Any]],
                 node_id: str) -> Dict[str, Any]:
    entry = catalog.get(node_id)
    if not entry:
        raise CapitolError(
            f"node {node_id!r} is not in the serving stack's catalog"
        )
    struct = json.loads(json.dumps(entry))
    struct.setdefault("trace", False)
    struct.setdefault("category", "")
    struct.setdefault("tool_category", "")
    struct.setdefault("tool_name", "")
    struct.setdefault("auth_type", "none")
    struct.setdefault("mcp_server", "")
    if not struct.get("name"):
        struct["name"] = str(struct.get("display_name") or node_id)
    for param in struct.get("params", []):
        # Volatile UI option lists would churn the payload digest.
        param.pop("options", None)
        param.pop("options_metadata", None)
    return struct


def _gnode(react_id: str, struct: Dict[str, Any], x: int,
           y: int) -> Dict[str, Any]:
    struct["react_flow_generated_id"] = react_id
    return {
        "id": react_id,
        "type": "GenericNode",
        "position": {"x": x, "y": y},
        "data": {"struct": struct},
        "isSelected": False,
        "result": None,
    }


def _edge(src: str, sport: str, dst: str, dport: str,
          target_param_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": f"xy-edge__{src}{sport}-{dst}{dport}",
        "source": src,
        "target": dst,
        "sourceHandle": sport,
        "targetHandle": dport,
        "edge_type": "port_to_param" if target_param_id else "port_to_port",
        "sourceNodeId": src,
        "targetNodeId": dst,
        "source_node_id": src,
        "source_port_id": sport,
        "target_node_id": dst,
        "target_port_id": dport,
        "target_param_id": target_param_id,
        "type": "default",
        "animated": False,
        "style": None,
        "data": {},
    }


def _rewrite_output_ports(struct: Dict[str, Any],
                          scope: str) -> Dict[str, str]:
    ports: Dict[str, str] = {}
    for port in struct.get("output_ports", []):
        name = str(port.get("name") or "out")
        port["id"] = stable_id(f"{scope}:out:{name}")
        ports[name] = port["id"]
    return ports


def _bind_param(struct: Dict[str, Any], field_id: str, scope: str,
                sources: List[Dict[str, str]]) -> str:
    param = _find_param(struct, field_id)
    bindable = param.get("bindable_config")
    if not isinstance(bindable, dict) or not bindable.get("input_port"):
        raise CapitolError(
            f"param {field_id!r} on {struct.get('node_id')!r} is not "
            "bindable"
        )
    bindable["is_bound"] = True
    port = bindable["input_port"]
    port["id"] = stable_id(f"{scope}:param:{field_id}")
    port["incoming_connections"] = [
        {"node_id": source["node_id"], "port_id": source["port_id"]}
        for source in sources
    ]
    input_ports = struct.setdefault("input_ports", [])
    if all(existing.get("id") != port["id"] for existing in input_ports):
        input_ports.append(port)
    return port["id"]


def _substitute_collections(text: str,
                            collection_ids: Dict[str, str]) -> str:
    def replace(match):
        ref = match.group(1)
        if ref not in collection_ids:
            raise CapitolError(
                f"unresolved collection reference $collection:{ref} — "
                "materialize collections before workflows"
            )
        return collection_ids[ref]
    return _COLLECTION_PLACEHOLDER_RE.sub(replace, str(text or ""))


def build_workflow_payload(
    catalog: Dict[str, Dict[str, Any]],
    workflow: Dict[str, Any],
    *,
    collection_ids: Optional[Dict[str, str]] = None,
    lineage: Optional[Dict[str, Any]] = None,
    set_param: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Assemble the full workflow payload for one created workflow.

    ``workflow`` is the normalized card entry (identity, name,
    description, stages, workflow_id). ``collection_ids`` maps
    ``$collection:<ref>`` placeholders to real platform collection ids.
    Deterministic and total: identical inputs produce identical bytes.
    """
    collection_ids = collection_ids or {}
    identity = str(workflow["identity"])
    stages: List[Dict[str, Any]] = workflow["stages"]
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    #: role → (react node id, {port name: port id}, kind)
    built: Dict[str, Dict[str, Any]] = {}

    for index, stage in enumerate(stages):
        kind = stage["kind"]
        scope = f"{identity}:{stage['role']}"
        react_id = node_uuid(identity, stage["role"])
        struct = _base_struct(catalog, STAGE_KINDS[kind])
        struct["name"] = stage["name"]
        x, y = 420 * index, 0

        if kind == "text_input":
            struct["user_config_name"] = stage["name"]
            param = _find_param(struct, "text_input")
            param["value"] = stage.get("default", "")
            if stage.get("info"):
                param["info"] = stage["info"]
        elif kind == "json_input":
            struct["user_config_name"] = stage["name"]
            _find_param(struct, "mode")["value"] = "value"
            _find_param(struct, "value")["value"] = stage.get("value")
            _find_param(struct, "on_invalid")["value"] = "error"
        elif kind == "agent":
            _find_param(struct, "name")["value"] = stage["name"]
            _find_param(struct, "system_prompt")["value"] = (
                _substitute_collections(
                    stage["system_prompt"], collection_ids
                )
            )
            _find_param(struct, "temperature")["value"] = float(
                stage.get("temperature", 0.2)
            )
            _find_param(struct, "timeout")["value"] = int(
                stage.get("timeout", 600)
            )
            _find_param(struct, "tool_choice")["value"] = "auto"
            if stage.get("model"):
                _find_param(struct, "model")["value"] = stage["model"]
            if stage.get("max_tokens"):
                _find_param(struct, "max_tokens")["value"] = int(
                    stage["max_tokens"]
                )
        elif kind == "docx":
            _find_param(
                struct, "ground_with_provided_info"
            )["value"] = True
        elif kind == "notify":
            _find_param(struct, "title")["value"] = stage["title"]
            _find_param(struct, "event_subtype")["value"] = stage[
                "event_subtype"
            ]
        if set_param is not None:
            set_param(stage, struct)

        ports = _rewrite_output_ports(struct, scope)

        # Attach the agent's tool nodes before wiring the prompt.
        if kind == "agent":
            tool_sources: List[Dict[str, str]] = []
            for tool_index, tool_id in enumerate(stage.get("tools") or []):
                tool_scope = f"{scope}:tool:{tool_id}"
                tool_struct = _base_struct(catalog, tool_id)
                tool_react_id = stable_id(tool_scope)
                tool_ports = _rewrite_output_ports(tool_struct, tool_scope)
                tool_port = tool_ports.get("tool") or next(
                    iter(tool_ports.values()), ""
                )
                nodes.append(_gnode(
                    tool_react_id, tool_struct, x - 210,
                    260 + tool_index * 170,
                ))
                tool_sources.append(
                    {"node_id": tool_react_id, "port_id": tool_port}
                )
            if tool_sources:
                tool_port_in = struct["input_ports"][0]
                tool_port_in["id"] = stable_id(f"{scope}:port:tools")
                tool_port_in["incoming_connections"] = tool_sources
                for source in tool_sources:
                    edges.append(_edge(
                        source["node_id"], source["port_id"],
                        react_id, tool_port_in["id"],
                    ))

        if index > 0:
            source_role = stage["from"]
            source = built[source_role]
            source_port = source["ports"][_SOURCE_PORTS[source["kind"]]]
            sink_param = _SINK_PARAMS[kind]
            bound_port = _bind_param(
                struct, sink_param, scope,
                [{"node_id": source["react_id"], "port_id": source_port}],
            )
            edges.append(_edge(
                source["react_id"], source_port, react_id, bound_port,
                sink_param,
            ))

        nodes.append(_gnode(react_id, struct, x, y))
        built[stage["role"]] = {
            "react_id": react_id, "ports": ports, "kind": kind,
        }

    return {
        "id": str(workflow["workflow_id"]),
        "session_id": "",
        "name": str(workflow["name"]),
        "description": str(workflow.get("description") or ""),
        "icon": "",
        "color": "",
        "bg_color": "",
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "conch": {
                **json.loads(json.dumps(lineage or {}, sort_keys=True)),
                "provisioner": "process_compiler",
                "identity": identity,
            },
        },
        "publish_to_api": True,
        "publish_to_mcp": False,
        "publish_to_template": False,
        "mcp_code": None,
        "api_code": None,
        "entry_node_id": None,
        "global_context": {},
        "allow_clarifications": False,
    }
