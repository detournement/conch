"""Workflow-version/Procedure reconciliation and materialization locks."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import CapitolError
from ..packs.manifest import pack_digest
from ..procedures import CapitolProcedureClient

LOCK_SCHEMA = "conch.materialization_lock.v1"
_LOCK_KEYS = {
    "schema",
    "compilation_id",
    "card_version",
    "card_digest",
    "pack_digest",
    "workflows",
    "schedules",
}
_WORKFLOW_KEYS = {
    "workflow_id",
    "workflow_version_id",
    "workflow_version_number",
    "workflow_payload_digest",
    "adopted",
    "procedure",
}
_PROCEDURE_KEYS = {
    "procedure_document_id",
    "procedure_content_digest",
    "compiler_version",
    "verification",
}
_SCHEDULE_KEYS = {
    "workflow_id",
    "schedule_id",
    "enabled",
    "cron_expression",
}


def client_for_admin(admin) -> CapitolProcedureClient:
    return CapitolProcedureClient(
        admin.workflow_url,
        admin.org_id,
        admin._token,
    )


def lineage_metadata(
    store,
    compilation_id: str,
    card_row: Dict[str, Any],
) -> Dict[str, Any]:
    capture = store.compilation_capture(compilation_id) or {}
    capture_refs = {key: value for key, value in capture.items() if key != "evidence"}
    return {
        "compilation_id": compilation_id,
        "card_version": int(card_row["card_version"]),
        "card_digest": str(card_row["digest"]),
        "capture_refs": capture_refs,
        "pack_digest": pack_digest(card_row["card"]["pack"]["manifest"]),
    }


def verify_exact_workflow(
    client: CapitolProcedureClient,
    receipt: Dict[str, Any],
    *,
    require_latest: bool = True,
) -> Dict[str, Any]:
    workflow_id = str(receipt.get("workflow_id") or "")
    version_id = str(receipt.get("version_pin") or "")
    version_number = int(receipt.get("version_number") or 0)
    payload_digest = str(receipt.get("payload_digest") or "")
    if not all((workflow_id, version_id, payload_digest)) or version_number < 1:
        raise CapitolError(
            "workflow materialization receipt lacks an exact version id/"
            "number/payload digest (failing closed)"
        )
    version = client.get_workflow_version(workflow_id, version_id)
    if (
        str(version["workflow_id"]) != workflow_id
        or str(version["id"]) != version_id
        or int(version["version_number"]) != version_number
        or str(version["payload_digest"]) != payload_digest
    ):
        raise CapitolError(
            f"workflow {workflow_id} version pin/payload drifted (failing closed)"
        )
    if require_latest and not version["is_latest"]:
        raise CapitolError(
            f"workflow {workflow_id} latest version no longer matches "
            f"recorded pin {version_id}; starts/schedules are not "
            "version-addressed, so operation is ambiguous (failing closed)"
        )
    return version


def reconcile_procedure(
    store,
    compilation_id: str,
    card_row: Dict[str, Any],
    receipt: Dict[str, Any],
    client: CapitolProcedureClient,
    *,
    lineage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verify the exact workflow and append an idempotent link event.

    A missing generated document is retryable ``documentation_pending``.
    Contract drift, stale versions, or digest conflicts fail closed.
    """
    verify_exact_workflow(client, receipt)
    workflow_id = str(receipt["workflow_id"])
    version_id = str(receipt["version_pin"])
    try:
        document = client.get(
            workflow_id,
            workflow_version_id=version_id,
        )
    except CapitolError as exc:
        if getattr(exc, "http_status", None) == 404:
            return {
                "status": "documentation_pending",
                "workflow_id": workflow_id,
                "workflow_version_id": version_id,
                "workflow_version_number": int(receipt["version_number"]),
                "workflow_payload_digest": str(receipt["payload_digest"]),
            }
        raise
    if (
        str(document["workflow_id"]) != workflow_id
        or str(document["workflow_version_id"]) != version_id
        or int(document["version_number"]) != int(receipt["version_number"])
    ):
        raise CapitolError(
            f"Procedure {document.get('id')} does not project the exact "
            f"workflow version {version_id} (failing closed)"
        )
    lineage = lineage or lineage_metadata(store, compilation_id, card_row)
    link = {
        "workflow_id": workflow_id,
        "workflow_version_id": version_id,
        "workflow_version_number": int(receipt["version_number"]),
        "workflow_payload_digest": str(receipt["payload_digest"]),
        "procedure_document_id": str(document["id"]),
        "procedure_content_digest": str(document["content_digest"]),
        "compiler_version": str(document["compiler_version"]),
        "verification": str(document["verification"]),
        "card_version": int(card_row["card_version"]),
        "card_digest": str(card_row["digest"]),
        "lineage": lineage,
        "linked_at": time.time(),
    }
    appended = store.record_compilation_procedure_link(
        compilation_id,
        link,
    )
    return {
        "status": "linked",
        "appended": appended,
        "link": link,
    }


def adopted_receipt(
    source: Dict[str, Any],
    client: CapitolProcedureClient,
) -> Dict[str, Any]:
    receipt = {
        "workflow_id": str(source["workflow_id"]),
        "version_pin": str(source["workflow_version_id"]),
        "version_number": int(source["workflow_version_number"]),
        "payload_digest": str(source["workflow_payload_digest"]),
        "created": False,
        "adopted_existing": True,
        "rollback_ref": {"kind": "noop"},
    }
    verify_exact_workflow(client, receipt)
    document = client.get(
        receipt["workflow_id"],
        workflow_version_id=receipt["version_pin"],
    )
    if (
        str(document["id"]) != str(source["procedure_document_id"])
        or str(document["content_digest"]) != str(source["procedure_content_digest"])
        or str(document["compiler_version"]) != str(source["compiler_version"])
    ):
        raise CapitolError(
            f"adopted Procedure/workflow {receipt['workflow_id']} changed "
            "since Card approval (failing closed)"
        )
    return receipt


