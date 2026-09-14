"""Materialization, validation gate, and rollback for approved cards.

Materialization drives the existing ledgered :class:`CapitolAdmin`
surface in the card's declared order — collections → workflows (payloads
generated deterministically from the approved stages against the live
node catalog) → agent + exact allowlist → schedules — then writes the
generated flow pack and installs the drill fixtures. Every mutation is
idempotency-keyed on the compilation id + step, so re-materializing an
already-materialized card replays receipts from the kernel ledger without
touching Capitol. Receipts (with their rollback refs) are recorded on the
compilation aggregate cumulatively, so a partial failure stops, reports,
and leaves everything needed for ``/compile rollback``.

The validation gate then loads the pack fail-closed, runs the generated
acceptance drill, and only on a pass creates the supervising mission
(dry-run enforced at card validation) — advancing status
compiled→materialized→verified→operating, one kernel event each.

Safety invariants enforced here:

- **User authority exactly**: everything goes through
  ``CapitolAdmin.from_config`` — no ``capitol_admin=true``, no
  materialization.
- **Approval is the authorization**: the compilation must be approved and
  the approved digest must still match the current card version.
- **Local org only (v1)**: non-local Capitol base URLs are refused.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from ...kernel.model import CompilationStatus, KernelError
from ..admin import CapitolAdmin
from ..errors import CapitolError
from . import drill as drill_mod
from .graph import build_workflow_payload, payload_digest

#: The compiler's kernel ledger anchor (the /capitol admin CLI pattern).
COMPILER_ANCHOR_GOAL_PREFIX = "process-compiler"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def ensure_local_stack(config: dict) -> None:
    """v1 materializes on the local/sandbox org only (fail closed)."""
    for key in ("capitol_base_url", "capitol_platform_url"):
        url = str(config.get(key) or "").strip()
        if not url:
            raise CapitolError(
                f"materialization needs {key} (the local serving stack)"
            )
        host = (urlparse(url).hostname or "").lower()
        if host not in _LOCAL_HOSTS:
            raise CapitolError(
                f"the process compiler materializes on the local stack "
                f"only in v1; {key}={url} is not a local address "
                "(failing closed)"
            )


def compiler_anchor_mission(store) -> str:
    """Find-or-create the kernel mission anchoring the compiler's
    external-action ledger entries."""
    for mission in store.list_missions():
        goal = str((mission.get("spec") or {}).get("goal") or "")
        if goal.startswith(COMPILER_ANCHOR_GOAL_PREFIX):
            return mission["mission_id"]
    return store.create_mission({
        "goal": (
            f"{COMPILER_ANCHOR_GOAL_PREFIX}: ledger anchor for compiled-"
            "process materialization"
        ),
        "budgets": {},
    })


def _admin(store, config: dict) -> CapitolAdmin:
    return CapitolAdmin.from_config(
        config, store=store, mission_id=compiler_anchor_mission(store),
    )


def _require_approved(compilation: Dict[str, Any],
                      card_row: Dict[str, Any]) -> None:
    status = compilation["status"]
    if status not in (CompilationStatus.APPROVED,
                      CompilationStatus.MATERIALIZED,
                      CompilationStatus.VERIFIED,
                      CompilationStatus.OPERATING):
        raise CapitolError(
            f"compilation {compilation['compilation_id']} is {status}; "
            "materialization needs an approved card "
            "(/compile approve <id>)"
        )
    if int(compilation["approved_version"] or 0) != int(
        card_row["card_version"]
    ) or str(compilation["approved_digest"] or "") != str(
        card_row["digest"]
    ):
        if status == CompilationStatus.APPROVED:
            raise CapitolError(
                "the approval does not pin the current card version — "
                "re-approve after the revision"
            )


def _step_key(compilation_id: str, step: str, extra: str = "") -> str:
    key = f"compile:{compilation_id}:{step}"
    return f"{key}:{extra}" if extra else key


def default_packs_dir() -> Path:
    from ..packs.registry import user_packs_dir

    return user_packs_dir()


def _write_pack(card: Dict[str, Any], packs_dir: Path) -> str:
    """Write the generated pack + drill fixtures (idempotent overwrite;
    the manifest was already validated fail-closed at card time)."""
    pack = card["pack"]
    directory = Path(packs_dir) / pack["name"]
    (directory / "assets").mkdir(parents=True, exist_ok=True)
    (directory / "pack.json").write_text(
        json.dumps(pack["manifest"], indent=2, sort_keys=True) + "\n"
    )
    (directory / "assets" / "drill.json").write_text(
        json.dumps(card["drill"]["fixtures"], indent=2, sort_keys=True)
        + "\n"
    )
    return str(directory)


def materialize_compilation(
    store,
    config: dict,
    compilation_id: str,
    *,
    admin: Optional[CapitolAdmin] = None,
    catalog: Optional[Dict[str, Dict[str, Any]]] = None,
    packs_dir: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Provision everything the approved card declares, in order.

    Idempotent end-to-end. On partial failure the cumulative receipts
    (with rollback refs) are recorded before the error propagates.
    """
    compilation = store.get_compilation(compilation_id)
    if compilation is None:
        raise CapitolError(f"unknown compilation {compilation_id!r}")
    card_row = store.compilation_card(compilation_id)
    _require_approved(compilation, card_row)
    ensure_local_stack(config)
    card = card_row["card"]
    admin = admin or _admin(store, config)
    if catalog is None:
        from ..together_funding import fetch_node_catalog

        catalog = fetch_node_catalog(
            admin.workflow_url, admin.org_id, admin._token
        )
    packs_dir = Path(packs_dir) if packs_dir else default_packs_dir()

    state: Dict[str, Any] = dict(compilation.get("materialization") or {})
    steps: List[Dict[str, Any]] = list(state.get("steps") or [])
    done = {step["step"] for step in steps}
    state["steps"] = steps
    state.pop("error", None)

    def record(complete: bool = False, error: str = "") -> None:
        store.record_compilation_materialization(
            compilation_id, state, complete=complete, error=error,
        )

    def run_step(step_name: str, fn) -> Dict[str, Any]:
        receipt = fn()
        row = {
            "step": step_name,
            "receipt": receipt,
            "rollback_ref": receipt.get("rollback_ref") or {},
            "adopted": bool(receipt.get("adopted_existing")),
        }
        replaced = False
        for index, existing in enumerate(steps):
            if existing["step"] == step_name:
                steps[index] = row
                replaced = True
        if not replaced:
            steps.append(row)
        done.add(step_name)
        record()
        return receipt

    create = card["assets"]["create"]
    collection_ids: Dict[str, str] = dict(state.get("collections") or {})
    try:
        # 1. Collections (their ids are baked into agent prompts).
        for collection in create.get("collections") or []:
            identity = collection["identity"]
            receipt = run_step(
                f"collection:{identity}",
                lambda c=collection: admin.create_collection(
                    c["name"],
                    idempotency_key=_step_key(
                        compilation_id, f"collection:{c['identity']}"
                    ),
                    description=c.get("description", ""),
                ),
            )
            collection_ids[identity] = str(
                receipt.get("collection_id") or ""
            )
            log(f"  collection {identity}: "
                f"{collection_ids[identity]}"
                + (" (replayed)" if receipt.get("replayed") else ""))
        state["collections"] = collection_ids

        # 2. Workflows — deterministic payloads from the approved stages.
        workflow_ids: List[str] = []
        for workflow in create.get("workflows") or []:
            identity = workflow["identity"]
            payload = build_workflow_payload(
                catalog, workflow,
                collection_ids=_collection_map(
                    workflow, collection_ids
                ),
            )
            digest = payload_digest(payload)
            receipt = run_step(
                f"workflow:{identity}",
                lambda p=payload, i=identity, d=digest:
                admin.persist_workflow(
                    p,
                    idempotency_key=_step_key(
                        compilation_id, f"workflow:{i}", d[:16]
                    ),
                ),
            )
            workflow_ids.append(str(receipt.get("workflow_id") or ""))
            log(f"  workflow {identity}: {receipt.get('workflow_id')} "
                f"version {receipt.get('version_pin')}"
                + (" (replayed)" if receipt.get("replayed") else ""))

        # 3. Agent + exact allowlist.
        agent_spec = create.get("agent")
        if agent_spec:
            receipt = run_step(
                f"agent:{agent_spec['identity']}",
                lambda a=agent_spec: admin.create_orchestrator_agent(
                    a["name"], list(a["workflows"]),
                    idempotency_key=_step_key(
                        compilation_id, f"agent:{a['identity']}"
                    ),
                    description=a.get("description", ""),
                    registry_alias=a["identity"][:48],
                ),
            )
            log(f"  agent {agent_spec['identity']}: "
                f"{receipt.get('agent_id')}"
                + (" (adopted)" if receipt.get("adopted_existing")
                   else ""))
            if receipt.get("adopted_existing"):
                run_step(
                    f"allowlist:{agent_spec['identity']}",
                    lambda a=agent_spec, r=receipt:
                    admin.set_workflow_allowlist(
                        str(r.get("agent_id") or ""),
                        list(a["workflows"]),
                        idempotency_key=_step_key(
                            compilation_id,
                            f"allowlist:{a['identity']}",
                        ),
                    ),
                )

        # 4. Schedules (enabled exactly as the approved card says —
        #    shadow rollout ships them disabled).
        for schedule in create.get("schedules") or []:
            identity = schedule["identity"]
            receipt = run_step(
                f"schedule:{identity}",
                lambda s=schedule: admin.create_schedule(
                    s["workflow"], s["name"], s["cron"],
                    idempotency_key=_step_key(
                        compilation_id, f"schedule:{s['identity']}"
                    ),
                    timezone=s.get("timezone", "UTC"),
                    input_overrides=s.get("input"),
                    enabled=bool(s.get("enabled", False)),
                ),
            )
            log(f"  schedule {identity}: {receipt.get('schedule_id')} "
                f"({schedule['cron']}, enabled="
                f"{bool(schedule.get('enabled', False))})"
                + (" (adopted)" if receipt.get("adopted_existing")
                   else ""))

        # 5. The generated pack + drill fixtures (local file write; the
        #    rollback ref is the directory).
        pack_dir = _write_pack(card, packs_dir)
        state["pack_dir"] = pack_dir
        log(f"  pack: {pack_dir}")
        record(complete=True)
    except (CapitolError, KernelError) as exc:
        try:
            record(complete=False, error=str(exc))
        except KernelError:
            pass
        raise CapitolError(
            f"materialization stopped after {len(steps)} step(s): {exc} "
            f"— everything provisioned so far is recorded; revert with "
            f"/compile rollback {compilation_id}"
        ) from exc
    return store.get_compilation(compilation_id)["materialization"]


