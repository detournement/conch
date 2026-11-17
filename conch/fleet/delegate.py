"""``fleet_delegate``: local agent sessions delegating bounded tasks to
fleet workers mid-conversation.

Registered like the personal_items/capitol_control precedents (a builtin
client constructed in bootstrap, its tool definition injected in
tooling.inject_builtin_tools) — but only for LOCAL sessions:

- interactive shell sessions and mission sessions get it (missions get an
  envelope-scoped variant carrying the mission spec's ``fleet`` block);
- remote/channel sessions never see it (REMOTE_EXCLUDED_TOOLS);
- delegated local sub-turns never inherit it implicitly (a skill may
  offer it explicitly — the operator's decision);
- workers themselves never receive it (WORKER_TOOL_DENYLIST +
  authority.HARD_EXCLUDED_TOOLS): distributed recursion is the
  controller-brokered ``delegate_task``, never a worker-side fleet call.

Every delegation is brokered through the controller/TaskPlane with the
authority-subset rule enforced in code: the child envelope is clamped to
``requested ∩ caller authority ∩ worker ceiling`` (see
conch/fleet/authority.py for the shipped matrix), and results return as
tool output with artifact references — never as new authority.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from ..kernel.model import DispatchState, KernelError
from . import authority

FLEET_DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "fleet_delegate",
        "description": (
            "Delegate a self-contained task to a remote fleet worker (a "
            "trusted SSH host running a bounded Conch worker). Use this "
            "for work better done on another machine: heavy computation, "
            "host-specific inspection, or long research the local session "
            "should not block on inline. The task runs with ONLY the "
            "tools/actions the worker's owner grant and your own "
            "authority allow — never more. Returns the worker's summary "
            "and any artifact references. Pass 'skill' to have the "
            "remote agent act as an installed skill (its prompt and tool "
            "scope). worker='auto' lets the scheduler pick."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Complete, self-contained task "
                                   "description",
                },
                "worker": {
                    "type": "string",
                    "description": "Worker name, or 'auto' (default) for "
                                   "scheduler placement",
                },
                "context": {
                    "type": "string",
                    "description": "Optional extra context the worker "
                                   "needs",
                },
                "skill": {
                    "type": "string",
                    "description": "Optional skill the remote agent acts "
                                   "as (must be installed on the worker)",
                },
                "tools": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Tool names to request (default: the "
                                   "narrow worker set; clamped to the "
                                   "grant ceiling)",
                },
                "model": {
                    "type": "string",
                    "description": "Model the worker must have resident "
                                   "(scheduler constraint)",
                },
                "wall_clock_seconds": {
                    "type": "integer",
                    "description": "Wall-clock budget (default 300)",
                },
            },
            "required": ["task"],
        },
    },
}


class FleetDelegateClient:
    """The model-callable fleet delegation surface for one local session.

    Bound with the session's caller authority; missions rebind with their
    spec's ``fleet`` block. Serialized like delegate_task (one in-flight
    fleet delegation per session).
    """

    name = "fleet_delegate"

    def __init__(self, config: Optional[dict] = None,
                 caller: Optional[Dict[str, Any]] = None,
                 principal: str = "user",
                 allowed_workers: Optional[list] = None,
                 allowed_skills: Optional[list] = None,
                 attach=None):
        self._config = dict(config or {})
        self._caller = dict(
            caller or authority.DEFAULT_DELEGATE_AUTHORITY
        )
        self._principal = str(principal or "user")
        self._allowed_workers = (
            [str(w) for w in allowed_workers]
            if allowed_workers is not None else None
        )
        self._allowed_skills = (
            [str(s).lower() for s in allowed_skills]
            if allowed_skills is not None else None
        )
        self._attach = attach  # injectable for tests
        self._lock = threading.Lock()

    def configure(self, *, caller: Optional[Dict[str, Any]] = None,
                  principal: str = "",
                  allowed_workers: Optional[list] = None,
                  allowed_skills: Optional[list] = None) -> None:
        if caller is not None:
            self._caller = dict(caller)
        if principal:
            self._principal = principal
        if allowed_workers is not None:
            self._allowed_workers = [str(w) for w in allowed_workers]
        if allowed_skills is not None:
            self._allowed_skills = [str(s).lower() for s in allowed_skills]

    @staticmethod
    def _text(msg: str) -> dict:
        return {"content": [{"type": "text", "text": msg}]}

    def call_tool(self, name: str, arguments: dict) -> dict:
        arguments = arguments or {}
        task = str(arguments.get("task") or "").strip()
        if not task:
            return self._text("Error: 'task' is required")
        worker = str(arguments.get("worker") or "auto").strip() or "auto"
        skill = str(arguments.get("skill") or "").strip().lower()
        if self._allowed_workers is not None and worker != "auto" and (
            worker not in self._allowed_workers
        ):
            return self._text(
                f"Error: worker {worker!r} is not in this session's "
                f"allowed fleet workers ({self._allowed_workers})"
            )
        if skill and self._allowed_skills is not None and (
            skill not in self._allowed_skills
        ):
            return self._text(
                f"Error: skill {skill!r} is not in this session's "
                f"allowed fleet skills ({self._allowed_skills})"
            )
        if not self._lock.acquire(blocking=False):
            return self._text(
                "Error: another fleet delegation is already in flight — "
                "they run one at a time per session."
            )
        try:
            return self._run(task, worker, skill, arguments)
        finally:
            self._lock.release()

    def _run(self, task: str, worker: str, skill: str,
             arguments: dict) -> dict:
        from .client import attach_fleet, run_fleet_task

        attach = self._attach or attach_fleet
        try:
            client = attach(self._config)
        except KernelError as exc:
            return self._text(f"Error: fleet unavailable: {exc}")
        try:
            tools = arguments.get("tools")
            wall = int(arguments.get("wall_clock_seconds") or 0)
            dispatch = run_fleet_task(
                client, task=task, worker=worker, skill=skill,
                tools=list(tools) if isinstance(tools, list) else None,
                model=str(arguments.get("model") or ""),
                context=str(arguments.get("context") or ""),
                principal=self._principal,
                caller=self._caller,
                wall_clock_seconds=wall,
                token_budget=int(
                    self._caller.get("token_budget") or 0
                ),
            )
        except (KernelError, authority.AuthorityError) as exc:
            return self._text(f"Fleet delegation refused: {exc}")
        except Exception as exc:
            return self._text(
                f"Error: fleet delegation failed: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            try:
                client.close()
            except Exception:
                pass
        return self._text(self._render(dispatch))

    @staticmethod
    def _render(dispatch: Dict[str, Any]) -> str:
        state = dispatch.get("state")
        task_id = dispatch.get("task_id", "?")
        worker_id = dispatch.get("worker_id") or "(unassigned)"
        result = dispatch.get("result") or {}
        if state == DispatchState.SUCCEEDED:
            lines = [
                f"Fleet task {task_id} succeeded on {worker_id}.",
                "",
                str(result.get("summary") or "(no summary)"),
            ]
            artifacts = result.get("artifacts") or []
            if artifacts:
                lines.append("")
                lines.append("Artifacts (pull with /fleet artifacts "
                             f"{task_id}):")
                for ref in artifacts:
                    lines.append(
                        f"  - {ref.get('name')} sha256:{ref.get('digest')}"
                        f" ({ref.get('size')} bytes)"
                    )
            tokens = (result.get("input_tokens"),
                      result.get("output_tokens"))
            if any(tokens):
                lines.append(
                    f"\n(worker used {tokens[0] or 0:,} in /"
                    f" {tokens[1] or 0:,} out tokens)"
                )
            return "\n".join(lines)
        if state == DispatchState.FAILED:
            return (
                f"Fleet task {task_id} FAILED"
                f" ({dispatch.get('failure_class') or 'unknown'}):"
                f" {dispatch.get('error') or '(no error detail)'}"
            )
        if state == DispatchState.CANCELLED:
            return f"Fleet task {task_id} was cancelled."
        return (
            f"Fleet task {task_id} is still {state} (timed out waiting)."
            f" Check later with /fleet task {task_id}."
        )


__all__ = ["FLEET_DELEGATE_TOOL", "FleetDelegateClient"]
