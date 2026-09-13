"""``capitol_control`` — the model-callable Capitol runtime surface.

The natural-language door onto :class:`~conch.capitol.client.CapitolRuntime`
(control design §2, the /capitol op→method mapping): a session's model can
discover the bound agent and org directory, read the workflow catalog,
start runs under a REQUIRED idempotency key, watch a run to a bounded
deadline and summarize its events, answer both HITL kinds, fetch outputs
and eval roll-ups, and move artifacts through the channel quarantine
directory. RUNTIME surface only:

- Every admin/provisioning op (agents, persist/publish/rollback,
  allowlists, schedules, collections, bearers) is refused by name with
  the user-explicit ``/capitol admin …`` command spelled out — the
  builder profile (:mod:`conch.capitol.admin`) is never model-callable.
- Pack manifests are user-edited data; the tool never mutates packs
  (``/capitol pack list|show|verify`` is the user surface).

Session authority (the personal_items precedent):

- Interactive sessions get the tool whenever Capitol is configured
  (``capitol_base_url`` set); construction is lazy per call, so
  sessions that never use it never touch the adapter.
- Remote/channel sessions keep reads and HITL responses; an effectful
  ``start`` becomes an origin-bound expiring approval instead (the
  RemoteShellClient propose→approve pattern): the payload pinned at
  propose time — workflow, inputs, idempotency key — is exactly what an
  approve executes, never fresh model input.
- Delegated sub-turns and fleet workers never see the tool implicitly;
  a skill's tool list or a task envelope naming it is the explicit
  offer (``DelegateTaskClient.IMPLICITLY_EXCLUDED_TOOLS``).
- Mission sessions keep the envelope-scoped ``capitol_control``
  (:mod:`conch.capitol.supervisor`); the kernel engine swaps this tool
  out for that one, so mission authority stays spec-derived.

Effectful calls consult the same required-policy events the mission
supervisor and pack engine already use (``capitol.run.start``,
``capitol.hitl.respond``) and always carry an idempotency key —
auto-derived as a canonical digest of (workflow, inputs) when the model
omits one, so an accidental retry replays the original run instead of
double-starting. Watch is bounded by a hard deadline and returns a
summary — never an indefinite stream from a tool call.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..policy import evaluate_required_policy
from .client import (
    FINAL_STATUS_EVENT,
    KEEPALIVE_EVENT,
    TERMINAL_EVENT_TYPES,
    TERMINAL_RUN_STATUSES,
    CapitolRuntime,
)
from .errors import CapitolAuthError, CapitolCapabilityError, CapitolError
from .packs.templates import clean_text

#: Hard bounds on the watch deadline (seconds): a tool call may poll, but
#: never streams indefinitely.
WATCH_DEADLINE_DEFAULT = 120.0
WATCH_DEADLINE_MAX = 600.0
WATCH_DEADLINE_MIN = 5.0
WATCH_POLL_SECONDS = 2.0

#: Payload render budget for JSON-ish op results.
RESULT_BUDGET = 6000

#: Approval-store kind for remote-proposed starts; consuming one
#: constructs the exact pinned call_workflow, never a command.
REMOTE_START_KIND = "capitol_start"

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Admin/provisioning intents by op name → the user-explicit command the
#: refusal names. Model-callable never; /capitol admin is the operator's.
ADMIN_REFUSALS = {
    "admin": "/capitol admin …",
    "create-agent": "/capitol admin create-agent <name> --workflows a,b",
    "create_agent": "/capitol admin create-agent <name> --workflows a,b",
    "delete-agent": "/capitol admin delete-agent <id>",
    "delete_agent": "/capitol admin delete-agent <id>",
    "allowlist": "/capitol admin allowlist <agent> <wf,…>",
    "bind-collections": "/capitol admin bind-collections <agent> <id,…>",
    "bind_collections": "/capitol admin bind-collections <agent> <id,…>",
    "persist": "/capitol admin persist @payload.json",
    "persist_workflow": "/capitol admin persist @payload.json",
    "publish": "/capitol admin publish <workflow>",
    "publish_workflow": "/capitol admin publish <workflow>",
    "rollback": "/capitol admin rollback <workflow>",
    "rollback_workflow": "/capitol admin rollback <workflow>",
    "delete-workflow": "/capitol admin delete-workflow <workflow>",
    "delete_workflow": "/capitol admin delete-workflow <workflow>",
    "schedule": "/capitol admin schedules <workflow> / schedule-add …",
    "schedules": "/capitol admin schedules <workflow> / schedule-add …",
    "schedule-add": "/capitol admin schedule-add <wf> <name> <cron>",
    "schedule_add": "/capitol admin schedule-add <wf> <name> <cron>",
    "schedule-update": "/capitol admin schedule-update <wf> <id> @updates.json",
    "schedule_update": "/capitol admin schedule-update <wf> <id> @updates.json",
    "schedule-delete": "/capitol admin schedule-delete <wf> <id>",
    "schedule_delete": "/capitol admin schedule-delete <wf> <id>",
    "collections": "/capitol admin collections [create <name> | delete <id>]",
    "create-collection": "/capitol admin collections create <name>",
    "create_collection": "/capitol admin collections create <name>",
    "rotate-bearer": "/capitol admin rotate-bearer <agent>",
    "rotate_bearer": "/capitol admin rotate-bearer <agent>",
    "mint-bearer": "/capitol admin mint-bearer <agent> <label>",
    "mint_bearer": "/capitol admin mint-bearer <agent> <label>",
    "revoke-bearer": "/capitol admin revoke-bearer <agent> <bearer_id>",
    "revoke_bearer": "/capitol admin revoke-bearer <agent> <bearer_id>",
    "provision": "/capitol admin persist/publish/schedule-add …",
}

#: Pack mutations are user edits to pack files, never tool calls.
PACK_REFUSALS = frozenset({
    "pack", "packs", "pack-edit", "pack_edit", "pack-write", "pack_write",
    "pack-verify", "pack_verify",
})

#: Runtime ops the CLI exposes but this tool deliberately does not.
ELSEWHERE = {
    "chat": "/capitol chat <message>",
    "pause": "/capitol pause <run>",
    "stop": "/capitol stop <run>",
    "resume": "/capitol resume <run>",
    "cancel": "/capitol cancel <task_id>",
    "card": "op='discover'",
    "events": "op='watch' (bounded) or /capitol events <run>",
    "up": "op='upload'",
    "down": "op='download'",
    "url": "/capitol url <file_id>",
}

CAPITOL_SESSION_TOOL = {
    "type": "function",
    "function": {
        "name": "capitol_control",
        "description": (
            "Drive the configured Capitol AI agent's workflows in plain "
            "language: discover the agent card and org directory, "
            "list/describe/suggest workflows (plus versions, stats, and "
            "recent runs), start a run (idempotency-keyed; derived from "
            "the inputs when omitted, so retries replay instead of "
            "double-starting), check status, watch a run to a bounded "
            "deadline and summarize its events, answer human-input "
            "checkpoints (clarifications and continue/stop "
            "interventions), fetch outputs and eval roll-ups, and "
            "upload/download artifacts through the channel quarantine "
            "directory. Use when the user asks to run, drive, check, or "
            "backfill a Capitol workflow, mentions runs, HITL, "
            "artifacts, or evals, or names a workflow. Runtime surface "
            "only: admin/provisioning and pack edits are refused — the "
            "user runs /capitol admin … themselves."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "discover", "workflows", "describe", "suggest",
                        "versions", "stats", "runs", "start", "status",
                        "watch", "respond", "outputs", "evals", "upload",
                        "download",
                    ],
                    "description": "The Capitol operation to perform.",
                },
                "workflow_id": {
                    "type": "string",
                    "description": (
                        "describe/versions/stats/runs/start: the workflow "
                        "id from op='workflows' — never invent one."
                    ),
                },
                "goal": {
                    "type": "string",
                    "description": "suggest: the user's goal, free-form.",
                },
                "run_id": {
                    "type": "string",
                    "description": (
                        "status/watch/respond/outputs/evals: the run."
                    ),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "start: the full workflow inputs map, passed "
                        "verbatim (keys from op='describe')."
                    ),
                },
                "input_value": {
                    "description": (
                        "start: a single input value; the tool wraps it "
                        "under the workflow's discovered request-input "
                        "key (use instead of 'inputs' for one-input "
                        "workflows)."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "description": (
                        "start: replay key. Omit to derive one from a "
                        "canonical digest of workflow+inputs; reuse a "
                        "printed key to replay that exact run."
                    ),
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "start: artifact ids from op='upload' to attach."
                    ),
                },
                "allow_clarifications": {
                    "type": "boolean",
                    "description": (
                        "start: let the run pause mid-flight and ask "
                        "(answer via op='respond')."
                    ),
                },
                "request_id": {
                    "type": "string",
                    "description": (
                        "respond: the checkpoint's request id (from the "
                        "watch summary's needs-input line)."
                    ),
                },
                "response": {
                    "type": "string",
                    "description": (
                        "respond: the answer text; for "
                        "kind='intervention' it must be exactly "
                        "'continue' or 'stop'."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["clarification", "intervention"],
                    "description": (
                        "respond: which HITL kind (default "
                        "clarification)."
                    ),
                },
                "decline": {
                    "type": "boolean",
                    "description": (
                        "respond: decline the clarification instead of "
                        "answering."
                    ),
                },
                "node_id": {
                    "type": "string",
                    "description": "respond (intervention): the node id.",
                },
                "deadline_seconds": {
                    "type": "integer",
                    "description": (
                        "watch: hard polling deadline (default 120, max "
                        "600); the summary reports how to resume."
                    ),
                },
                "since_sequence": {
                    "type": "integer",
                    "description": (
                        "watch: resume cursor (last seen sequence + 1)."
                    ),
                },
                "days": {"type": "integer", "description": "stats window."},
                "limit": {"type": "integer", "description": "runs: max rows."},
                "status_filter": {
                    "type": "string",
                    "description": "runs: filter by run status.",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "discover: refetch the agent card.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "upload: file path — must live under the channel "
                        "quarantine dir (where inbound attachments land)."
                    ),
                },
                "file_id": {
                    "type": "string",
                    "description": "download: the artifact/file id.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "download: destination filename (always written "
                        "into the quarantine dir)."
                    ),
                },
            },
            "required": ["op"],
        },
    },
}


def derive_idempotency_key(workflow_id: str,
                           inputs: Optional[Dict[str, Any]]) -> str:
    """Canonical start key: same workflow + same inputs replay the same
    run (the funding provisioner's payload-digest pattern)."""
    canonical = json.dumps(inputs or {}, sort_keys=True,
                           separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"capitol-tool:{workflow_id}:{digest[:16]}"


def quarantine_root() -> Path:
    from ..channels import quarantine_dir

    return quarantine_dir()


def _bounded_json(payload: Any, budget: int = RESULT_BUDGET) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if len(text) > budget:
        text = text[:budget] + "\n… [truncated]"
    return text


def _hitl_lines(event: Dict[str, Any]) -> Optional[str]:
    """Relay a ``node.input_required`` checkpoint verbatim (bounded)."""
    if event.get("event_type") != "node.input_required":
        return None
    data = event.get("data") or {}
    extra = data.get("extra") or {}
    request_id = clean_text(
        data.get("request_id") or extra.get("request_id"), 60
    )
    prompt = clean_text(data.get("prompt") or extra.get("prompt"), 400)
    input_kind = clean_text(
        data.get("input_kind") or extra.get("input_kind")
        or "clarification", 30,
    )
    node = clean_text(
        (event.get("node") or {}).get("display_name")
        or (event.get("node") or {}).get("node_id"), 60,
    )
    return (
        f"NEEDS INPUT [{input_kind}] request_id={request_id} "
        f"node={node}: {prompt}"
    )


class CapitolSessionClient:
    """Builtin fronting ``CapitolRuntime`` for chat sessions.

    A fresh runtime is built per call (bearer resolved by reference at
    call time, never stored on the client), matching the /capitol CLI's
    per-command construction. ``remote_origin`` switches the client into
    channel mode: reads and HITL responses pass through, ``start``
    proposes an origin-bound approval instead of executing.
    """

    name = "capitol_control"

    def __init__(self, config: dict, *,
                 remote_origin: Optional[Dict[str, str]] = None,
                 approvals=None, notify=None):
        self._config = config or {}
        self._remote = dict(remote_origin) if remote_origin else None
        self._approvals = approvals
        self._notify = notify

    @staticmethod
    def _text(message: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": message}]}

    def _runtime(self) -> CapitolRuntime:
        return CapitolRuntime.from_config(self._config)

    # -- dispatch -----------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> Dict[str, Any]:
        arguments = arguments or {}
        op = str(arguments.get("op") or "").strip().lower()
        refused = self._refuse(op)
        if refused is not None:
            return self._text(refused)
        try:
            return self._dispatch(op, arguments)
        except CapitolCapabilityError as exc:
            return self._text(
                "capitol_control: this agent's card does not advertise "
                f"that capability — {clean_text(exc, 300)} (failing "
                "closed; op='discover' lists what it does advertise)"
            )
        except CapitolAuthError as exc:
            return self._text(
                "capitol_control: Capitol credential needed — "
                f"{clean_text(exc, 300)}. No automatic re-auth: the user "
                "sets $CAPITOL_A2A_BEARER (or the env named by "
                "capitol_bearer_env) or adds the agent to "
                "~/.capitol-a2a/agents.yaml. Park this and continue with "
                "what you can."
            )
        except CapitolError as exc:
            hint = clean_text(getattr(exc, "hint", ""), 200)
            return self._text(
                f"capitol_control error: {clean_text(exc, 500)}"
                + (f" — {hint}" if hint else "")
            )

    def _refuse(self, op: str) -> Optional[str]:
        if op in ADMIN_REFUSALS:
            return (
                f"capitol_control refuses {op!r}: admin/provisioning is "
                "user-explicit, never model-callable. The user runs "
                f"`{ADMIN_REFUSALS[op]}` themselves in the shell "
                "(requires capitol_admin=true; policy-gated and "
                "ledgered)."
            )
        if op in PACK_REFUSALS:
            return (
                f"capitol_control refuses {op!r}: flow packs are "
                "user-edited data files, never tool mutations. The user "
                "runs `/capitol pack list|show|verify <name>` themselves; "
                "authoring guidance lives in the pack-author skill."
            )
        if op in ELSEWHERE:
            return (
                f"capitol_control does not expose {op!r}; use "
                f"{ELSEWHERE[op]} instead."
            )
        return None

    def _dispatch(self, op: str, arguments: dict) -> Dict[str, Any]:
        handlers = {
            "discover": self._op_discover,
            "workflows": self._op_workflows,
            "describe": self._op_describe,
            "suggest": self._op_suggest,
            "versions": self._op_versions,
            "stats": self._op_stats,
            "runs": self._op_runs,
            "start": self._op_start,
            "status": self._op_status,
            "watch": self._op_watch,
            "respond": self._op_respond,
            "outputs": self._op_outputs,
            "evals": self._op_evals,
            "upload": self._op_upload,
            "download": self._op_download,
        }
        handler = handlers.get(op)
        if handler is None:
            return self._text(
                f"Unknown capitol_control op {op!r} — one of: "
                + ", ".join(sorted(handlers))
            )
        return handler(arguments)

    # -- reads --------------------------------------------------------------

    def _op_discover(self, arguments: dict) -> Dict[str, Any]:
        runtime = self._runtime()
        card = runtime.discover(refresh=bool(arguments.get("refresh")))
        skills = sorted(runtime.skill_ids())
        lines = [
            f"agent: {clean_text(card.get('name'), 80)} "
            f"({runtime.org_id}/{runtime.agent_id})",
            "wire schema: " + clean_text(
                card.get("wireSchemaVersion") or card.get("version") or "?",
                20,
            ),
        ]
        for key, value in sorted(runtime.capability_flags().items()):
            lines.append(f"{key}: {value}")
        lines.append(f"skills ({len(skills)}): " + ", ".join(skills))
        agents = runtime.list_org_agents()
        lines.append(f"org agents ({len(agents)}):")
        for agent in agents:
            exposure = "a2a" if agent.get("exposed_via_a2a") else "internal"
            lines.append(
                f"  {clean_text(agent.get('agent_id'), 50)}  "
                f"{clean_text(agent.get('name'), 60)}  [{exposure}] "
                f"{len(agent.get('workflow_ids') or [])} workflow(s)"
            )
        return self._text("\n".join(lines))

    def _op_workflows(self, _arguments: dict) -> Dict[str, Any]:
        workflows = self._runtime().list_workflows()
        if not workflows:
            return self._text("No workflows on this agent's allowlist.")
        lines = [f"{len(workflows)} workflow(s):"]
        for workflow in workflows:
            identifier = workflow.get("workflow_id") or workflow.get("id")
            lines.append(
                f"  {clean_text(identifier, 60)}  "
                f"{clean_text(workflow.get('name'), 80)}"
            )
        lines.append(
            "(pass the id verbatim as workflow_id; describe it before "
            "starting)"
        )
        return self._text("\n".join(lines))

    def _require(self, arguments: dict, key: str, op: str) -> str:
        value = str(arguments.get(key) or "").strip()
        if not value:
            raise CapitolError(f"op='{op}' needs {key}")
        return value

    def _op_describe(self, arguments: dict) -> Dict[str, Any]:
        workflow_id = self._require(arguments, "workflow_id", "describe")
        runtime = self._runtime()
        details = runtime.describe_workflow(workflow_id)
        from .packs.engine import workflow_inputs_key

        inputs_key = workflow_inputs_key(runtime, workflow_id)
        return self._text(
            f"workflow {workflow_id} — inputs key: {inputs_key!r} "
            "(a bare input_value on op='start' is wrapped under it)\n"
            + _bounded_json(details)
        )

    def _op_suggest(self, arguments: dict) -> Dict[str, Any]:
        goal = self._require(arguments, "goal", "suggest")
        rows = self._runtime().suggest_workflows(
            goal, max_suggestions=int(arguments.get("max_suggestions") or 3)
        )
        if not rows:
            return self._text("No suggestions for that goal.")
        lines = [f"{len(rows)} suggestion(s):"]
        for row in rows:
            lines.append(
                f"  {clean_text(row.get('workflow_id'), 60)}  "
                f"{clean_text(row.get('name'), 60)}  "
                f"confidence={row.get('confidence')}  "
                f"{clean_text(row.get('reason'), 120)}"
            )
        lines.append(
            "(suggestions are ranked guesses — confirm the workflow with "
            "the user before starting it)"
        )
        return self._text("\n".join(lines))

    def _op_versions(self, arguments: dict) -> Dict[str, Any]:
        workflow_id = self._require(arguments, "workflow_id", "versions")
        return self._text(_bounded_json(
            self._runtime().workflow_versions(workflow_id)
        ))

    def _op_stats(self, arguments: dict) -> Dict[str, Any]:
        workflow_id = self._require(arguments, "workflow_id", "stats")
        days = arguments.get("days")
        return self._text(_bounded_json(self._runtime().workflow_stats(
            workflow_id, days=int(days) if days is not None else None,
        )))

    def _op_runs(self, arguments: dict) -> Dict[str, Any]:
        workflow_id = self._require(arguments, "workflow_id", "runs")
        listing = self._runtime().list_runs(
            workflow_id,
            limit=int(arguments.get("limit") or 20),
            status_filter=arguments.get("status_filter"),
        )
        runs = (listing or {}).get("runs") or []
        if not runs:
            return self._text(f"No runs for workflow {workflow_id}.")
        lines = [f"{len(runs)} run(s) for {workflow_id}:"]
        for run in runs:
            lines.append(
                f"  {clean_text(run.get('run_id'), 50)}  "
                f"{clean_text(run.get('status'), 20)}  "
                f"{clean_text(run.get('started_at'), 30)}"
            )
        return self._text("\n".join(lines))

    def _op_status(self, arguments: dict) -> Dict[str, Any]:
        run_id = self._require(arguments, "run_id", "status")
        return self._text(_bounded_json(self._runtime().run_status(run_id)))

    def _op_outputs(self, arguments: dict) -> Dict[str, Any]:
        run_id = self._require(arguments, "run_id", "outputs")
        return self._text(_bounded_json(
            self._runtime().workflow_output(run_id)
        ))

    def _op_evals(self, arguments: dict) -> Dict[str, Any]:
        run_id = self._require(arguments, "run_id", "evals")
        rollup = self._runtime().eval_report(run_id)
        summary = rollup.get("summary") or {}
        head = (
            f"evals for run {run_id}: has_evals={rollup.get('has_evals')} "
            f"total={summary.get('total')} passed={summary.get('passed')} "
            f"failed={summary.get('failed')} "
            f"suite_passed={summary.get('suite_passed')}"
        )
        return self._text(head + "\n" + _bounded_json(rollup, 3000))

    # -- start (the effectful op) --------------------------------------------

    def _start_payload(self, arguments: dict) -> Dict[str, Any]:
        """Validate + normalize a start request; derives the key."""
        workflow_id = self._require(arguments, "workflow_id", "start")
        inputs = arguments.get("inputs")
        input_value = arguments.get("input_value")
        if inputs is not None and not isinstance(inputs, dict):
            raise CapitolError("start: inputs must be an object")
        if inputs is not None and input_value is not None:
            raise CapitolError(
                "start: pass inputs OR input_value, not both"
            )
        if input_value is not None:
            from .packs.engine import workflow_inputs_key

            key_name = workflow_inputs_key(self._runtime(), workflow_id)
            inputs = {key_name: input_value}
        idempotency_key = str(arguments.get("idempotency_key") or "").strip()
        derived = False
        if not idempotency_key:
            idempotency_key = derive_idempotency_key(workflow_id, inputs)
            derived = True
        payload: Dict[str, Any] = {
            "workflow_id": workflow_id,
            "inputs": inputs,
            "idempotency_key": idempotency_key,
            "derived_key": derived,
        }
        artifacts = [
            str(artifact_id)
            for artifact_id in arguments.get("artifacts") or []
            if str(artifact_id).strip()
        ]
        if artifacts:
            payload["artifacts"] = artifacts
        if arguments.get("allow_clarifications") is not None:
            payload["allow_clarifications"] = bool(
                arguments["allow_clarifications"]
            )
        return payload

    def _op_start(self, arguments: dict) -> Dict[str, Any]:
        payload = self._start_payload(arguments)
        if self._remote is not None:
            return self._propose_remote_start(payload)
        decision = evaluate_required_policy("capitol.run.start", {
            "workflow_id": payload["workflow_id"],
            "idempotency_key": payload["idempotency_key"],
            "source": "session",
        })
        if not decision.allowed:
            return self._text(
                "start denied by required policy: "
                f"{decision.reason or decision.check or 'no reason given'}"
            )
        runtime = self._runtime()
        runtime.ensure_skill("call_workflow", "capitol_control start")
        submission = runtime.call_workflow(
            payload["workflow_id"], payload["inputs"],
            idempotency_key=payload["idempotency_key"],
            artifacts=[
                {"artifact_id": artifact_id}
                for artifact_id in payload.get("artifacts") or []
            ] or None,
            allow_clarifications=payload.get("allow_clarifications"),
        )
        run_id = str(submission.get("run_id") or "")
        replayed = " (replayed — this key already started it)" if (
            submission.get("replayed")
        ) else ""
        key_note = "derived from workflow+inputs" if payload["derived_key"] \
            else "caller-supplied"
        return self._text(
            f"run {run_id} started for {payload['workflow_id']}{replayed}\n"
            f"idempotency key: {payload['idempotency_key']} ({key_note}; "
            "reuse it to replay this exact run)\n"
            "op='watch' summarizes progress; op='outputs' after terminal."
        )

    def _propose_remote_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Channel sessions: pin the exact request into an origin-bound
        approval; approve executes those bytes, deny discards."""
        if self._approvals is None or self._notify is None:
            return self._text(
                "capitol_control: this channel session has no approval "
                "store wired; start is unavailable here."
            )
        origin = self._remote or {}
        description = (
            f"capitol start {payload['workflow_id']} "
            f"(key {payload['idempotency_key']})"
        )
        request_id = self._approvals.add(
            description,
            origin.get("channel", ""),
            origin.get("thread_id", ""),
            origin.get("sender", ""),
            kind=REMOTE_START_KIND,
            payload={
                "workflow_id": payload["workflow_id"],
                "inputs": payload["inputs"],
                "idempotency_key": payload["idempotency_key"],
                "artifacts": payload.get("artifacts") or [],
                "allow_clarifications": payload.get("allow_clarifications"),
            },
        )
        self._notify(
            f"Capitol start approval needed [#{request_id}]:\n"
            f"  workflow {payload['workflow_id']}\n"
            f"  idempotency key {payload['idempotency_key']}\n"
            f"Reply 'approve {request_id}' or 'deny {request_id}'.",
            origin.get("thread_id", ""),
        )
        return self._text(
            f"Starting a Capitol run requires user approval (request "
            f"#{request_id} sent over {origin.get('channel', 'channel')}). "
            "The exact workflow, inputs, and idempotency key are pinned "
            "to that approval. Do not retry; tell the user it is pending "
            "and continue with reads."
        )

    # -- watch (bounded) -------------------------------------------------------

    def _op_watch(self, arguments: dict, *, _sleep=time.sleep,
                  _clock=time.monotonic) -> Dict[str, Any]:
        run_id = self._require(arguments, "run_id", "watch")
        deadline = float(arguments.get("deadline_seconds")
                         or WATCH_DEADLINE_DEFAULT)
        deadline = max(WATCH_DEADLINE_MIN,
                       min(WATCH_DEADLINE_MAX, deadline))
        since = int(arguments.get("since_sequence") or 0)
        runtime = self._runtime()
        started = _clock()
        last_seen = max(0, since - 1)
        counts: Dict[str, int] = {}
        hitl: List[str] = []
        recent: List[str] = []
        total = 0
        final_state = ""
        terminal_event = False
        while True:
            payload = runtime.run_events(run_id, since_sequence=last_seen + 1)
            for event in (payload or {}).get("events") or []:
                if not isinstance(event, dict):
                    continue
                if event.get("event_type") == KEEPALIVE_EVENT:
                    continue
                sequence = event.get("sequence")
                if isinstance(sequence, (int, float)):
                    if int(sequence) <= last_seen:
                        continue
                    last_seen = int(sequence)
                total += 1
                kind = str(event.get("event_type") or "unknown")
                counts[kind] = counts.get(kind, 0) + 1
                if kind in TERMINAL_EVENT_TYPES:
                    terminal_event = True
                needs_input = _hitl_lines(event)
                if needs_input:
                    hitl.append(needs_input)
                node = clean_text(
                    (event.get("node") or {}).get("display_name")
                    or (event.get("node") or {}).get("node_id"), 50,
                )
                recent.append(f"seq {sequence} {kind} {node}".rstrip())
                if len(recent) > 8:
                    recent.pop(0)
            status_payload = runtime.run_status(run_id)
            state = str((status_payload or {}).get("status") or "").lower()
            if state in TERMINAL_RUN_STATUSES or terminal_event:
                final_state = state or "ended"
                break
            if _clock() - started >= deadline:
                break
            _sleep(min(WATCH_POLL_SECONDS,
                       max(0.0, deadline - (_clock() - started))))
        lines = [
            f"run {run_id}: "
            + (f"terminal state {final_state}" if final_state else
               f"still running at the {int(deadline)}s watch deadline"),
            f"{total} event(s) observed, last sequence {last_seen}",
        ]
        if counts:
            lines.append("by type: " + ", ".join(
                f"{kind}×{count}"
                for kind, count in sorted(counts.items())
            ))
        if recent:
            lines.append("recent: " + "; ".join(recent[-5:]))
        for needs_input in hitl[-3:]:
            lines.append(needs_input)
        if hitl:
            lines.append(
                "answer with op='respond' (relay the question to the "
                "user verbatim first; never invent an answer)"
            )
        if final_state == "failed":
            error = clean_text(
                (runtime.run_status(run_id) or {}).get("error_message"), 300
            )
            if error:
                lines.append(f"error: {error}")
        if final_state in ("success", "succeeded"):
            lines.append("fetch results with op='outputs' / op='evals'")
        if not final_state:
            lines.append(
                f"resume with op='watch' since_sequence={last_seen + 1} "
                "(or op='status' for a cheap check)"
            )
        return self._text("\n".join(lines))

    # -- HITL ------------------------------------------------------------------

    def _op_respond(self, arguments: dict) -> Dict[str, Any]:
        run_id = self._require(arguments, "run_id", "respond")
        request_id = self._require(arguments, "request_id", "respond")
        kind = str(arguments.get("kind") or "clarification").strip().lower()
        response = str(arguments.get("response") or "").strip()
        declined = bool(arguments.get("decline"))
        decision = evaluate_required_policy("capitol.hitl.respond", {
            "run_id": run_id,
            "request_id": request_id,
            "kind": kind,
            "source": "remote" if self._remote is not None else "session",
        })
        if not decision.allowed:
            return self._text(
                "respond denied by required policy: "
                f"{decision.reason or decision.check or 'no reason given'}"
            )
        runtime = self._runtime()
        if kind == "intervention":
            if response not in ("continue", "stop"):
                return self._text(
                    "intervention responses are the literal tokens "
                    "'continue' or 'stop' — map the user's decision to "
                    "one of those exactly."
                )
            runtime.submit_intervention(
                run_id, str(arguments.get("node_id") or ""), request_id,
                response,
            )
            return self._text(
                f"intervention '{response}' delivered for run {run_id}"
            )
        if kind != "clarification":
            return self._text(
                f"unknown respond kind {kind!r} — clarification or "
                "intervention"
            )
        if not response and not declined:
            return self._text(
                "respond needs the answer text (or decline=true); relay "
                "the run's question to the user verbatim if you don't "
                "have it"
            )
        runtime.submit_clarification(
            run_id, request_id, response, declined=declined,
        )
        verb = "declined" if declined else "answered"
        return self._text(
            f"clarification {verb} for run {run_id}; watch or status "
            "shows the run continuing"
        )

    # -- artifacts ----------------------------------------------------------------

    def _op_upload(self, arguments: dict) -> Dict[str, Any]:
        raw = self._require(arguments, "path", "upload")
        root = quarantine_root().resolve()
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            return self._text(
                f"capitol_control uploads only files under the channel "
                f"quarantine dir ({root}) — inbound channel attachments "
                "land there already; ask the user to copy other files in "
                "first."
            )
        if not resolved.is_file():
            return self._text(f"no such file: {resolved}")
        result = self._runtime().upload_artifact(str(resolved))
        return self._text(
            f"uploaded {result['filename']} — artifact_id "
            f"{result['artifact_id']}, {result['size_bytes']:,} bytes, "
            f"digest {result['digest']} (bind it into a start via "
            "artifacts=[…])"
        )

    def _op_download(self, arguments: dict) -> Dict[str, Any]:
        file_id = self._require(arguments, "file_id", "download")
        raw_name = str(arguments.get("filename") or "").strip()
        safe = _SAFE_FILENAME_RE.sub(
            "-", Path(raw_name).name if raw_name else ""
        ).strip("-.")
        filename = safe or f"{_SAFE_FILENAME_RE.sub('-', file_id)[:40]}.bin"
        root = quarantine_root()
        destination = root / f"capitol-{filename}"
        result = self._runtime().download_artifact(
            file_id, str(destination)
        )
        return self._text(
            f"downloaded to {result['path']} ({result['size_bytes']:,} "
            f"bytes)\ndigest {result['digest']} — verify-on-fetch: "
            "compare against the producer's recorded digest before "
            "trusting the bytes."
        )


def consume_capitol_start(request_id: int, entry: Dict[str, Any],
                          verb: str, config: dict) -> str:
    """Consume an approved (or denied) remote ``capitol_start`` proposal.

    Called by the remote loop after the origin-bound atomic consume:
    builds the exact pinned ``call_workflow`` — never fresh model input —
    re-checking required policy at consume time (approvals do not bypass
    deterministic authorization).
    """
    payload = entry.get("payload") or {}
    workflow_id = str(payload.get("workflow_id") or "")
    idempotency_key = str(payload.get("idempotency_key") or "")
    if verb == "deny":
        return (
            f"Denied #{request_id}: workflow {workflow_id} will not "
            "start."
        )
    if not workflow_id or not idempotency_key:
        return (
            f"Approval #{request_id} is missing its pinned start payload; "
            "nothing was started."
        )
    decision = evaluate_required_policy("capitol.run.start", {
        "workflow_id": workflow_id,
        "idempotency_key": idempotency_key,
        "source": "remote_approval",
        "approval_id": request_id,
    })
    if not decision.allowed:
        return (
            f"Approval #{request_id} denied by required policy: "
            f"{decision.reason or decision.check or 'no reason given'}"
        )
    try:
        runtime = CapitolRuntime.from_config(config)
        runtime.ensure_skill("call_workflow", "capitol_start approval")
        submission = runtime.call_workflow(
            workflow_id,
            payload.get("inputs"),
            idempotency_key=idempotency_key,
            artifacts=[
                {"artifact_id": artifact_id}
                for artifact_id in payload.get("artifacts") or []
            ] or None,
            allow_clarifications=payload.get("allow_clarifications"),
        )
    except CapitolAuthError as exc:
        return (
            f"Capitol credential needed: {clean_text(exc, 300)} — "
            "nothing was started."
        )
    except CapitolError as exc:
        return (
            f"Capitol start failed: {clean_text(exc, 400)} — nothing "
            "was started."
        )
    run_id = str(submission.get("run_id") or "")
    replayed = " (replayed)" if submission.get("replayed") else ""
    return (
        f"Started #{request_id}: run {run_id}{replayed} for workflow "
        f"{workflow_id} (key {idempotency_key})."
    )
