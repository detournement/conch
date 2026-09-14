"""Shared deterministic fixtures for the ProcessCompiler tests.

A fake node catalog (the minimal struct shapes the payload generator
needs), a fake discovery snapshot, a valid candidate card (the plan's
weekday-funding-report process), a fake CapitolAdmin recording every
mutation with its own idempotency replay, and a fake run driver for the
drill gate.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from conch.capitol.errors import CapitolError

PREFIX = "conch-compile"
WORKFLOW_IDENTITY = f"{PREFIX}-funding-daily-report"
AGENT_IDENTITY = f"{PREFIX}-funding-report"
SCHEDULE_IDENTITY = f"{PREFIX}-funding-report-5pm"
PACK_NAME = f"{PREFIX}-funding-report"


def catalog_struct(node_id: str, params, out_ports,
                   in_ports: int = 0) -> Dict[str, Any]:
    return {
        "node_id": node_id,
        "display_name": node_id,
        "params": [
            {
                "field_id": field,
                "value": None,
                "bindable_config": (
                    {"is_bound": False,
                     "input_port": {"id": "p", "name": field,
                                    "incoming_connections": []}}
                    if bindable else None
                ),
            }
            for field, bindable in params
        ],
        "output_ports": [{"id": "o", "name": name} for name in out_ports],
        "input_ports": [
            {"id": f"in{i}", "name": "tools", "incoming_connections": []}
            for i in range(in_ports)
        ],
    }


def fake_catalog() -> Dict[str, Dict[str, Any]]:
    return {
        "text_input_node": catalog_struct(
            "text_input_node", [("text_input", False)], ["text"],
        ),
        "json_input_node": catalog_struct(
            "json_input_node",
            [("mode", False), ("value", False), ("on_invalid", False)],
            ["value"],
        ),
        "agent_node": catalog_struct(
            "agent_node",
            [("name", False), ("system_prompt", False),
             ("temperature", False), ("timeout", False),
             ("tool_choice", False), ("model", False),
             ("max_tokens", False), ("user_prompt", True)],
            ["text"], in_ports=1,
        ),
        "docx_chat_node": catalog_struct(
            "docx_chat_node",
            [("ground_with_provided_info", False), ("request", True)],
            ["docx_output"],
        ),
        "markdown_output_node": catalog_struct(
            "markdown_output_node", [("markdown_content", True)], [],
        ),
        "notify_node": catalog_struct(
            "notify_node",
            [("title", False), ("event_subtype", False),
             ("message", True)],
            [],
        ),
        "search_unstructured_collection": catalog_struct(
            "search_unstructured_collection", [], ["tool"],
        ),
    }


def fake_discovery() -> Dict[str, Any]:
    return {
        "workflows": [
            {"id": "wf-ingest", "name": "together-funding-ingest"},
            {"id": "wf-packet", "name": "together-funding-packet"},
        ],
        "agents": [{"id": "ag-funding", "name": "together-funding"}],
        "collections": [
            {"id": "col-ledger", "name": "together-funding-requests"},
        ],
        "packs": [{"name": "ebay-listing"}],
        "node_catalog": sorted(fake_catalog()),
        "conch": ["capitol_control: drive Capitol workflows"],
        "notes": [],
    }


def candidate_card() -> Dict[str, Any]:
    """The plan's C2 candidate as a raw (pre-normalization) card."""
    return {
        "schema": "conch.architecture_card.v1",
        "goal": (
            "each weekday at 5pm, summarize the day's together-funding "
            "ledger activity into a short report and notify me"
        ),
        "success_criteria": [
            "a report document is produced each weekday at 5pm",
            "the report reflects that day's ledger events only",
        ],
        "narrative": (
            "A single small workflow reads the day's events from the "
            "existing together-funding-requests ledger collection, an "
            "agent summarizes them into a short report, the docx node "
            "renders it, and the notify node announces it. The schedule "
            "fires weekdays at 17:00 and ships disabled (shadow)."
        ),
        "assets": {
            "reuse": [{
                "kind": "collection", "id": "col-ledger",
                "name": "together-funding-requests",
                "reason": "the ledger the report summarizes",
            }],
            "create": {
                "workflows": [{
                    "identity": WORKFLOW_IDENTITY,
                    "name": WORKFLOW_IDENTITY,
                    "description": "daily funding-ledger digest",
                    "stages": [
                        {"kind": "text_input", "role": "window",
                         "name": "Report Window",
                         "default": "today",
                         "info": (
                             "'today' (scheduled default) or a JSON "
                             "array of synthetic ledger rows (test seam)"
                         )},
                        {"kind": "agent", "role": "summarize",
                         "name": "Ledger Summarizer",
                         "system_prompt": (
                             "You summarize the day's together-funding "
                             "ledger activity from collection "
                             "$collection:col-ledger. If the input "
                             "parses as a JSON array, treat it as "
                             "synthetic ledger rows and summarize those "
                             "instead of searching. Start the report "
                             "with the exact line FUNDING LEDGER DAILY "
                             "REPORT."
                         ),
                         "tools": ["search_unstructured_collection"],
                         "temperature": 0.1, "timeout": 300},
                        {"kind": "docx", "role": "report",
                         "name": "Daily Report Document",
                         "from": "summarize"},
                        {"kind": "notify", "role": "notify",
                         "name": "Report Ready",
                         "title": "Funding ledger daily report",
                         "from": "summarize"},
                    ],
                }],
                "agent": {
                    "identity": AGENT_IDENTITY,
                    "name": AGENT_IDENTITY,
                    "description": "operates the daily report",
                    "workflows": [f"$create:{WORKFLOW_IDENTITY}"],
                },
                "schedules": [{
                    "identity": SCHEDULE_IDENTITY,
                    "name": SCHEDULE_IDENTITY,
                    "workflow": f"$create:{WORKFLOW_IDENTITY}",
                    "cron": "0 17 * * 1-5",
                    "timezone": "UTC",
                    "enabled": False,
                }],
                "collections": [],
            },
        },
        "pack": {
            "name": PACK_NAME,
            "description": "compiled daily funding-ledger report",
        },
        "caps": ["read-only over the ledger; no external spend"],
        "approval_classes": ["schedule enablement is operator-explicit"],
        "hitl": ["the user reviews the report; schedule armed manually"],
        "eval_criteria": [
            "the summary covers every ledger event in the window",
        ],
        "drill": {
            "fixtures": [{
                "workflow": f"$create:{WORKFLOW_IDENTITY}",
                "input": json.dumps([
                    {"external_id": "together:funding:synth-1",
                     "status": "triggered", "company": "Lumen Robotics",
                     "from": "ada@example.com", "confidence": 0.91},
                    {"external_id": "together:funding:synth-2",
                     "status": "dismissed",
                     "from": "digest@example.org", "confidence": 0.05},
                ]),
                "expect": {
                    "status": "success",
                    "output_contains": ["FUNDING LEDGER DAILY REPORT"],
                },
            }],
            "notes": "synthetic ledger rows through the real workflow",
        },
        "rollout": {"rung": "shadow",
                    "notes": "schedule disabled until the user arms it"},
        "rollback": [
            "delete the schedule, agent, and workflow",
            "remove the generated pack directory",
            "abort the supervising mission",
        ],
        "estimates": {"cost": "one small agent call per weekday",
                      "latency": "~1 minute per report"},
        "open_questions": [],
        "mission": {
            "goal": "supervise the compiled funding daily report",
            "budgets": {"sessions": 24},
            "cadence_seconds": 3600,
            "capitol": {
                "workflows": [f"$create:{WORKFLOW_IDENTITY}"],
                "allow_start": False,
                "allow_respond": True,
                "bind_scheduled": True,
            },
        },
    }


