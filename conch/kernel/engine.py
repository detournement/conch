"""Mission engine: bounded, checkpointed work sessions over AgentSession.

The engine turns kernel state into finite scheduled sessions — never one
immortal LLM loop. Each session:

1. honors STOP (global kill file + per-mission flag) at start,
2. atomically reserves budgets, takes the session lease, and transitions
   ready→active,
3. rehydrates a **bounded** fresh context from the mission spec, current
   plan, latest checkpoint, open tasks, recent event tail, and remaining
   budgets — never a growing transcript,
4. runs one AgentSession turn under hard wall/token/round caps, with a
   required-policy check (fail closed) re-checking STOP and the wall
   deadline before every tool round,
5. checkpoints only at the completed turn boundary: checkpoint + budget
   commit + state transition + next-wake timer + outbox notification in one
   kernel transaction.

The model may set its own next wake via the ``mission_control`` tool;
otherwise the mission's default cadence applies. A crash anywhere leaves
either a claimable timer or an expired session lease — reconciliation
resumes the mission without duplicating any committed effect.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .model import (
    BudgetExceededError,
    KernelError,
    MissionKind,
    MissionState,
    kernel_id,
    model_completion_allowed,
)
from .store import MissionStore, default_kernel_dir

#: Hard ceiling on rehydrated context size, independent of journal length.
MAX_CONTEXT_CHARS = 16000

_SECTION_CAPS = {
    "spec": 2500,
    "plan": 2500,
    "lessons": 800,
    "checkpoint": 4000,
    "tasks": 2000,
    "events": 3500,
    "budgets": 800,
}

MISSION_SYSTEM_PROMPT = (
    "You are Conch running one bounded, headless work session for a durable "
    "mission. The mission outlives this session: everything you need is in "
    "the mission context below, and everything you want to persist must go "
    "through tools before the session ends. Work efficiently toward the "
    "goal; do not re-litigate it. Use the mission_control tool to record "
    "plan updates, notes, and tasks; to schedule your next wake "
    "(set_next_wake) when more work remains; to park the mission for a "
    "human (request_input); or to finish it (complete_mission / "
    "fail_mission). If you do nothing, the mission wakes again on its "
    "default cadence. Never ask questions in plain text — the user is not "
    "watching this session."
)

MISSION_CONTROL_TOOL = {
    "type": "function",
    "function": {
        "name": "mission_control",
        "description": (
            "Control this mission's durable state: record plans/notes/"
            "tasks, set the next wake time, request human input, or finish "
            "the mission. Effects are transactional and survive restarts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "set_next_wake", "update_plan", "note", "add_task",
                        "complete_task", "request_input",
                        "complete_mission", "fail_mission", "status",
                    ],
                    "description": "The mission operation to perform.",
                },
                "seconds": {
                    "type": "integer",
                    "description": (
                        "set_next_wake: seconds from now until the next "
                        "work session (60..604800)."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "note/request_input/complete_mission/fail_mission: "
                        "the note, question, summary, or reason. add_task: "
                        "the task title."
                    ),
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "update_plan: the full ordered plan.",
                },
                "task_id": {
                    "type": "string",
                    "description": "complete_task: the task to mark done.",
                },
            },
            "required": ["op"],
        },
    },
}


def _clip(text: str, cap: int) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    head = int(cap * 0.7)
    tail = cap - head - 24
    return text[:head] + "\n... [clipped] ...\n" + text[-max(tail, 0):]


def global_stop_file(kernel_dir: Optional[Path] = None) -> Path:
    return (kernel_dir or default_kernel_dir()) / "STOP"


def _read_stop_state(db_path: str, mission_id: str):
    """Standalone STOP probe, safe to call from policy-check threads: a
    fresh short-lived read connection, never the store's thread-local one."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT stop_requested, status FROM missions WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return True, "mission row missing"
    if bool(row[0]):
        return True, "mission STOP flag set"
    if row[1] not in (MissionState.ACTIVE,):
        return True, f"mission no longer active (status {row[1]})"
    return False, ""