def _strict_keys(where: str, value: Any, expected: set) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CapitolError(f"{where} has unknown/missing fields (failing closed)")
    return value


def build_lock(
    compilation_id: str,
    card_row: Dict[str, Any],
    materialization: Dict[str, Any],
    procedure_state: Dict[str, Any],
) -> Dict[str, Any]:
    links = {
        str(link["workflow_version_id"]): link
        for link in procedure_state.get("links") or []
    }
    workflows: List[Dict[str, Any]] = []
    schedules: List[Dict[str, Any]] = []
    for step in materialization.get("steps") or []:
        receipt = step.get("receipt") or {}
        if step.get("step", "").startswith("workflow"):
            version_id = str(receipt.get("version_pin") or "")
            link = links.get(version_id)
            workflows.append(
                {
                    "workflow_id": str(receipt.get("workflow_id") or ""),
                    "workflow_version_id": version_id,
                    "workflow_version_number": int(receipt.get("version_number") or 0),
                    "workflow_payload_digest": str(receipt.get("payload_digest") or ""),
                    "adopted": bool(step.get("adopted")),
                    "procedure": (
                        {
                            "procedure_document_id": link["procedure_document_id"],
                            "procedure_content_digest": link[
                                "procedure_content_digest"
                            ],
                            "compiler_version": link["compiler_version"],
                            "verification": link["verification"],
                        }
                        if link
                        else None
                    ),
                }
            )
        elif step.get("step", "").startswith("schedule:"):
            schedules.append(
                {
                    "workflow_id": str(receipt.get("workflow_id") or ""),
                    "schedule_id": str(receipt.get("schedule_id") or ""),
                    "enabled": bool(receipt.get("enabled", False)),
                    "cron_expression": str(receipt.get("cron_expression") or ""),
                }
            )
    workflows.sort(
        key=lambda row: (
            row["workflow_id"],
            row["workflow_version_id"],
        )
    )
    schedules.sort(
        key=lambda row: (
            row["workflow_id"],
            row["schedule_id"],
        )
    )
    return {
        "schema": LOCK_SCHEMA,
        "compilation_id": compilation_id,
        "card_version": int(card_row["card_version"]),
        "card_digest": str(card_row["digest"]),
        "pack_digest": pack_digest(card_row["card"]["pack"]["manifest"]),
        "workflows": workflows,
        "schedules": schedules,
    }


def write_lock(directory: Path, lock: Dict[str, Any]) -> Dict[str, str]:
    path = Path(directory) / "materialization-lock.json"
    text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                lock,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    return {"path": str(path), "digest": digest}


def load_lock(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CapitolError(
            f"materialization lock unreadable at {path}: {exc}"
        ) from None
    lock = _strict_keys("materialization lock", value, _LOCK_KEYS)
    if lock["schema"] != LOCK_SCHEMA:
        raise CapitolError(f"unsupported materialization lock {lock['schema']!r}")
    if not isinstance(lock["workflows"], list) or not isinstance(
        lock["schedules"], list
    ):
        raise CapitolError("materialization lock lists are invalid")
    for index, workflow in enumerate(lock["workflows"]):
        row = _strict_keys(
            f"materialization lock workflows[{index}]",
            workflow,
            _WORKFLOW_KEYS,
        )
        procedure = row["procedure"]
        if procedure is not None:
            _strict_keys(
                f"materialization lock workflows[{index}].procedure",
                procedure,
                _PROCEDURE_KEYS,
            )
    for index, schedule in enumerate(lock["schedules"]):
        _strict_keys(
            f"materialization lock schedules[{index}]",
            schedule,
            _SCHEDULE_KEYS,
        )
    return lock


def verify_lock(
    lock: Dict[str, Any],
    *,
    card_row: Dict[str, Any],
    live_pack_digest: str,
    client: CapitolProcedureClient,
) -> Dict[str, Any]:
    if (
        str(lock["card_digest"]) != str(card_row["digest"])
        or int(lock["card_version"]) != int(card_row["card_version"])
        or str(lock["pack_digest"]) != str(live_pack_digest)
    ):
        raise CapitolError(
            "materialization lock/card/pack digest drift (failing closed)"
        )
    pending = []
    for workflow in lock["workflows"]:
        receipt = {
            "workflow_id": workflow["workflow_id"],
            "version_pin": workflow["workflow_version_id"],
            "version_number": workflow["workflow_version_number"],
            "payload_digest": workflow["workflow_payload_digest"],
        }
        verify_exact_workflow(client, receipt)
        procedure = workflow["procedure"]
        if procedure is None:
            pending.append(workflow["workflow_id"])
            continue
        document = client.get(
            workflow["workflow_id"],
            workflow_version_id=workflow["workflow_version_id"],
        )
        if (
            str(document["id"]) != procedure["procedure_document_id"]
            or str(document["content_digest"]) != procedure["procedure_content_digest"]
            or str(document["compiler_version"]) != procedure["compiler_version"]
        ):
            raise CapitolError(
                f"Procedure drift for workflow {workflow['workflow_id']} "
                "(failing closed)"
            )
    enabled = [row["schedule_id"] for row in lock["schedules"] if row["enabled"]]
    if enabled:
        raise CapitolError(
            "version-ambiguous schedules must remain disabled in shadow: "
            + ", ".join(enabled)
        )
    return {"documentation_pending": pending}
