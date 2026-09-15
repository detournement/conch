"""The /fleet command family (the fleet awakening).

``/fleet`` drives the distributed task plane — against the
conch-controller daemon when it runs, direct-drive (one-shot schedule +
poll) when it doesn't; the same two-transport client abstraction the
mission commands use. The shell dispatcher reaches this module only
through the slash-command seam (conch.plugins), and the runtime imports
stay lazy and gated on fleet_controller=true so shell-only users never
load the fleet.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

_FLEET_USAGE = (
    "\n  \033[1;36m/fleet — the distributed task plane:\033[0m\n"
    "    /fleet workers                 registry list + live status probe\n"
    "    /fleet run <worker|auto> \"<prompt>\" [--skill s] [--tools a,b]\n"
    "              [--actions read,...] [--model m] [--budget tokens]\n"
    "              [--data class] [--wall seconds]\n"
    "    /fleet task <id>               one dispatch: status + events\n"
    "    /fleet tasks [state]           list dispatches\n"
    "    /fleet cancel <id>             cancel a dispatch (propagates)\n"
    "    /fleet artifacts <id> [pull]   list / pull a task's artifacts\n"
    "    /fleet drain|enable <worker>   worker state transitions\n"
    "    /fleet grant <worker> [--actions ...] [--tools a,b|full]\n"
    "              [--data class] | --revoke     raise/reset the ceiling\n"
    "    /fleet enroll <name> <user@host> [port]  enroll a trusted host\n"
    "    /fleet status                  controller/plane health\n"
    "  \033[2mWorks against the running conch-controller, or drives the\n"
    "  fleet kernel directly when no controller is up. Defaults are\n"
    "  READ-only and narrow; /fleet grant raises one worker's ceiling.\033[0m\n"
)


def _fleet_attach(config: dict):
    """Fleet client, or None after printing why not (mission pattern)."""
    from ..config import get_bool

    if not get_bool(config, "fleet_controller"):
        print(
            "\n  \033[2mThe fleet is not enabled. Set `fleet_controller ="
            " true`\n  in your conch config (see README \"Trusted SSH"
            " fleet\") to enroll\n  workers and dispatch tasks.\033[0m\n"
        )
        return None
    try:
        from .client import attach_fleet

        return attach_fleet(config)
    except Exception as exc:
        print(f"\n  \033[31mFleet unavailable: {exc}\033[0m\n")
        return None


def _parse_fleet_flags(tokens: List[str]):
    """(positional, flags) from a shlex-split /fleet argument tail."""
    positional: List[str] = []
    flags: Dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:]
            if key == "revoke":
                flags["revoke"] = "true"
                index += 1
                continue
            if index + 1 >= len(tokens):
                raise ValueError(f"flag --{key} needs a value")
            flags[key] = tokens[index + 1]
            index += 2
        else:
            positional.append(token)
            index += 1
    return positional, flags


def _csv(value: str) -> Optional[List[str]]:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _print_fleet_dispatch(dispatch: Dict[str, Any], verbose: bool = False):
    state_colors = {
        "succeeded": "32", "failed": "31", "cancelled": "31",
        "running": "36", "queued": "2", "offering": "36",
        "waiting_child": "35", "needs_reconcile": "33",
    }
    color = state_colors.get(dispatch["state"], "0")
    envelope = dispatch.get("envelope") or {}
    task_text = str(envelope.get("task") or "")
    if len(task_text) > 56:
        task_text = task_text[:53] + "..."
    skill = ""
    if envelope.get("skills"):
        skill = f" skill={','.join(envelope['skills'])}"
    print(
        f"    \033[{color}m[{dispatch['state']}]\033[0m {task_text}"
        f"  \033[2m{dispatch['task_id'][:22]}…"
        f" worker={dispatch.get('worker_id', '')[:18] or '-'}"
        f" attempt={dispatch.get('attempt')}{skill}\033[0m"
    )
    if verbose:
        result = dispatch.get("result") or {}
        if result.get("summary"):
            summary = str(result["summary"])
            if len(summary) > 900:
                summary = summary[:900] + "…"
            print(f"      \033[2m{summary}\033[0m")
        for ref in result.get("artifacts") or []:
            print(
                f"      \033[2martifact {ref.get('name')} "
                f"sha256:{str(ref.get('digest'))[:16]}… "
                f"({ref.get('size')} bytes)\033[0m"
            )
        if dispatch.get("error"):
            print(f"      \033[31m{dispatch['error']}\033[0m")


def _fleet_run(client, tokens: List[str], config: dict) -> None:
    from .client import run_fleet_task

    positional, flags = _parse_fleet_flags(tokens)
    if len(positional) < 2:
        print(
            "\n  \033[2mUsage: /fleet run <worker|auto> \"<prompt>\""
            " [--skill s] [--tools a,b] [--actions ...] [--model m]"
            " [--budget n] [--data class] [--wall seconds]\033[0m\n"
        )
        return
    worker = positional[0]
    prompt = " ".join(positional[1:])
    print(
        f"\n  \033[2mdispatching to {worker}"
        + (f" as skill '{flags['skill']}'" if flags.get("skill") else "")
        + " …\033[0m"
    )
    dispatch = run_fleet_task(
        client, task=prompt, worker=worker,
        skill=flags.get("skill", ""),
        tools=_csv(flags.get("tools", "")),
        actions=_csv(flags.get("actions", "")),
        model=flags.get("model", ""),
        data=flags.get("data", ""),
        token_budget=int(flags.get("budget") or 0),
        wall_clock_seconds=int(flags.get("wall") or 0),
    )
    print()
    _print_fleet_dispatch(dispatch, verbose=True)
    print()


def _resolve_fleet_task(client, ref: str) -> Optional[Dict[str, Any]]:
    ref = (ref or "").strip()
    if not ref:
        return None
    matches = [
        task for task in client.list_tasks()
        if task["task_id"] == ref or task["task_id"].startswith(ref)
    ]
    return matches[0] if len(matches) == 1 else None


def handle_fleet_command(arg: str, config: dict) -> None:
    arg = (arg or "").strip()
    if not arg or arg.lower() in ("help", "-h", "--help"):
        print(_FLEET_USAGE)
        return
    try:
        tokens = shlex.split(arg)
    except ValueError as exc:
        print(f"\n  \033[31mInvalid /fleet arguments: {exc}\033[0m\n")
        return
    sub = tokens[0].lower()
    rest = tokens[1:]
    client = _fleet_attach(config)
    if client is None:
        return
    try:
        _dispatch_fleet_command(client, sub, rest, config)
    except Exception as exc:
        print(f"\n  \033[31mFleet command failed: {exc}\033[0m\n")
    finally:
        try:
            client.close()
        except Exception:
            pass


def _dispatch_fleet_command(client, sub: str, rest: List[str],
                            config: dict) -> None:
    if sub == "status":
        status = client.status()
        mode = getattr(client, "mode", "?")
        attach = (
            "controller socket" if mode == "socket"
            else "direct fleet kernel — controller not running"
        )
        print(f"\n  \033[1;36mFleet\033[0m \033[2m[{attach}]\033[0m")
        print(f"    holder: {status.get('holder')}  epoch:"
              f" {status.get('epoch')}")
        workers = status.get("workers") or {}
        print("    workers: " + (", ".join(
            f"{name}={state}" for name, state in sorted(workers.items())
        ) or "none enrolled"))
        dispatches = status.get("dispatches") or {}
        print("    dispatches: " + (", ".join(
            f"{count} {state}"
            for state, count in sorted(dispatches.items())
        ) or "none") + "\n")
        return
    if sub == "workers":
        entries = client.list_workers()
        if not entries:
            print(
                "\n  \033[2mNo workers enrolled. Enroll one with"
                " /fleet enroll <name> <user@host>.\033[0m\n"
            )
            return
        print(f"\n  \033[1;36mFleet workers ({len(entries)})\033[0m")
        state_colors = {
            "active": "32", "pending": "36", "draining": "35",
            "unreachable": "31", "offline": "2", "quarantined": "31",
            "revoked": "31", "updating": "33",
        }
        for entry in entries:
            probe = client.probe_worker(entry["name"])
            live = (
                "\033[32mreachable\033[0m" if probe.get("reachable")
                else "\033[31munreachable\033[0m"
                     f" \033[2m({str(probe.get('error'))[:60]})\033[0m"
            )
            color = state_colors.get(entry["state"], "0")
            ceiling = entry.get("ceiling") or {}
            print(
                f"    \033[1m{entry['name']}\033[0m"
                f" \033[{color}m[{entry['state']}]\033[0m"
                f" {entry.get('ssh_user')}@{entry['host']}  {live}"
            )
            print(
                f"      \033[2mtrust={entry['trust_level']}"
                f" data≤{entry['data_ceiling']}"
                f" profile={entry.get('runtime_profile') or '-'}"
                f" actions={','.join(ceiling.get('actions') or [])}\033[0m"
            )
            print(
                f"      \033[2mtools={','.join(ceiling.get('tools') or [])}"
                + (f" skills={','.join(entry['skills'])}"
                   if entry.get("skills") else "")
                + "\033[0m"
            )
        print()
        return
    if sub == "run":
        _fleet_run(client, rest, config)
        return
    if sub == "tasks":
        state = rest[0] if rest else ""
        tasks = client.list_tasks(state)
        if not tasks:
            print("\n  \033[2mNo fleet tasks"
                  + (f" in state {state!r}" if state else "")
                  + ".\033[0m\n")
            return
        print(f"\n  \033[1;36mFleet tasks ({len(tasks)})\033[0m")
        for task in tasks[-30:]:
            _print_fleet_dispatch(task)
        print()
        return
    if sub == "task":
        if not rest:
            print("\n  \033[2mUsage: /fleet task <id>\033[0m\n")
            return
        dispatch = _resolve_fleet_task(client, rest[0])
        if dispatch is None:
            print(f"\n  \033[31mNo task matching {rest[0]!r}.\033[0m\n")
            return
        print()
        _print_fleet_dispatch(dispatch, verbose=True)
        events = client.task_events(dispatch["task_id"])
        if events:
            print("    \033[2mevents: " + ", ".join(
                event["kind"] for event in events[-12:]
            ) + "\033[0m")
        print()
        return
    if sub == "cancel":
        if not rest:
            print("\n  \033[2mUsage: /fleet cancel <id>\033[0m\n")
            return
        dispatch = _resolve_fleet_task(client, rest[0])
        if dispatch is None:
            print(f"\n  \033[31mNo task matching {rest[0]!r}.\033[0m\n")
            return
        done = client.cancel(dispatch["task_id"])
        print(
            f"\n  \033[1;32m✓ cancelled\033[0m \033[2m"
            f"{dispatch['task_id']}\033[0m\n" if done else
            f"\n  \033[2m{dispatch['task_id']} was already terminal."
            "\033[0m\n"
        )
        return
    if sub == "artifacts":
        if not rest:
            print("\n  \033[2mUsage: /fleet artifacts <id> [pull]\033[0m\n")
            return
        dispatch = _resolve_fleet_task(client, rest[0])
        if dispatch is None:
            print(f"\n  \033[31mNo task matching {rest[0]!r}.\033[0m\n")
            return
        references = (dispatch.get("result") or {}).get("artifacts") or []
        if not references:
            print("\n  \033[2mThe task declared no artifacts (files in"
                  " the workspace's out/ directory become artifacts)."
                  "\033[0m\n")
            return
        pull = len(rest) > 1 and rest[1].lower() == "pull"
        print(f"\n  \033[1;36mArtifacts for {dispatch['task_id'][:22]}…"
              f"\033[0m")
        for ref in references:
            line = (f"    {ref.get('name')}  \033[2msha256:"
                    f"{ref.get('digest')} ({ref.get('size')} bytes)\033[0m")
            print(line)
            if pull:
                result = client.pull_artifact(
                    dispatch["task_id"], str(ref.get("digest"))
                )
                print(f"      \033[1;32m→ {result['path']}\033[0m")
        print()
        return
    if sub in ("drain", "enable"):
        if not rest:
            print(f"\n  \033[2mUsage: /fleet {sub} <worker>\033[0m\n")
            return
        result = (
            client.drain(rest[0]) if sub == "drain"
            else client.enable(rest[0])
        )
        print(f"\n  \033[1;32m✓ {rest[0]} is now"
              f" {result.get('state')}\033[0m\n")
        return
    if sub == "grant":
        positional, flags = _parse_fleet_flags(rest)
        if not positional:
            print(
                "\n  \033[2mUsage: /fleet grant <worker>"
                " [--actions read,write_local,...]"
                " [--tools a,b|full] [--data class] | --revoke\033[0m\n"
            )
            return
        worker = positional[0]
        if flags.get("revoke"):
            result = client.revoke_grants(worker)
            print(f"\n  \033[1;32m✓ grants revoked\033[0m — ceiling now"
                  f" \033[2m{result.get('ceiling')}\033[0m\n")
            return
        tools_flag = flags.get("tools")
        tools = "full" if tools_flag == "full" else _csv(tools_flag or "")
        result = client.grant(
            worker,
            actions=_csv(flags.get("actions", "")),
            tools=tools,
            data=flags.get("data", ""),
            granted_by=os.environ.get("USER", "owner"),
        )
        print(f"\n  \033[1;32m✓ grant applied to {worker}\033[0m —"
              f" ceiling now \033[2m{result.get('ceiling')}\033[0m\n")
        return
    if sub == "enroll":
        _fleet_enroll(client, rest, config)
        return
    print(_FLEET_USAGE)


def _fleet_enroll(client, rest: List[str], config: dict) -> None:
    """Enroll a trusted SSH host as a worker (interactive host-key
    confirmation; BatchMode probe decides autonomy capability)."""
    if len(rest) < 2:
        print("\n  \033[2mUsage: /fleet enroll <name> <user@host>"
              " [port]\033[0m\n")
        return
    from ..ssh_control import SSHValidationError, parse_ssh_target
    from .client import DirectFleetClient
    from .enroll import Enroller, EnrollmentError, build_ssh_target

    if not isinstance(client, DirectFleetClient):
        # Enrollment writes the registry and needs the local SSH session;
        # do it against the kernel directly (safe: enrollment is a
        # registry insert, not a dispatch).
        client = DirectFleetClient(config)
    name = rest[0]
    try:
        target = parse_ssh_target(
            rest[1], rest[2] if len(rest) > 2 else None
        )
    except SSHValidationError as exc:
        print(f"\n  \033[31mInvalid SSH target: {exc}\033[0m\n")
        return
    signers = str(config.get("fleet_allowed_signers") or "").strip()
    signers_bytes = b""
    if signers:
        path = Path(os.path.expanduser(signers))
        if path.is_file():
            signers_bytes = path.read_bytes()

    def confirm(prompt: str) -> bool:
        try:
            answer = input(f"  {prompt} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    enroller = Enroller(client.registry, confirm=confirm)
    try:
        receipt = enroller.enroll(
            name, build_ssh_target(target.host, target.user, target.port),
            allowed_signers_bytes=signers_bytes,
        )
    except EnrollmentError as exc:
        print(f"\n  \033[31mEnrollment failed: {exc}\033[0m\n")
        return
    # Record where hostctl landed so the transport can find it, and
    # activate: an enrolled worker starts PENDING; the operator asked for
    # it by name, so admit it for work.
    worker_id = receipt["worker_id"]
    install = receipt.get("install") or {}
    if install.get("installed"):
        worker = client.registry.require(worker_id)
        labels = dict(worker.get("labels") or {})
        labels["hostctl"] = (
            f"python3 {install['installed']} --home"
            f" {enroller.remote_dir}"
        )
        client.registry.assign_authority(worker_id, labels=labels)
    client.registry.activate(worker_id, reason="operator enroll")
    autonomy = (
        "autonomy-capable (key-based BatchMode works)"
        if receipt.get("autonomy_capable")
        else "NOT autonomy-capable (no restart-safe key auth — the"
             " controller cannot reach it unattended)"
    )
    print(
        f"\n  \033[1;32m✓ enrolled {name}\033[0m"
        f" \033[2m({worker_id})\033[0m\n"
        f"  \033[2mprofiles: {', '.join(receipt.get('profiles') or [])}"
        f" — {autonomy}\033[0m\n"
    )