class MissionControlClient:
    """The ``mission_control`` builtin for mission sessions.

    Durable ops (plan/note/tasks) hit the kernel immediately — they are
    transactional facts. Session-outcome ops (next wake, completion,
    input requests) are *staged* and folded into the single checkpoint
    transaction at the completed turn boundary.
    """

    def __init__(self, store: MissionStore, mission_id: str,
                 session_id: str):
        self._store = store
        self._mission_id = mission_id
        self._session_id = session_id
        #: Bounded Capitol tool client, set by the engine only when the
        #: mission spec's ``capitol`` envelope grants authority.
        self.capitol = None
        self.staged: Dict[str, Any] = {
            "next_wake_seconds": None,
            "outcome": None,
            "summary": "",
            "question": "",
        }

    @staticmethod
    def _text(message: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": message}]}

    def call_tool(self, name: str, arguments: dict) -> Dict[str, Any]:
        try:
            return self._dispatch(arguments or {})
        except KernelError as exc:
            return self._text(f"mission_control error: {exc}")

    def _dispatch(self, arguments: dict) -> Dict[str, Any]:
        op = str(arguments.get("op") or "").strip()
        store, mission_id = self._store, self._mission_id
        if op == "set_next_wake":
            try:
                seconds = int(arguments.get("seconds", 0))
            except (TypeError, ValueError):
                return self._text("set_next_wake needs integer seconds")
            seconds = max(60, min(seconds, 7 * 86400))
            self.staged["next_wake_seconds"] = seconds
            return self._text(
                f"Next wake staged for {seconds}s after this session ends."
            )
        if op == "update_plan":
            steps = arguments.get("steps") or []
            if not isinstance(steps, list) or not all(
                isinstance(step, str) for step in steps
            ):
                return self._text("update_plan needs a list of step strings")
            plan_id = store.record_plan(mission_id, {"steps": steps})
            return self._text(f"Plan recorded ({plan_id}).")
        if op == "note":
            text = str(arguments.get("text") or "").strip()
            if not text:
                return self._text("note needs text")
            store.record_note(mission_id, text, author=self._session_id)
            return self._text("Note recorded.")
        if op == "add_task":
            title = str(arguments.get("text") or "").strip()
            if not title:
                return self._text("add_task needs text (the task title)")
            task_id = store.create_task(mission_id, title)
            return self._text(f"Task created: {task_id}")
        if op == "complete_task":
            task_id = str(arguments.get("task_id") or "").strip()
            if not task_id:
                return self._text("complete_task needs task_id")
            store.transition_task(task_id, "done")
            return self._text(f"Task {task_id} marked done.")
        if op == "request_input":
            question = str(arguments.get("text") or "").strip()
            if not question:
                return self._text("request_input needs text (the question)")
            self.staged["outcome"] = MissionState.WAITING_INPUT
            self.staged["question"] = question
            return self._text(
                "Input request staged: the mission will park for the user "
                "after this session. Finish your remaining local work."
            )
        if op == "complete_mission":
            mission = store.get_mission(mission_id)
            if mission is not None and not model_completion_allowed(
                mission["spec"]
            ):
                store.record_completion_denied(
                    mission_id,
                    "complete_mission refused: spec disallows model "
                    "completion (cadence mission)",
                    author=self._session_id,
                )
                return self._text(
                    "complete_mission denied: this cadence mission has no "
                    "terminal success criteria and its spec sets "
                    "allow_model_completion=false — only an operator "
                    "finishes it. The mission stays on its schedule; use "
                    "set_next_wake to adjust cadence or request_input to "
                    "park it for the user."
                )
            self.staged["outcome"] = MissionState.SUCCEEDED
            self.staged["summary"] = str(arguments.get("text") or "")
            return self._text("Mission completion staged.")
        if op == "fail_mission":
            self.staged["outcome"] = MissionState.FAILED
            self.staged["summary"] = str(arguments.get("text") or "")
            return self._text("Mission failure staged.")
        if op == "status":
            mission = store.get_mission(mission_id)
            scope = mission["root_scope_id"]
            return self._text(
                f"status={mission['status']} runs={mission['runs']} "
                f"budgets={store.budget_status(scope)}"
            )
        return self._text(f"Unknown mission_control op {op!r}")


def _default_session_factory(config: dict) -> Callable:
    """Real AgentSession runner (bootstrap wiring, fresh session per run)."""

    def run(mission: Dict[str, Any], messages: List[dict],
            control: MissionControlClient, caps: Dict[str, int]):
        from ..bootstrap import build_agent_session
        from ..session import SessionBudgets
        from ..tooling import default_permissions

        session = build_agent_session(
            config, interactive=False, permissions=default_permissions(),
        )
        try:
            session.budgets = SessionBudgets(
                max_tool_rounds=caps["max_tool_rounds"],
                turn_token_budget=caps["token_budget"],
            )
            session.builtin_clients["mission_control"] = control
            tools = list(getattr(session.chat_state, "tools", None) or [])
            tools.append(MISSION_CONTROL_TOOL)
            # Mission authority is spec-derived: the session builtin
            # capitol_control (interactive runtime surface) is dropped
            # so only the envelope-scoped tool exists here — and only
            # when the spec grants a capitol envelope at all.
            tools = [
                tool for tool in tools
                if tool.get("function", {}).get("name") != "capitol_control"
            ]
            session.builtin_clients.pop("capitol_control", None)
            if getattr(control, "capitol", None) is not None:
                from ..capitol.supervisor import CAPITOL_CONTROL_TOOL

                session.builtin_clients["capitol_control"] = control.capitol
                tools.append(CAPITOL_CONTROL_TOOL)
            return session.run_turn(
                messages, tools=tools,
                max_tool_rounds=caps["max_tool_rounds"],
            )
        finally:
            session.close()
    return run


class MissionEngine:
    """Runs bounded work sessions for ready missions against one kernel."""

    def __init__(self, store: MissionStore, config: dict,
                 holder: str = "", session_factory: Optional[Callable] = None,
                 kernel_dir: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None,
                 review_runner: Optional[Callable] = None,
                 consolidator: Optional[Callable] = None):
        self.store = store
        self.config = config or {}
        self.holder = holder or f"engine-{os.getpid()}"
        self._session_factory = session_factory
        #: Review-session model runner (tests inject a scripted one); the
        #: default makes one weak-model call with no tools.
        self._review_runner = review_runner
        #: Post-checkpoint consolidation model runner (tests inject).
        self._consolidator = consolidator
        self.kernel_dir = Path(kernel_dir) if kernel_dir else (
            Path(store.path).parent
        )
        self._log = log or (lambda line: None)

    # -- creation / activation ------------------------------------------------

    def create_mission(self, spec: Dict[str, Any],
                       activate: bool = True) -> str:
        """Create a mission; when activating, park it on its wake timer so
        the first session runs one cadence from now (scheduled prompts) or
        immediately (standard missions run now, then follow cadence)."""
        mission_id = self.store.create_mission(spec)
        if not activate:
            return mission_id
        mission = self.store.get_mission(mission_id)
        spec = mission["spec"]
        cadence = int(spec.get("cadence_seconds") or 0)
        now = float(self.store.clock())
        self.store.transition_mission(
            mission_id, MissionState.READY, reason="activated"
        )
        if cadence > 0:
            self.store.create_timer(
                mission_id, "wake", now + cadence,
                interval_seconds=cadence,
                misfire_policy=spec.get("misfire_policy", "coalesce"),
                catch_up_limit=int(spec.get("catch_up_limit", 5)),
            )
        if spec.get("kind") == MissionKind.SCHEDULED_PROMPT:
            # scheduled prompts wait out their first interval, exactly like
            # the legacy scheduler did
            self.store.transition_mission(
                mission_id, MissionState.WAITING_TIMER,
                reason="parked until first cadence wake",
            )
        return mission_id

    # -- STOP ------------------------------------------------------------------

    def stop_file(self) -> Path:
        return global_stop_file(self.kernel_dir)

    def global_stop(self) -> bool:
        return self.stop_file().exists()

    # -- lifecycle controls (shared by daemon ops and shell attach) -----------

    def pause_mission(self, mission_id: str) -> None:
        self.store.transition_mission(
            mission_id, MissionState.PAUSED, reason="operator pause"
        )

    def resume_mission(self, mission_id: str) -> None:
        mission = self.store.get_mission(mission_id)
        if mission is None:
            raise KernelError(f"unknown mission {mission_id!r}")
        if mission["stop_requested"]:
            self.store.set_stop(mission_id, False)
        if mission["status"] in (MissionState.READY, MissionState.ACTIVE):
            return  # already running or runnable
        self.store.transition_mission(
            mission_id, MissionState.READY, reason="operator resume"
        )

    def abort_mission(self, mission_id: str) -> None:
        mission = self.store.get_mission(mission_id)
        if mission is None:
            raise KernelError(f"unknown mission {mission_id!r}")
        self.store.set_stop(mission_id, True)
        if mission["status"] not in MissionState.TERMINAL:
            self.store.transition_mission(
                mission_id, MissionState.CANCELLED, reason="operator abort"
            )
        for timer in self.store.list_timers(mission_id, status="active"):
            self.store.cancel_timer(timer["timer_id"], reason="abort")

    def decide_approval(self, approval_id: str, verb: str, *, nonce: str,
                        origin_channel: str = "local",
                        origin_thread: str = "", origin_sender: str = "",
                        decided_by: str = "") -> Dict[str, Any]:
        """Decide an approval and wake the mission if it was parked on it."""
        result = self.store.decide_approval(
            approval_id, verb, nonce=nonce, origin_channel=origin_channel,
            origin_thread=origin_thread, origin_sender=origin_sender,
            decided_by=decided_by,
        )
        mission = self.store.get_mission(result["mission_id"])
        if mission and mission["status"] == MissionState.WAITING_APPROVAL:
            self.store.transition_mission(
                result["mission_id"], MissionState.READY,
                reason=f"approval {approval_id} {result['status']}",
            )
        return result

    def provide_input(self, mission_id: str, text: str,
                      source: str = "local") -> Dict[str, Any]:
        """Answer a mission with user input and wake it immediately."""
        return self.deliver_event(
            source, f"input:{mission_id}:{kernel_id('obx')}",
            {"text": str(text)}, mission_id,
        )

    def deliver_event(self, source: str, idempotency_key: str,
                      payload: Dict[str, Any], mission_id: str = "",
                      wake: bool = True) -> Dict[str, Any]:
        """General kernel-inbox event API (always-on daemon work item).

        Records one external event — an inbound channel message, a webhook,
        a filesystem watch — durably and idempotently on *idempotency_key*.
        When the event is addressed to a mission and ``wake`` is true, a
        mission parked on its timer or waiting for input transitions to
        ready in the same call, so the next scheduler slot runs its session
        instead of waiting out the timer. Paused missions, missions parked
        on an approval, and terminal missions are never woken: operator
        pauses stick, and an unsolicited event cannot bypass an approval
        gate.
        """
        if not str(idempotency_key).strip():
            raise KernelError("deliver_event needs an idempotency key")
        if mission_id and self.store.get_mission(mission_id) is None:
            raise KernelError(f"unknown mission {mission_id!r}")
        result = self.store.receive_inbox(
            str(source or "external"), str(idempotency_key),
            dict(payload or {}), mission_id,
        )
        woken = False
        if wake and mission_id and not result["duplicate"]:
            woken = self.wake_mission(
                mission_id, reason=f"inbox event from {source}"
            )
        return {
            "inbox_id": result["inbox_id"],
            "duplicate": result["duplicate"],
            "woken": woken,
        }

    def wake_mission(self, mission_id: str, reason: str = "event") -> bool:
        """Wake a parked mission so its next session runs at the next
        scheduler slot. Only timer- and input-parked missions are eligible;
        returns False (never raises) when the mission is elsewhere in its
        lifecycle or a concurrent transition wins the race."""
        mission = self.store.get_mission(mission_id)
        if mission is None or mission["status"] not in (
            MissionState.WAITING_INPUT, MissionState.WAITING_TIMER,
        ):
            return False
        try:
            self.store.transition_mission(
                mission_id, MissionState.READY, reason=reason
            )
        except KernelError:
            return False  # racing transition; the event journal explains
        return True

    # -- bounded rehydration ---------------------------------------------------

    def build_context(self, mission: Dict[str, Any]) -> str:
        """Fresh, bounded session context — size-capped per section and
        overall, no matter how large the journal has grown."""
        mission_id = mission["mission_id"]
        spec = mission["spec"]
        parts: List[str] = []
        spec_lines = [
            f"Mission {mission_id} (run #{mission['runs'] + 1})",
            f"Goal: {spec['goal']}",
        ]
        if spec.get("success_criteria"):
            spec_lines.append(
                "Success criteria: " + "; ".join(spec["success_criteria"])
            )
        if spec.get("constraints"):
            spec_lines.append(
                "Constraints: " + "; ".join(spec["constraints"])
            )
        spec_lines.append(
            "Dry run: %s" % ("yes — take no external actions with real-world"
                             " effects" if spec.get("dry_run", True) else "no")
        )
        parts.append(_clip("\n".join(spec_lines), _SECTION_CAPS["spec"]))

        plan = self.store.latest_plan(mission_id)
        if plan:
            steps = plan["content"].get("steps", [])
            plan_text = "Current plan (v%s):\n%s" % (
                plan["version"],
                "\n".join(f"  {i + 1}. {step}"
                          for i, step in enumerate(steps)),
            )
            parts.append(_clip(plan_text, _SECTION_CAPS["plan"]))

        tasks = self.store.open_tasks(mission_id)
        try:
            from . import consolidate as _consolidate

            lessons = _consolidate.lessons_block(
                mission,
                plan["content"].get("steps", []) if plan else [],
                [task["title"] for task in tasks],
                self.config,
            )
        except Exception:
            lessons = ""  # the memory tier can never break rehydration
        if lessons:
            parts.append(_clip(lessons, _SECTION_CAPS["lessons"]))

        checkpoint = self.store.latest_checkpoint(mission_id)
        if checkpoint:
            parts.append(_clip(
                f"Latest checkpoint ({checkpoint['checkpoint_id']}):\n"
                f"{checkpoint['summary']}",
                _SECTION_CAPS["checkpoint"],
            ))

        if tasks:
            task_text = "Open tasks:\n" + "\n".join(
                f"  - [{task['state']}] {task['task_id']}: {task['title']}"
                for task in tasks[:20]
            )
            parts.append(_clip(task_text, _SECTION_CAPS["tasks"]))

        events = self.store.event_tail(mission_id, limit=15)
        event_lines = []
        for event in events:
            data = event["data"]
            brief = {
                key: data[key] for key in ("to", "reason", "summary", "text",
                                           "status", "error")
                if data.get(key)
            }
            event_lines.append(f"  - {event['kind']} {brief}")
        if event_lines:
            parts.append(_clip(
                "Recent events:\n" + "\n".join(event_lines),
                _SECTION_CAPS["events"],
            ))

        budgets = self.store.budget_status(mission["root_scope_id"])
        if budgets:
            budget_lines = [
                f"  - {line}: {values['available']} of {values['cap']} left"
                for line, values in budgets.items()
            ]
            parts.append(_clip(
                "Remaining budgets:\n" + "\n".join(budget_lines),
                _SECTION_CAPS["budgets"],
            ))
        inputs = [
            row for row in self._pending_inputs(mission_id)
        ]
        if inputs:
            parts.append(_clip(
                "New input and events:\n"
                + "\n".join(f"  - {t}" for t in inputs),
                _SECTION_CAPS["events"],
            ))
        context = "\n\n".join(part for part in parts if part)
        return _clip(context, MAX_CONTEXT_CHARS)

    def _pending_inputs(self, mission_id: str) -> List[str]:
        rows = self.store._read_conn().execute(
            "SELECT idempotency_key, payload FROM inbox WHERE mission_id=?"
            " AND status='pending' ORDER BY inbox_id LIMIT 10",
            (mission_id,),
        ).fetchall()
        texts = []
        for row in rows:
            import json
            try:
                payload = json.loads(row[1])
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            # Events without a text field (webhooks, watches) still reach
            # the session as their compact payload — an event the mission
            # can never see is an event that never happened.
            text = str(payload.get("text") or "")
            if not text:
                text = json.dumps(payload, sort_keys=True)[:200]
            texts.append(text)
        return texts

    def _consume_inputs(self, mission_id: str) -> None:
        rows = self.store._read_conn().execute(
            "SELECT idempotency_key FROM inbox WHERE mission_id=? AND"
            " status='pending' ORDER BY inbox_id LIMIT 10",
            (mission_id,),
        ).fetchall()
        for (key,) in rows:
            try:
                self.store.mark_inbox_processed(key, ok=True)
            except KernelError:
                pass

    # -- session execution -----------------------------------------------------

    def run_ready_sessions(self, limit: int = 1) -> List[Dict[str, Any]]:
        if self.global_stop():
            return []
        results = []
        for mission in self.store.list_missions(status=MissionState.READY):
            if len(results) >= limit:
                break
            results.append(self.run_session(mission["mission_id"]))
        return results

    def run_session(self, mission_id: str) -> Dict[str, Any]:
        mission = self.store.get_mission(mission_id)
        if mission is None:
            raise KernelError(f"unknown mission {mission_id!r}")
        if self.global_stop():
            return {"mission_id": mission_id, "outcome": "skipped",
                    "error": "global STOP file present"}
        if mission["stop_requested"]:
            self.store.transition_mission(
                mission_id, MissionState.PAUSED,
                reason="STOP flag set — parked",
            )
            return {"mission_id": mission_id, "outcome": "paused",
                    "error": "mission STOP flag set"}
        if mission["status"] != MissionState.READY:
            return {"mission_id": mission_id, "outcome": "skipped",
                    "error": f"not ready (status {mission['status']})"}
        # Mission judgment: when a review is owed, it takes this session
        # slot (the mission stays ready, so the work session runs on the
        # next tick with any re-plan already in place). Reviews sit behind
        # the same STOP/pause/approval gates as work sessions — an
        # ineligible mission never reaches this branch.
        from . import review as _review

        policy = _review.resolve_policy(mission["spec"], self.config)
        if _review.review_due(
            self.store, mission, policy, float(self.store.clock())
        ):
            return self.run_review_session(mission, policy)
        spec = mission["spec"]
        session_id = kernel_id("ses")
        scope = mission["root_scope_id"]
        lines = self.store.budget_status(scope)
        reserves: Dict[str, int] = {}
        if "sessions" in lines:
            reserves["sessions"] = 1
        if "tokens" in lines:
            reserves["tokens"] = min(
                int(spec["session_token_budget"]),
                max(lines["tokens"]["available"], 0),
            )
        wall_seconds = int(spec["session_wall_seconds"])
        try:
            self.store.start_session(
                mission_id, session_id, self.holder, reserves,
                lease_seconds=wall_seconds + 300,
            )
        except BudgetExceededError as exc:
            # Hard budget enforcement: a mission that cannot afford one more
            # session fails terminally rather than silently spinning.
            self.store.transition_mission(
                mission_id, MissionState.FAILED,
                reason="budget exhausted", error=str(exc),
            )
            return {"mission_id": mission_id, "outcome": "failed",
                    "error": f"budget exhausted: {exc}"}
        control = MissionControlClient(self.store, mission_id, session_id)
        capitol_spec = spec.get("capitol") or {}
        if capitol_spec.get("allow_start") or capitol_spec.get(
            "allow_respond"
        ):
            # Import stays lazy: missions without Capitol authority never
            # load the adapter (shell-first invariant).
            from ..capitol.supervisor import CapitolControlClient

            control.capitol = CapitolControlClient(
                self.store, mission, session_id, self.config
            )
        caps = {
            "max_tool_rounds": int(spec["session_max_tool_rounds"]),
            "token_budget": int(spec["session_token_budget"]),
        }
        messages = self._build_messages(mission)
        deadline = float(self.store.clock()) + wall_seconds
        check_name = f"mission-stop:{session_id}"
        db_path = str(self.store.path)
        clock = self.store.clock

        def stop_check(event: str, payload: dict):
            from ..policy import PolicyDecision
            if float(clock()) > deadline:
                return PolicyDecision.deny(
                    "mission session wall budget exhausted", check=check_name
                )
            if global_stop_file(self.kernel_dir).exists():
                return PolicyDecision.deny(
                    "global STOP file present", check=check_name
                )
            stopped, reason = _read_stop_state(db_path, mission_id)
            if stopped:
                return PolicyDecision.deny(
                    f"mission STOP: {reason}", check=check_name
                )
            return PolicyDecision.allow()

        from ..policy import register_required_policy, unregister_required_policy
        register_required_policy(check_name, stop_check)
        error = ""
        reply, usage = "", {}
        try:
            factory = self._session_factory or _default_session_factory(
                self.config
            )
            reply, usage = factory(mission, messages, control, caps)
            if isinstance(usage, dict) and usage.get("error"):
                error = str(usage["error"])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            unregister_required_policy(check_name)
        return self._finish_session(
            mission, session_id, control, reply, usage, error
        )

    def run_review_session(self, mission: Dict[str, Any],
                           policy: Dict[str, Any]) -> Dict[str, Any]:
        """One bounded critic session (mission judgment work item).

        Deterministic stall detection runs BEFORE the model sees anything;
        the verdict lands as kernel events (``review_recorded``, plus
        ``plan_recorded``/``plan_revised`` on a re-plan, plus an outbox
        notification on escalation) in one transaction. Budget exhaustion
        and unusable model output degrade to a journaled skip — a review
        can never crash or fail its mission.
        """
        from . import review as _review

        mission_id = mission["mission_id"]
        spec = mission["spec"]
        scope = mission["root_scope_id"]
        signals = _review.stall_signals(self.store, mission_id, policy)
        review_id = kernel_id("rev")

        lines = self.store.budget_status(scope)
        reserves: Dict[str, int] = {}
        if "reviews" in lines:
            if lines["reviews"]["available"] < 1:
                self.store.record_review_skip(
                    mission_id, "review budget exhausted", review_id
                )
                return {"mission_id": mission_id, "session_id": review_id,
                        "outcome": "review_skipped",
                        "error": "review budget exhausted"}
            reserves["reviews"] = 1
        if "tokens" in lines:
            available = max(lines["tokens"]["available"], 0)
            if available <= 0:
                self.store.record_review_skip(
                    mission_id, "token budget exhausted", review_id
                )
                return {"mission_id": mission_id, "session_id": review_id,
                        "outcome": "review_skipped",
                        "error": "token budget exhausted"}
            reserves["tokens"] = min(int(policy["token_budget"]), available)
        wall_seconds = int(policy["wall_seconds"])
        try:
            self.store.start_session(
                mission_id, review_id, self.holder, reserves,
                lease_seconds=wall_seconds + 300,
            )
        except (BudgetExceededError, KernelError) as exc:
            # Lost a race (budget or lease) — skip, never crash or fail.
            try:
                self.store.record_review_skip(
                    mission_id, f"could not start review: {exc}", review_id
                )
            except KernelError:
                pass
            return {"mission_id": mission_id, "session_id": review_id,
                    "outcome": "review_skipped", "error": str(exc)}

        context = _review.build_review_context(self.store, mission, signals)
        runner = self._review_runner or _review.default_runner(self.config)
        reply, usage, error = "", {}, ""
        try:
            reply, usage, error = runner(mission, context, signals)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        verdict = _review.parse_verdict(reply) if not error else None
        if verdict is None and not error:
            error = "review reply carried no valid JSON verdict"
        outcome = _review.resolve_outcome(verdict, signals, error)

        actuals: Dict[str, int] = {}
        if "reviews" in reserves:
            actuals["reviews"] = 1
        if "tokens" in reserves:
            actuals["tokens"] = min(
                self._tokens_used(usage), reserves["tokens"]
            )
        if outcome is None:
            # Journaled skip: abandon the review session (reservation
            # released, mission back to ready) and journal why. The next
            # cadence period retries; the mission is never failed.
            try:
                self.store.release_lease(
                    "mission_session", mission_id, self.holder
                )
                self.store.abandon_session(
                    mission_id, review_id,
                    reason="review skipped: model unusable",
                )
                self.store.record_review_skip(
                    mission_id,
                    f"review model unusable: {_clip(error, 200)}",
                    review_id,
                )
            except KernelError as exc:
                self._log(f"review {review_id} skip refused: {exc}")
                return {"mission_id": mission_id, "session_id": review_id,
                        "outcome": "abandoned", "error": str(exc)}
            self._log(
                f"review {review_id} for {mission_id}: skipped"
                f" ({_clip(error, 120)})"
            )
            return {"mission_id": mission_id, "session_id": review_id,
                    "outcome": "review_skipped", "error": error}

        action = outcome["action"]
        plan_content = None
        plan_rationale = ""
        if action == "re-plan":
            plan_content = {"steps": outcome["plan"]}
            plan_rationale = outcome.get("rationale", "")
        notify_payload = None
        if action == "escalate":
            goal = _clip(spec.get("goal", ""), 120)
            verdict_lines = "\n".join(
                f"- [{row['verdict']}] {row['criterion']}: {row['evidence']}"
                for row in outcome.get("criteria", [])
            )
            notify_payload = {
                "text": (
                    f"[conch mission {mission_id}] {goal} — review"
                    " escalation\n\n"
                    f"{outcome.get('rationale', '')}\n\n{verdict_lines}"
                ).strip(),
                "channel": str(spec.get("channel") or ""),
            }
        content = {
            "criteria": outcome.get("criteria", []),
            "rationale": outcome.get("rationale", ""),
            "stall": signals,
            "model": str((usage or {}).get("model", "")),
        }
        try:
            result = self.store.record_review(
                mission_id, review_id, self.holder,
                action=action, content=content,
                plan_content=plan_content, plan_rationale=plan_rationale,
                notify_payload=notify_payload,
                notify_dedupe_key=f"review:{review_id}",
                actuals=actuals,
            )
        except KernelError as exc:
            self._log(f"review {review_id} verdict refused: {exc}")
            return {"mission_id": mission_id, "session_id": review_id,
                    "outcome": "abandoned", "error": str(exc)}
        self._log(
            f"review {review_id} for {mission_id}: {action}"
            + (f" (plan v{result['plan_version']})"
               if result.get("plan_version") else "")
        )
        return {
            "mission_id": mission_id, "session_id": review_id,
            "outcome": f"review_{action.replace('-', '')}",
            "error": "", "tokens": self._tokens_used(usage),
        }

    def _build_messages(self, mission: Dict[str, Any]) -> List[dict]:
        spec = mission["spec"]
        if spec.get("kind") == MissionKind.SCHEDULED_PROMPT:
            from ..prompts import get_chat_prompt
            provider = (self.config.get("provider") or "").lower()
            model = self.config.get(
                "chat_model", self.config.get("model", "")
            )
            return [
                {"role": "system",
                 "content": get_chat_prompt(provider, model, self.config)},
                {"role": "user", "content": spec["prompt"]},
            ]
        context = self.build_context(mission)
        return [
            {"role": "system", "content": MISSION_SYSTEM_PROMPT},
            {"role": "user", "content": (
                context
                + "\n\nRun this work session now. End with mission_control "
                "(set_next_wake / request_input / complete_mission / "
                "fail_mission) if the default cadence is wrong."
            )},
        ]

    @staticmethod
    def _tokens_used(usage: Dict[str, Any]) -> int:
        if not isinstance(usage, dict):
            return 0
        for key in ("total_tokens", "tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        # chat_turn reports input_tokens/output_tokens; some providers use
        # prompt/completion naming.
        total = 0
        for key in ("input_tokens", "output_tokens", "prompt_tokens",
                    "completion_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value > 0:
                total += value
        return total

    def _finish_session(self, mission: Dict[str, Any], session_id: str,
                        control: MissionControlClient, reply: str,
                        usage: Dict[str, Any], error: str) -> Dict[str, Any]:
        mission_id = mission["mission_id"]
        spec = mission["spec"]
        now = float(self.store.clock())
        cadence = int(spec.get("cadence_seconds") or 0)
        staged = control.staged
        outcome = staged["outcome"] or MissionState.WAITING_TIMER
        if error and staged["outcome"] is None:
            outcome = MissionState.WAITING_TIMER
        if spec.get("kind") == MissionKind.SCHEDULED_PROMPT:
            if spec.get("run_once") and not error:
                outcome = MissionState.SUCCEEDED
        next_wake: Optional[float] = None
        if outcome == MissionState.WAITING_TIMER:
            if staged["next_wake_seconds"] is not None:
                next_wake = now + float(staged["next_wake_seconds"])
            elif error:
                next_wake = now + max(300.0, min(float(cadence or 300), 3600.0))
            elif cadence > 0:
                next_wake = now + cadence
            else:
                next_wake = now + 86400.0
        summary = staged["summary"] or _clip(reply or "(no output)", 4000)
        if staged["question"]:
            summary += f"\n\nWaiting on user input: {staged['question']}"
        if error:
            summary = f"[session error: {_clip(error, 500)}]\n\n" + summary
        scope = mission["root_scope_id"]
        lines = self.store.budget_status(scope)
        actuals: Dict[str, int] = {}
        if "sessions" in lines:
            actuals["sessions"] = 1
        if "tokens" in lines:
            reserved = 0
            reservation = [
                row for row in self.store._read_conn().execute(
                    "SELECT line, amount FROM budget_reservations WHERE"
                    " reservation_id=? AND scope_id=? AND status='active'",
                    (session_id, scope),
                ).fetchall()
            ]
            for line, amount in reservation:
                if line == "tokens":
                    reserved = int(amount)
            actuals["tokens"] = min(self._tokens_used(usage), reserved)
        notify_payload = self._notification(
            mission, outcome, summary, error
        )
        try:
            result = self.store.checkpoint_session(
                mission_id, session_id, self.holder, summary, outcome,
                state={
                    "reply_clip": _clip(reply or "", 2000),
                    "error": _clip(error, 500),
                    "tokens_used": self._tokens_used(usage),
                },
                actuals=actuals,
                next_wake_at=next_wake,
                notify_payload=notify_payload,
                notify_dedupe_key=f"session:{session_id}",
                error=_clip(error, 500),
            )
        except KernelError as exc:
            # Lease lost (wall overrun past the grace, STOP mid-flight, or a
            # competing daemon superseded us): the work is abandoned, and
            # reconciliation owns the repair. No effects escape: everything
            # the session recorded went through its own transactions.
            self._log(
                f"session {session_id} checkpoint refused: {exc}"
            )
            return {"mission_id": mission_id, "session_id": session_id,
                    "outcome": "abandoned", "error": str(exc)}
        self._consume_inputs(mission_id)
        if staged["question"]:
            # journal the question so shell attach can show it
            self.store.record_note(
                mission_id, f"input requested: {staged['question']}",
                author=session_id,
            )
        # Cross-mission memory consolidation: a post-checkpoint side task
        # (the checkpoint above already committed). Bounded by its own
        # timeout; any failure is a logged skip — it can never block or
        # break the session path.
        try:
            from . import consolidate as _consolidate

            _consolidate.after_checkpoint(
                self.store, mission, session_id, self.config,
                log=self._log, runner=self._consolidator,
            )
        except Exception as exc:
            self._log(
                f"consolidation for {mission_id} session {session_id}"
                f" skipped: {type(exc).__name__}: {exc}"
            )
        return {
            "mission_id": mission_id, "session_id": session_id,
            "outcome": result["outcome"], "error": error,
            "tokens": self._tokens_used(usage),
        }

    def _notification(self, mission: Dict[str, Any], outcome: str,
                      summary: str, error: str) -> Optional[Dict[str, Any]]:
        spec = mission["spec"]
        goal = _clip(spec.get("goal", ""), 120)
        mission_id = mission["mission_id"]
        channel = str(spec.get("channel") or "")
        if spec.get("kind") == MissionKind.SCHEDULED_PROMPT:
            body = _clip(summary, 2500)
            text = f"[conch mission {mission_id}] {goal}\n\n{body}"
            return {"text": text, "channel": channel}
        if outcome in (MissionState.SUCCEEDED, MissionState.FAILED,
                       MissionState.WAITING_INPUT,
                       MissionState.WAITING_APPROVAL):
            label = {
                MissionState.SUCCEEDED: "succeeded",
                MissionState.FAILED: "FAILED",
                MissionState.WAITING_INPUT: "needs your input",
                MissionState.WAITING_APPROVAL: "awaiting approval",
            }[outcome]
            text = (
                f"[conch mission {mission_id}] {goal} — {label}\n\n"
                f"{_clip(summary, 2000)}"
            )
            return {"text": text, "channel": channel}
        if outcome == MissionState.WAITING_TIMER and not error and (
            spec.get("notify") == "sessions"
        ):
            # digest-style missions: every checkpoint is the deliverable
            text = (
                f"[conch mission {mission_id}] {goal} — session digest\n\n"
                f"{_clip(summary, 2500)}"
            )
            return {"text": text, "channel": channel}
        if error:
            return {
                "text": (
                    f"[conch mission {mission_id}] {goal} — session error\n\n"
                    f"{_clip(error, 800)}"
                ),
                "channel": channel,
            }
        return None
