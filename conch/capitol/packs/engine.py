"""The generic flow-pack engine: use-case behavior as data, one runtime.

This module is the extraction of everything that was generic inside the
bespoke eBay drivers (``conch/capitol/ebay.py`` + ``channel_flow.py``),
per the Conch↔Capitol control design (§3.3/§3.5). The engine implements
the v1 feature set — request templating (E1), the typed-contract walker
(E2), the resumable phase machine with durable parking (E3), thread↔
session binding + message-ts dedupe (E4), attachment validation +
private-artifact upload (E5), chat intake with run reconciliation (E6),
inputs-key discovery (E7), workflow discovery under config pins (E8),
the caps-clamp evaluator (E9), approval classes whose consume constructs
byte-exact requests behind the required-policy gate (E10/E17), run
supervision with persisted cursors (E11), gateway retry-key suffixing
(E16), and durable pack state (E15) — driven entirely by a validated
``conch.flow_pack.v1`` manifest. Nothing in this module names a use
case.

Boundaries (design §3.4): packs are data — the template language is
deterministic and bounded, prompts/wording are pack *values*, and a pack
can never grant tools or authority. Caps clamp outcomes; they never
script reasoning: the engine relays clarification questions verbatim and
evaluates cap checks over outcome fields only. Required policy is
consulted before every effect and fails closed. Inbound message text and
workflow/model text stay untrusted business data — control bytes
stripped, bounded, never a tool or workflow selector; the only inbound
control strings are the origin-bound approval verbs and the literal
HITL continue/stop tokens.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ...config import get_bool
from ...policy import evaluate_required_policy
from ..client import FINAL_STATUS_EVENT, CapitolRuntime
from ..errors import CapitolAuthError, CapitolError, CapitolProtocolError
from .manifest import FlowPack, PackError
from .state import PackState
from .templates import (
    RenderContext,
    clean_text,
    evaluate_expression,
    is_expression,
    is_formula,
    parse_expression,
    render_formula,
    render_inline,
    render_lines,
)

__all__ = [
    "CapsDecision",
    "PackChannelFlow",
    "PackShellFlow",
    "build_request",
    "clean_text",
    "evaluate_caps",
    "extract_contracts",
    "extract_effect_facts",
    "find_contract",
    "render_challenge",
    "request_key",
    "resolve_workflows",
    "start_chat_intake",
    "workflow_inputs_key",
]


# ---------------------------------------------------------------------------
# E2 — contract walker (schema prefix)
# ---------------------------------------------------------------------------

def extract_contracts(value: Any, prefix: str) -> List[Dict[str, Any]]:
    """Recursively collect every object whose ``schema`` starts *prefix*.

    Machine contracts are read from workflow outputs (never parsed out of
    chat prose, where the gateway redacts hashes).
    """
    found: List[Dict[str, Any]] = []

    def walk(node: Any):
        if isinstance(node, dict):
            schema = node.get("schema")
            if isinstance(schema, str) and schema.startswith(prefix):
                found.append(node)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def find_contract(value: Any, schema: str,
                  prefix: str = "") -> Optional[Dict[str, Any]]:
    for contract in extract_contracts(value, prefix or schema):
        if contract.get("schema") == schema:
            return contract
    return None


def _dig(node: Any, path: List[str]) -> Any:
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


# ---------------------------------------------------------------------------
# E9 — caps-clamp evaluator (data-driven checks; clamps clamp, never guess)
# ---------------------------------------------------------------------------

class CapsDecision:
    """Outcome of the clamp for one drafted contract."""

    def __init__(self, auto: bool, reasons: List[str]):
        self.auto = bool(auto)
        self.reasons = list(reasons)

    def __repr__(self) -> str:
        return f"CapsDecision(auto={self.auto}, reasons={self.reasons!r})"


def evaluate_caps(pack: FlowPack, config: dict,
                  contract: Dict[str, Any]) -> CapsDecision:
    """Evaluate the pack's cap checks over one contract's outcome fields.

    When a configured cap cannot read its outcome, the clamp routes to
    exact approval — clamps clamp; they never guess.
    """
    approval = pack.approval() or {}
    caps = approval.get("caps") or {}
    contract = contract or {}
    reasons: List[str] = []
    toggle = str(caps.get("toggle_key") or "")
    if toggle and not get_bool(config, toggle, True):
        reasons.append(f"auto-publish is disabled ({toggle}=false)")
    for check in caps.get("checks") or []:
        raw = str(config.get(check["config_key"]) or "").strip()
        if not raw:
            if check.get("skip_when_unset"):
                continue
            reasons.append(f"{check['config_key']} is not configured")
            continue
        value = _dig(contract, str(check["field"]).split("."))
        label = str(
            check.get("label") or str(check["field"]).rsplit(".", 1)[-1]
        )
        if check["op"] == "in_csv":
            allowed = {c.strip() for c in raw.split(",") if c.strip()}
            current = str(value or "").strip()
            if current not in allowed:
                reasons.append(
                    f"{label} {current or '(none)'} is outside the "
                    f"allowlist ({', '.join(sorted(allowed))})"
                )
        elif check["op"] == "lte_float":
            try:
                ceiling: Optional[float] = float(raw)
            except ValueError:
                ceiling = None
                reasons.append(
                    f"{check['config_key']}={raw!r} is not a number"
                )
            if ceiling is not None:
                try:
                    current_value: Optional[float] = float(value)
                except (TypeError, ValueError):
                    current_value = None
                if current_value is None:
                    reasons.append(
                        f"the {pack.session_contract_name} has no "
                        f"readable {label} to clamp"
                    )
                elif current_value > ceiling:
                    unit = str(check.get("unit") or "")
                    reasons.append(
                        f"{label} {current_value:.2f}"
                        f"{' ' + unit if unit else ''} is above the "
                        f"{ceiling:.2f} ceiling"
                    )
    return CapsDecision(not reasons, reasons)


# ---------------------------------------------------------------------------
# E8 — workflow discovery under config pins
# ---------------------------------------------------------------------------

def _resolve_scalar(expr: Any, config: dict) -> str:
    if is_expression(expr):
        ctx = RenderContext(config=config)
        return str(
            evaluate_expression(parse_expression(expr), ctx) or ""
        ).strip()
    return str(expr or "").strip()


def _pin_config_key(expr: Any, alias: str) -> str:
    """The config key inside a ``${config.KEY:…}`` pin, for error hints."""
    if is_expression(expr):
        try:
            spec = parse_expression(expr)
        except CapitolError:
            return alias
        if spec.get("root") == "config" and spec.get("path"):
            return ".".join(spec["path"])
    return alias


def resolve_workflows(runtime: CapitolRuntime, pack: FlowPack,
                      config: dict) -> Dict[str, str]:
    """Locate the pack's workflows on the agent's allowlist.

    Config pins win; otherwise the pack's ``discover.name_contains_any``
    rules match by name (each listed workflow claims at most one alias,
    in the pack's declared alias order). ``list_workflows`` returns the
    id ``call_workflow`` expects, so the discovered value is passed back
    verbatim.
    """
    resolved: Dict[str, str] = {}
    for alias, spec in pack.workflows.items():
        pin = _resolve_scalar(spec.get("pin") or "", config)
        if not pin and spec.get("id"):
            pin = _resolve_scalar(spec.get("id"), config)
        resolved[alias] = pin
    if all(resolved.values()):
        return resolved
    workflows = runtime.list_workflows()
    for workflow in workflows:
        name = str(workflow.get("name") or "").lower()
        identifier = str(
            workflow.get("workflow_id") or workflow.get("id") or ""
        )
        if not identifier:
            continue
        for alias, spec in pack.workflows.items():
            if resolved.get(alias):
                continue
            needles = [
                str(n).lower()
                for n in (spec.get("discover") or {}).get(
                    "name_contains_any"
                ) or []
            ]
            if any(needle in name for needle in needles):
                resolved[alias] = identifier
                break
    if not all(resolved.values()):
        names = ", ".join(
            clean_text(w.get("name"), 80) or "?" for w in workflows
        ) or "(none)"
        aliases = "/".join(pack.workflow_aliases())
        pin_keys = " / ".join(
            _pin_config_key(spec.get("pin"), alias)
            for alias, spec in pack.workflows.items()
        )
        raise CapitolError(
            f"could not locate the {aliases} workflows on this "
            f"agent's allowlist (saw: {names}); pin them with "
            f"{pin_keys}"
        )
    return resolved


# ---------------------------------------------------------------------------
# E7 — inputs-key discovery
# ---------------------------------------------------------------------------

def workflow_inputs_key(
    runtime: CapitolRuntime,
    workflow_id: str,
    cache: Optional[Dict[str, str]] = None,
    *,
    strict: bool = False,
) -> str:
    """Canonical inputs key for the workflow's request-input node.

    Preference order: the JSON input node (``field_id == "value"``), then
    exactly one text input node (``field_id == "text_input"`` — verified
    live: multi-field workflows like together-funding-ingest expose their
    window as ``{node}.text_input`` among many tool-config fields), then
    a single overridable field. With ``strict`` the unresolvable case
    raises naming the required fields' canonical keys instead of
    defaulting to ``"value"`` (which multi-field workflows reject).
    """
    if cache is not None and workflow_id in cache:
        return cache[workflow_id]
    key = "value"
    resolved = False
    fields: List[Dict[str, Any]] = []
    try:
        details = runtime.describe_workflow(workflow_id) or {}
        fields = [
            field for field in details.get("fields") or []
            if isinstance(field, dict)
        ]
        value_fields = [
            field for field in fields
            if str(field.get("field_id")) == "value"
        ]
        text_fields = [
            field for field in fields
            if str(field.get("field_id")) == "text_input"
        ]
        target = None
        if value_fields:
            target = value_fields[0]
        elif len(text_fields) == 1:
            target = text_fields[0]
        elif len(fields) == 1:
            target = fields[0]
        if target:
            node = str(target.get("node_instance_id") or "").strip()
            field_id = str(target.get("field_id") or "value")
            key = f"{node}.{field_id}" if node else field_id
            resolved = True
    except CapitolError:
        if strict:
            raise
    if strict and not resolved:
        required = [
            f"{str(field.get('node_instance_id') or '').strip()}."
            f"{field.get('field_id')}"
            for field in fields
            if field.get("required")
        ]
        raise CapitolError(
            f"workflow {workflow_id} has no single request-input node "
            f"({len(fields)} overridable fields) — pass the full inputs "
            "map keyed by '<node_instance_id>.<field_id>' from the "
            "describe fields"
            + (f"; required: {', '.join(required)}" if required else "")
        )
    if cache is not None:
        cache[workflow_id] = key
    return key


# ---------------------------------------------------------------------------
# E1 — deterministic request construction from templates
# ---------------------------------------------------------------------------

class _Deferred:
    """A formula field resolved in pass 2 over the rendered request."""

    def __init__(self, template: str):
        self.template = template


def _render_value(value: Any, ctx: RenderContext, where: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _render_value(item, ctx, f"{where}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _render_value(item, ctx, f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if is_expression(value):
            return evaluate_expression(
                parse_expression(value), ctx, where=where
            )
        if is_formula(value):
            return _Deferred(value)
    return value


def _apply_deferred(node: Any, request: Dict[str, Any]) -> Any:
    """Resolve pass-2 formula fields in place-ish: containers are only
    copied where a formula actually resolved, so values echoed verbatim
    from the contract (``${contract}``) keep their identity."""
    if isinstance(node, _Deferred):
        return render_formula(node.template, request)
    if isinstance(node, dict):
        replaced: Optional[Dict[str, Any]] = None
        for key, item in node.items():
            resolved = _apply_deferred(item, request)
            if resolved is not item:
                if replaced is None:
                    replaced = dict(node)
                replaced[key] = resolved
        return replaced if replaced is not None else node
    if isinstance(node, list):
        replaced_list: Optional[List[Any]] = None
        for index, item in enumerate(node):
            resolved = _apply_deferred(item, request)
            if resolved is not item:
                if replaced_list is None:
                    replaced_list = list(node)
                replaced_list[index] = resolved
        return replaced_list if replaced_list is not None else node
    return node


def build_request(
    pack: FlowPack,
    name: str,
    *,
    config: dict,
    session: Optional[Dict[str, Any]] = None,
    intake_text: Optional[str] = None,
    contract: Optional[Dict[str, Any]] = None,
    unique_id: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """Render one request template into the exact typed request.

    ``from_contract`` templates echo their ``require`` fields verbatim
    from the pinned immutable contract — a missing field refuses to
    construct (fail closed), so lineage cannot drift. Formula fields
    (challenge/confirmation/idempotency-key strings) resolve against the
    rendered request in a second pass, so what the user approves is
    byte-identical to what the effect validates.
    """
    spec = pack.request_spec(name)
    contract_ref = str(spec.get("from_contract") or "")
    if contract_ref:
        if not isinstance(contract, dict) or not contract:
            raise CapitolError(
                f"the {name!r} request needs the current "
                f"{pack.session_contract_name} contract; refusing to "
                "construct without one"
            )
        for field in spec.get("require") or []:
            value = contract.get(field)
            if value is None or value == "":
                raise CapitolError(
                    f"the current {pack.session_contract_name} contract "
                    f"is missing {field!r}; refusing to construct the "
                    f"{name!r} request"
                )
    ctx = RenderContext(
        config=config,
        session=session or {},
        intake={"text": intake_text},
        contract=contract,
        unique_id=unique_id,
    )
    rendered = _render_value(spec["fields"], ctx, f"requests.{name}")
    if ctx.missing_required:
        raise CapitolError(
            f"the {name!r} request requires config keys that are not "
            "set: " + ", ".join(ctx.missing_required)
        )
    request: Dict[str, Any] = {"schema": spec["schema"]}
    request.update(rendered)
    return _apply_deferred(request, request)


def request_key(pack: FlowPack, name: str,
                request: Dict[str, Any]) -> str:
    """The request template's idempotency-key formula over the rendered
    request (caller-supplied keys are the wire contract)."""
    spec = pack.request_spec(name)
    template = str(spec.get("idempotency_key") or "")
    if not template:
        return str(request.get("idempotency_key") or "")
    return render_formula(template, request)


def gateway_key(pack: FlowPack, binding_name: str,
                request: Dict[str, Any], attempt: int = 1) -> str:
    """The gateway call key for a binding: the request key, plus the
    binding's retry suffix on re-attempts (E16). The *embedded* contract
    key never varies; effectively-once stays guaranteed by the effect
    ledger on the embedded key."""
    binding = pack.binding(binding_name)
    spec = binding.get("gateway_key") or {}
    base_ref = str(spec.get("base") or "")
    if base_ref.startswith("request."):
        base = str(request.get(base_ref[len("request."):]) or "")
    else:
        base = request_key(pack, str(binding.get("request") or ""), request)
    suffix = ""
    if attempt > 1 and spec.get("retry_suffix"):
        suffix = render_formula(str(spec["retry_suffix"]),
                                {"attempt": attempt})
    return base + suffix


def render_challenge(pack: FlowPack, contract: Dict[str, Any]) -> str:
    """The approval class's exact challenge formula over the contract."""
    approval = pack.approval() or {}
    template = str((approval.get("exact") or {}).get("challenge") or "")
    return render_formula(template, contract) if template else ""


# ---------------------------------------------------------------------------
# Effect facts
# ---------------------------------------------------------------------------

def extract_effect_facts(pack: FlowPack,
                         effect: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the effect contract's declared facts: each fact path is
    read at its path, falling back to the top-level last segment."""
    facts: Dict[str, Any] = {}
    for path in pack.effect_facts:
        parts = path.split(".")
        value = _dig(effect, parts)
        if not value and len(parts) > 1:
            value = effect.get(parts[-1])
        facts[parts[-1]] = value
    return facts


def effect_succeeded(pack: FlowPack, facts: Dict[str, Any]) -> bool:
    success = (pack.contracts.get("effect") or {}).get("success") or {}
    fact = str(success.get("fact") or "")
    expected = str(success.get("equals_ci") or "")
    if not fact or not expected:
        return False
    return str(facts.get(fact) or "").upper() == expected.upper()


# ---------------------------------------------------------------------------
# E6 — chat intake with run reconciliation
# ---------------------------------------------------------------------------

def start_chat_intake(
    runtime: CapitolRuntime,
    workflow_id: str,
    message: str,
    attachment_paths: List[str],
    say: Callable[[str], None],
    *,
    alias: str = "draft",
    attempts: int = 6,
    delay_seconds: float = 5.0,
) -> tuple:
    """Launch the initial run over chat FileParts; return (run_id, reply).

    Image FileParts are the one A2A upload the gateway promotes into
    durable org artifacts. If the blocking chat turn outlives its
    transport, the launched run is reconciled from the conversation's
    run history instead of resending the intake (a resend would start a
    second run). The assistant reply is untrusted prose; contracts are
    read from run outputs, never parsed out of chat text.
    """
    reply = ""
    payload: Dict[str, Any] = {}
    try:
        payload = runtime.chat(message, files=attachment_paths) or {}
        reply = clean_text(payload.get("assistant_reply"), 600)
    except (CapitolAuthError, CapitolProtocolError):
        raise
    except CapitolError:
        say("  chat transport dropped; reconciling run state …")
    run_id = str(payload.get("run_id") or "")
    for _attempt in range(int(attempts)):
        if run_id:
            break
        listing = runtime.list_runs(workflow_id, limit=10)
        for run in listing.get("runs") or []:
            context = str(
                run.get("started_by_context") or run.get("context_id") or ""
            )
            if context and context == runtime.context_id:
                run_id = str(run.get("run_id") or "")
                break
        if not run_id:
            time.sleep(delay_seconds)
    if not run_id:
        raise CapitolError(
            f"the agent did not start a {alias} run"
            + (f" — it said: {reply}" if reply else "")
        )
    return run_id, reply


# ---------------------------------------------------------------------------
# Shared pack-flow plumbing
# ---------------------------------------------------------------------------

REQUIRED_CHANNEL_MESSAGES = frozenset({
    "restart_hint", "auth_fix_hint", "step_failed", "retry_hint",
    "started_notify", "duplicate_intake", "extra_attachments_note",
    "done", "failed", "interrupted", "no_contract", "clarify_exhausted",
    "revising_notify", "hitl_lost", "clarify_header", "unexpected_status",
    "auto_effect_notify", "channel_auto_off_reason", "approval_header",
    "approval_reply_hint", "deny", "approval_missing_session",
    "approval_stale", "stale_rearm_reason", "expired_rearm_reason",
    "expired_reissue_intro", "approve_auth_failed", "prepublish_failed",
    "policy_denied", "effect_failed", "gate_rejected",
    "no_effect_contract",
})

REQUIRED_SHELL_MESSAGES = frozenset({
    "session_started", "uploading", "attaching", "agent_reply",
    "clarify_header", "clarify_exhausted", "caps_auto", "confirm_effect",
    "declined", "phrase_mismatch", "attachment_missing",
    "attachment_too_many", "attachment_not_found", "attachment_bad_type",
    "attachment_too_big",
})


class _PackFlowBase:
    """Pack lookups both flow surfaces share."""

    pack: FlowPack
    config: dict

    def _init_pack(self, pack: FlowPack, surface: str):
        self.pack = pack
        gate = pack.gate_rule() or {}
        approval_kind = str(gate.get("approval") or "")
        self.approval_spec = pack.approval(approval_kind) or {}
        if not self.approval_spec:
            raise PackError(
                f"pack {pack.name} declares no approval class for its "
                "gate (failing closed)"
            )
        clarify = pack.clarify_rule() or {}
        self.clarify_status = pack.status_for_phase("clarify")
        self.gate_status = pack.status_for_phase("gate")
        self.questions_field = str(
            clarify.get("questions_field") or "open_questions"
        )
        self.max_clarify_rounds = pack.max_clarify_rounds
        self.revise_binding = str(clarify.get("reply_action") or "")
        self.effect_binding = str(self.approval_spec.get("constructs"))
        self.effect_alias = str(
            pack.binding(self.effect_binding)["workflow"]
        )
        self.contract_key = pack.session_contract_name
        self.status_field = pack.status_field
        self.identity_fields = pack.identity_fields
        self.effect_field = str(
            pack.state_spec.get("effect_field") or "effect"
        )
        self.link_fact = str(
            (pack.contracts.get("effect") or {}).get("link_fact") or ""
        )
        required = (REQUIRED_CHANNEL_MESSAGES if surface == "channel"
                    else REQUIRED_SHELL_MESSAGES)
        table = pack.messages.get(surface) or {}
        missing = sorted(required - set(table))
        if missing:
            raise PackError(
                f"pack {pack.name} is missing messages.{surface} entries "
                "(failing closed): " + ", ".join(missing)
            )
        collections = pack.state_spec.get("collections") or {}
        history = [name for name in collections if name != "runs"]
        self.history_collection = history[0] if history else ""
        self.history_fields = (
            [str(f) for f in collections.get(self.history_collection) or []]
            if self.history_collection else []
        )

    # -- shared helpers -------------------------------------------------------

    def _intake_spec(self, kind: str) -> Dict[str, Any]:
        return self.pack.intake(kind) or {}

    def _attachments_spec(self) -> Dict[str, Any]:
        for intake in self.pack.intakes:
            if intake.get("attachments"):
                return intake["attachments"]
        return {}

    def _context_default(self) -> str:
        for intake in self.pack.intakes:
            if intake.get("context_default"):
                return str(intake["context_default"])
        return ""

    def _initial_binding_name(self) -> str:
        for intake in self.pack.intakes:
            if intake.get("start"):
                return str(intake["start"])
        raise PackError(f"pack {self.pack.name} declares no intake start")

    def _extract_contract_from(self, output: Dict[str, Any],
                               initial_alias: str) -> Dict[str, Any]:
        contract = find_contract(output, self.pack.session_schema,
                                 self.pack.contract_prefix)
        if contract is None:
            guidance = find_contract(output, self.pack.guidance_schema,
                                     self.pack.contract_prefix)
            detail = ""
            if guidance:
                detail = clean_text(
                    (guidance.get("error_detail") or {}).get("message")
                    or guidance.get("message"), 300,
                )
            raise CapitolError(
                f"{initial_alias} run produced no "
                f"{self.pack.session_schema} contract"
                + (f" (workflow guidance: {detail})" if detail else "")
            )
        return contract

    def _record_history(self, state: PackState, session_id: str,
                        contract: Dict[str, Any]):
        if self.history_collection:
            state.append(session_id, self.history_collection, {
                field: contract.get(field) for field in self.history_fields
            })

    def _challenge(self, contract: Dict[str, Any]) -> str:
        return render_challenge(self.pack, contract)

    def _policy_payload(self, workflow_id: str, request: Dict[str, Any],
                        caps_auto: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"workflow_id": workflow_id}
        for field in self.approval_spec.get("policy_fields") or []:
            payload[field] = request.get(field)
        payload["idempotency_key"] = request.get("idempotency_key")
        payload["caps_auto"] = bool(caps_auto)
        return payload

    def _effect_reply(self, surface: str, facts: Dict[str, Any]) -> str:
        spec = (self.pack.presentation.get("effect") or {}).get(surface)
        if not spec:
            raise PackError(
                f"pack {self.pack.name} has no presentation.effect."
                f"{surface}"
            )
        return render_inline(spec, facts)


# ---------------------------------------------------------------------------
# The channel surface (message-first sessions over channel threads)
# ---------------------------------------------------------------------------

class PackChannelFlow(_PackFlowBase):
    """Drives message-first pack sessions over channel threads.

    A resumable state machine rather than a blocking conversation: each
    inbound message advances exactly one step and every step ends parked
    in durable state (``clarify`` — a terminal clarify-status contract
    awaits answers; ``hitl`` — a run is parked server-side on
    ``node.input_required``; ``awaiting_approval`` — a drafted contract
    awaits the origin-bound effect decision). No thread ever waits in
    memory for a human.

    Approval semantics: the store entry pins the immutable contract
    identity; consuming it *constructs* the exact effect request from
    that contract — never runs a command. Two independent staleness
    checks run in series: the engine refuses when the pinned identity no
    longer matches the session's current contract, and the Capitol-side
    approval node re-verifies the same identity plus the challenge/
    confirmation/idempotency-key formulas before any provider call. Any
    new contract revision drops the pending approval. Within-caps
    auto-effect over a channel additionally requires the pack's explicit
    per-channel opt-in key (default: every channel effect takes the
    approval path).
    """

    def __init__(self, pack: FlowPack, config: dict, approvals,
                 notify: Callable[[str, str, str], Any], *,
                 state: Optional[PackState] = None):
        self.config = config or {}
        self.approvals = approvals
        self.notify = notify
        self._init_pack(pack, "channel")
        self.state = state or PackState(filename=pack.state_file)
        self._input_keys: Dict[str, str] = {}
        self.intake = self._intake_spec("channel_message")
        if not self.intake:
            raise PackError(
                f"pack {pack.name} declares no channel_message intake"
            )
        self.channel = str(self.intake.get("channel") or "")
        self.initial_binding = self.pack.binding(
            str(self.intake.get("start"))
        )
        self.initial_alias = str(self.initial_binding["workflow"])
        self._restart_hint = self._msg("restart_hint")

    # -- wording ---------------------------------------------------------------

    def _msg(self, key: str, **values) -> str:
        values.setdefault("restart_hint", getattr(
            self, "_restart_hint", ""
        ))
        return self.pack.message(f"channel.{key}", **values)

    # -- gating ---------------------------------------------------------------

    def enabled(self) -> bool:
        enabled_key = str(self.intake.get("enabled_key") or "")
        return bool(
            str(self.config.get("capitol_base_url") or "").strip()
            and (not enabled_key
                 or get_bool(self.config, enabled_key, True))
        )

    # -- runtime --------------------------------------------------------------

    def _runtime(self, session: Optional[Dict[str, Any]] = None
                 ) -> CapitolRuntime:
        """A fresh runtime per step: the bearer is resolved at call time
        (never stored) and the session's A2A context is re-threaded so
        audit attribution stays continuous across restarts."""
        runtime = CapitolRuntime.from_config(self.config)
        runtime.discover()
        context = str((session or {}).get("context_id") or "")
        if context:
            runtime.context_id = context
        return runtime

    # -- entry points ----------------------------------------------------------

    def _mime_matches(self, mime: str) -> bool:
        for pattern in (self.intake.get("match") or {}).get(
            "attachments_mime"
        ) or []:
            pattern = str(pattern)
            if pattern.endswith("/*"):
                if mime.startswith(pattern[:-1]):
                    return True
            elif mime == pattern:
                return True
        return False

    def handle_message(self, message) -> Optional[str]:
        """One inbound (already allowlisted) message → one flow step.

        Returns the in-thread reply, or None when the message is not for
        this flow (it then falls through to the normal remote turn).
        """
        key = f"{message.channel}:{message.thread_id}"
        session_id = self.state.thread_session(key)
        session = self.state.session(session_id) if session_id else None
        if session_id and session is None:
            session_id = None  # dangling binding: recover via a fresh start
        images = [
            attachment
            for attachment in getattr(message, "attachments", None) or []
            if self._mime_matches(str(attachment.mime_type))
        ]
        if not session_id:
            if not images:
                return None
            if message.channel != self.channel or not self.enabled():
                return None
        try:
            if session_id:
                return self._steer(key, session_id, session, message,
                                   images)
            return self._start_session(key, message, images)
        except CapitolAuthError as exc:
            if session_id:
                self._recover_phase(session_id)
            return (
                f"Capitol credential needed: {clean_text(exc, 400)} — "
                + self._msg("auth_fix_hint")
            )
        except CapitolError as exc:
            hint = clean_text(getattr(exc, "hint", ""), 200)
            recovered = (
                self._recover_phase(session_id) if session_id else ""
            )
            if not session_id or recovered == "failed":
                tail = f" {self._restart_hint}"
            else:
                tail = " " + self._msg("retry_hint")
            return self._msg(
                "step_failed",
                error=clean_text(exc, 500),
                hint_part=f" ({hint})" if hint else "",
                tail=tail,
            )

    def handle_approval(self, request_id: int, entry: Dict[str, Any],
                        verb: str, message) -> str:
        """Consume a pack approval (already origin-checked and popped by
        the store): deny drops it; approve constructs the exact effect
        request from the pinned contract — after re-checking that the
        pin still matches the session's current contract."""
        payload = entry.get("payload") or {}
        session_id = str(payload.get("session_id") or "")
        key = str(payload.get("thread_key")
                  or f"{message.channel}:{message.thread_id}")
        session = self.state.session(session_id)
        if verb == "deny":
            if session is not None:
                self.state.update_session(session_id, approval_id=None)
            return self._msg("deny", id=request_id)
        if session is None:
            return self._msg("approval_missing_session", id=request_id)
        contract = session.get(self.contract_key) or {}
        pin_fields = [
            pin[len("contract."):]
            for pin in self.approval_spec.get("pin") or []
            if str(pin).startswith("contract.")
        ]
        stale = int(session.get("approval_id") or 0) != int(request_id)
        for field in pin_fields:
            pinned = payload.get(field)
            current = contract.get(field)
            if pinned in (None, "") or current in (None, "") or (
                str(pinned) != str(current)
            ):
                stale = True
        if stale:
            self.state.update_session(session_id, approval_id=None)
            fresh = ""
            if str(contract.get(self.status_field) or "") == (
                self.gate_status
            ):
                fresh = "\n" + self._request_approval(
                    key, session_id, contract,
                    [self._msg("stale_rearm_reason")],
                )
            return self._msg("approval_stale", id=request_id) + fresh
        self.state.update_session(session_id, approval_id=None)
        try:
            caps = evaluate_caps(self.pack, self.config, contract)
            return self._effect(
                key, session_id, contract,
                caps_auto=caps.auto,
                approval_context={
                    "id": int(request_id),
                    "channel": str(message.channel),
                    "thread_id": str(message.thread_id),
                    "sender": str(message.sender),
                },
            )
        except CapitolAuthError as exc:
            self._recover_phase(session_id)
            return self._msg("approve_auth_failed",
                             error=clean_text(exc, 400))
        except CapitolError as exc:
            self._recover_phase(session_id)
            return self._msg("prepublish_failed",
                             error=clean_text(exc, 500))

    def reissue_expired(self, message) -> Optional[str]:
        """An expired approval verb landed in a thread whose session still
        awaits approval: mint a fresh origin-bound approval for the
        (unchanged) current contract."""
        key = f"{message.channel}:{message.thread_id}"
        session_id = self.state.thread_session(key)
        if not session_id:
            return None
        session = self.state.session(session_id)
        if not session or session.get("phase") != "awaiting_approval":
            return None
        contract = session.get(self.contract_key)
        hash_field = self.identity_fields[-1] if self.identity_fields \
            else ""
        if not isinstance(contract, dict) or not contract.get(hash_field):
            return None
        fresh = self._request_approval(
            key, session_id, contract,
            [self._msg("expired_rearm_reason")],
        )
        return self._msg("expired_reissue_intro") + "\n" + fresh

    # -- session start ----------------------------------------------------------

    def _start_session(self, key: str, message, images: List[Any]) -> str:
        attachments_spec = self._attachments_spec()
        max_count = int(attachments_spec.get("max_count") or 12)
        photos = images[:max_count]
        session_id = f"{self.channel}-" + hashlib.sha256(
            f"{key}:{message.ts}".encode("utf-8")
        ).hexdigest()[:16]
        if self.state.session(session_id) is not None:
            # Same message delivered twice (crash between binding and
            # session write, or a cursor replay): never start a second
            # run for the same message ts.
            self.state.bind_thread(key, session_id)
            return self._msg("duplicate_intake", session_id=session_id)
        self.state.bind_thread(key, session_id)
        notes = clean_text(message.text, 4000).strip()
        item_context = notes or self._context_default()
        self.state.update_session(
            session_id,
            phase="drafting",
            channel=str(message.channel),
            thread_id=str(message.thread_id),
            sender=str(message.sender),
            origin_ts=str(message.ts),
            item_context=item_context,
            photos=[a.path for a in photos],
        )
        runtime = self._runtime()
        workflows = resolve_workflows(runtime, self.pack, self.config)
        mode_key = str(self.initial_binding.get("mode_key") or "")
        default_mode = str(
            self.initial_binding.get("default_mode") or "chat"
        )
        intake_mode = str(
            (self.config.get(mode_key) if mode_key else "")
            or default_mode
        ).strip().lower()
        runtime.handshake()  # fresh A2A context per session
        self.state.update_session(
            session_id,
            workflows=workflows,
            context_id=runtime.context_id,
            intake=intake_mode,
        )
        self.notify(
            self._msg("started_notify", count=len(photos),
                      session_id=session_id),
            message.channel, message.thread_id,
        )
        outcome, value = self._initial_run(
            runtime, session_id, key, workflows,
            [a.path for a in photos], item_context, intake_mode,
        )
        if outcome == "parked":
            return value
        contract = self._extract_contract(session_id, value)
        return self._after_contract(key, session_id, contract)

    def _initial_run(
        self,
        runtime: CapitolRuntime,
        session_id: str,
        key: str,
        workflows: Dict[str, str],
        attachment_paths: List[str],
        item_context: str,
        intake_mode: str,
    ) -> tuple:
        modes = self.initial_binding.get("modes") or {}
        mode = modes.get(intake_mode) or {}
        if mode.get("kind") == "typed_request":
            media = []
            for index, path in enumerate(attachment_paths):
                uploaded = runtime.upload_artifact(path)
                uploaded["order"] = index
                media.append(uploaded)
            self.state.update_session(session_id, media=[
                {k: item[k]
                 for k in ("artifact_id", "digest", "order", "filename")}
                for item in media
            ])
            request_name = str(mode.get("request"))
            request = build_request(
                self.pack, request_name,
                config=self.config,
                session={"id": session_id, "thread_key": key,
                         "media": media},
                intake_text=item_context,
            )
            return self._run_typed(
                runtime, session_id, self.initial_alias,
                workflows[self.initial_alias], request,
                request_key(self.pack, request_name, request),
            )
        chat_spec = modes.get("chat") or {}
        chat_message = render_formula(
            str(chat_spec.get("message") or "{item_context}"),
            {"item_context": item_context},
        )
        reconcile = chat_spec.get("reconcile") or {}
        run_id, _reply = start_chat_intake(
            runtime, workflows[self.initial_alias], chat_message,
            attachment_paths, say=lambda _text: None,
            alias=self.initial_alias,
            attempts=int(reconcile.get("attempts") or 6),
            delay_seconds=float(reconcile.get("delay_seconds") or 5),
        )
        self.state.record_run(session_id, self.initial_alias, run_id, "")
        return self._supervise(runtime, session_id, self.initial_alias,
                               run_id)

    # -- steering (replies in a bound thread) -------------------------------------

    def _steer(self, key: str, session_id: str, session: Dict[str, Any],
               message, images: List[Any]) -> Optional[str]:
        phase = str(session.get("phase") or "")
        note = ""
        if images and phase not in ("done", "failed"):
            note = self._msg("extra_attachments_note")
        if phase in ("clarify", "awaiting_approval"):
            return note + self._revise(key, session_id, session, message)
        if phase == "hitl":
            return note + self._resume_hitl(key, session_id, session,
                                            message)
        if phase == "done":
            facts = session.get(self.effect_field) or {}
            url = clean_text(facts.get(self.link_fact), 200) if (
                self.link_fact
            ) else ""
            return self._msg(
                "done", url_part=f": {url}" if url else "."
            )
        if phase == "failed":
            return self._msg("failed")
        # drafting/publishing: only reachable when a step was interrupted
        # mid-run (crash) — the in-memory watch is gone.
        return self._msg("interrupted")

    def _revise(self, key: str, session_id: str,
                session: Dict[str, Any], message) -> str:
        contract = session.get(self.contract_key)
        if not isinstance(contract, dict) or not contract:
            self.state.update_session(session_id, phase="failed")
            return self._msg("no_contract")
        clarifying = str(session.get("phase")) == "clarify"
        rounds = int(session.get("clarify_rounds") or 0)
        if clarifying and rounds >= self.max_clarify_rounds:
            self.state.update_session(session_id, phase="failed")
            return self._msg("clarify_exhausted",
                             rounds=self.max_clarify_rounds)
        # A new contract revision invalidates any pending approval.
        if self.pack.revision_invalidates_approval:
            previous_approval = session.get("approval_id")
            if previous_approval:
                self.approvals.pop(int(previous_approval))
                self.state.update_session(session_id, approval_id=None)
        feedback = clean_text(message.text, 4000).strip()
        binding = self.pack.binding(self.revise_binding)
        request_name = str(binding.get("request"))
        request = build_request(
            self.pack, request_name,
            config=self.config,
            session={"id": session_id, "thread_key": key,
                     "item_context": session.get("item_context")},
            intake_text=feedback,
            contract=contract,
        )
        runtime = self._runtime(session)
        workflows = dict(session.get("workflows") or {}) or (
            resolve_workflows(runtime, self.pack, self.config)
        )
        self.state.update_session(
            session_id,
            phase="drafting",
            clarify_rounds=rounds + 1 if clarifying else rounds,
        )
        self.notify(self._msg("revising_notify"), message.channel,
                    message.thread_id)
        alias = str(binding["workflow"])
        outcome, value = self._run_typed(
            runtime, session_id, alias, workflows[alias], request,
            request_key(self.pack, request_name, request),
        )
        if outcome == "parked":
            return value
        new_contract = self._extract_contract(session_id, value)
        return self._after_contract(key, session_id, new_contract)

    def _resume_hitl(self, key: str, session_id: str,
                     session: Dict[str, Any], message) -> str:
        hitl = dict(session.get("hitl") or {})
        run_id = str(hitl.get("run_id") or "")
        if not run_id:
            self.state.update_session(session_id, phase="failed",
                                      hitl=None)
            return self._msg("hitl_lost")
        answer = clean_text(message.text, 2000).strip()
        runtime = self._runtime(session)
        if str(hitl.get("input_kind") or "") == "clarification":
            runtime.submit_clarification(
                run_id, str(hitl.get("request_id") or ""), answer,
                declined=not answer,
            )
        else:
            token = answer.lower()
            if token not in ("continue", "stop"):
                return ("This run is waiting on a checkpoint: reply "
                        "exactly 'continue' or 'stop'.")
            runtime.submit_intervention(
                run_id, str(hitl.get("node_id") or ""),
                str(hitl.get("request_id") or ""), token,
            )
        kind = str(hitl.get("kind") or self.initial_alias)
        self.state.update_session(session_id, phase="drafting", hitl=None)
        outcome, value = self._supervise(
            runtime, session_id, kind, run_id,
            since=int(hitl.get("last_sequence") or 0) + 1,
        )
        if outcome == "parked":
            return value
        if kind == self.effect_alias:
            return self._finish_effect(session_id, value)
        contract = self._extract_contract(session_id, value)
        return self._after_contract(key, session_id, contract)

    # -- run supervision -----------------------------------------------------------

    def _run_typed(self, runtime: CapitolRuntime, session_id: str,
                   kind: str, workflow_id: str, request: Dict[str, Any],
                   idempotency_key: str) -> tuple:
        inputs = {
            workflow_inputs_key(runtime, workflow_id, self._input_keys):
            request
        }
        submission = runtime.call_workflow(
            workflow_id, inputs, idempotency_key=idempotency_key
        )
        run_id = str(submission["run_id"])
        self.state.record_run(session_id, kind, run_id, idempotency_key)
        return self._supervise(runtime, session_id, kind, run_id)

    def _supervise(self, runtime: CapitolRuntime, session_id: str,
                   kind: str, run_id: str, since: int = 0) -> tuple:
        """Watch a run to terminal, or park on ``node.input_required``.

        Returns ``("output", output)`` or ``("parked", reply_text)``. The
        parked run waits durably server-side; the persisted sequence
        cursor lets the resume watch continue without replay or loss.
        """
        final_state = ""
        last_sequence = max(0, int(since) - 1)
        for event in runtime.watch_run(run_id, since_sequence=since):
            event_type = str(event.get("event_type") or "")
            sequence = event.get("sequence")
            if isinstance(sequence, (int, float)):
                last_sequence = int(sequence)
                self.state.update_run(
                    session_id, run_id, last_sequence=last_sequence
                )
            if event_type == "node.input_required":
                return "parked", self._park_hitl(
                    session_id, kind, run_id, event, last_sequence
                )
            if event_type == FINAL_STATUS_EVENT:
                final_state = str(
                    (event.get("data") or {}).get("state") or ""
                )
        status = runtime.run_status(run_id)
        run_state = str((status or {}).get("status") or final_state or "")
        self.state.update_run(session_id, run_id, status=run_state)
        if run_state.lower() != "success":
            error = clean_text((status or {}).get("error_message"), 500)
            raise CapitolError(
                f"{kind} run {run_id} ended {run_state or 'unknown'}"
                f"{': ' + error if error else ''}"
            )
        return "output", runtime.workflow_output(run_id) or {}

    def _park_hitl(self, session_id: str, kind: str, run_id: str,
                   event: Dict[str, Any], last_sequence: int) -> str:
        data = event.get("data") or {}
        node = event.get("node") or {}
        prompt = clean_text(
            data.get("prompt")
            or (data.get("extra") or {}).get("prompt")
            or "The workflow needs input to continue.",
            2000,
        )
        input_kind = str(data.get("input_kind") or "")
        self.state.update_session(session_id, phase="hitl", hitl={
            "run_id": run_id,
            "kind": kind,
            "request_id": str(
                data.get("request_id")
                or (data.get("extra") or {}).get("request_id")
                or ""
            ),
            "node_id": str(node.get("node_id") or ""),
            "input_kind": input_kind,
            "last_sequence": int(last_sequence),
        })
        if input_kind == "clarification":
            return (f"The workflow asks: {prompt}\n"
                    "Reply in this thread to answer.")
        return (f"Workflow checkpoint: {prompt}\n"
                "Reply exactly 'continue' to proceed or 'stop' to halt.")

    # -- outcomes -------------------------------------------------------------------

    def _extract_contract(self, session_id: str,
                          output: Dict[str, Any]) -> Dict[str, Any]:
        contract = self._extract_contract_from(output, self.initial_alias)
        self._record_history(self.state, session_id, contract)
        # The full immutable contract is what later steps revise from and
        # what an approval consume constructs the effect request from.
        self.state.update_session(session_id,
                                  **{self.contract_key: contract})
        return contract

    def _after_contract(self, key: str, session_id: str,
                        contract: Dict[str, Any]) -> str:
        status = str(contract.get(self.status_field) or "")
        if status == self.clarify_status:
            self.state.update_session(session_id, phase="clarify")
            questions = [
                clean_text(question, 500)
                for question in contract.get(self.questions_field) or []
            ] or ["(no specific question provided)"]
            lines = [self._msg("clarify_header")]
            lines.extend(f"  • {question}" for question in questions)
            lines.append("Reply in this thread with the answers.")
            return "\n".join(lines)
        if status != self.gate_status:
            self.state.update_session(session_id, phase="failed")
            return self._msg("unexpected_status", status=repr(status))
        summary = self._present(contract)
        decision = evaluate_caps(self.pack, self.config, contract)
        auto_spec = (
            self.approval_spec.get("auto_within_caps") or {}
        ).get("channel") or {}
        opt_in_key = str(auto_spec.get("opt_in_key") or "")
        if decision.auto and opt_in_key and get_bool(
            self.config, opt_in_key, bool(auto_spec.get("default", False))
        ):
            challenge = self._challenge(contract)
            session = self.state.session(session_id) or {}
            self.notify(
                self._msg("auto_effect_notify", summary=summary,
                          challenge=challenge),
                str(session.get("channel") or self.channel),
                str(session.get("thread_id") or ""),
            )
            return self._effect(key, session_id, contract, caps_auto=True)
        reasons = list(decision.reasons) or [
            self._msg("channel_auto_off_reason", opt_in_key=opt_in_key)
        ]
        return summary + "\n" + self._request_approval(
            key, session_id, contract, reasons
        )

    def _present(self, contract: Dict[str, Any]) -> str:
        spec = self.pack.presentation.get("channel")
        if not spec:
            raise PackError(
                f"pack {self.pack.name} has no presentation.channel"
            )
        return "\n".join(render_lines(spec, contract))

    # -- approvals --------------------------------------------------------------------

    def _request_approval(self, key: str, session_id: str,
                          contract: Dict[str, Any],
                          reasons: List[str]) -> str:
        session = self.state.session(session_id) or {}
        previous = session.get("approval_id")
        if previous:
            self.approvals.pop(int(previous))
        payload: Dict[str, Any] = {"thread_key": key}
        for pin in self.approval_spec.get("pin") or []:
            pin = str(pin)
            if pin == "session.id":
                payload["session_id"] = session_id
            elif pin.startswith("contract."):
                field = pin[len("contract."):]
                payload[field] = contract.get(field)
        describe = render_formula(
            str(self.approval_spec.get("describe") or ""), contract
        )
        request_id = self.approvals.add(
            describe,
            str(session.get("channel") or self.channel),
            str(session.get("thread_id") or ""),
            str(session.get("sender") or ""),
            kind=str(self.approval_spec["kind"]),
            payload=payload,
        )
        self.state.update_session(
            session_id, phase="awaiting_approval", approval_id=request_id
        )
        lines = [self._msg("approval_header", id=request_id)]
        lines.extend(
            f"  • {clean_text(reason, 200)}" for reason in reasons
        )
        lines.append(f"Challenge: {self._challenge(contract)}")
        lines.append(self._msg("approval_reply_hint", id=request_id))
        return "\n".join(lines)

    # -- the gated effect ---------------------------------------------------------------

    def _effect(self, key: str, session_id: str,
                contract: Dict[str, Any], *, caps_auto: bool,
                approval_context: Optional[Dict[str, Any]] = None) -> str:
        session = self.state.session(session_id) or {}
        runtime = self._runtime(session)
        workflows = dict(session.get("workflows") or {}) or (
            resolve_workflows(runtime, self.pack, self.config)
        )
        binding = self.pack.binding(self.effect_binding)
        request_name = str(binding.get("request"))
        request = build_request(
            self.pack, request_name,
            config=self.config,
            session={"id": session_id, "thread_key": key},
            contract=contract,
        )
        policy_payload = self._policy_payload(
            workflows[self.effect_alias], request, caps_auto
        )
        if approval_context:
            policy_payload["channel_approval"] = approval_context
        decision = evaluate_required_policy(
            str(self.approval_spec["policy_event"]), policy_payload
        )
        if not decision.allowed:
            self.state.update_session(session_id, phase="failed")
            return self._msg(
                "policy_denied",
                reason=(decision.reason or decision.check
                        or "no reason given"),
            )
        self.state.update_session(session_id, phase="publishing")
        # The request's embedded idempotency_key is the exact contract
        # formula and never varies. The *gateway* call key gets a retry
        # suffix on re-approval after a failed attempt — otherwise the
        # gateway would keep replaying the failed run id. Effectively-once
        # is still guaranteed by Capitol's effect ledger on the embedded
        # key: a duplicate attempt replays the durable receipt.
        attempts = int(
            (self.state.session(session_id) or {}).get("publish_attempts")
            or 0
        ) + 1
        self.state.update_session(session_id, publish_attempts=attempts)
        call_key = gateway_key(self.pack, self.effect_binding, request,
                               attempt=attempts)
        try:
            outcome, value = self._run_typed(
                runtime, session_id, self.effect_alias,
                workflows[self.effect_alias], request, call_key,
            )
        except CapitolError as exc:
            # The effect failed upstream. The contract is still valid and
            # the effect key is revision-scoped, so re-approving retries
            # safely — replay can never double-post.
            hint = clean_text(getattr(exc, "hint", ""), 200)
            failure = self.approval_spec.get("on_effect_failure") or {}
            rearm = ""
            if failure.get("reissue"):
                rearm = self._request_approval(
                    key, session_id, contract,
                    [str(failure.get("reason") or "the previous attempt "
                         "failed upstream")],
                )
            return self._msg(
                "effect_failed",
                error=clean_text(exc, 500),
                hint_part=f" ({hint})" if hint else "",
                rearm=rearm,
            )
        if outcome == "parked":
            return value
        return self._finish_effect(session_id, value)

    def _finish_effect(self, session_id: str,
                       output: Dict[str, Any]) -> str:
        effect = find_contract(output, self.pack.effect_schema,
                               self.pack.contract_prefix)
        rejection = find_contract(output, self.pack.rejection_schema,
                                  self.pack.contract_prefix)
        if effect is None and rejection is not None:
            detail = clean_text(
                (rejection.get("error_detail") or {}).get("message")
                or rejection.get("message"), 300,
            )
            self.state.update_session(session_id, phase="failed")
            return self._msg("gate_rejected", detail=detail)
        if effect is None:
            self.state.update_session(session_id, phase="failed")
            return self._msg("no_effect_contract",
                             schema=self.pack.effect_schema)
        facts = extract_effect_facts(self.pack, effect)
        self.state.update_session(
            session_id, phase="done", **{self.effect_field: facts}
        )
        return self._effect_reply("channel", facts)

    # -- recovery ---------------------------------------------------------------------

    def _recover_phase(self, session_id: str) -> str:
        """After a failed step, park the session in the most honest durable
        phase: a parked run keeps priority, then the last good contract,
        else failed. Returns the phase chosen."""
        session = self.state.session(session_id) or {}
        if session.get("hitl"):
            phase = "hitl"
        else:
            contract = session.get(self.contract_key)
            hash_field = self.identity_fields[-1] if (
                self.identity_fields
            ) else ""
            if isinstance(contract, dict) and contract.get(hash_field):
                phase = (
                    "clarify"
                    if str(contract.get(self.status_field) or "") == (
                        self.clarify_status
                    )
                    else "awaiting_approval"
                )
            else:
                phase = "failed"
        self.state.update_session(session_id, phase=phase)
        return phase


# ---------------------------------------------------------------------------
# The shell surface (interactive intake, injected UI callables)
# ---------------------------------------------------------------------------

class PackShellFlow(_PackFlowBase):
    """Drives one pack session interactively; UI arrives as injected
    callables so the same flow serves the shell and tests identically."""

    def __init__(
        self,
        pack: FlowPack,
        runtime: CapitolRuntime,
        config: dict,
        *,
        state: Optional[PackState] = None,
        say: Callable[[str], None] = print,
        ask: Optional[Callable[[str], str]] = None,
        confirm: Optional[Callable[[str], bool]] = None,
    ):
        self.runtime = runtime
        self.config = config or {}
        self._init_pack(pack, "shell")
        self.state = state or PackState(filename=pack.state_file)
        self.say = say
        self.ask = ask or (lambda prompt: input(prompt))
        self.confirm = confirm or self._default_confirm
        self._input_keys: Dict[str, str] = {}
        self.initial_binding = self.pack.binding(
            self._initial_binding_name()
        )
        self.initial_alias = str(self.initial_binding["workflow"])

    def _default_confirm(self, prompt: str) -> bool:
        return self.ask(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

    def _msg(self, key: str, **values) -> str:
        return self.pack.message(f"shell.{key}", **values)

    # -- discovery ---------------------------------------------------------

    def resolve_workflows(self) -> Dict[str, str]:
        return resolve_workflows(self.runtime, self.pack, self.config)

    def _inputs_key(self, workflow_id: str) -> str:
        return workflow_inputs_key(self.runtime, workflow_id,
                                   self._input_keys)

    # -- attachments --------------------------------------------------------------

    def _validate_attachment(self, raw: str) -> Path:
        spec = self._attachments_spec()
        path = Path(raw).expanduser()
        if not path.is_file():
            raise CapitolError(self._msg("attachment_not_found",
                                         path=str(path)))
        suffixes = tuple(spec.get("suffixes") or ())
        if suffixes and path.suffix.lower() not in suffixes:
            raise CapitolError(self._msg(
                "attachment_bad_type", name=path.name,
                suffixes=", ".join(suffixes),
            ))
        max_bytes = int(spec.get("max_bytes") or 0)
        if max_bytes and path.stat().st_size > max_bytes:
            raise CapitolError(self._msg(
                "attachment_too_big", name=path.name,
                cap=f"{max_bytes // (1024 * 1024)} MB",
            ))
        return path

    def _check_attachment_count(self, paths: List[str]):
        spec = self._attachments_spec()
        if not paths:
            raise CapitolError(self._msg("attachment_missing"))
        max_count = int(spec.get("max_count") or 0)
        if max_count and len(paths) > max_count:
            raise CapitolError(self._msg("attachment_too_many",
                                         max=max_count))

    def upload_attachments(self, paths: List[str]) -> List[Dict[str, Any]]:
        self._check_attachment_count(paths)
        media: List[Dict[str, Any]] = []
        for index, raw in enumerate(paths):
            path = self._validate_attachment(raw)
            self.say(self._msg("uploading", name=path.name))
            uploaded = self.runtime.upload_artifact(str(path))
            uploaded["order"] = index
            media.append(uploaded)
        return media

    # -- run supervision -------------------------------------------------------

    def run_workflow(
        self,
        session_id: str,
        kind: str,
        workflow_id: str,
        request_value: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Start a typed run, supervise it to terminal, return its output."""
        inputs = {self._inputs_key(workflow_id): request_value}
        submission = self.runtime.call_workflow(
            workflow_id, inputs, idempotency_key=idempotency_key
        )
        return self.supervise_run(
            session_id, kind, str(submission["run_id"]), idempotency_key
        )

    def supervise_run(
        self,
        session_id: str,
        kind: str,
        run_id: str,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        """Watch a run to terminal and return its output.

        Mid-run ``node.input_required`` events are relayed to the user and
        answered through the HITL skills; every event advances the
        persisted ``last_sequence`` cursor so a resumed watch never
        replays or drops events.
        """
        self.state.record_run(session_id, kind, run_id, idempotency_key)
        self.say(f"  {kind} run {run_id} started")
        final_state = ""
        for event in self.runtime.watch_run(run_id):
            event_type = str(event.get("event_type") or "")
            sequence = event.get("sequence")
            if isinstance(sequence, (int, float)):
                self.state.update_run(
                    session_id, run_id, last_sequence=int(sequence)
                )
            if event_type == "node.node_started":
                node = (event.get("node") or {}).get("display_name") or ""
                if node:
                    self.say(f"    · {clean_text(node, 80)}")
            elif event_type == "node.input_required":
                self._answer_input_required(run_id, event)
            elif event_type == FINAL_STATUS_EVENT:
                final_state = str(
                    (event.get("data") or {}).get("state") or ""
                )
        status = self.runtime.run_status(run_id)
        run_state = str((status or {}).get("status") or final_state or "")
        self.state.update_run(session_id, run_id, status=run_state)
        if run_state.lower() != "success":
            error = clean_text((status or {}).get("error_message"), 500)
            raise CapitolError(
                f"{kind} run {run_id} ended {run_state or 'unknown'}"
                f"{': ' + error if error else ''}"
            )
        return self.runtime.workflow_output(run_id) or {}

    def _answer_input_required(self, run_id: str, event: Dict[str, Any]):
        """Relay a HITL checkpoint to the user; answers go back through the
        typed HITL skills (never through chat prose)."""
        data = event.get("data") or {}
        node = event.get("node") or {}
        request_id = str(
            data.get("request_id")
            or (data.get("extra") or {}).get("request_id")
            or ""
        )
        prompt = clean_text(
            data.get("prompt")
            or (data.get("extra") or {}).get("prompt")
            or "The workflow needs input to continue.",
            2000,
        )
        if str(data.get("input_kind") or "") == "clarification":
            self.say(f"\n  The workflow asks: {prompt}")
            answer = self.ask("  your answer: ").strip()
            self.runtime.submit_clarification(
                run_id, request_id, answer, declined=not answer
            )
            return
        # Human-Intervention panel: literal continue/stop token protocol.
        self.say(f"\n  Checkpoint: {prompt}")
        proceed = self.confirm("  continue this run?")
        self.runtime.submit_intervention(
            run_id,
            str(node.get("node_id") or ""),
            request_id,
            "continue" if proceed else "stop",
        )

    # -- presentation -----------------------------------------------------------

    def present_contract(self, contract: Dict[str, Any]):
        spec = self.pack.presentation.get("shell")
        if not spec:
            raise PackError(
                f"pack {self.pack.name} has no presentation.shell"
            )
        self.say("\n".join(render_lines(spec, contract)))

    # -- intake paths -------------------------------------------------------------

    def _extract_contract(
        self, session_id: str, output: Dict[str, Any]
    ) -> Dict[str, Any]:
        contract = self._extract_contract_from(output, self.initial_alias)
        self._record_history(self.state, session_id, contract)
        return contract

    def _initial_typed(
        self,
        session_id: str,
        workflows: Dict[str, str],
        attachment_paths: List[str],
        item_context: str,
    ) -> Dict[str, Any]:
        """Typed intake: upload attachments as private org artifacts, then
        call the initial workflow with the constructed request."""
        media = self.upload_attachments(attachment_paths)
        self.state.update_session(session_id, media=[
            {k: item[k]
             for k in ("artifact_id", "digest", "order", "filename")}
            for item in media
        ])
        mode = (self.initial_binding.get("modes") or {}).get("typed") or {}
        request_name = str(mode.get("request"))
        request = build_request(
            self.pack, request_name,
            config=self.config,
            session={"id": session_id, "thread_key": f"{session_id}:shell",
                     "media": media},
            intake_text=item_context,
        )
        output = self.run_workflow(
            session_id, self.initial_alias, workflows[self.initial_alias],
            request, request_key(self.pack, request_name, request),
        )
        return self._extract_contract(session_id, output)

    def _initial_chat(
        self,
        session_id: str,
        workflows: Dict[str, str],
        attachment_paths: List[str],
        item_context: str,
    ) -> Dict[str, Any]:
        """Chat intake (the agent's designed path): attachments ride the
        chat message as FileParts and the orchestrator's allowlisted tool
        launches the run. The reply prose is untrusted and only
        displayed; the contract is read from the run's typed outputs."""
        self._check_attachment_count(attachment_paths)
        for path in attachment_paths:
            self._validate_attachment(path)
        self.say(self._msg("attaching"))
        mode = (self.initial_binding.get("modes") or {}).get("chat") or {}
        chat_message = render_formula(
            str(mode.get("message") or "{item_context}"),
            {"item_context": item_context},
        )
        reconcile = mode.get("reconcile") or {}
        run_id, reply = start_chat_intake(
            self.runtime, workflows[self.initial_alias], chat_message,
            attachment_paths, self.say,
            alias=self.initial_alias,
            attempts=int(reconcile.get("attempts") or 6),
            delay_seconds=float(reconcile.get("delay_seconds") or 5),
        )
        if reply:
            self.say(self._msg("agent_reply", reply=reply))
        output = self.supervise_run(session_id, self.initial_alias, run_id)
        return self._extract_contract(session_id, output)

    # -- the flow -----------------------------------------------------------------

    def run_intake(self, attachment_paths: List[str],
                   notes: str = "") -> Dict[str, Any]:
        """Full interactive loop: attachments → initial run (+clarify) →
        policy gate → exact approval → gated effect → effect facts."""
        workflows = self.resolve_workflows()
        session_id = (
            f"conch-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        mode_key = str(self.initial_binding.get("mode_key") or "")
        default_mode = str(
            self.initial_binding.get("default_mode") or "chat"
        )
        intake_mode = str(
            (self.config.get(mode_key) if mode_key else "")
            or default_mode
        ).strip().lower()
        # Chat intake binds the server-side session to the A2A context,
        # so every session gets a fresh handshake.
        if intake_mode == "chat" or not self.runtime.context_id:
            self.runtime.handshake()
        self.state.update_session(
            session_id,
            intake=intake_mode,
            context_id=self.runtime.context_id,
            workflows=workflows,
        )
        self.say(self._msg("session_started", session_id=session_id,
                           intake=intake_mode))

        item_context = notes.strip() or self._context_default()
        if intake_mode == "typed":
            contract = self._initial_typed(
                session_id, workflows, attachment_paths, item_context
            )
        else:
            contract = self._initial_chat(
                session_id, workflows, attachment_paths, item_context
            )

        clarify_rounds = 0
        revise_spec = self.pack.binding(self.revise_binding)
        revise_request_name = str(revise_spec.get("request"))
        revise_alias = str(revise_spec["workflow"])
        while str(contract.get(self.status_field) or "") == (
            self.clarify_status
        ):
            if clarify_rounds >= self.max_clarify_rounds:
                raise CapitolError(self._msg(
                    "clarify_exhausted", rounds=self.max_clarify_rounds
                ))
            clarify_rounds += 1
            questions = [
                clean_text(question, 500)
                for question in contract.get(self.questions_field) or []
            ]
            self.say(self._msg("clarify_header"))
            answers: List[str] = []
            for question in questions or [
                "(no specific question provided)"
            ]:
                answer = self.ask(f"    {question}\n    → ").strip()
                if answer:
                    answers.append(f"Q: {question} A: {answer}")
            request = build_request(
                self.pack, revise_request_name,
                config=self.config,
                session={"id": session_id,
                         "item_context": item_context},
                intake_text=" ".join(answers),
                contract=contract,
            )
            output = self.run_workflow(
                session_id, revise_alias, workflows[revise_alias],
                request,
                request_key(self.pack, revise_request_name, request),
            )
            contract = self._extract_contract(session_id, output)

        status = str(contract.get(self.status_field) or "")
        if status != self.gate_status:
            raise CapitolError(
                f"unexpected {self.contract_key} status {status!r}"
            )
        self.present_contract(contract)

        decision = evaluate_caps(self.pack, self.config, contract)
        challenge = self._challenge(contract)
        result_base = {"session_id": session_id, "published": False,
                       self.contract_key: contract}
        if decision.auto:
            self.say(self._msg("caps_auto", challenge=challenge))
            if not self.confirm(self._msg("confirm_effect")):
                self.say(self._msg("declined"))
                return result_base
        else:
            phrase = str(
                (self.approval_spec.get("exact") or {}).get("phrase") or ""
            )
            self.say("\n  Policy: exact approval required —")
            for reason in decision.reasons:
                self.say(f"    · {reason}")
            self.say(f"  To approve, type exactly: {phrase}")
            typed = self.ask("  approval: ").strip().lower()
            if typed != phrase:
                self.say(self._msg("phrase_mismatch"))
                return result_base

        binding = self.pack.binding(self.effect_binding)
        request_name = str(binding.get("request"))
        request = build_request(
            self.pack, request_name,
            config=self.config,
            session={"id": session_id},
            contract=contract,
        )
        policy = evaluate_required_policy(
            str(self.approval_spec["policy_event"]),
            self._policy_payload(
                workflows[self.effect_alias], request, decision.auto
            ),
        )
        if not policy.allowed:
            raise CapitolError(
                f"{self.effect_binding} denied by required policy: "
                f"{policy.reason}"
            )
        output = self.run_workflow(
            session_id, self.effect_alias, workflows[self.effect_alias],
            request, str(request.get("idempotency_key") or ""),
        )
        effect = find_contract(output, self.pack.effect_schema,
                               self.pack.contract_prefix)
        rejection = find_contract(output, self.pack.rejection_schema,
                                  self.pack.contract_prefix)
        if effect is None and rejection is not None:
            detail = clean_text(
                (rejection.get("error_detail") or {}).get("message")
                or rejection.get("message"), 300,
            )
            raise CapitolError(
                f"{self.effect_binding} was rejected by the approval "
                f"gate: {detail}"
            )
        if effect is None:
            raise CapitolError(
                f"{self.effect_binding} run produced no "
                f"{self.pack.effect_schema} contract"
            )
        facts = extract_effect_facts(self.pack, effect)
        self.state.update_session(session_id,
                                  **{self.effect_field: facts})
        self.say(self._effect_reply("shell", facts))
        return dict(
            result_base,
            published=effect_succeeded(self.pack, facts),
            effect=effect,
            **{self.effect_field: facts},
        )
