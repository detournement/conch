"""Bounded worker task execution over AgentSession (Swarm Phase 2).

The worker supervisor spawns this in a fresh process/session group per
task attempt. It:

1. loads the task envelope the supervisor persisted,
2. builds tool clients restricted to the **envelope's exact tool
   intersection** — the model can call nothing the controller did not
   name — with ``delegate_task`` swapped for the brokered client (no
   local recursive delegation on a worker),
3. runs one bounded ``chat_turn`` (request-scoped authorization,
   compaction, budgets — unchanged),
4. spools task events to the per-attempt JSONL the supervisor serves, and
5. writes a terminal result the supervisor finalizes into a receipt.

Brokered delegation: when the model calls ``delegate_task``, the brokered
client raises :class:`DelegationRequested`, which escapes ``chat_turn``
cleanly (it is a ``BaseException``, not caught by the tool loop). The
executor persists resume state and exits with
:data:`conch.fleet.worker.CHILD_EXIT_WAITING`; the supervisor parks the
task ``waiting_child`` and releases the slot. On resume the child's result
is folded back in as a complete tool-result group and ``chat_turn`` runs
again — so the parent model sees its delegated call fulfilled, exactly as
local delegation would.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..swarm.protocol import FailureClass, TaskEnvelope, new_id

#: Tools a worker never receives regardless of the envelope: the local
#: self-management surface and interactive/credentialed tools. delegate_task
#: is allowed but rebound to the brokered client.
WORKER_TOOL_DENYLIST = frozenset({
    "conch_config", "manage_tools", "interactive_terminal", "ssh_remote",
    "skill_manage", "fleet_delegate",
})

#: Files a task leaves under ``<workspace>/out`` are published as
#: content-addressed artifacts on completion (bounded count and size).
ARTIFACT_OUT_DIR = "out"
ARTIFACT_MAX_FILES = 32
ARTIFACT_MAX_BYTES = 64 * 1024 * 1024

WORKER_SYSTEM_PROMPT = (
    "You are a Conch fleet worker executing one bounded, delegated task on "
    "a remote host. You are replaceable compute: everything you may do is "
    "in the task and the tools you were given — nothing else. Work "
    "efficiently and autonomously toward the task, then reply with a "
    "concise summary of what you did and found. You cannot ask the user "
    "questions; there is no interactive session. If a subtask is better "
    "handled by a separate agent, call delegate_task — it is brokered by "
    "the controller and its result returns to you."
)


class SkillUnavailable(Exception):
    """The envelope names a skill this host does not have (policy failure)."""


class DelegationRequested(BaseException):
    """Escapes chat_turn to park the task for a brokered child delegation.

    Deliberately a ``BaseException`` so the tool-dispatch ``except
    Exception`` in ``chat_turn`` does not swallow it.
    """

    def __init__(self, arguments: Dict[str, Any], tool_call_id: str):
        super().__init__("delegation requested")
        self.arguments = dict(arguments)
        self.tool_call_id = tool_call_id


class BrokeredDelegateClient:
    """The worker's ``delegate_task``: never runs a local subagent; it
    hands the request to the controller by raising to park the task."""

    name = "delegate_task"

    def call_tool(self, name: str, arguments: dict) -> dict:  # noqa: ARG002
        task = str((arguments or {}).get("task") or "").strip()
        if not task:
            return {"content": [{"type": "text",
                                 "text": "Error: 'task' is required"}]}
        raise DelegationRequested(arguments or {}, new_id("rpc"))


class EventSpool:
    """Append-only per-attempt event JSONL the supervisor serves to the
    controller. Never carries secrets (envelopes/results are business
    data)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, payload: Optional[Dict[str, Any]] = None,
             failure_class: str = "") -> None:
        line = json.dumps({
            "kind": kind, "payload": payload or {},
            "failure_class": failure_class, "created_at": time.time(),
        }, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class TaskExecutor:
    def __init__(self, home, task_id: str, attempt: int):
        self.home = Path(home)
        self.task_id = task_id
        self.attempt = int(attempt)
        self.workspace = self.home / "workspaces" / task_id
        self.spool = EventSpool(
            self.workspace / f"events-{self.attempt}.jsonl"
        )
        self._task = json.loads(
            (self.workspace / "task.json").read_text(encoding="utf-8")
        )
        self.envelope = TaskEnvelope.from_dict(dict(self._task["envelope"]))

    # -- result / resume files --------------------------------------------------

    def _result_path(self) -> Path:
        return self.workspace / f"result-{self.attempt}.json"

    def _resume_path(self) -> Path:
        return self.workspace / f"resume-{self.attempt}.json"

    def _write_result(self, outcome: str, *, failure_class: str = "",
                      error: str = "",
                      payload: Optional[Dict[str, Any]] = None) -> None:
        self._result_path().write_text(json.dumps({
            "outcome": outcome, "failure_class": failure_class,
            "error": error, "payload": payload or {},
        }, sort_keys=True), encoding="utf-8")

    # -- skill resolution (skill-addressed dispatch) ------------------------------

    def _resolve_skills(self) -> List[Dict[str, Any]]:
        """Load the envelope's named skills from THIS host's skill set.

        Fail closed: a skill the envelope names but the host does not have
        is a policy failure, never a silent no-skill run. (The controller
        validates availability before dispatch; this is the worker-side
        recheck.)
        """
        if not self.envelope.skills:
            return []
        from ..skills import get_skill

        resolved = []
        for name in self.envelope.skills:
            skill = get_skill(name)
            if skill is None:
                raise SkillUnavailable(
                    f"skill {name!r} is not installed on this worker — "
                    "refusing the task (install the skill or dispatch "
                    "without --skill)"
                )
            resolved.append(skill)
        return resolved

    # -- tool intersection ------------------------------------------------------

    def _build_tools(self, config: dict,
                     skills: Optional[List[Dict[str, Any]]] = None):
        """Clients + tool definitions restricted to the envelope's tools.

        A skill that scopes tools narrows the intersection further — the
        envelope stays the outer authority bound; a skill can only shrink
        it, never widen it.
        """
        from ..bootstrap import make_builtin_clients
        from ..memory import MemoryStore
        from ..tooling import default_permissions, inject_builtin_tools

        requested = set(self.envelope.tools) - WORKER_TOOL_DENYLIST
        for skill in skills or []:
            if skill.get("tools") is not None:
                requested &= set(skill["tools"])
        memory = MemoryStore()
        permissions = default_permissions()
        all_clients = make_builtin_clients(
            memory, config, interactive=False, permissions=permissions,
        )
        clients: Dict[str, Any] = {
            name: client for name, client in all_clients.items()
            if name in requested and name not in WORKER_TOOL_DENYLIST
        }
        if "delegate_task" in requested:
            clients["delegate_task"] = BrokeredDelegateClient()
        defs: List[dict] = []
        tool_map: Dict[str, Any] = {}
        inject_builtin_tools(defs, tool_map, clients)
        tools = [
            tool_def for tool_def in defs
            if tool_def.get("function", {}).get("name") in requested
        ]
        for client in clients.values():
            if hasattr(client, "set_cwd"):
                client.set_cwd(str(self.workspace))
            if hasattr(client, "bind_permissions"):
                client.bind_permissions(permissions)
        return clients, tools

    # -- messages ---------------------------------------------------------------

    def _fresh_messages(self, config: dict,
                        skills: Optional[List[Dict[str, Any]]] = None
                        ) -> List[dict]:
        context = self.envelope.context.strip()
        user = self.envelope.task
        if context:
            user += f"\n\nContext:\n{context}"
        system = WORKER_SYSTEM_PROMPT
        if skills:
            from ..skills import render_skill

            for skill in skills:
                system += (
                    "\n\nYou are acting as the following skill — follow "
                    "its procedure:\n\n" + render_skill(skill)
                )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _resume_messages(self) -> List[dict]:
        resume = json.loads(self._resume_path().read_text(encoding="utf-8"))
        messages = list(resume["messages"])
        tool_call_id = resume["tool_call_id"]
        deleg_args = resume["arguments"]
        child = resume.get("child_result") or {}
        result_text = str(child.get("result_text") or "(no child result)")
        # Fold the brokered child's result back in as a complete tool-result
        # group so the parent model sees its delegate_task call fulfilled.
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": tool_call_id, "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps(deleg_args, sort_keys=True),
                },
            }],
        })
        messages.append({
            "role": "tool", "tool_call_id": tool_call_id,
            "name": "delegate_task", "content": result_text,
        })
        return messages

    # -- raw_fn resolution ------------------------------------------------------

    def _resolve_execution(self, config: dict):
        """Return (provider, raw_fn). A scripted script file (tests / no
        live model) drives the real chat_turn with canned responses."""
        script_path = os.environ.get("CONCH_FLEET_TASK_SCRIPT", "").strip()
        if script_path:
            return "openai", _scripted_raw_fn(script_path, self.envelope.task)
        from ..providers import RAW_FNS

        provider = (config.get("provider") or "").lower()
        raw_fn = RAW_FNS.get(provider)
        if raw_fn is None:
            raise RuntimeError(f"unknown provider {provider!r} for worker task")
        return provider, raw_fn

    # -- run --------------------------------------------------------------------

    def run(self, resume: bool = False) -> int:
        from ..bootstrap import apply_agent_mode_from_config
        from ..fleet.worker import CHILD_EXIT_WAITING
        from ..runtime import chat_turn

        config = self._load_config()
        # A headless worker honors the configured permission mode so
        # envelope-authorized shell tools auto-execute; the envelope's tool
        # intersection and action classes remain the outer authority bound.
        apply_agent_mode_from_config(config)
        provider, raw_fn = self._resolve_execution(config)
        config = dict(config)
        config["provider"] = provider
        try:
            skills = self._resolve_skills()
        except SkillUnavailable as exc:
            self.spool.emit(
                "failed", {"error": str(exc)},
                failure_class=FailureClass.POLICY,
            )
            self._write_result(
                "failure", failure_class=FailureClass.POLICY,
                error=str(exc),
            )
            return 1
        clients, tools = self._build_tools(config, skills)
        if resume and self._resume_path().is_file():
            messages = self._resume_messages()
        else:
            messages = self._fresh_messages(config, skills)
            self.spool.emit("started", {
                "task_id": self.task_id, "attempt": self.attempt,
                "principal": self.envelope.principal,
            })
        tool_map: Dict[str, Any] = {}
        try:
            reply, usage = chat_turn(
                config, provider, raw_fn, messages, tools or None,
                tool_map, clients,
                max_tool_rounds=int(self.envelope.max_tool_rounds),
            )
        except DelegationRequested as delegation:
            self._park_for_delegation(messages, delegation)
            return CHILD_EXIT_WAITING
        except BaseException as exc:  # noqa: BLE001 — bounded child process
            self.spool.emit(
                "failed", {"error": f"{type(exc).__name__}: {exc}"},
                failure_class=FailureClass.BUG,
            )
            self._write_result(
                "failure", failure_class=FailureClass.BUG,
                error=f"{type(exc).__name__}: {exc}",
            )
            return 1
        summary = str(reply or "").strip()
        if not summary:
            self.spool.emit(
                "failed", {"error": "worker produced no reply"},
                failure_class=FailureClass.TRANSIENT,
            )
            self._write_result(
                "failure", failure_class=FailureClass.TRANSIENT,
                error="worker produced no reply (provider error?)",
            )
            return 1
        artifacts = self._publish_artifacts()
        self.spool.emit("result", {
            "summary": summary[:4000],
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "artifacts": artifacts,
        })
        self._write_result("success", payload={
            "summary": summary,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "artifacts": artifacts,
        })
        # A clean completion supersedes any stale resume marker.
        try:
            self._resume_path().unlink()
        except OSError:
            pass
        return 0

    def _publish_artifacts(self) -> List[Dict[str, Any]]:
        """Publish files the task left in ``<workspace>/out`` into the
        worker's content-addressed store; return their references.

        Bounded and best-effort: artifact transfer must never turn a
        successful task into a failure. The controller pulls the bytes by
        digest over ``artifact.get`` (digest-verified both ways)."""
        import hashlib
        import shutil

        out_dir = self.workspace / ARTIFACT_OUT_DIR
        if not out_dir.is_dir():
            return []
        store = self.home / "artifacts" / "sha256"
        references: List[Dict[str, Any]] = []
        total = 0
        try:
            files = sorted(
                path for path in out_dir.rglob("*") if path.is_file()
            )
        except OSError:
            return []
        for path in files[:ARTIFACT_MAX_FILES]:
            try:
                size = path.stat().st_size
                if total + size > ARTIFACT_MAX_BYTES:
                    break
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                store.mkdir(parents=True, exist_ok=True)
                target = store / digest
                if not target.is_file():
                    shutil.copyfile(path, target)
                references.append({
                    "name": str(path.relative_to(out_dir)),
                    "digest": digest,
                    "size": size,
                })
                total += size
            except OSError:
                continue
        return references

    def _park_for_delegation(self, messages: List[dict],
                             delegation: DelegationRequested) -> None:
        args = delegation.arguments
        self._resume_path().write_text(json.dumps({
            "messages": messages,
            "tool_call_id": delegation.tool_call_id,
            "arguments": args,
        }, sort_keys=True), encoding="utf-8")
        self.spool.emit("delegation_requested", {
            "delegation_id": delegation.tool_call_id,
            "task": str(args.get("task") or ""),
            "context": str(args.get("context") or ""),
            "skill": str(args.get("skill") or ""),
            "parent_task_id": self.task_id,
        })

    def _load_config(self) -> dict:
        path = self.home / "config.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
        return {}


