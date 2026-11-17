"""The Architecture Card (``conch.architecture_card.v1``): model +
fail-closed validation + canonical digest + rendering + version diffing.

A card is the reviewable unit of compilation: goal and success criteria,
the process narrative, reuse-vs-create asset lists (created workflows as
declarative stage graphs with uuid5 identities — the deterministic
generator in :mod:`conch.capitol.compiler.graph` turns an approved card's
stages into the exact ``AdvancedWorkflowPayload`` bytes at
materialization), the generated FlowPack manifest, caps and approval
classes, HITL points, eval criteria, generated drill fixtures (synthetic
inputs + expected gates), rollout rung, rollback plan, estimates, and
open questions.

Validation is fail-closed everywhere: unknown sections or fields are
rejected, created-asset identities must carry the compile prefix and must
not collide with anything discovery already found (**reuse-first is
enforced in code**, not just prompted), drill fixtures are refused when
they reference real-looking accounts, the generated pack manifest must
pass the flow-pack loader, and the whole card passes the credential
guard. Cards diff cleanly across versions — recompilation is a new
version, never mutation.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from ...secretguard import CredentialRejected, credential_findings
from ..errors import CapitolError
from .graph import (
    stage_input_override_key,
    validate_stages,
    workflow_uuid,
)

CARD_SCHEMA_V1 = "conch.architecture_card.v1"
CARD_SCHEMA_V2 = "conch.architecture_card.v2"
# Ordinary goal/capture compilations continue to emit v1. Procedure-seeded
# compilations opt into v2 because immutable workflow/Procedure pins belong
# in the approved authorization artifact.
CARD_SCHEMA = CARD_SCHEMA_V1
CARD_SCHEMAS = frozenset({CARD_SCHEMA_V1, CARD_SCHEMA_V2})

#: Created-asset identity prefix (disposable, greppable, rollback-safe).
DEFAULT_ASSET_PREFIX = "conch-compile"

#: Rollout rungs (shadow is the v1 default and the only rung the
#: materializer arms without operator action).
ROLLOUT_RUNGS = ("shadow", "assist", "auto-within-caps")

_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_REF_RE = re.compile(r"^\$create:([a-z0-9][a-z0-9-]{2,63})$")
_COLLECTION_REF_RE = re.compile(r"^\$collection:([a-z0-9][a-z0-9-]{2,63})$")
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)

#: Domains a drill fixture may mention: synthetic by construction.
SYNTHETIC_DOMAINS = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$",
    re.IGNORECASE,
)


class CardError(CapitolError):
    """An architecture card failed validation (fail closed)."""


_TOP_KEYS_V1 = {
    "schema", "goal", "success_criteria", "narrative", "assets", "pack",
    "caps", "approval_classes", "hitl", "eval_criteria", "drill",
    "rollout", "rollback", "estimates", "open_questions", "mission",
}
_TOP_KEYS_V2 = _TOP_KEYS_V1 | {"procedure_sources"}
_REQUIRED_TOP = ("schema", "goal", "narrative", "assets", "pack",
                 "drill", "rollback", "mission")
_ASSET_KEYS = {"reuse", "create"}
_CREATE_KEYS = {"workflows", "agent", "schedules", "collections"}
_REUSE_KINDS = {"workflow", "agent", "collection", "node", "pack",
                "conch"}
_REUSE_KEYS = {"kind", "id", "name", "reason"}
_WORKFLOW_KEYS = {"identity", "name", "description", "stages",
                  "workflow_id", "input_override_key"}
_AGENT_KEYS = {"identity", "name", "description", "workflows"}
_SCHEDULE_KEYS = {"identity", "name", "workflow", "cron", "timezone",
                  "enabled", "input"}
_COLLECTION_KEYS = {"identity", "name", "description"}
_PACK_KEYS = {"name", "description", "manifest"}
_DRILL_KEYS = {"fixtures", "notes"}
_FIXTURE_KEYS = {"workflow", "input", "expect", "workflow_id",
                 "override_key"}
_EXPECT_KEYS = {"status", "output_contains"}
_ROLLOUT_KEYS = {"rung", "notes"}
_ESTIMATE_KEYS = {"cost", "latency", "notes"}
_PROCEDURE_SOURCE_KEYS = {
    "relationship", "org_id", "procedure_document_id", "workflow_id",
    "workflow_version_id", "workflow_version_number",
    "workflow_payload_digest", "procedure_content_digest",
    "compiler_version",
}
_PROCEDURE_RELATIONSHIPS = frozenset({"evidence", "adopt", "adapt"})
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_CRON_RE = re.compile(
    r"^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$"
)


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def card_digest(card: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(card).encode("utf-8")
    ).hexdigest()


def _check_keys(where: str, data: Any, allowed: set,
                required: tuple = ()) -> None:
    if not isinstance(data, dict):
        raise CardError(f"{where} must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise CardError(
            f"{where} has unknown fields (failing closed): "
            + ", ".join(sorted(unknown))
        )
    for key in required:
        if key not in data:
            raise CardError(f"{where} is missing required field {key!r}")


def _str_list(where: str, value: Any, *, required: bool = False
              ) -> List[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise CardError(f"{where} must be a list of strings")
    items = [item.strip() for item in value if item.strip()]
    if required and not items:
        raise CardError(f"{where} must not be empty")
    return items


def _identity(where: str, value: Any, prefix: str) -> str:
    identity = str(value or "").strip()
    if not _IDENTITY_RE.match(identity):
        raise CardError(
            f"{where} identity {identity!r} is invalid (lowercase "
            "letters, digits, hyphens, 3-64 chars)"
        )
    if prefix and not identity.startswith(prefix):
        raise CardError(
            f"{where} identity {identity!r} must start with the compile "
            f"asset prefix {prefix!r} (disposable, rollback-safe naming)"
        )
    return identity


def _normalized_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


# ---------------------------------------------------------------------------
# Discovery shape (what the compilation session saw)
# ---------------------------------------------------------------------------

def empty_discovery() -> Dict[str, Any]:
    return {"org_id": "", "workflows": [], "agents": [], "collections": [],
            "packs": [], "node_catalog": [], "conch": [], "notes": []}


def discovery_names(discovery: Dict[str, Any], kind: str) -> Dict[str, str]:
    """``{normalized name: id}`` for one discovered asset kind."""
    names: Dict[str, str] = {}
    for row in (discovery or {}).get(kind) or []:
        name = _normalized_name(row.get("name"))
        if name:
            names[name] = str(row.get("id") or row.get("name") or "")
    return names


# ---------------------------------------------------------------------------
# Fail-closed validation + normalization
# ---------------------------------------------------------------------------

def _guard_drill_fixtures(fixtures: List[Dict[str, Any]]) -> None:
    """Drill fixtures never reference real accounts: every email-like
    token must live on a synthetic domain, and no credential bytes may
    appear anywhere in the fixture payloads (whole-card rejection)."""
    text = canonical_json(fixtures)
    for match in _EMAIL_RE.finditer(text):
        domain = match.group(1)
        if not SYNTHETIC_DOMAINS.search(domain):
            raise CardError(
                f"drill fixture references a real-looking account "
                f"{match.group(0)!r} — fixtures are synthetic only "
                "(use example.com / *.test / *.invalid addresses)"
            )


def _resolve_workflow_ref(where: str, ref: Any,
                          created: Dict[str, str],
                          discovery: Dict[str, Any]) -> str:
    """A workflow reference: ``$create:<identity>`` resolves to the
    created workflow's deterministic uuid5 id; anything else must be an
    id discovery actually listed (never an invented one)."""
    ref = str(ref or "").strip()
    match = _REF_RE.match(ref)
    if match:
        identity = match.group(1)
        if identity not in created:
            raise CardError(
                f"{where} references unknown created workflow "
                f"{identity!r}"
            )
        return created[identity]
    known = {
        str(row.get("id") or "")
        for row in (discovery or {}).get("workflows") or []
    }
    if ref not in known:
        raise CardError(
            f"{where} references workflow id {ref!r} that discovery did "
            "not list — reuse only real assets, create via $create:<id>"
        )
    return ref


def _validate_reuse(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = card["assets"].get("reuse") or []
    if not isinstance(rows, list):
        raise CardError("assets.reuse must be a list")
    clean = []
    for index, row in enumerate(rows):
        where = f"assets.reuse[{index}]"
        _check_keys(where, row, _REUSE_KEYS, ("kind", "name", "reason"))
        kind = str(row.get("kind") or "")
        if kind not in _REUSE_KINDS:
            raise CardError(
                f"{where}.kind {kind!r} is not one of "
                + ", ".join(sorted(_REUSE_KINDS))
            )
        clean.append({
            "kind": kind,
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or ""),
            "reason": str(row.get("reason") or ""),
        })
    return clean


def _require_digest(where: str, value: Any) -> str:
    digest = str(value or "").strip()
    if not _SHA256_RE.fullmatch(digest):
        raise CardError(
            f"{where} must be a lowercase sha256:<64 hex> digest"
        )
    return digest


def _validate_procedure_sources(
    card: Dict[str, Any],
    discovery: Dict[str, Any],
    reused: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows = card.get("procedure_sources")
    if not isinstance(rows, list) or not rows:
        raise CardError(
            "card.procedure_sources must be a non-empty list for "
            "conch.architecture_card.v2"
        )
    known_workflows = {
        str(row.get("id") or ""): row
        for row in discovery.get("workflows") or []
        if str(row.get("id") or "")
    }
    reused_workflows = {
        str(row.get("id") or "")
        for row in reused
        if row.get("kind") == "workflow"
    }
    local_org = str(discovery.get("org_id") or "")
    clean: List[Dict[str, Any]] = []
    seen_versions = set()
    for index, row in enumerate(rows):
        where = f"card.procedure_sources[{index}]"
        _check_keys(where, row, _PROCEDURE_SOURCE_KEYS,
                    tuple(sorted(_PROCEDURE_SOURCE_KEYS)))
        relationship = str(row.get("relationship") or "")
        if relationship not in _PROCEDURE_RELATIONSHIPS:
            raise CardError(
                f"{where}.relationship {relationship!r} is not one of "
                + ", ".join(sorted(_PROCEDURE_RELATIONSHIPS))
            )
        org_id = str(row.get("org_id") or "").strip()
        document_id = str(row.get("procedure_document_id") or "").strip()
        workflow_id = str(row.get("workflow_id") or "").strip()
        version_id = str(row.get("workflow_version_id") or "").strip()
        compiler_version = str(row.get("compiler_version") or "").strip()
        try:
            version_number = int(row.get("workflow_version_number"))
        except (TypeError, ValueError):
            version_number = 0
        if not all((org_id, document_id, workflow_id, version_id,
                    compiler_version)) or version_number < 1:
            raise CardError(
                f"{where} requires non-empty immutable ids/compiler_version "
                "and a positive workflow_version_number"
            )
        version_key = (org_id, workflow_id, version_id)
        if version_key in seen_versions:
            raise CardError(
                f"{where} duplicates workflow version {version_id!r}"
            )
        seen_versions.add(version_key)
        if relationship == "adopt":
            if not local_org:
                raise CardError(
                    f"{where}: direct adoption requires a discovered local "
                    "organization id"
                )
            if org_id != local_org:
                raise CardError(
                    f"{where}: cross-org Procedure sources may be evidence "
                    "or adapt, never direct adopt"
                )
            if workflow_id not in known_workflows:
                raise CardError(
                    f"{where}: adopted workflow {workflow_id!r} was not "
                    "present in discovery"
                )
            if workflow_id not in reused_workflows:
                raise CardError(
                    f"{where}: relationship='adopt' requires the exact "
                    "workflow in assets.reuse"
                )
            discovered = known_workflows[workflow_id]
            discovered_version = str(
                discovered.get("workflow_version_id") or ""
            )
            discovered_number = discovered.get("version_number")
            discovered_digest = str(
                discovered.get("workflow_payload_digest") or ""
            )
            if (
                discovered_version != version_id
                or int(discovered_number or 0) != version_number
                or discovered_digest
                != str(row.get("workflow_payload_digest") or "")
            ):
                raise CardError(
                    f"{where}: adopted Procedure source does not match the "
                    "exact workflow version discovered (failing closed)"
                )
        clean.append({
            "relationship": relationship,
            "org_id": org_id,
            "procedure_document_id": document_id,
            "workflow_id": workflow_id,
            "workflow_version_id": version_id,
            "workflow_version_number": version_number,
            "workflow_payload_digest": _require_digest(
                f"{where}.workflow_payload_digest",
                row.get("workflow_payload_digest"),
            ),
            "procedure_content_digest": _require_digest(
                f"{where}.procedure_content_digest",
                row.get("procedure_content_digest"),
            ),
            "compiler_version": compiler_version,
        })
    return clean


def _enforce_reuse_first(kind: str, name: str, identity: str,
                         discovery: Dict[str, Any]) -> None:
    """The hard rule: never create what already exists. A created
    asset whose (normalized) name or identity matches a discovered
    asset of the same kind fails validation, naming the existing id."""
    existing = discovery_names(discovery, kind + "s")
    for candidate in (_normalized_name(name), _normalized_name(identity)):
        if candidate and candidate in existing:
            raise CardError(
                f"reuse-first: {kind} {name!r} already exists "
                f"({existing[candidate]}) — reuse it in assets.reuse "
                "instead of creating a duplicate"
            )


def normalize_card(card: Dict[str, Any], discovery: Dict[str, Any], *,
                   prefix: str = DEFAULT_ASSET_PREFIX) -> Dict[str, Any]:
    """Validate and normalize a card (fail closed). Returns a new dict
    with deterministic identities resolved: created workflow uuid5 ids,
    drill override keys, mission workflow references, and the generated
    flow-pack manifest (validated by the fail-closed pack loader).
    """
    discovery = discovery or empty_discovery()
    schema = str(card.get("schema") or "")
    if schema not in CARD_SCHEMAS:
        raise CardError(
            f"unsupported card schema {card.get('schema')!r} "
            f"(supported: {', '.join(sorted(CARD_SCHEMAS))}) — "
            "failing closed"
        )
    _check_keys(
        "card", card,
        _TOP_KEYS_V2 if schema == CARD_SCHEMA_V2 else _TOP_KEYS_V1,
        _REQUIRED_TOP
        + (("procedure_sources",) if schema == CARD_SCHEMA_V2 else ()),
    )
    goal = str(card.get("goal") or "").strip()
    if not goal:
        raise CardError("card.goal must be a non-empty string")
    narrative = str(card.get("narrative") or "").strip()
    if not narrative:
        raise CardError("card.narrative must be a non-empty string")

    normalized: Dict[str, Any] = {
        "schema": schema,
        "goal": goal,
        "narrative": narrative,
        "success_criteria": _str_list(
            "card.success_criteria", card.get("success_criteria"),
            required=True,
        ),
        "caps": _str_list("card.caps", card.get("caps")),
        "approval_classes": _str_list(
            "card.approval_classes", card.get("approval_classes")
        ),
        "hitl": _str_list("card.hitl", card.get("hitl")),
        "eval_criteria": _str_list(
            "card.eval_criteria", card.get("eval_criteria")
        ),
        "rollback": _str_list(
            "card.rollback", card.get("rollback"), required=True
        ),
        "open_questions": _str_list(
            "card.open_questions", card.get("open_questions")
        ),
    }

    rollout = card.get("rollout") or {"rung": "shadow"}
    _check_keys("card.rollout", rollout, _ROLLOUT_KEYS)
    rung = str(rollout.get("rung") or "shadow")
    if rung not in ROLLOUT_RUNGS:
        raise CardError(
            f"card.rollout.rung {rung!r} is not one of "
            + ", ".join(ROLLOUT_RUNGS)
        )
    normalized["rollout"] = {
        "rung": rung, "notes": str(rollout.get("notes") or ""),
    }

    estimates = card.get("estimates") or {}
    _check_keys("card.estimates", estimates, _ESTIMATE_KEYS)
    normalized["estimates"] = {
        key: str(estimates.get(key) or "") for key in sorted(_ESTIMATE_KEYS)
        if estimates.get(key)
    }

    # -- assets ------------------------------------------------------------
    assets = card["assets"]
    _check_keys("card.assets", assets, _ASSET_KEYS, ("create",))
    create = assets.get("create") or {}
    _check_keys("card.assets.create", create, _CREATE_KEYS)
    normalized_assets: Dict[str, Any] = {
        "reuse": _validate_reuse(card),
        "create": {"workflows": [], "agent": None, "schedules": [],
                   "collections": []},
    }
    procedure_sources: List[Dict[str, Any]] = []
    if schema == CARD_SCHEMA_V2:
        procedure_sources = _validate_procedure_sources(
            card, discovery, normalized_assets["reuse"],
        )
        normalized["procedure_sources"] = procedure_sources

    catalog_ids = [
        str(node) for node in (discovery.get("node_catalog") or [])
    ]

    created_collections: Dict[str, str] = {}
    for index, row in enumerate(create.get("collections") or []):
        where = f"assets.create.collections[{index}]"
        _check_keys(where, row, _COLLECTION_KEYS, ("identity", "name"))
        identity = _identity(where, row["identity"], prefix)
        if identity in created_collections:
            raise CardError(f"{where} duplicates identity {identity!r}")
        name = str(row.get("name") or identity)
        _enforce_reuse_first("collection", name, identity, discovery)
        created_collections[identity] = name
        normalized_assets["create"]["collections"].append({
            "identity": identity, "name": name,
            "description": str(row.get("description") or ""),
        })

    created_workflows: Dict[str, str] = {}
    for index, row in enumerate(create.get("workflows") or []):
        where = f"assets.create.workflows[{index}]"
        _check_keys(where, row, _WORKFLOW_KEYS,
                    ("identity", "name", "stages"))
        identity = _identity(where, row["identity"], prefix)
        if identity in created_workflows:
            raise CardError(f"{where} duplicates identity {identity!r}")
        name = str(row.get("name") or identity)
        _enforce_reuse_first("workflow", name, identity, discovery)
        stages = validate_stages(
            f"{where}.stages", row["stages"],
            catalog_ids=catalog_ids,
            collection_identities=set(created_collections),
            discovery_collections={
                str(entry.get("id") or "")
                for entry in discovery.get("collections") or []
            },
        )
        workflow_id = workflow_uuid(identity)
        created_workflows[identity] = workflow_id
        normalized_assets["create"]["workflows"].append({
            "identity": identity,
            "name": name,
            "description": str(row.get("description") or ""),
            "stages": stages,
            "workflow_id": workflow_id,
            "input_override_key": stage_input_override_key(
                identity, stages
            ),
        })

    agent = create.get("agent")
    if agent is not None:
        where = "assets.create.agent"
        _check_keys(where, agent, _AGENT_KEYS, ("identity", "name",
                                                "workflows"))
        identity = _identity(where, agent["identity"], prefix)
        name = str(agent.get("name") or identity)
        _enforce_reuse_first("agent", name, identity, discovery)
        allowlist = [
            _resolve_workflow_ref(
                f"{where}.workflows[{i}]", ref, created_workflows,
                discovery,
            )
            for i, ref in enumerate(agent["workflows"] or [])
        ]
        if not allowlist:
            raise CardError(f"{where}.workflows must not be empty")
        normalized_assets["create"]["agent"] = {
            "identity": identity, "name": name,
            "description": str(agent.get("description") or ""),
            "workflows": allowlist,
        }

    schedule_identities = set()
    for index, row in enumerate(create.get("schedules") or []):
        where = f"assets.create.schedules[{index}]"
        _check_keys(where, row, _SCHEDULE_KEYS,
                    ("identity", "name", "workflow", "cron"))
        identity = _identity(where, row["identity"], prefix)
        if identity in schedule_identities:
            raise CardError(f"{where} duplicates identity {identity!r}")
        schedule_identities.add(identity)
        cron = str(row.get("cron") or "").strip()
        if not _CRON_RE.match(cron):
            raise CardError(
                f"{where}.cron {cron!r} is not a 5-field cron expression"
            )
        normalized_assets["create"]["schedules"].append({
            "identity": identity,
            "name": str(row.get("name") or identity),
            "workflow": _resolve_workflow_ref(
                f"{where}.workflow", row["workflow"], created_workflows,
                discovery,
            ),
            "cron": cron,
            "timezone": str(row.get("timezone") or "UTC"),
            # Capitol starts/schedules address a workflow id, not an exact
            # version. Procedure-linked cards therefore remain disabled in
            # shadow until the platform supports version-addressed starts.
            "enabled": (
                False if procedure_sources
                else bool(row.get("enabled", False))
            ),
            "input": row.get("input"),
        })
    normalized["assets"] = normalized_assets

    # -- drill ---------------------------------------------------------------
    drill = card["drill"]
    _check_keys("card.drill", drill, _DRILL_KEYS, ("fixtures",))
    fixtures_in = drill.get("fixtures")
    if not isinstance(fixtures_in, list) or not fixtures_in:
        raise CardError("card.drill.fixtures must be a non-empty list")
    fixtures: List[Dict[str, Any]] = []
    for index, fixture in enumerate(fixtures_in):
        where = f"drill.fixtures[{index}]"
        _check_keys(where, fixture, _FIXTURE_KEYS,
                    ("workflow", "input", "expect"))
        workflow_id = _resolve_workflow_ref(
            f"{where}.workflow", fixture["workflow"], created_workflows,
            discovery,
        )
        expect = fixture["expect"]
        _check_keys(f"{where}.expect", expect, _EXPECT_KEYS, ("status",))
        status = str(expect.get("status") or "").strip().lower()
        if status not in ("success", "failed"):
            raise CardError(
                f"{where}.expect.status must be 'success' or 'failed'"
            )
        override_key = ""
        for workflow in normalized_assets["create"]["workflows"]:
            if workflow["workflow_id"] == workflow_id:
                override_key = workflow["input_override_key"]
        if not override_key:
            matches = [
                str(row.get("input_override_key") or "")
                for row in discovery.get("workflows") or []
                if str(row.get("id") or "") == workflow_id
            ]
            override_key = matches[0] if matches else ""
        if not override_key:
            raise CardError(
                f"{where}: reused workflow {workflow_id!r} has no "
                "discovered exact-version input_override_key; refusing "
                "to guess a drill input"
            )
        fixtures.append({
            "workflow": str(fixture["workflow"]),
            "workflow_id": workflow_id,
            "override_key": override_key,
            "input": fixture["input"],
            "expect": {
                "status": status,
                "output_contains": _str_list(
                    f"{where}.expect.output_contains",
                    expect.get("output_contains"),
                ),
            },
        })
    _guard_drill_fixtures(fixtures)
    normalized["drill"] = {
        "fixtures": fixtures, "notes": str(drill.get("notes") or ""),
    }

    # -- generated pack manifest ------------------------------------------------
    pack = card["pack"]
    _check_keys("card.pack", pack, _PACK_KEYS, ("name",))
    pack_name = _identity("card.pack", pack["name"], prefix)
    manifest = generate_pack_manifest(
        pack_name, str(pack.get("description") or ""),
        normalized_assets["create"]["workflows"],
        normalized["drill"],
        procedure_sources=procedure_sources,
    )
    from ..packs.manifest import PackError, load_pack_data

    try:
        load_pack_data(manifest, source="generated")
    except PackError as exc:
        raise CardError(
            f"generated pack manifest failed the fail-closed loader: "
            f"{exc}"
        ) from None
    normalized["pack"] = {
        "name": pack_name,
        "description": str(pack.get("description") or ""),
        "manifest": manifest,
    }

    # -- supervising mission ------------------------------------------------------
    mission = card["mission"]
    if not isinstance(mission, dict):
        raise CardError("card.mission must be a mission spec object")
    mission = json.loads(canonical_json(mission))  # deep copy
    capitol_spec = mission.get("capitol")
    if isinstance(capitol_spec, dict):
        refs = capitol_spec.get("workflows") or []
        capitol_spec["workflows"] = [
            _resolve_workflow_ref(
                f"mission.capitol.workflows[{i}]", ref,
                created_workflows, discovery,
            )
            for i, ref in enumerate(refs)
        ]
    # Shadow rung: the supervising mission is dry-run, whatever the
    # model asked for (going live is an explicit operator act).
    mission["dry_run"] = True
    from ...kernel.model import KernelError, normalize_spec

    try:
        normalized["mission"] = normalize_spec(mission)
    except KernelError as exc:
        raise CardError(f"card.mission is not a valid spec: {exc}") from None

    # -- credential guard over the whole card -------------------------------------
    findings = credential_findings(canonical_json(normalized))
    if findings:
        raise CredentialRejected(sorted(set(findings)))
    return normalized


def generate_pack_manifest(
    name: str,
    description: str,
    workflows: List[Dict[str, Any]],
    drill: Dict[str, Any],
    *,
    procedure_sources: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The generated FlowPack manifest — deterministic scaffolding, no
    model bytes: workflow aliases bound by literal uuid5 ids, org/agent
    by config reference, and the ``workflow_drill`` acceptance pointing
    at the installed fixture file."""
    bindings: Dict[str, Any] = {}
    for workflow in workflows:
        bindings[workflow["identity"]] = {"id": workflow["workflow_id"]}
    for source in procedure_sources or []:
        if source.get("relationship") != "adopt":
            continue
        workflow_id = str(source["workflow_id"])
        alias = "adopted-" + hashlib.sha256(
            workflow_id.encode("utf-8")
        ).hexdigest()[:12]
        bindings[alias] = {"id": workflow_id}
    if not bindings:
        raise CardError(
            "a generated pack needs at least one created workflow"
        )
    return {
        "schema": "conch.flow_pack.v1",
        "pack": {
            "name": name,
            "version": "1.0.0",
            "description": description
            or "Compiled by the Conch ProcessCompiler.",
        },
        "capitol": {
            "org": "${config.capitol_org}",
            "agent": "${config.capitol_agent:}",
            "workflows": bindings,
        },
        "acceptance": {
            "kind": "workflow_drill",
            "fixtures": "assets/drill.json",
            "note": str(drill.get("notes") or "")
            or "Generated acceptance drill: synthetic inputs through the"
            " real workflows, expected gates asserted.",
        },
    }


