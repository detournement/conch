"""Flow-pack manifests (``conch.flow_pack.v1``): model + fail-closed
validation + canonical digest.

A pack is data, never code: the manifest declares workflow bindings,
typed-contract shapes, intakes, deterministic request templates, the
phase machine, approval classes with caps, durable-state shape, and
wording — the generic engine (:mod:`conch.capitol.packs.engine`)
executes it over ``CapitolRuntime``. Unknown sections or fields are
rejected (fail closed), and every template expression/formula is parsed
at load time so a typo cannot surface mid-flow.

``pack_digest`` is the sha256 of the canonical (sorted, separator-free)
JSON — the pack pin stamped into derived idempotency keys.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ..errors import CapitolError
from .templates import (
    is_expression,
    is_formula,
    parse_expression,
    validate_formula,
    validate_line_spec,
)

PACK_SCHEMA = "conch.flow_pack.v1"


class PackError(CapitolError):
    """A pack manifest failed validation (fail closed)."""


_TOP_LEVEL_KEYS = {
    "schema", "pack", "capitol", "contracts", "intakes", "bindings",
    "requests", "flow", "approvals", "state", "notifications",
    "messages", "presentation", "acceptance", "scope",
}
_REQUIRED_TOP = ("schema", "pack", "capitol")

_SECTION_KEYS = {
    "pack": {"name", "version", "description", "replaces"},
    "capitol": {"org", "agent", "workflows"},
    "contracts": {"prefix", "session", "effect", "rejection", "guidance"},
    "contracts.session": {"name", "schema", "status_field", "identity"},
    "contracts.effect": {"schema", "facts", "link_fact", "success"},
    "contracts.rejection": {"schema"},
    "contracts.guidance": {"schema"},
    "workflow": {"pin", "id", "discover", "inputs_key", "request_schema"},
    "request": {"schema", "fields", "from_contract", "require",
                "idempotency_key"},
    "flow": {"session_contract", "phases", "on_status", "hitl",
             "recovery_order", "revision_invalidates_approval"},
    "flow.on_status.clarify": {"phase", "questions_field", "max_rounds",
                               "reply_action"},
    "flow.on_status.gate": {"phase", "approval"},
    "approval": {"kind", "constructs", "pin", "policy_event",
                 "policy_fields", "caps", "auto_within_caps", "exact",
                 "on_effect_failure", "ttl_key", "describe"},
    "approval.caps": {"toggle_key", "checks", "unreadable_outcome"},
    "approval.caps.check": {"field", "op", "config_key", "skip_when_unset",
                            "label", "unit"},
    "approval.exact": {"phrase", "challenge"},
    "state": {"store", "file", "effect_field", "session_fields",
              "collections", "threads"},
    "scope": {"org_config_key", "agent_config_key"},
}

_INTAKE_KEYS = {
    "channel_message": {"kind", "channel", "enabled_key", "match",
                        "attachments", "session", "start",
                        "context_default"},
    "shell_command": {"kind", "command", "usage", "start"},
    "watched_folder": {"kind", "path", "enabled", "note"},
}

_BINDING_KEYS = {"workflow", "kind", "request", "mode_key", "default_mode",
                 "modes", "gateway_key"}

_CAP_OPS = {"in_csv", "lte_float"}

#: The engine's phase machine (E3). Manifests must declare exactly these
#: phases and the supported recovery ordering; anything else fails closed
#: (the v1 engine implements one machine, not arbitrary ones).
ENGINE_PHASES = ("drafting", "clarify", "hitl", "awaiting_approval",
                 "publishing", "done", "failed")
ENGINE_RECOVERY_ORDER = ("hitl", "contract_status", "failed")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def pack_digest(data: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def _check_keys(where: str, data: Dict[str, Any], allowed: set,
                required: tuple = ()):
    if not isinstance(data, dict):
        raise PackError(f"{where} must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise PackError(
            f"{where} has unknown fields (failing closed): "
            + ", ".join(sorted(unknown))
        )
    for key in required:
        if key not in data:
            raise PackError(f"{where} is missing required field {key!r}")


def _validate_value_templates(where: str, value: Any):
    """Parse every template expression/formula reachable in a value."""
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_value_templates(f"{where}.{key}", item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_value_templates(f"{where}[{index}]", item)
    elif isinstance(value, str):
        if is_expression(value):
            parse_expression(value)
        elif is_formula(value):
            validate_formula(value, where)


class FlowPack:
    """One validated flow-pack manifest with typed accessors."""

    def __init__(self, data: Dict[str, Any], *, source: str = ""):
        self.raw = data
        self.source = source
        self.digest = pack_digest(data)
        _validate(data)

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return str(self.raw["pack"]["name"])

    @property
    def version(self) -> str:
        return str(self.raw["pack"].get("version") or "0")

    @property
    def description(self) -> str:
        return str(self.raw["pack"].get("description") or "")

    # -- capitol / workflows -------------------------------------------------

    @property
    def workflows(self) -> Dict[str, Dict[str, Any]]:
        return self.raw["capitol"]["workflows"]

    def workflow_aliases(self) -> List[str]:
        return list(self.workflows)

    # -- contracts ------------------------------------------------------------

    @property
    def contracts(self) -> Dict[str, Any]:
        return self.raw.get("contracts") or {}

    @property
    def contract_prefix(self) -> str:
        return str(self.contracts.get("prefix") or "")

    @property
    def session_contract(self) -> Dict[str, Any]:
        return self.contracts.get("session") or {}

    @property
    def session_contract_name(self) -> str:
        return str(self.session_contract.get("name") or "contract")

    @property
    def session_schema(self) -> str:
        return str(self.session_contract.get("schema") or "")

    @property
    def status_field(self) -> str:
        return str(self.session_contract.get("status_field") or "status")

    @property
    def identity_fields(self) -> List[str]:
        return [str(f) for f in self.session_contract.get("identity") or []]

    @property
    def effect_schema(self) -> str:
        return str((self.contracts.get("effect") or {}).get("schema") or "")

    @property
    def effect_facts(self) -> List[str]:
        return [
            str(f)
            for f in (self.contracts.get("effect") or {}).get("facts") or []
        ]

    @property
    def rejection_schema(self) -> str:
        return str(
            (self.contracts.get("rejection") or {}).get("schema") or ""
        )

    @property
    def guidance_schema(self) -> str:
        return str(
            (self.contracts.get("guidance") or {}).get("schema") or ""
        )

    # -- intakes / bindings / requests ----------------------------------------

    @property
    def intakes(self) -> List[Dict[str, Any]]:
        return self.raw.get("intakes") or []

    def intake(self, kind: str) -> Optional[Dict[str, Any]]:
        for intake in self.intakes:
            if intake.get("kind") == kind:
                return intake
        return None

    @property
    def bindings(self) -> Dict[str, Dict[str, Any]]:
        return self.raw.get("bindings") or {}

    def binding(self, name: str) -> Dict[str, Any]:
        try:
            return self.bindings[name]
        except KeyError:
            raise PackError(
                f"pack {self.name} has no binding {name!r}"
            ) from None

    @property
    def requests(self) -> Dict[str, Dict[str, Any]]:
        return self.raw.get("requests") or {}

    def request_spec(self, name: str) -> Dict[str, Any]:
        try:
            return self.requests[name]
        except KeyError:
            raise PackError(
                f"pack {self.name} has no request template {name!r}"
            ) from None

    # -- flow ------------------------------------------------------------------

    @property
    def flow(self) -> Dict[str, Any]:
        return self.raw.get("flow") or {}

    @property
    def on_status(self) -> Dict[str, Dict[str, Any]]:
        return self.flow.get("on_status") or {}

    def clarify_rule(self) -> Optional[Dict[str, Any]]:
        for rule in self.on_status.values():
            if rule.get("phase") == "clarify":
                return rule
        return None

    def gate_rule(self) -> Optional[Dict[str, Any]]:
        for rule in self.on_status.values():
            if rule.get("phase") == "gate":
                return rule
        return None

    def status_for_phase(self, phase: str) -> str:
        for status, rule in self.on_status.items():
            if rule.get("phase") == phase:
                return status
        return ""

    @property
    def max_clarify_rounds(self) -> int:
        rule = self.clarify_rule() or {}
        return int(rule.get("max_rounds") or 5)

    @property
    def revision_invalidates_approval(self) -> bool:
        return bool(self.flow.get("revision_invalidates_approval", True))

    # -- approvals ----------------------------------------------------------------

    @property
    def approvals(self) -> List[Dict[str, Any]]:
        return self.raw.get("approvals") or []

    def approval(self, kind: str = "") -> Optional[Dict[str, Any]]:
        for approval in self.approvals:
            if not kind or approval.get("kind") == kind:
                return approval
        return None

    def approval_kinds(self) -> List[str]:
        return [str(a.get("kind")) for a in self.approvals]

    # -- state / wording ------------------------------------------------------------

    @property
    def state_spec(self) -> Dict[str, Any]:
        return self.raw.get("state") or {}

    @property
    def state_file(self) -> str:
        return str(self.state_spec.get("file") or f"{self.name}.json")

    @property
    def messages(self) -> Dict[str, Any]:
        return self.raw.get("messages") or {}

    def message(self, key: str, **values) -> str:
        from .templates import render_formula

        section, _, name = key.partition(".")
        table = self.messages.get(section) or {}
        template = table.get(name)
        if template is None:
            raise PackError(
                f"pack {self.name} defines no message {key!r}"
            )
        return render_formula(str(template), values)

    @property
    def presentation(self) -> Dict[str, Any]:
        return self.raw.get("presentation") or {}

    def __repr__(self) -> str:
        return (
            f"FlowPack(name={self.name!r}, version={self.version!r}, "
            f"digest={self.digest[:19]!r})"
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(data: Dict[str, Any]):
    _check_keys("manifest", data, _TOP_LEVEL_KEYS, _REQUIRED_TOP)
    if data.get("schema") != PACK_SCHEMA:
        raise PackError(
            f"unsupported pack schema {data.get('schema')!r} "
            f"(supported: {PACK_SCHEMA}) — failing closed"
        )
    _check_keys("pack", data["pack"], _SECTION_KEYS["pack"],
                ("name", "version"))
    _check_keys("capitol", data["capitol"], _SECTION_KEYS["capitol"],
                ("workflows",))
    workflows = data["capitol"]["workflows"]
    if not isinstance(workflows, dict) or not workflows:
        raise PackError("capitol.workflows must name at least one workflow")
    for alias, spec in workflows.items():
        _check_keys(f"capitol.workflows.{alias}", spec,
                    _SECTION_KEYS["workflow"])
        if not (spec.get("pin") or spec.get("id") or spec.get("discover")):
            raise PackError(
                f"capitol.workflows.{alias} needs a pin, id, or discover "
                "rule"
            )
        discover = spec.get("discover")
        if discover is not None:
            _check_keys(f"capitol.workflows.{alias}.discover", discover,
                        {"name_contains_any"}, ("name_contains_any",))

    contracts = data.get("contracts")
    if contracts is not None:
        _check_keys("contracts", contracts, _SECTION_KEYS["contracts"])
        for section in ("session", "effect", "rejection", "guidance"):
            body = contracts.get(section)
            if body is not None:
                _check_keys(f"contracts.{section}", body,
                            _SECTION_KEYS[f"contracts.{section}"],
                            ("schema",))

    for index, intake in enumerate(data.get("intakes") or []):
        kind = (intake or {}).get("kind")
        if kind not in _INTAKE_KEYS:
            raise PackError(
                f"intakes[{index}] has unsupported kind {kind!r} "
                "(failing closed)"
            )
        _check_keys(f"intakes[{index}]", intake, _INTAKE_KEYS[kind])
        session = intake.get("session")
        if session is not None:
            _check_keys(f"intakes[{index}].session", session,
                        {"scope", "id_scheme", "dedupe"})
            if session.get("id_scheme") not in (None, "channel-sha16"):
                raise PackError(
                    f"intakes[{index}].session.id_scheme "
                    f"{session.get('id_scheme')!r} is not supported"
                )

    for name, binding in (data.get("bindings") or {}).items():
        _check_keys(f"bindings.{name}", binding, _BINDING_KEYS,
                    ("workflow",))
        if binding["workflow"] not in workflows:
            raise PackError(
                f"bindings.{name} references unknown workflow alias "
                f"{binding['workflow']!r}"
            )
        for mode, spec in (binding.get("modes") or {}).items():
            _check_keys(f"bindings.{name}.modes.{mode}", spec,
                        {"kind", "message", "attachments", "reconcile",
                         "upload", "request"})

    for name, request in (data.get("requests") or {}).items():
        _check_keys(f"requests.{name}", request, _SECTION_KEYS["request"],
                    ("schema", "fields"))
        _validate_value_templates(f"requests.{name}.fields",
                                  request["fields"])
        if request.get("idempotency_key"):
            validate_formula(str(request["idempotency_key"]),
                             f"requests.{name}.idempotency_key")

    flow = data.get("flow")
    if flow is not None:
        _check_keys("flow", flow, _SECTION_KEYS["flow"])
        phases = tuple(flow.get("phases") or ENGINE_PHASES)
        if phases != ENGINE_PHASES:
            raise PackError(
                "flow.phases must match the engine phase machine "
                f"{list(ENGINE_PHASES)} (failing closed)"
            )
        recovery = tuple(flow.get("recovery_order")
                         or ENGINE_RECOVERY_ORDER)
        if recovery != ENGINE_RECOVERY_ORDER:
            raise PackError(
                "flow.recovery_order must match the engine ordering "
                f"{list(ENGINE_RECOVERY_ORDER)} (failing closed)"
            )
        for status, rule in (flow.get("on_status") or {}).items():
            phase = (rule or {}).get("phase")
            if phase == "clarify":
                _check_keys(f"flow.on_status.{status}", rule,
                            _SECTION_KEYS["flow.on_status.clarify"])
                reply_action = rule.get("reply_action")
                if reply_action and reply_action not in (
                    data.get("bindings") or {}
                ):
                    raise PackError(
                        f"flow.on_status.{status}.reply_action "
                        f"{reply_action!r} is not a binding"
                    )
            elif phase == "gate":
                _check_keys(f"flow.on_status.{status}", rule,
                            _SECTION_KEYS["flow.on_status.gate"])
            else:
                raise PackError(
                    f"flow.on_status.{status}.phase {phase!r} is not "
                    "supported (clarify or gate)"
                )

    for index, approval in enumerate(data.get("approvals") or []):
        where = f"approvals[{index}]"
        _check_keys(where, approval, _SECTION_KEYS["approval"],
                    ("kind", "constructs", "pin", "policy_event"))
        if approval["constructs"] not in (data.get("bindings") or {}):
            raise PackError(
                f"{where}.constructs {approval['constructs']!r} is not a "
                "binding"
            )
        caps = approval.get("caps")
        if caps is not None:
            _check_keys(f"{where}.caps", caps,
                        _SECTION_KEYS["approval.caps"])
            for cap_index, check in enumerate(caps.get("checks") or []):
                cap_where = f"{where}.caps.checks[{cap_index}]"
                _check_keys(cap_where, check,
                            _SECTION_KEYS["approval.caps.check"],
                            ("field", "op", "config_key"))
                if check["op"] not in _CAP_OPS:
                    raise PackError(
                        f"{cap_where}.op {check['op']!r} is not supported "
                        f"({', '.join(sorted(_CAP_OPS))})"
                    )
        exact = approval.get("exact")
        if exact is not None:
            _check_keys(f"{where}.exact", exact,
                        _SECTION_KEYS["approval.exact"], ("phrase",))
            if exact.get("challenge"):
                validate_formula(str(exact["challenge"]),
                                 f"{where}.exact.challenge")
        if approval.get("describe"):
            validate_formula(str(approval["describe"]),
                             f"{where}.describe")

    state = data.get("state")
    if state is not None:
        _check_keys("state", state, _SECTION_KEYS["state"])

    for section, table in (data.get("messages") or {}).items():
        if section not in ("channel", "shell"):
            raise PackError(
                f"messages.{section} is not a supported surface "
                "(channel, shell)"
            )
        if not isinstance(table, dict):
            raise PackError(f"messages.{section} must be an object")
        for key, template in table.items():
            validate_formula(str(template), f"messages.{section}.{key}")

    for section, spec in (data.get("presentation") or {}).items():
        if section == "effect":
            for surface, lines in (spec or {}).items():
                validate_line_spec(lines, f"presentation.effect.{surface}")
        elif section in ("channel", "shell"):
            validate_line_spec(spec, f"presentation.{section}")
        else:
            raise PackError(
                f"presentation.{section} is not supported "
                "(channel, shell, effect)"
            )

    acceptance = data.get("acceptance")
    if acceptance is not None:
        _check_keys("acceptance", acceptance,
                    {"kind", "module", "fixtures", "checks", "note"},
                    ("kind",))

    scope = data.get("scope")
    if scope is not None:
        _check_keys("scope", scope, _SECTION_KEYS["scope"])


def load_pack_data(data: Dict[str, Any], *, source: str = "") -> FlowPack:
    """Validate a manifest dict into a :class:`FlowPack` (fail closed)."""
    if not isinstance(data, dict):
        raise PackError("a pack manifest must be a JSON object")
    return FlowPack(data, source=source)