def _scripted_raw_fn(script_path: str, task_text: str = ""):
    """A raw_fn that replays canned provider responses (test / offline
    execution). Each response is ``{"content", "tool_calls", "_usage"}``.

    The script file is either a JSON list of responses, or a dict
    ``{"match": [["substr", [responses]], ...], "default": [responses]}``
    selecting a response list by a substring of the task — so one worker
    can serve distinct parent/child behaviours.

    Within a chosen list, the position is derived from the number of
    assistant messages already in the conversation (not a per-process
    counter) — so a resumed task subprocess continues past a delegation
    instead of replaying it.
    """
    loaded = json.loads(Path(script_path).read_text(encoding="utf-8"))
    if isinstance(loaded, list):
        responses = loaded
    elif isinstance(loaded, dict):
        responses = list(loaded.get("default") or [])
        for entry in loaded.get("match") or []:
            substr, candidate = entry[0], entry[1]
            if substr in (task_text or ""):
                responses = candidate
                break
    else:
        raise RuntimeError("fleet task script must be a JSON list or dict")

    def raw_fn(config, messages, tools):  # noqa: ARG001
        index = sum(
            1 for message in messages
            if message.get("role") == "assistant"
        )
        if index >= len(responses):
            return {"content": "(script exhausted)", "_usage": {
                "input_tokens": 0, "output_tokens": 0, "model": "scripted",
            }}
        response = dict(responses[index])
        response.setdefault("_usage", {
            "input_tokens": 1, "output_tokens": 1, "model": "scripted",
        })
        return response

    return raw_fn


def child_main() -> int:
    home = os.environ.get("CONCH_FLEET_TASK_HOME", "")
    task_id = os.environ.get("CONCH_FLEET_TASK_ID", "")
    attempt = os.environ.get("CONCH_FLEET_TASK_ATTEMPT", "1")
    resume = os.environ.get("CONCH_FLEET_TASK_RESUME") == "1"
    if not home or not task_id:
        print("taskexec: missing task environment", file=sys.stderr)
        return 2
    executor = TaskExecutor(home, task_id, int(attempt))
    return executor.run(resume=resume)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(child_main())
