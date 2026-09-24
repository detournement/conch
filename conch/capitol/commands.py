"""``/capitol`` — the generic Capitol control surface (A2Actrl parity).

One shell command family over the two adapters, per the control design
(§2): read/observe and start/steer subcommands map onto
:class:`~conch.capitol.client.CapitolRuntime` (reads never touch the
kernel ledger), the gated ``admin`` subcommands map onto
:class:`~conch.capitol.admin.CapitolAdmin` (off unless
``capitol_admin=true``; every mutation passes the ``capitol.admin.{op}``
required-policy gate and the kernel external-action ledger under a
find-or-create ``capitol-admin-cli`` ops mission), and the ``pack``
subcommands drive the flow-pack loader and each pack's acceptance drill.

Dispositions for the design's named gaps (§2.3): ``runs`` requires a
workflow reference (there is no org-wide run listing skill);
skill-gated features (``suggest``/``versions``/``stats``/``resume``)
surface a missing card skill as "this agent's card does not advertise
…", not a crash; HTTP 401 parks with the credential-needed message (no
automatic re-auth); ``admin rollback`` is the persist-based inverse of
publish and says so (the platform-api rollback endpoint reads a version
store workflow-api publishes don't populate); ``watch`` persists its
cursor in versioned CLI state (``capitol_cli.json``) — durable daemon
supervision of a watched run stays the mission envelope's job
(``/mission new`` with a ``capitol`` envelope), not this CLI's.

Secrets stay by reference throughout: bearers/admin tokens resolve per
call from the environment or the A2Actrl registry, minted bearers sink
straight to the registry (only fingerprints print), and workflow/model
text is rendered as untrusted prose (control bytes stripped, bounded).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .client import FINAL_STATUS_EVENT, CapitolRuntime
from .errors import (
    CapitolAuthError,
    CapitolCapabilityError,
    CapitolError,
)
from .packs.templates import clean_text

CLI_STATE_VERSION = 1

USAGE = """
  \033[1;36m/capitol — generic Capitol control (A2Actrl parity)\033[0m
  Read / observe:
    /capitol card [--refresh]              agent card, capabilities, skills
    /capitol agents                        org A2A agent directory
    /capitol workflows                     workflow catalog
    /capitol describe <wf>                 workflow details + input fields
    /capitol suggest <goal> [-n N]         rank workflows against a goal
    /capitol versions <wf>                 version history
    /capitol stats <wf> [--days N]         aggregate run statistics
    /capitol runs <wf> [--limit N] [--status S]   recent runs (per workflow)
    /capitol status <run>                  run status
    /capitol events <run> [--since N]      persisted run events
    /capitol watch <run> [--since N]       stream to terminal (resumable)
    /capitol output <run>                  workflow output
    /capitol evals <run>                   eval roll-up
    /capitol procedure search "<query>"    side-effect-free Procedure discovery
    /capitol procedure show <wf> [--version N]   read one Procedure projection
  Start / steer:
    /capitol start <wf> [--input JSON|@file] [--raw] [--key K]
                   [--artifact ID ...] [--watch]
    /capitol chat <message> [--file PATH ...]
    /capitol respond <run> <request_id> <answer…> | --decline
    /capitol respond <run> <request_id> --continue|--stop [--node ID]
    /capitol pause <run> [--reason …]      /capitol stop <run> [--reason …]
    /capitol resume <run> [--payload JSON] [--edited-nodes a,b]
    /capitol cancel <task_id>              spec-level A2A CancelTask
  Artifacts:
    /capitol up <path> [--inline]          upload (presigned PUT | inline)
    /capitol down <file_id> <dest>         download (digest printed)
    /capitol url <file_id>                 click-time presigned URL
  Flow packs:
    /capitol packs | pack list             discovered packs + digests
    /capitol pack show <name>              manifest summary
    /capitol pack verify <name>            validate + run the acceptance drill
  Admin (requires capitol_admin=true; policy-gated, ledgered):
    /capitol admin agents|agent <id>|create-agent <name> --workflows a,b
                   [--alias R]|delete-agent <id>|allowlist <agent> <wf,…>
                   |bind-collections <agent> <id,…>
    /capitol admin persist @payload.json | publish <wf> | rollback <wf>
                   | delete-workflow <wf> | versions <wf>
    /capitol admin schedules <wf> | schedule-add <wf> <name> <cron>
                   [--tz TZ] [--input-overrides JSON] [--disabled]
                   | schedule-update <wf> <id> @updates.json
                   | schedule-delete <wf> <id>
    /capitol admin collections [create <name> [--destination d]
                   | delete <id>]
    /capitol admin rotate-bearer <agent> [--alias R]
                   | mint-bearer <agent> <label> | revoke-bearer <agent> <id>
    (mutations take --key to replay a prior idempotency key)
"""


# ---------------------------------------------------------------------------
# Versioned CLI state (the resumable-watch cursor file)
# ---------------------------------------------------------------------------

def _cli_state_path() -> Path:
    root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return root / "conch" / "capitol_cli.json"


def _load_cli_state() -> Dict[str, Any]:
    try:
        data = json.loads(_cli_state_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": CLI_STATE_VERSION, "watch": {}}
    if not isinstance(data, dict) or "watch" not in data:
        return {"version": CLI_STATE_VERSION, "watch": {}}
    return data


def _save_cli_state(data: Dict[str, Any]):
    path = _cli_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def watch_cursor(run_id: str) -> int:
    return int((_load_cli_state().get("watch") or {}).get(run_id) or 0)


def _store_watch_cursor(run_id: str, sequence: int):
    data = _load_cli_state()
    data.setdefault("watch", {})[run_id] = int(sequence)
    _save_cli_state(data)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _print(text: str = ""):
    print(text)


def _print_json(payload: Any):
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _split_flags(tokens: List[str],
                 flag_spec: Dict[str, bool]) -> Tuple[List[str], Dict]:
    """Minimal flag parser: ``flag_spec`` maps ``--flag`` to whether it
    takes a value; repeatable value flags collect into lists."""
    positional: List[str] = []
    flags: Dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in flag_spec:
            if flag_spec[token]:
                if index + 1 >= len(tokens):
                    raise CapitolError(f"{token} needs a value")
                value = tokens[index + 1]
                key = token.lstrip("-")
                if key in flags:
                    existing = flags[key]
                    flags[key] = (
                        existing + [value]
                        if isinstance(existing, list)
                        else [existing, value]
                    )
                else:
                    flags[key] = value
                index += 2
            else:
                flags[token.lstrip("-")] = True
                index += 1
        else:
            positional.append(token)
            index += 1
    return positional, flags


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _load_json_arg(raw: str, what: str) -> Any:
    """Parse an inline-JSON or ``@file`` argument."""
    text = raw
    if raw.startswith("@"):
        try:
            text = Path(raw[1:]).expanduser().read_text()
        except OSError as exc:
            raise CapitolError(f"cannot read {what} file {raw[1:]}: {exc}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CapitolError(f"{what} is not valid JSON: {exc}")


def _runtime(config: dict) -> CapitolRuntime:
    runtime = CapitolRuntime.from_config(config)
    runtime.discover()
    return runtime


def _digest_key(op: str, payload: Any) -> str:
    """Default admin idempotency key: a canonical digest of the
    subcommand arguments (the funding provisioner's pattern)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"capitol-cli:{op}:{digest[:16]}"


# ---------------------------------------------------------------------------
# Read / observe
# ---------------------------------------------------------------------------

def _cmd_card(runtime: CapitolRuntime, tokens: List[str]):
    _, flags = _split_flags(tokens, {"--refresh": False})
    card = runtime.discover(refresh=bool(flags.get("refresh")))
    skills = sorted(runtime.skill_ids())
    _print(f"\n  \033[1;36m{clean_text(card.get('name'), 80)}\033[0m")
    wire = card.get("wireSchemaVersion") or card.get("version") or "?"
    _print(f"    wire schema:  {clean_text(wire, 20)}")
    for key, value in sorted(runtime.capability_flags().items()):
        _print(f"    {key + ':':<14}{value}")
    _print(f"    skills ({len(skills)}):")
    for skill in skills:
        _print(f"      · {skill}")
    _print()


def _cmd_agents(runtime: CapitolRuntime, _tokens: List[str]):
    agents = runtime.list_org_agents()
    if not agents:
        _print("\n  \033[2mNo A2A agents in this org.\033[0m\n")
        return
    _print(f"\n  \033[1;36mOrg A2A agents ({len(agents)}):\033[0m")
    for agent in agents:
        exposed = "a2a" if agent.get("exposed_via_a2a") else "internal"
        workflows = agent.get("workflow_ids") or []
        _print(
            f"    \033[1m{clean_text(agent.get('agent_id'), 60)}\033[0m  "
            f"{clean_text(agent.get('name'), 60)}  "
            f"\033[2m[{exposed}] {len(workflows)} workflow(s)\033[0m"
        )
    _print()


def _cmd_workflows(runtime: CapitolRuntime, _tokens: List[str]):
    workflows = runtime.list_workflows()
    if not workflows:
        _print("\n  \033[2mNo workflows on this agent's allowlist.\033[0m\n")
        return
    _print(f"\n  \033[1;36mWorkflows ({len(workflows)}):\033[0m")
    for workflow in workflows:
        identifier = workflow.get("workflow_id") or workflow.get("id")
        _print(
            f"    \033[1m{clean_text(identifier, 60)}\033[0m  "
            f"{clean_text(workflow.get('name'), 80)}"
        )
    _print()


def _cmd_describe(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol describe <workflow>")
    _print_json(runtime.describe_workflow(tokens[0]))


def _cmd_suggest(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {"-n": True})
    if not positional:
        raise CapitolError("usage: /capitol suggest <goal> [-n N]")
    rows = runtime.suggest_workflows(
        " ".join(positional), max_suggestions=int(flags.get("n") or 3)
    )
    if not rows:
        _print("\n  \033[2mNo suggestions.\033[0m\n")
        return
    _print(f"\n  \033[1;36mSuggestions ({len(rows)}):\033[0m")
    for row in rows:
        confidence = row.get("confidence")
        _print(
            f"    \033[1m{clean_text(row.get('workflow_id'), 60)}\033[0m  "
            f"{clean_text(row.get('name'), 60)}  "
            f"\033[2mconfidence={confidence} "
            f"{clean_text(row.get('reason'), 100)}\033[0m"
        )
    _print()


def _cmd_versions(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol versions <workflow>")
    _print_json(runtime.workflow_versions(tokens[0]))


def _cmd_stats(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {"--days": True})
    if not positional:
        raise CapitolError("usage: /capitol stats <workflow> [--days N]")
    days = int(flags["days"]) if flags.get("days") else None
    _print_json(runtime.workflow_stats(positional[0], days=days))


def _cmd_runs(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(
        tokens, {"--limit": True, "--status": True}
    )
    if not positional:
        raise CapitolError(
            "usage: /capitol runs <workflow> [--limit N] [--status S] — "
            "run listing is per-workflow (no org-wide skill); "
            "cross-workflow history lives in kernel bindings (/missions)"
        )
    listing = runtime.list_runs(
        positional[0],
        limit=int(flags.get("limit") or 20),
        status_filter=flags.get("status"),
    )
    runs = (listing or {}).get("runs") or []
    if not runs:
        _print("\n  \033[2mNo runs.\033[0m\n")
        return
    _print(f"\n  \033[1;36mRuns ({len(runs)}):\033[0m")
    for run in runs:
        _print(
            f"    \033[1m{clean_text(run.get('run_id'), 50)}\033[0m  "
            f"{clean_text(run.get('status'), 20)}  "
            f"\033[2m{clean_text(run.get('started_at'), 30)}\033[0m"
        )
    _print()


def _cmd_status(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol status <run>")
    _print_json(runtime.run_status(tokens[0]))


def _cmd_events(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(
        tokens, {"--since": True, "--types": True}
    )
    if not positional:
        raise CapitolError(
            "usage: /capitol events <run> [--since N] [--types a,b]"
        )
    types = [t for t in str(flags.get("types") or "").split(",") if t]
    payload = runtime.run_events(
        positional[0],
        since_sequence=int(flags.get("since") or 0),
        types=types or None,
    )
    events = (payload or {}).get("events") or []
    if not events:
        _print("\n  \033[2mNo events at or past that cursor.\033[0m\n")
        return
    _print(f"\n  \033[1;36mEvents ({len(events)}):\033[0m")
    for event in events:
        _print_event(event)
    _print()


def _print_event(event: Dict[str, Any]):
    sequence = event.get("sequence", "-")
    kind = clean_text(event.get("event_type"), 40)
    node = clean_text(
        (event.get("node") or {}).get("display_name")
        or (event.get("node") or {}).get("node_id"), 60,
    )
    detail = ""
    data = event.get("data") or {}
    if event.get("event_type") == "node.input_required":
        prompt = clean_text(
            data.get("prompt")
            or (data.get("extra") or {}).get("prompt"), 160,
        )
        request_id = clean_text(
            data.get("request_id")
            or (data.get("extra") or {}).get("request_id"), 40,
        )
        detail = (f"  \033[33mneeds input (request {request_id}): "
                  f"{prompt}\033[0m")
    elif event.get("event_type") == FINAL_STATUS_EVENT:
        detail = f"  state={clean_text(data.get('state'), 20)}"
    _print(f"    seq {sequence:>4}  {kind:<28} {node}{detail}")


def _cmd_watch(runtime: CapitolRuntime, tokens: List[str],
               config: dict):
    positional, flags = _split_flags(tokens, {"--since": True})
    if not positional:
        raise CapitolError("usage: /capitol watch <run> [--since N]")
    run_id = positional[0]
    if flags.get("since"):
        since = int(flags["since"])
    else:
        cursor = watch_cursor(run_id)
        since = cursor + 1 if cursor else 0
    if since:
        _print(f"\n  \033[2mresuming run {run_id} from sequence "
               f"{since} (cursor in {_cli_state_path()})\033[0m")
    else:
        _print(f"\n  \033[2mwatching run {run_id} (SSE; degrades to "
               "polling when streaming is not advertised)\033[0m")
    final_state = ""
    for event in runtime.watch_run(run_id, since_sequence=since):
        _print_event(event)
        sequence = event.get("sequence")
        if isinstance(sequence, (int, float)):
            _store_watch_cursor(run_id, int(sequence))
        if event.get("event_type") == FINAL_STATUS_EVENT:
            final_state = str((event.get("data") or {}).get("state") or "")
    _print(f"\n  \033[1;32mrun {run_id} ended: "
           f"{final_state or 'unknown'}\033[0m")
    if str(final_state).lower() == "failed":
        status = runtime.run_status(run_id)
        error = clean_text((status or {}).get("error_message"), 400)
        if error:
            _print(f"  \033[31m{error}\033[0m")
    _print()


def _cmd_output(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol output <run>")
    _print_json(runtime.workflow_output(tokens[0]))


def _cmd_evals(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol evals <run>")
    _print_json(runtime.eval_report(tokens[0]))


def _cmd_procedure(tokens: List[str], config: dict):
    from .procedures import CapitolProcedureClient

    if not tokens:
        raise CapitolError(
            "usage: /capitol procedure search \"<query>\" | "
            "show <workflow-id> [--version N]"
        )
    sub, rest = tokens[0].lower(), tokens[1:]
    client = CapitolProcedureClient.from_config(config)
    if sub == "search":
        positional, flags = _split_flags(rest, {"--limit": True})
        query = " ".join(positional).strip()
        if not query:
            raise CapitolError(
                "usage: /capitol procedure search \"<query>\" [--limit N]"
            )
        payload = client.search(
            query, limit=int(flags.get("limit") or 10),
        )
        rows = payload["results"]
        _print(
            f"\n  \033[1;36mProcedures ({len(rows)} result(s)):\033[0m"
        )
        for row in rows:
            _print(
                f"    \033[1m{clean_text(row['workflow_id'], 60)}\033[0m  "
                f"{clean_text(row['workflow_name'], 80)}  "
                f"\033[2mv{row['version_number']} "
                f"{row['verification']} "
                f"{clean_text(row['content_digest'], 24)}…\033[0m"
            )
        _print()
        return
    if sub == "show":
        positional, flags = _split_flags(rest, {"--version": True})
        if not positional:
            raise CapitolError(
                "usage: /capitol procedure show <workflow-id> "
                "[--version N]"
            )
        version = (
            int(flags["version"]) if flags.get("version") is not None
            else None
        )
        document = client.get(
            positional[0], version_number=version,
        )
        _print(
            f"\n  \033[1;36mProcedure — "
            f"{clean_text(document['workflow_id'], 60)} "
            f"v{document['version_number']}\033[0m"
        )
        _print(f"    document:     {document['id']}")
        _print(f"    version id:   {document['workflow_version_id']}")
        _print(f"    digest:       {document['content_digest']}")
        _print(f"    compiler:     {document['compiler_version']}")
        _print(
            f"    verification: {document['verification']}"
            + (
                f" at {clean_text(document['verified_at'], 40)}"
                if document.get("verified_at") else ""
            )
        )
        exposure = document["exposure"]
        enabled_exposure = ", ".join(
            key
            for key in (
                "publish_to_api",
                "publish_to_mcp",
                "publish_to_template",
            )
            if exposure.get(key) is True
        )
        _print(
            "    exposure:     " + (enabled_exposure or "none")
        )
        _print("\n  \033[2m(untrusted Procedure prose; read-only)\033[0m")
        markdown = str(document["markdown"])
        if len(markdown) > 12_000:
            markdown = markdown[:12_000] + "\n… [truncated]"
        for line in markdown.splitlines():
            _print("  " + line)
        _print()
        return
    raise CapitolError(
        "usage: /capitol procedure search \"<query>\" | "
        "show <workflow-id> [--version N]"
    )


# ---------------------------------------------------------------------------
# Start / steer
# ---------------------------------------------------------------------------

def _cmd_start(runtime: CapitolRuntime, tokens: List[str], config: dict):
    positional, flags = _split_flags(tokens, {
        "--input": True, "--key": True, "--artifact": True,
        "--watch": False, "--raw": False,
    })
    if not positional:
        raise CapitolError(
            "usage: /capitol start <workflow> [--input JSON|@file] "
            "[--raw] [--key K] [--artifact ID ...] [--watch]"
        )
    workflow_id = positional[0]
    inputs: Optional[Dict[str, Any]] = None
    if flags.get("input") is not None:
        value = _load_json_arg(str(flags["input"]), "--input")
        if flags.get("raw"):
            if not isinstance(value, dict):
                raise CapitolError("--raw --input must be a JSON object "
                                   "(the full inputs map)")
            inputs = value
        else:
            # The workflow's JSON-input node key is discovered from
            # describe_workflow (engine util E7); the value goes under it.
            key = _input_key(runtime, workflow_id)
            inputs = {key: value}
            _print(f"\n  \033[2minputs key: {key}\033[0m")
    idempotency_key = str(flags.get("key") or "")
    if not idempotency_key:
        idempotency_key = (
            f"capitol-cli:{time.strftime('%Y%m%d%H%M%S')}-"
            f"{uuid.uuid4().hex[:6]}"
        )
    artifacts = [
        {"artifact_id": artifact_id}
        for artifact_id in _as_list(flags.get("artifact"))
    ]
    submission = runtime.call_workflow(
        workflow_id, inputs,
        idempotency_key=idempotency_key,
        artifacts=artifacts or None,
    )
    run_id = str(submission.get("run_id") or "")
    replayed = " (replayed)" if submission.get("replayed") else ""
    _print(f"\n  \033[1;32mrun {run_id} started{replayed}\033[0m")
    _print(f"  \033[2midempotency key: {idempotency_key} — reuse with "
           "--key to replay this exact run\033[0m\n")
    if flags.get("watch") and run_id:
        _cmd_watch(runtime, [run_id], config)


def _input_key(runtime: CapitolRuntime, workflow_id: str) -> str:
    from .packs.engine import workflow_inputs_key

    return workflow_inputs_key(runtime, workflow_id)


def _cmd_chat(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {"--file": True})
    if not positional:
        raise CapitolError(
            "usage: /capitol chat <message> [--file PATH ...]"
        )
    runtime.handshake()
    payload = runtime.chat(
        " ".join(positional), files=_as_list(flags.get("file")) or None
    )
    reply = clean_text((payload or {}).get("assistant_reply"), 2000)
    _print("\n  \033[2m(untrusted assistant prose)\033[0m")
    _print(f"  {reply or '(no reply text)'}")
    run_id = (payload or {}).get("run_id")
    if run_id:
        _print(f"  \033[2mlaunched run: {run_id}\033[0m")
    _print()


def _cmd_respond(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {
        "--decline": False, "--continue": False, "--stop": False,
        "--node": True,
    })
    if len(positional) < 2:
        raise CapitolError(
            "usage: /capitol respond <run> <request_id> <answer…> "
            "| --decline | --continue|--stop [--node ID]"
        )
    run_id, request_id = positional[0], positional[1]
    answer = " ".join(positional[2:])
    if flags.get("continue") or flags.get("stop"):
        token = "continue" if flags.get("continue") else "stop"
        runtime.submit_intervention(
            run_id, str(flags.get("node") or ""), request_id, token
        )
        _print(f"\n  \033[1;32m✓ intervention '{token}' delivered\033[0m\n")
        return
    declined = bool(flags.get("decline"))
    if not answer and not declined:
        raise CapitolError(
            "provide an answer, or --decline to decline the clarification"
        )
    runtime.submit_clarification(
        run_id, request_id, answer, declined=declined
    )
    verb = "declined" if declined else "answered"
    _print(f"\n  \033[1;32m✓ clarification {verb}\033[0m\n")


def _cmd_pause(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {"--reason": True})
    if not positional:
        raise CapitolError("usage: /capitol pause <run> [--reason …]")
    _print_json(runtime.pause_run(positional[0],
                                  reason=str(flags.get("reason") or "")))


def _cmd_stop(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {"--reason": True})
    if not positional:
        raise CapitolError("usage: /capitol stop <run> [--reason …]")
    _print_json(runtime.stop_run(positional[0],
                                 reason=str(flags.get("reason") or "")))


def _cmd_resume(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(
        tokens, {"--payload": True, "--edited-nodes": True}
    )
    if not positional:
        raise CapitolError(
            "usage: /capitol resume <run> [--payload JSON] "
            "[--edited-nodes a,b]"
        )
    payload = None
    if flags.get("payload"):
        payload = _load_json_arg(str(flags["payload"]), "--payload")
    edited = [
        node for node in str(flags.get("edited-nodes") or "").split(",")
        if node
    ]
    _print_json(runtime.resume_run(
        positional[0], payload=payload, edited_node_ids=edited or None,
    ))


def _cmd_cancel(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol cancel <task_id>")
    _print_json(runtime.cancel_task(tokens[0]))


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

def _cmd_up(runtime: CapitolRuntime, tokens: List[str]):
    positional, flags = _split_flags(tokens, {"--inline": False})
    if not positional:
        raise CapitolError("usage: /capitol up <path> [--inline]")
    path = str(Path(positional[0]).expanduser())
    if flags.get("inline"):
        result = runtime.upload_file_inline(path)
    else:
        result = runtime.upload_artifact(path)
    _print(f"\n  \033[1;32m✓ uploaded {result['filename']}\033[0m")
    _print(f"    artifact_id: {result['artifact_id']}")
    _print(f"    digest:      {result['digest']}")
    _print(f"    size:        {result['size_bytes']:,} bytes\n")


def _cmd_down(runtime: CapitolRuntime, tokens: List[str]):
    if len(tokens) < 2:
        raise CapitolError("usage: /capitol down <file_id> <dest>")
    result = runtime.download_artifact(
        tokens[0], str(Path(tokens[1]).expanduser())
    )
    _print(f"\n  \033[1;32m✓ downloaded to {result['path']}\033[0m")
    _print(f"    digest: {result['digest']}  "
           f"({result['size_bytes']:,} bytes)")
    _print("    \033[2mverify-on-fetch: compare this digest against the "
           "producer's record\033[0m\n")


def _cmd_url(runtime: CapitolRuntime, tokens: List[str]):
    if not tokens:
        raise CapitolError("usage: /capitol url <file_id>")
    payload = runtime.download_url(tokens[0])
    _print(f"\n  {clean_text(payload.get('download_url'), 500)}")
    _print("  \033[2mclick-time presigned URL — use immediately\033[0m\n")


# ---------------------------------------------------------------------------
# Flow packs
# ---------------------------------------------------------------------------

def _cmd_packs(tokens: List[str], config: dict):
    from .packs import list_pack_errors, load_pack, pack_dirs
    from .packs.registry import load_pack_dir

    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list":
        directories = pack_dirs()
        problems = list_pack_errors()
        if not directories:
            _print("\n  \033[2mNo flow packs found (built-in or in "
                   "~/.config/conch/packs/).\033[0m\n")
            return
        _print(f"\n  \033[1;36mFlow packs ({len(directories)}):\033[0m")
        for directory in directories:
            if directory.name in problems:
                _print(f"    \033[31m{directory.name}: INVALID — "
                       f"{clean_text(problems[directory.name], 160)}"
                       "\033[0m")
                continue
            pack = load_pack_dir(directory)
            _print(
                f"    \033[1m{pack.name}\033[0m v{pack.version}  "
                f"\033[2m{pack.digest[:23]}…  {directory}\033[0m"
            )
            if pack.description:
                _print(f"      \033[2m{clean_text(pack.description, 160)}"
                       "\033[0m")
        _print()
        return
    if sub in ("show", "verify") and not rest:
        raise CapitolError(f"usage: /capitol pack {sub} <name>")
    if sub == "show":
        pack = load_pack(rest[0])
        _print(f"\n  \033[1;36m{pack.name}\033[0m v{pack.version}")
        _print(f"    digest:    {pack.digest}")
        _print(f"    source:    {pack.source}")
        replaces = pack.raw["pack"].get("replaces") or []
        if replaces:
            _print(f"    replaces:  {', '.join(replaces)}")
        _print(f"    workflows: {', '.join(pack.workflow_aliases())}")
        intakes = ", ".join(
            str(intake.get("kind")) for intake in pack.intakes
        )
        _print(f"    intakes:   {intakes or '(none)'}")
        _print("    approvals: "
               + (", ".join(pack.approval_kinds()) or "(none)"))
        _print(f"    requests:  {', '.join(sorted(pack.requests))}")
        acceptance = pack.raw.get("acceptance") or {}
        if acceptance:
            _print(f"    acceptance: {acceptance.get('kind')}"
                   f" ({acceptance.get('module') or acceptance.get('fixtures') or ''})")
        _print()
        return
    if sub == "verify":
        _verify_pack(rest[0], config)
        return
    raise CapitolError("usage: /capitol pack list|show|verify <name>")


def _verify_pack(name: str, config: Optional[dict] = None):
    """Validate the manifest (fail closed) and run the pack's acceptance
    drill — the funding pack's verify pattern generalized."""
    from .packs import load_pack

    pack = load_pack(name)  # full fail-closed validation + template parse
    _print(f"\n  \033[1;36mverify {pack.name}\033[0m v{pack.version}")
    _print(f"    manifest: valid ({pack.digest})")
    acceptance = pack.raw.get("acceptance") or {}
    kind = str(acceptance.get("kind") or "")
    if not kind:
        _print("    acceptance: none declared — manifest validation "
               "only\n")
        return
    if kind == "golden_scenarios":
        module_name = str(acceptance.get("module") or "")
        try:
            import importlib

            module = importlib.import_module(module_name)
        except ImportError:
            _print(
                f"    acceptance: SKIPPED — drill module {module_name!r} "
                "is not importable here (the golden-scenario suite ships "
                "with the source checkout's tests/, not the installed "
                "package)\n"
            )
            return
        _print("    acceptance: replaying the golden scenario suite "
               "against the fake gateway …")
        report = module.verify_all(log=lambda line: _print(f"  {line}"))
        failed = [row["scenario"] for row in report if not row["ok"]]
        if failed:
            for row in report:
                if not row["ok"] and row.get("diff_hint"):
                    _print(f"    \033[31mdiverged: {row['scenario']} at "
                           f"{row['diff_hint']}\033[0m")
            raise CapitolError(
                f"acceptance drill FAILED for {pack.name}: "
                + ", ".join(failed)
            )
        _print(f"    \033[1;32m✓ {len(report)} scenario(s) equivalent"
               "\033[0m\n")
        return
    if kind == "workflow_drill":
        # Compiled packs: synthetic fixtures through the REAL workflows
        # on the serving stack, expected gates asserted (fail closed).
        from .compiler.drill import run_workflow_drill

        _print("    acceptance: running the workflow drill against the "
               "serving stack …")
        evidence = run_workflow_drill(
            pack, config or {}, log=lambda line: _print(f"  {line}")
        )
        _print(f"    \033[1;32m✓ {len(evidence.get('runs') or [])} "
               "drill run(s) passed\033[0m\n")
        return
    raise CapitolError(
        f"acceptance kind {kind!r} is not runnable by this engine "
        "(failing closed)"
    )


# ---------------------------------------------------------------------------
# Gated admin
# ---------------------------------------------------------------------------

ADMIN_ANCHOR_GOAL_PREFIX = "capitol-admin-cli"


def _admin_context(config: dict):
    """CapitolAdmin + its ledger anchor: the kernel store and a
    find-or-create ops mission (the funding CLI's pattern). Mutations
    refuse without both — the external-action ledger is mandatory."""
    from .admin import CapitolAdmin
    from ..kernel.store import MissionStore, default_kernel_db_path

    store = MissionStore(default_kernel_db_path())
    mission_id = ""
    try:
        for mission in store.list_missions():
            goal = str((mission.get("spec") or {}).get("goal") or "")
            if goal.startswith(ADMIN_ANCHOR_GOAL_PREFIX):
                mission_id = mission["mission_id"]
                break
        if not mission_id:
            mission_id = store.create_mission({
                "goal": (
                    f"{ADMIN_ANCHOR_GOAL_PREFIX}: ledger anchor for "
                    "/capitol admin mutations"
                ),
                "budgets": {},
            })
        admin = CapitolAdmin.from_config(
            config, store=store, mission_id=mission_id
        )
    except Exception:
        store.close()
        raise
    return admin, store


def _admin_key(flags: Dict[str, Any], op: str, payload: Any) -> str:
    key = str(flags.get("key") or "")
    if key:
        return key
    key = _digest_key(op, payload)
    _print(f"\n  \033[2midempotency key: {key} — reuse with --key to "
           "replay\033[0m")
    return key


def _cmd_admin(tokens: List[str], config: dict):
    if not tokens:
        raise CapitolError(
            "usage: /capitol admin <agents|agent|create-agent|delete-agent"
            "|allowlist|bind-collections|persist|publish|rollback"
            "|delete-workflow|versions|schedules|schedule-add"
            "|schedule-update|schedule-delete|collections|rotate-bearer"
            "|mint-bearer|revoke-bearer> …"
        )
    sub, rest = tokens[0], tokens[1:]
    admin, store = _admin_context(config)
    try:
        _dispatch_admin(admin, sub, rest)
    finally:
        store.close()


def _dispatch_admin(admin, sub: str, rest: List[str]):
    positional, flags = _split_flags(rest, {
        "--workflows": True, "--alias": True, "--description": True,
        "--key": True, "--tz": True, "--input-overrides": True,
        "--disabled": False, "--destination": True,
    })

    if sub == "agents":
        _print_json(admin.list_agents())
        return
    if sub == "agent":
        if not positional:
            raise CapitolError("usage: /capitol admin agent <id>")
        _print_json(admin.get_agent(positional[0]))
        return
    if sub == "create-agent":
        if not positional or not flags.get("workflows"):
            raise CapitolError(
                "usage: /capitol admin create-agent <name> "
                "--workflows a,b [--alias R] [--description …]"
            )
        name = positional[0]
        workflows = [w for w in str(flags["workflows"]).split(",") if w]
        key = _admin_key(flags, "create-agent",
                         {"name": name, "workflows": workflows})
        result = admin.create_orchestrator_agent(
            name, workflows,
            idempotency_key=key,
            description=str(flags.get("description") or ""),
            registry_alias=str(flags.get("alias") or ""),
        )
        _print_json(result)
        _print("\n  \033[2mthe minted bearer went straight to the "
               "A2Actrl registry; only its fingerprint is shown\033[0m")
        return
    if sub == "delete-agent":
        if not positional:
            raise CapitolError("usage: /capitol admin delete-agent <id>")
        key = _admin_key(flags, "delete-agent", {"agent": positional[0]})
        _print_json(admin.delete_agent(positional[0],
                                       idempotency_key=key))
        return
    if sub == "allowlist":
        if len(positional) < 2:
            raise CapitolError(
                "usage: /capitol admin allowlist <agent> <wf,…>"
            )
        workflows = [w for w in positional[1].split(",") if w]
        key = _admin_key(flags, "allowlist",
                         {"agent": positional[0], "workflows": workflows})
        _print_json(admin.set_workflow_allowlist(
            positional[0], workflows, idempotency_key=key,
        ))
        return
    if sub == "bind-collections":
        if len(positional) < 2:
            raise CapitolError(
                "usage: /capitol admin bind-collections <agent> <id,…>"
            )
        collections = [c for c in positional[1].split(",") if c]
        key = _admin_key(flags, "bind-collections",
                         {"agent": positional[0],
                          "collections": collections})
        _print_json(admin.bind_agent_collections(
            positional[0], collections, idempotency_key=key,
        ))
        return
    if sub == "persist":
        if not positional:
            raise CapitolError(
                "usage: /capitol admin persist @payload.json "
                "(the payload carries its stable workflow id)"
            )
        payload = _load_json_arg(positional[0], "persist payload")
        if not isinstance(payload, dict):
            raise CapitolError("persist payload must be a JSON object")
        key = _admin_key(flags, "persist", payload)
        _print_json(admin.persist_workflow(payload, idempotency_key=key))
        return
    if sub in ("publish", "rollback", "delete-workflow"):
        if not positional:
            raise CapitolError(f"usage: /capitol admin {sub} <workflow>")
        workflow_id = positional[0]
        key = _admin_key(flags, sub, {"workflow": workflow_id})
        if sub == "publish":
            _print_json(admin.publish_workflow(workflow_id,
                                               idempotency_key=key))
        elif sub == "rollback":
            _print_json(admin.rollback_workflow(workflow_id,
                                                idempotency_key=key))
            _print("\n  \033[2mrollback is the persist-based inverse of "
                   "publish (publish_to_api cleared, same workflow-api "
                   "version lineage); the platform-api rollback endpoint "
                   "reads a version store workflow-api publishes don't "
                   "populate\033[0m")
        else:
            _print_json(admin.delete_workflow(workflow_id,
                                              idempotency_key=key))
        return
    if sub == "versions":
        if not positional:
            raise CapitolError("usage: /capitol admin versions <workflow>")
        _print_json(admin.workflow_versions(positional[0]))
        return
    if sub == "schedules":
        if not positional:
            raise CapitolError("usage: /capitol admin schedules <workflow>")
        _print_json(admin.list_schedules(positional[0]))
        return
    if sub == "schedule-add":
        if len(positional) < 3:
            raise CapitolError(
                "usage: /capitol admin schedule-add <wf> <name> <cron> "
                "[--tz TZ] [--input-overrides JSON] [--disabled]"
            )
        workflow_id, name, cron = positional[0], positional[1], positional[2]
        overrides = None
        if flags.get("input-overrides"):
            overrides = _load_json_arg(
                str(flags["input-overrides"]), "--input-overrides"
            )
        key = _admin_key(flags, "schedule-add",
                         {"workflow": workflow_id, "name": name,
                          "cron": cron})
        _print_json(admin.create_schedule(
            workflow_id, name, cron,
            idempotency_key=key,
            timezone=str(flags.get("tz") or "UTC"),
            input_overrides=overrides,
            enabled=not flags.get("disabled"),
        ))
        return
    if sub == "schedule-update":
        if len(positional) < 3:
            raise CapitolError(
                "usage: /capitol admin schedule-update <wf> <id> "
                "@updates.json"
            )
        updates = _load_json_arg(positional[2], "schedule updates")
        if not isinstance(updates, dict):
            raise CapitolError("schedule updates must be a JSON object")
        key = _admin_key(flags, "schedule-update",
                         {"workflow": positional[0], "id": positional[1],
                          "updates": updates})
        _print_json(admin.update_schedule(
            positional[0], positional[1], updates, idempotency_key=key,
        ))
        return
    if sub == "schedule-delete":
        if len(positional) < 2:
            raise CapitolError(
                "usage: /capitol admin schedule-delete <wf> <id>"
            )
        key = _admin_key(flags, "schedule-delete",
                         {"workflow": positional[0], "id": positional[1]})
        _print_json(admin.delete_schedule(
            positional[0], positional[1], idempotency_key=key,
        ))
        return
    if sub == "collections":
        if not positional:
            _print_json(admin.list_collections())
            return
        action = positional[0]
        if action == "create" and len(positional) >= 2:
            key = _admin_key(flags, "collections-create",
                             {"name": positional[1]})
            _print_json(admin.create_collection(
                positional[1], idempotency_key=key,
                destination=str(flags.get("destination") or "qdrant"),
            ))
            return
        if action == "delete" and len(positional) >= 2:
            key = _admin_key(flags, "collections-delete",
                             {"collection": positional[1]})
            _print_json(admin.delete_collection(
                positional[1], idempotency_key=key,
            ))
            return
        raise CapitolError(
            "usage: /capitol admin collections "
            "[create <name> [--destination d] | delete <id>]"
        )
    if sub == "rotate-bearer":
        if not positional:
            raise CapitolError(
                "usage: /capitol admin rotate-bearer <agent> [--alias R]"
            )
        key = _admin_key(flags, "rotate-bearer", {"agent": positional[0]})
        _print_json(admin.rotate_bearer(
            positional[0], idempotency_key=key,
            registry_alias=str(flags.get("alias") or ""),
        ))
        return
    if sub == "mint-bearer":
        if len(positional) < 2:
            raise CapitolError(
                "usage: /capitol admin mint-bearer <agent> <label>"
            )
        key = _admin_key(flags, "mint-bearer",
                         {"agent": positional[0], "label": positional[1]})
        _print_json(admin.mint_deployment_bearer(
            positional[0], positional[1], idempotency_key=key,
        ))
        return
    if sub == "revoke-bearer":
        if len(positional) < 2:
            raise CapitolError(
                "usage: /capitol admin revoke-bearer <agent> <bearer_id>"
            )
        key = _admin_key(flags, "revoke-bearer",
                         {"agent": positional[0],
                          "bearer": positional[1]})
        _print_json(admin.revoke_bearer(
            positional[0], positional[1], idempotency_key=key,
        ))
        return
    raise CapitolError(f"unknown admin subcommand {sub!r} — /capitol help")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_RUNTIME_COMMANDS = {
    "card": _cmd_card,
    "agents": _cmd_agents,
    "workflows": _cmd_workflows,
    "start": _cmd_start,
    "watch": _cmd_watch,
    "describe": _cmd_describe,
    "suggest": _cmd_suggest,
    "versions": _cmd_versions,
    "stats": _cmd_stats,
    "runs": _cmd_runs,
    "status": _cmd_status,
    "events": _cmd_events,
    "output": _cmd_output,
    "evals": _cmd_evals,
    "chat": _cmd_chat,
    "respond": _cmd_respond,
    "pause": _cmd_pause,
    "stop": _cmd_stop,
    "resume": _cmd_resume,
    "cancel": _cmd_cancel,
    "up": _cmd_up,
    "down": _cmd_down,
    "url": _cmd_url,
}


def run_capitol_command(arg: str, config: dict) -> None:
    """Handle ``/capitol …`` from the interactive shell."""
    arg = (arg or "").strip()
    if not arg or arg.lower() in ("help", "-h", "--help"):
        print(USAGE)
        return
    try:
        tokens = shlex.split(arg)
    except ValueError as exc:
        print(f"\n  \033[31mInvalid arguments: {exc}\033[0m\n")
        return
    sub, rest = tokens[0].lower(), tokens[1:]
    try:
        if sub in ("packs", "pack"):
            _cmd_packs(rest if sub == "pack" else ["list"], config)
            return
        if sub == "admin":
            _cmd_admin(rest, config)
            return
        if sub == "procedure":
            _cmd_procedure(rest, config)
            return
        handler = _RUNTIME_COMMANDS.get(sub)
        if handler is None:
            print(f"\n  \033[31mUnknown subcommand {sub!r}.\033[0m")
            print(USAGE)
            return
        runtime = _runtime(config)
        if sub in ("watch", "start"):
            handler(runtime, rest, config)
        else:
            handler(runtime, rest)
    except CapitolCapabilityError as exc:
        print(
            "\n  \033[33mThis agent's card does not advertise that "
            f"capability: {clean_text(exc, 300)}\033[0m\n"
        )
    except CapitolAuthError as exc:
        print(
            f"\n  \033[31mCapitol credential needed: "
            f"{clean_text(exc, 400)}\033[0m"
        )
        print("  \033[2mno automatic re-auth: set $CAPITOL_A2A_BEARER "
              "(or the env named by capitol_bearer_env), or add the "
              "agent to ~/.capitol-a2a/agents.yaml; admin ops read "
              "$CAPITOL_ADMIN_TOKEN (Procedure REST reads use that "
              "authenticated user/API token too)\033[0m\n")
    except CapitolError as exc:
        print(f"\n  \033[31mCapitol: {clean_text(exc, 600)}\033[0m")
        hint = clean_text(getattr(exc, "hint", ""), 200)
        if hint:
            print(f"  \033[2m{hint}\033[0m")
        print()
    except (KeyboardInterrupt, EOFError):
        print("\n  \033[33mstopped.\033[0m\n")