class FakeAdmin:
    """CapitolAdmin stand-in: records mutations in order, replays by
    idempotency key (the kernel-ledger behavior), and can be armed to
    fail at a named step."""

    def __init__(self, fail_at: str = ""):
        self.calls: List[str] = []
        self.effects: List[str] = []
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self.fail_at = fail_at
        self.workflow_url = "http://localhost:8300"
        self.platform_url = "http://localhost:8811"
        self.org_id = "org-test"
        self._token = "test-token"

    def _mutate(self, op: str, key: str, effect) -> Dict[str, Any]:
        self.calls.append(f"{op}:{key}")
        if op == self.fail_at:
            raise CapitolError(f"injected failure at {op}")
        if key in self._by_key:
            return dict(self._by_key[key], replayed=True)
        result = effect()
        self._by_key[key] = result
        self.effects.append(op)
        return dict(result)

    def create_collection(self, name, *, idempotency_key,
                          destination="qdrant", description=""):
        return self._mutate(
            "create_collection", idempotency_key,
            lambda: {"collection_id": f"col-{name}", "name": name,
                     "rollback_ref": {"kind": "delete_collection",
                                      "collection_id": f"col-{name}"}},
        )

    def persist_workflow(self, payload, *, idempotency_key):
        workflow_id = payload["id"]
        return self._mutate(
            "persist_workflow", idempotency_key,
            lambda: {"workflow_id": workflow_id,
                     "name": payload.get("name", ""),
                     "created": True, "version_pin": "v1",
                     "rollback_ref": {"kind": "delete_workflow",
                                      "workflow_id": workflow_id}},
        )

    def create_orchestrator_agent(self, name, workflow_ids, *,
                                  idempotency_key, description="",
                                  registry_alias="", **_kwargs):
        return self._mutate(
            "create_agent", idempotency_key,
            lambda: {"agent_id": f"ag-{name}", "name": name,
                     "workflow_allowlist": list(workflow_ids),
                     "rollback_ref": {"kind": "delete_agent",
                                      "agent_id": f"ag-{name}"}},
        )

    def set_workflow_allowlist(self, agent_id, workflow_ids, *,
                               idempotency_key):
        return self._mutate(
            "set_workflow_allowlist", idempotency_key,
            lambda: {"agent_id": agent_id,
                     "applied": {"workflow_allowlist": list(workflow_ids)},
                     "rollback_ref": {"kind": "patch_agent",
                                      "agent_id": agent_id, "prior": {}}},
        )

    def create_schedule(self, workflow_id, name, cron, *,
                        idempotency_key, timezone="UTC",
                        input_overrides=None, enabled=True):
        return self._mutate(
            "create_schedule", idempotency_key,
            lambda: {"workflow_id": workflow_id,
                     "schedule_id": f"sch-{name}",
                     "cron_expression": cron, "enabled": enabled,
                     "rollback_ref": {"kind": "delete_schedule",
                                      "workflow_id": workflow_id,
                                      "schedule_id": f"sch-{name}"}},
        )

    def delete_schedule(self, workflow_id, schedule_id, *,
                        idempotency_key):
        return self._mutate(
            "delete_schedule", idempotency_key,
            lambda: {"workflow_id": workflow_id,
                     "schedule_id": schedule_id, "deleted": True},
        )

    def delete_agent(self, agent_id, *, idempotency_key):
        return self._mutate(
            "delete_agent", idempotency_key,
            lambda: {"agent_id": agent_id, "deleted": True},
        )

    def delete_workflow(self, workflow_id, *, idempotency_key):
        return self._mutate(
            "delete_workflow", idempotency_key,
            lambda: {"workflow_id": workflow_id, "deleted": True},
        )

    def delete_collection(self, collection_id, *, idempotency_key):
        return self._mutate(
            "delete_collection", idempotency_key,
            lambda: {"collection_id": collection_id, "deleted": True},
        )


class FakeRunDriver:
    """Run driver stand-in for the drill gate."""

    def __init__(self, status: str = "success",
                 output_marker: str = "FUNDING LEDGER DAILY REPORT"):
        self.status = status
        self.output_marker = output_marker
        self.triggers: List[Dict[str, Any]] = []
        self._runs = 0

    def trigger(self, workflow_id, overrides=None, version_id=""):
        self._runs += 1
        self.triggers.append({
            "workflow_id": workflow_id,
            "overrides": dict(overrides or {}),
        })
        return {"run_id": f"run-{self._runs}"}

    def wait_terminal(self, workflow_id, run_id, *, timeout=900.0,
                      poll=10.0, log=print):
        detail: Dict[str, Any] = {
            "status": self.status, "run_id": run_id,
            "workflow_id": workflow_id,
        }
        if self.status == "success":
            detail["node_results"] = [
                {"output_data": {"text": self.output_marker}}
            ]
        else:
            detail["error_message"] = "injected drill failure"
        return detail


LOCAL_CONFIG = {
    "capitol_base_url": "http://localhost:8300",
    "capitol_platform_url": "http://localhost:8811",
    "capitol_org": "org-test",
    "capitol_admin": "true",
}