# ---------------------------------------------------------------------------
# Rendering + diffing
# ---------------------------------------------------------------------------

def _bullets(lines: List[str], items: List[str], empty: str = "") -> None:
    if not items:
        if empty:
            lines.append(f"- {empty}")
        return
    for item in items:
        lines.append(f"- {item}")


def render_card_markdown(card: Dict[str, Any], *,
                         compilation: Optional[Dict[str, Any]] = None,
                         card_version: Optional[int] = None) -> str:
    """Human-reviewable markdown for one card version."""
    lines: List[str] = []
    header = "# Architecture Card"
    if compilation:
        header += f" — {compilation.get('compilation_id', '')}"
    lines.append(header)
    meta = []
    if card_version is not None:
        meta.append(f"card v{card_version}")
    if compilation:
        meta.append(f"status {compilation.get('status', '?')}")
    meta.append(f"digest {card_digest(card)[:23]}…")
    lines.append("_" + " · ".join(meta) + "_")
    lines.append("")
    lines.append(f"**Goal.** {card['goal']}")
    lines.append("")
    lines.append("## Success criteria")
    _bullets(lines, card.get("success_criteria") or [])
    lines.append("")
    lines.append("## Process narrative")
    lines.append(card.get("narrative", ""))
    lines.append("")
    lines.append("## Assets — reuse")
    _bullets(lines, [
        f"[{row['kind']}] {row['name']}"
        + (f" ({row['id']})" if row.get("id") else "")
        + f" — {row['reason']}"
        for row in card["assets"].get("reuse") or []
    ], empty="(nothing reused)")
    lines.append("")
    lines.append("## Assets — create")
    create = card["assets"]["create"]
    for workflow in create.get("workflows") or []:
        stages = " → ".join(
            f"{stage['kind']}:{stage['role']}"
            for stage in workflow["stages"]
        )
        lines.append(
            f"- workflow **{workflow['name']}** "
            f"(`{workflow['workflow_id']}`): {stages}"
        )
        if workflow.get("description"):
            lines.append(f"  - {workflow['description']}")
    agent = create.get("agent")
    if agent:
        lines.append(
            f"- agent **{agent['name']}** — allowlist exactly "
            f"{len(agent['workflows'])} workflow(s)"
        )
    for schedule in create.get("schedules") or []:
        state = "enabled" if schedule.get("enabled") else "DISABLED"
        lines.append(
            f"- schedule **{schedule['name']}**: `{schedule['cron']}` "
            f"{schedule.get('timezone', 'UTC')} ({state}) → workflow "
            f"`{schedule['workflow']}`"
        )
    for collection in create.get("collections") or []:
        lines.append(f"- collection **{collection['name']}**")
    if not any((create.get("workflows"), create.get("agent"),
                create.get("schedules"), create.get("collections"))):
        lines.append("- (nothing created)")
    lines.append("")
    if card.get("procedure_sources"):
        lines.append("## Procedure sources")
        for source in card["procedure_sources"]:
            lines.append(
                f"- **{source['relationship']}** Procedure "
                f"`{source['procedure_document_id']}` → workflow "
                f"`{source['workflow_id']}` v"
                f"{source['workflow_version_number']} "
                f"(`{source['workflow_version_id']}`), content "
                f"`{source['procedure_content_digest'][:23]}…`"
            )
        lines.append("")
    lines.append("## Pack")
    pack = card.get("pack") or {}
    lines.append(
        f"- **{pack.get('name', '?')}** — "
        f"{pack.get('description') or '(no description)'}"
    )
    lines.append("")
    lines.append("## Caps and approval classes")
    _bullets(lines, (card.get("caps") or [])
             + (card.get("approval_classes") or []), empty="(none)")
    lines.append("")
    lines.append("## HITL points")
    _bullets(lines, card.get("hitl") or [], empty="(none)")
    lines.append("")
    lines.append("## Eval criteria")
    _bullets(lines, card.get("eval_criteria") or [], empty="(none)")
    lines.append("")
    lines.append("## Acceptance drill")
    for fixture in card["drill"]["fixtures"]:
        lines.append(
            f"- workflow `{fixture['workflow_id']}` ← synthetic input; "
            f"expect status={fixture['expect']['status']}"
            + (", output contains: "
               + "; ".join(fixture["expect"]["output_contains"])
               if fixture["expect"]["output_contains"] else "")
        )
    if card["drill"].get("notes"):
        lines.append(f"- notes: {card['drill']['notes']}")
    lines.append("")
    rollout = card.get("rollout") or {}
    lines.append(
        f"## Rollout — rung: {rollout.get('rung', 'shadow')}"
    )
    if rollout.get("notes"):
        lines.append(rollout["notes"])
    lines.append("")
    lines.append("## Rollback plan")
    _bullets(lines, card.get("rollback") or [])
    estimates = card.get("estimates") or {}
    if estimates:
        lines.append("")
        lines.append("## Estimates")
        _bullets(lines, [
            f"{key}: {value}" for key, value in sorted(estimates.items())
        ])
    lines.append("")
    lines.append("## Supervising mission")
    mission = card.get("mission") or {}
    lines.append(
        f"- goal: {mission.get('goal', '?')}"
    )
    lines.append(
        f"- dry_run: {mission.get('dry_run', True)}, cadence: "
        f"{mission.get('cadence_seconds', '?')}s, capitol workflows: "
        f"{len((mission.get('capitol') or {}).get('workflows') or [])}"
    )
    questions = card.get("open_questions") or []
    lines.append("")
    lines.append("## Open questions")
    _bullets(lines, questions, empty="(none — the design is unambiguous)")
    return "\n".join(lines)


def diff_cards(old: Dict[str, Any], new: Dict[str, Any], *,
               old_label: str = "card v1",
               new_label: str = "card v2") -> str:
    """Unified diff between two card versions (pretty canonical JSON,
    so versions diff cleanly and deterministically)."""
    old_text = json.dumps(old, indent=2, sort_keys=True).splitlines()
    new_text = json.dumps(new, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(
        old_text, new_text, fromfile=old_label, tofile=new_label,
        lineterm="",
    ))