def _collection_map(workflow: Dict[str, Any],
                    created: Dict[str, str]) -> Dict[str, str]:
    """``$collection:`` reference map for one workflow: created
    identities resolve to their provisioned ids; anything else (a
    discovered collection id, validated at card time) maps to itself."""
    from .graph import _COLLECTION_PLACEHOLDER_RE

    mapping = dict(created)
    for stage in workflow.get("stages") or []:
        for ref in _COLLECTION_PLACEHOLDER_RE.findall(
            str(stage.get("system_prompt") or "")
        ):
            mapping.setdefault(ref, ref)
    return mapping


# ---------------------------------------------------------------------------
# Validation gate: pack load → drill → supervising mission
# ---------------------------------------------------------------------------

def verify_compilation(
    store,
    config: dict,
    compilation_id: str,
    *,
    driver=None,
    packs_dir: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Fail-closed pack load → acceptance drill → supervising mission.

    A drill failure records the failure on the compilation (status stays
    ``materialized``) and raises; only a pass advances
    materialized→verified→operating.
    """
    compilation = store.get_compilation(compilation_id)
    if compilation is None:
        raise CapitolError(f"unknown compilation {compilation_id!r}")
    if compilation["status"] not in (CompilationStatus.MATERIALIZED,
                                     CompilationStatus.VERIFIED,
                                     CompilationStatus.OPERATING):
        raise CapitolError(
            f"compilation {compilation_id} is {compilation['status']}; "
            "verification runs after materialization"
        )
    card = store.compilation_card(compilation_id)["card"]
    packs_dir = Path(packs_dir) if packs_dir else default_packs_dir()
    pack_name = card["pack"]["name"]

    # 1. Fail-closed pack load (the loader validates the manifest and
    #    the directory-name pin).
    from ..packs.registry import load_pack_dir

    pack = load_pack_dir(packs_dir / pack_name)
    log(f"  pack {pack.name}: loads clean ({pack.digest[:23]}…)")

    # 2. The generated acceptance drill.
    try:
        evidence = drill_mod.run_workflow_drill(
            pack, config, driver=driver, log=log,
        )
    except CapitolError as exc:
        store.record_compilation_drill(
            compilation_id,
            {"error": str(exc)[:1000], "pack": pack.name},
            passed=False,
        )
        raise CapitolError(
            f"acceptance drill FAILED: {exc} — the compilation stays "
            f"materialized with the failure attached; revert with "
            f"/compile rollback {compilation_id} or fix and re-verify"
        ) from exc
    store.record_compilation_drill(compilation_id, evidence, passed=True)
    log(f"  drill: {len(evidence.get('runs') or [])} run(s) passed")

    # 3. The supervising mission (dry-run enforced at card validation).
    compilation = store.get_compilation(compilation_id)
    mission_id = compilation.get("mission_id") or ""
    if compilation["status"] == CompilationStatus.VERIFIED:
        from ...kernel.engine import MissionEngine

        engine = MissionEngine(store, config)
        mission_id = engine.create_mission(dict(card["mission"]))
        store.transition_compilation(
            compilation_id, CompilationStatus.OPERATING,
            reason="supervising mission created", mission_id=mission_id,
        )
        log(f"  supervising mission: {mission_id} (dry-run)")
    return {
        "drill": evidence,
        "mission_id": mission_id,
        "status": store.get_compilation(compilation_id)["status"],
    }


# ---------------------------------------------------------------------------
# Rollback — everything materialized so far, in reverse, via the
# recorded rollback refs
# ---------------------------------------------------------------------------

def rollback_compilation(
    store,
    config: dict,
    compilation_id: str,
    *,
    admin: Optional[CapitolAdmin] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    compilation = store.get_compilation(compilation_id)
    if compilation is None:
        raise CapitolError(f"unknown compilation {compilation_id!r}")
    status = compilation["status"]
    if status not in (CompilationStatus.APPROVED,
                      CompilationStatus.MATERIALIZED,
                      CompilationStatus.VERIFIED,
                      CompilationStatus.OPERATING):
        raise CapitolError(
            f"compilation {compilation_id} is {status}; nothing to roll "
            "back"
        )
    state = dict(compilation.get("materialization") or {})
    steps = list(state.get("steps") or [])
    if not steps and not state.get("pack_dir"):
        raise CapitolError(
            f"compilation {compilation_id} has no recorded "
            "materialization receipts"
        )
    ensure_local_stack(config)
    admin = admin or _admin(store, config)
    outcome: Dict[str, Any] = {"reverted": [], "skipped": []}

    # The supervising mission first (stop supervision before tearing
    # down what it supervises).
    if compilation.get("mission_id"):
        from ...kernel.engine import MissionEngine

        try:
            MissionEngine(store, config).abort_mission(
                compilation["mission_id"]
            )
            outcome["reverted"].append(
                f"mission:{compilation['mission_id']}"
            )
            log(f"  mission {compilation['mission_id']}: aborted")
        except KernelError as exc:
            outcome["skipped"].append(
                f"mission:{compilation['mission_id']} ({exc})"
            )

    for step in reversed(steps):
        name = step["step"]
        ref = step.get("rollback_ref") or {}
        if step.get("adopted"):
            outcome["skipped"].append(
                f"{name} (adopted a pre-existing asset; not deleting)"
            )
            log(f"  {name}: skipped (adopted existing)")
            continue
        kind = str(ref.get("kind") or "")
        key = _step_key(compilation_id, f"rollback:{name}")
        try:
            if kind == "delete_schedule":
                admin.delete_schedule(
                    str(ref.get("workflow_id") or ""),
                    str(ref.get("schedule_id") or ""),
                    idempotency_key=key,
                )
            elif kind == "delete_agent":
                admin.delete_agent(
                    str(ref.get("agent_id") or ""), idempotency_key=key,
                )
            elif kind == "delete_workflow":
                admin.delete_workflow(
                    str(ref.get("workflow_id") or ""),
                    idempotency_key=key,
                )
            elif kind == "delete_collection":
                admin.delete_collection(
                    str(ref.get("collection_id") or ""),
                    idempotency_key=key,
                )
            elif kind == "patch_agent":
                outcome["skipped"].append(
                    f"{name} (allowlist patch on an adopted agent)"
                )
                log(f"  {name}: skipped (patch on adopted agent)")
                continue
            elif kind in ("noop", ""):
                outcome["skipped"].append(f"{name} (no rollback ref)")
                log(f"  {name}: skipped (no rollback ref)")
                continue
            else:
                outcome["skipped"].append(
                    f"{name} (unknown rollback kind {kind!r})"
                )
                log(f"  {name}: skipped (unknown kind {kind!r})")
                continue
        except CapitolError as exc:
            raise CapitolError(
                f"rollback stopped at {name}: {exc} — already-reverted "
                f"steps are recorded; re-run /compile rollback "
                f"{compilation_id} after fixing"
            ) from exc
        outcome["reverted"].append(name)
        log(f"  {name}: reverted ({kind})")

    pack_dir = str(state.get("pack_dir") or "")
    if pack_dir:
        path = Path(pack_dir)
        card = store.compilation_card(compilation_id)["card"]
        if path.name == card["pack"]["name"] and path.is_dir():
            shutil.rmtree(path)
            outcome["reverted"].append(f"pack_dir:{path}")
            log(f"  pack dir {path}: removed")
    state["rolled_back"] = outcome
    store.record_compilation_materialization(
        compilation_id, state, complete=False,
    )
    store.transition_compilation(
        compilation_id, CompilationStatus.ROLLED_BACK,
        reason="operator rollback",
    )
    return outcome
