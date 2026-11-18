"""Fan-out: independent subtasks run in parallel worker processes.

A turn is serialized process-wide (one terminal, one local model server),
so parallel work cannot share this process. Each fan-out task instead runs
one bounded ``chat_turn`` in its own process and session group, with its own
model, and hands back only a summary.

Which model a worker uses is the ``fanout_model`` setting:

* unset or ``default`` — the session's model;
* ``provider/model`` or a model name on the session's provider — that model;
* ``auto`` — chosen per task from the models this machine can actually use
  (see :func:`available_models`), matched to the task's effort.

A worker's authority never exceeds its parent's. It cannot prompt, so a
command that would need approval is refused and reported back. A session in
``prompt_all`` mode asks once before a fan-out starts, because its workers
run read-only commands without asking.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import subagent_activity
from .ebrake import AgentsCancelled, PauseController, checkpoint

EFFORTS = ("light", "standard", "heavy")
MAX_TASKS = 8
DEFAULT_CONCURRENCY = 4
DEFAULT_ROUNDS = 10
DEFAULT_TIMEOUT = 600

# Tools a worker never receives, beyond what a delegated subagent is denied:
# no recursion, and nothing that writes the user's durable state from several
# processes at once.
WORKER_EXTRA_EXCLUDED_TOOLS = frozenset({
    "fan_out", "save_memory", "skill_manage", "conch_introspect",
})

WORKER_PROMPT = (
    "You are a Conch fan-out worker handling one of several independent "
    "subtasks that run in parallel, each with a fresh, clean context. Work "
    "autonomously with the available tools. You cannot ask the user "
    "anything: a command that needs approval is refused, so report what you "
    "could not do instead of retrying it. When finished, reply with a "
    "concise summary of what you did and found (results, key facts, file "
    "paths, commands) — the parent agent sees ONLY your final reply."
)

FAN_OUT_TOOL = {
    "type": "function",
    "function": {
        "name": "fan_out",
        "description": (
            "Run 2 to 8 independent subtasks in parallel, each in a fresh worker "
            "with clean context, and get one summary back per task. Use it when "
            "work splits cleanly: searching several areas, reading several files, "
            "comparing options. Give every task all the context it needs; workers "
            "share nothing with you or each other. Workers cannot ask the user: "
            "they run read-only shell commands and refuse the rest unless this "
            "session auto-executes. Set effort so a suitable model is chosen: "
            "light for lookups, extraction and summaries; standard for typical "
            "multi-step work; heavy for hard debugging or design."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "The independent subtasks to run in parallel",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Complete, self-contained task description",
                            },
                            "context": {
                                "type": "string",
                                "description": "Optional extra context this worker needs",
                            },
                            "effort": {
                                "type": "string",
                                "enum": list(EFFORTS),
                                "description": "How demanding the task is",
                            },
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
}


# ---------------------------------------------------------------------------
# Models: what is available, and which one a task gets
# ---------------------------------------------------------------------------

# Auto never moves work to a costlier kind of billing than the session uses:
# a local session stays local, a subscription seat never reaches API billing.
_BILLING_RANK = {"local": 0, "seat": 1, "api": 2, "unknown": 2}

_SEAT_TIERS = {
    ("claude-code", "haiku"): "light",
    ("claude-code", "sonnet"): "standard",
    ("claude-code", "opus"): "heavy",
}
_SMALL_NAME = re.compile(r"(?:^|[-_/:.])(nano|mini|flash|haiku|lite|small|tiny)(?:$|[-_/:.])")
_PARAMETER_COUNT = re.compile(r"(\d+(?:\.\d+)?)b(?:$|[-_/:.])")
# When a tier has no model, the nearest stronger one is tried before a weaker.
_TIER_FALLBACK = {
    "light": ("light", "standard", "heavy"),
    "standard": ("standard", "heavy", "light"),
    "heavy": ("heavy", "standard", "light"),
}
_HEAVY_HINTS = (
    "debug", "root cause", "diagnos", "design", "architect", "refactor",
    "prove", "optimi", "security", "race condition", "deadlock", "migrat",
    "trade-off", "tradeoff",
)
_LIGHT_HINTS = (
    "list ", "find ", "grep", "count ", "summar", "extract", "look up",
    "lookup", "locate", "which file", "read ", "check whether", "check if",
)


@dataclass
class ModelChoice:
    provider: str
    model: str
    tier: str
    billing: str
    price: Optional[float] = None  # USD per 1M output tokens, when metered
    overrides: Dict[str, str] = field(default_factory=dict)
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def apply(self, config: dict) -> dict:
        """A copy of *config* routed to this model."""
        from .modelcheck import Candidate

        routed = dict(config)
        Candidate(self.provider, self.model, "fan-out",
                  overrides=dict(self.overrides)).apply(routed)
        return routed


def model_tier(provider: str, model: str) -> str:
    """light, standard or heavy.

    Seat aliases are known. Metered models are banded by output price, the
    one capability signal the catalog carries for every one of them.
    Unpriced (local) models go by the parameter count in their name.
    """
    from .providers import MODEL_PRICING

    name = (model or "").lower()
    if (provider, name) in _SEAT_TIERS:
        return _SEAT_TIERS[(provider, name)]
    if provider == "codex":
        return "standard"
    price = MODEL_PRICING.get(model)
    if price is not None and provider != "ollama":
        return "light" if price[1] <= 5 else "standard" if price[1] <= 12 else "heavy"
    if _SMALL_NAME.search(name):
        return "light"
    size = _PARAMETER_COUNT.search(name)
    if size:
        billions = float(size.group(1))
        return "light" if billions <= 14 else "standard" if billions <= 80 else "heavy"
    return "standard"


def classify_effort(task: str) -> str:
    """Effort for a task whose caller did not state one."""
    text = (task or "").lower()
    if any(hint in text for hint in _HEAVY_HINTS):
        return "heavy"
    if len(text) < 400 and any(hint in text for hint in _LIGHT_HINTS):
        return "light"
    return "standard"


def _seat_ready(provider: str) -> bool:
    from . import auth, seats

    try:
        return bool(auth.cli_installed(provider)
                    and seats.status(provider, timeout=5)[0])
    except Exception:
        return False


def _choice(provider: str, model: str, config: dict,
            overrides: Optional[Dict[str, str]] = None) -> ModelChoice:
    from .providers import MODEL_PRICING, _billing_kind

    billing = _billing_kind(provider, model, config)
    price = MODEL_PRICING.get(model)
    return ModelChoice(
        provider, model, model_tier(provider, model), billing,
        price[1] if price is not None and billing == "api" else None,
        dict(overrides or {}),
    )


def session_choice(config: dict) -> ModelChoice:
    from .modelcheck import _same_endpoint_overrides

    provider = (config.get("provider") or "").lower()
    model = config.get("chat_model") or config.get("model") or ""
    choice = _choice(provider, model, config,
                     _same_endpoint_overrides(provider, config))
    choice.reason = "session model"
    return choice


def available_models(config: dict,
                     seat_ready: Callable[[str], bool] = _seat_ready) -> List[ModelChoice]:
    """Models a fan-out worker could use right now, most preferred first.

    The session's model, then the rest of its provider's catalog, then other
    providers that are already usable without a new credential: a key in the
    environment, a signed-in seat, a reachable local server. ``local_only``
    and the session's own kind of billing bound the list.
    """
    from .config import local_only_enabled
    from .modelcheck import CLOUD_PROVIDERS, _same_endpoint_overrides
    from .providers import (
        CROSS_PROVIDER_FALLBACK_ORDER,
        KNOWN_MODELS,
        SEAT_PROVIDERS,
        _has_key,
        get_custom_base_url,
        is_local_inference_url,
        list_custom_models,
        list_ollama_models,
    )

    session = session_choice(config)
    ceiling = _BILLING_RANK[session.billing]
    if local_only_enabled(config, session.provider):
        ceiling = 0
    found: Dict[Tuple[str, str], ModelChoice] = {(session.provider, session.model): session}

    def add(provider: str, names, overrides: Optional[Dict[str, str]] = None) -> None:
        for name in names or ():
            if name and (provider, name) not in found:
                found[(provider, name)] = _choice(provider, name, config, overrides)

    def listed(lister) -> List[str]:
        try:
            return list(lister(config) or [])
        except Exception:
            return []

    same_endpoint = _same_endpoint_overrides(session.provider, config)
    if session.provider == "ollama":
        add("ollama", listed(list_ollama_models), same_endpoint)
    elif session.provider == "custom":
        add("custom", listed(list_custom_models), same_endpoint)
    else:
        add(session.provider, KNOWN_MODELS.get(session.provider), same_endpoint)
    if ceiling >= _BILLING_RANK["api"]:
        for provider in CROSS_PROVIDER_FALLBACK_ORDER:
            if (provider in CLOUD_PROVIDERS and provider != session.provider
                    and _has_key(provider)):
                add(provider, KNOWN_MODELS.get(provider))
    if ceiling >= _BILLING_RANK["seat"]:
        for provider in SEAT_PROVIDERS:
            if provider != session.provider and seat_ready(provider):
                add(provider, KNOWN_MODELS.get(provider))
    if session.provider != "ollama":
        add("ollama", listed(list_ollama_models))
    custom_base = get_custom_base_url(config)
    if (session.provider != "custom" and custom_base
            and is_local_inference_url(custom_base)):
        add("custom", listed(list_custom_models), {"custom_base_url": custom_base})
    return list(found.values())


def choose_auto(candidates: List[ModelChoice], effort: str,
                session_provider: str) -> ModelChoice:
    """The first candidate of the wanted tier, the session's provider first.

    Candidates arrive in preference order, so within a provider this is the
    catalog's own ordering rather than a price race to the oldest model.
    """
    for tier in _TIER_FALLBACK[effort]:
        pool = [candidate for candidate in candidates if candidate.tier == tier]
        if pool:
            best = min(pool, key=lambda candidate: candidate.provider != session_provider)
            return ModelChoice(best.provider, best.model, best.tier, best.billing,
                               best.price, dict(best.overrides), f"auto: {effort}")
    raise ValueError("no fan-out model candidates")


def fanout_setting(config: dict) -> str:
    """The configured worker model; ``subagent_model`` is the older spelling
    of the same intent and applies when ``fanout_model`` is unset."""
    return str(config.get("fanout_model") or config.get("subagent_model") or "").strip()


def resolve_model(config: dict, effort: str,
                  inventory: Callable[[], List[ModelChoice]]) -> Tuple[ModelChoice, str]:
    """(model for one task, warning). An unusable setting falls back to the
    session's model and says why."""
    from .modelcheck import Candidate, _same_endpoint_overrides, candidate_from_fallback
    from .providers import (
        RAW_FNS,
        SEAT_PROVIDERS,
        api_key_env_for,
        validate_model_for_provider,
    )

    session = session_choice(config)
    setting = fanout_setting(config)
    if not setting or setting.lower() == "default":
        return session, ""
    if setting.lower() == "auto":
        return choose_auto(inventory(), effort, session.provider), ""
    prefix = setting.split("/", 1)[0].lower()
    if "/" in setting and (prefix in RAW_FNS or prefix == "llamaidx"):
        candidate, reason = candidate_from_fallback(setting, config)
        if candidate is None:
            return session, f"fanout_model {reason}; using {session.label}"
    else:
        candidate = Candidate(
            session.provider, setting, "fan-out",
            overrides=_same_endpoint_overrides(session.provider, config))
    routed = dict(config)
    candidate.apply(routed)
    usable, reason = validate_model_for_provider(candidate.provider, candidate.model, routed)
    if usable is True and candidate.provider != session.provider:
        # Another provider is only usable with its own credential in place.
        key_env = api_key_env_for(candidate.provider, routed)
        if key_env and not os.environ.get(key_env, "").strip():
            usable, reason = False, f"{key_env} is not set"
        elif candidate.provider in SEAT_PROVIDERS:
            from . import auth

            if not auth.cli_installed(candidate.provider):
                usable, reason = False, "its official CLI is not installed"
    if usable is not True:
        return session, (f"fanout_model '{setting}' is unusable"
                         f" ({reason or 'unverified'}); using {session.label}")
    choice = _choice(candidate.provider, candidate.model, routed, candidate.overrides)
    choice.reason = "configured"
    return choice, ""


# ---------------------------------------------------------------------------
# Parent side: the fan_out tool
# ---------------------------------------------------------------------------

def _positive_int(config: dict, key: str, default: int) -> int:
    try:
        value = int(config.get(key) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def worker_limit(config: dict) -> int:
    """How many workers may run at once."""
    return _positive_int(config, "fanout_concurrency", DEFAULT_CONCURRENCY)


class FanOutClient:
    """The model-callable ``fan_out`` tool for one session."""

    name = "fan_out"

    def __init__(self):
        self._session = None
        self._usage: Optional[dict] = None
        self._processes: set = set()
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        # One local model server cannot serve several loads at once.
        self._local_gate = threading.Lock()

    def bind_session(self, session, usage: Optional[dict] = None) -> None:
        """Bind the parent session; *usage* receives the workers' token use
        so session cost stays truthful when workers run other models."""
        self._session = session
        self._usage = usage

    @staticmethod
    def _text(message: str) -> dict:
        return {"content": [{"type": "text", "text": message}]}

    # -- planning -------------------------------------------------------------

    def _parse(self, arguments: dict) -> Tuple[List[dict], str]:
        tasks = arguments.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return [], "Error: 'tasks' must be a non-empty list"
        if len(tasks) > MAX_TASKS:
            return [], f"Error: at most {MAX_TASKS} tasks per fan-out"
        parsed = []
        for item in tasks:
            if not isinstance(item, dict) or not str(item.get("task") or "").strip():
                return [], "Error: every task needs a non-empty 'task'"
            task = str(item["task"]).strip()
            effort = str(item.get("effort") or "").strip().lower()
            parsed.append({
                "task": task,
                "context": str(item.get("context") or "").strip(),
                "effort": effort if effort in EFFORTS else classify_effort(task),
            })
        return parsed, ""

    def _worker_tools(self) -> Tuple[List[str], str]:
        """(tool names workers get, note for the model when one is withheld)."""
        from .tooling import DelegateTaskClient

        session = self._session
        excluded = (DelegateTaskClient.EXCLUDED_TOOLS
                    | DelegateTaskClient.IMPLICITLY_EXCLUDED_TOOLS
                    | WORKER_EXTRA_EXCLUDED_TOOLS)
        active = {
            tool.get("function", {}).get("name")
            for tool in (getattr(session.chat_state, "tools", None) or [])
        }
        names = sorted(
            name for name in session.builtin_clients
            if name in active and name not in excluded
        )
        shell = session.builtin_clients.get("local_shell")
        if "local_shell" in names and getattr(shell, "_exec_backend", None) is not None:
            # Workers are separate processes outside the sandbox; running
            # their commands on the host would widen what this session may do.
            names.remove("local_shell")
            return names, ("Workers had no shell: this session runs commands in a"
                           " sandbox, which fan-out workers do not share.")
        return names, ""

    def _permission_mode(self) -> Optional[str]:
        """The mode workers run in, or None when the user declines."""
        session = self._session
        mode = session.permissions.get_permission_mode()
        shell = session.builtin_clients.get("local_shell")
        policy = getattr(shell, "policy", None)
        if mode == "yolo" or getattr(policy, "allow_auto_execute", False):
            return "yolo"
        if mode != "prompt_all" or policy is None or not policy.interactive:
            return mode
        print("  \033[2mWorkers run read-only commands without asking and refuse"
              " the rest.\033[0m")
        ask = policy.input_fn or input
        try:
            sys.stdout.flush()
            answer = ask("  \033[1;33mFan out? [y/n]\033[0m ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        return "safe_auto" if answer in ("", "y", "yes") else None

    # -- execution ------------------------------------------------------------

    def _spawn(self, envelope: dict) -> subprocess.Popen:
        import conch

        env = dict(os.environ)
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(conch.__file__)))
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = package_root + (os.pathsep + existing if existing else "")
        env["CONCH_NO_WIZARD"] = "1"
        return subprocess.Popen(
            [sys.executable, "-c",
             "from conch.fanout import worker_main; raise SystemExit(worker_main())"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=envelope.get("cwd") or None, env=env, text=True,
            start_new_session=True,
        )

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _run_worker(self, envelope: dict, timeout: float) -> dict:
        """Run one worker process to completion; never raises."""
        from .secretguard import redact_credentials

        controller = self._session._pause_controller if self._session is not None else None
        try:
            if controller is not None:
                controller.wait((self._cancelled, self._session._closed_event))
                envelope = dict(envelope, pause_path=controller.export_path())
            if self._cancelled.is_set():
                return {"ok": False, "error": "cancelled"}
            process = self._spawn(envelope)
        except AgentsCancelled:
            return {"ok": False, "error": "cancelled"}
        except OSError as exc:
            return {"ok": False, "error": f"could not start a worker ({type(exc).__name__})"}
        with self._lock:
            self._processes.add(process)
        clock = controller.active_time if controller is not None else time.monotonic
        started = clock()
        payload = json.dumps(envelope)
        try:
            while True:
                if (self._cancelled.is_set()
                        or (controller is not None and controller.closed)
                        or (self._session is not None and self._session._closed_event.is_set())):
                    self._kill(process)
                    process.communicate()
                    return {"ok": False, "error": "cancelled"}
                remaining = timeout - (clock() - started)
                if remaining <= 0:
                    self._kill(process)
                    process.communicate()
                    return {"ok": False, "error": f"timed out after {timeout:g}s"}
                try:
                    stdout, stderr = process.communicate(
                        payload, timeout=min(PauseController.POLL_SECONDS, remaining))
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains partial output; input is sent only once.
                    payload = None
        finally:
            # A worker may exit while a command it started is still running.
            self._kill(process)
            with self._lock:
                self._processes.discard(process)
        if self._cancelled.is_set():
            return {"ok": False, "error": "cancelled"}
        try:
            result = json.loads(stdout)
        except ValueError:
            result = None
        if not isinstance(result, dict):
            lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
            detail, _ = redact_credentials(lines[-1][:200] if lines else "", "[credential redacted]")
            return {"ok": False, "error": (
                f"worker exited with status {process.returncode}"
                + (f": {detail}" if detail else ""))}
        return result

    def _execute(self, envelope: dict, timeout: float, serial: bool) -> dict:
        started = time.monotonic()
        if serial:
            with self._local_gate:
                result = self._run_worker(envelope, timeout)
        else:
            result = self._run_worker(envelope, timeout)
        result["seconds"] = time.monotonic() - started
        return result

    def stop(self) -> None:
        """Stop the running fan-out: workers still running are killed and
        tasks not yet started never start. Safe from any thread."""
        self._cancelled.set()
        with self._lock:
            running = list(self._processes)
        for process in running:
            self._kill(process)

    def call_tool(self, name: str, arguments: dict) -> dict:  # noqa: ARG002
        from .providers import record_session_usage
        from .render import _get_active_ui
        from .runtime import tool_result_char_budget, truncate_middle

        session = self._session
        if session is None or session.chat_state is None:
            return self._text("Error: fan_out not initialized")
        tasks, error = self._parse(arguments or {})
        if error:
            return self._text(error)
        config = session.config
        inventory: List[ModelChoice] = []

        def models() -> List[ModelChoice]:
            if not inventory:
                inventory.extend(available_models(config))
            return inventory

        warnings: List[str] = []
        for task in tasks:
            task["choice"], warning = resolve_model(config, task["effort"], models)
            if warning and warning not in warnings:
                warnings.append(warning)
        # One row per task on screen: the task text gives way, not the model.
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        plan = []
        for index, task in enumerate(tasks, start=1):
            head = f"{index}. {task['choice'].label} ({task['choice'].reason}) \u2014 "
            room = max(16, columns - len(head) - 6)
            text = " ".join(task["task"].split())
            plan.append(head + (text if len(text) <= room else text[:room - 1] + "\u2026"))
        tools, tool_note = self._worker_tools()
        from .render import clear_active_spinners

        clear_active_spinners()
        print(f"\n  \033[1;33m\u26a0 Fan out {len(tasks)} tasks:\033[0m", flush=True)
        for line in plan:
            print(f"    {line}")
        mode = self._permission_mode()
        if mode is None:
            return self._text("User declined the fan-out.")
        if tool_note:
            warnings.append(tool_note)

        shell = session.builtin_clients.get("local_shell")
        rounds = _positive_int(config, "fanout_rounds",
                               _positive_int(config, "subagent_rounds", DEFAULT_ROUNDS))
        timeout = float(_positive_int(config, "fanout_timeout", DEFAULT_TIMEOUT))
        limit = worker_limit(config)
        # Several loads on one local server evict each other unless the user
        # has said the server can take them.
        serialize_local = not str(config.get("fanout_concurrency") or "").strip()

        def routed(choice: ModelChoice) -> dict:
            return {
                key: value for key, value in choice.apply(config).items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }

        envelopes = [{
            "task": task["task"],
            "context": task["context"],
            "config": routed(task["choice"]),
            "cwd": session.cwd,
            "tools": tools,
            "rounds": rounds,
            "permission_mode": mode,
            "allow_prefixes": sorted(getattr(shell, "_allowed_prefixes", ()) or ()),
        } for task in tasks]
        own = session_choice(config)

        ui = _get_active_ui()
        self._cancelled.clear()
        started = time.monotonic()
        # Each task is announced above; the live rows show which still run.
        activities = [
            ui.start_activity(f"fan-out {line[:60]}") if ui is not None else None
            for line in plan
        ]

        def run(index: int) -> dict:
            choice = tasks[index]["choice"]
            token = subagent_activity.start("fan-out", tasks[index]["task"], choice.label)
            status, summary = "failed", "no result"
            try:
                result = self._execute(
                    envelopes[index], timeout,
                    serialize_local and choice.billing == "local")
                error = str(result.get("error") or "")
                if (not result.get("ok") and choice.reason.startswith("auto")
                        and choice.label != own.label and not self._cancelled.is_set()
                        and not error.startswith("timed out")):
                    # Auto chose from what looked usable (a key that is set, a
                    # server that listed the model). When that turns out wrong
                    # the task still deserves an answer from the session model.
                    envelopes[index] = dict(envelopes[index], config=routed(own))
                    result = self._execute(
                        envelopes[index], timeout,
                        serialize_local and own.billing == "local")
                    result["fallback"] = (
                        f"{choice.label} failed ({error or 'no result'});"
                        f" ran on {own.label} instead")
                if result.get("ok"):
                    status = "done"
                    summary = result.get("fallback") or str(result.get("summary") or "")
                else:
                    summary = str(result.get("error") or "no result")
                return result
            except BaseException as exc:
                summary = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                if status != "done" and self._cancelled.is_set():
                    status = "cancelled"
                subagent_activity.finish(token, status, summary)
                if activities[index] is not None:
                    ui.end_activity(activities[index])

        with ThreadPoolExecutor(max_workers=min(limit, len(tasks))) as pool:
            futures = [pool.submit(run, index) for index in range(len(tasks))]
            try:
                results = []
                for future in futures:
                    while True:
                        try:
                            results.append(future.result(timeout=PauseController.POLL_SECONDS))
                            break
                        except FutureTimeout:
                            # Park the calling turn too, releasing its global
                            # execution lock while child processes are paused.
                            checkpoint(cancelled=(self._cancelled,))
            except BaseException:
                # Ctrl+C stops the whole fan-out, not just the wait for it.
                self.stop()
                raise
        elapsed = time.monotonic() - started

        budget = max(1500, tool_result_char_budget(session.provider, config) // len(tasks))
        succeeded = sum(1 for result in results if result.get("ok"))
        sections = [
            f"Fan-out finished: {succeeded} of {len(tasks)} tasks succeeded"
            f" in {elapsed:.1f}s."
        ]
        sections.extend(warnings)
        for index, (task, result) in enumerate(zip(tasks, results), start=1):
            choice = task["choice"]
            tokens_in = int(result.get("input_tokens") or 0)
            tokens_out = int(result.get("output_tokens") or 0)
            provider = str(result.get("provider") or choice.provider)
            model = str(result.get("model") or choice.model)
            if self._usage is not None and (tokens_in or tokens_out):
                record_session_usage(
                    self._usage, provider, model, tokens_in, tokens_out,
                    envelopes[index - 1]["config"], count_turn=False)
            status = "done" if result.get("ok") else "failed"
            print(
                f"  \033[2m  {index}. {status} \u00b7 {provider}/{model} \u00b7"
                f" {result.get('seconds', 0.0):.1f}s \u00b7 {tokens_in:,} in /"
                f" {tokens_out:,} out\033[0m", file=sys.stderr)
            head = f"[{index}] {' '.join(task['task'].split())[:120]}"
            used = f"model: {provider}/{model} ({choice.reason})"
            if tokens_in or tokens_out:
                used += f" \u00b7 {tokens_in:,} in / {tokens_out:,} out tokens"
            if result.get("fallback"):
                used += f"\nnote: {result['fallback']}"
            body = (
                truncate_middle(str(result.get("summary") or ""), budget)
                if result.get("ok")
                else f"FAILED: {result.get('error') or 'no result'}"
            )
            sections.append(f"{head}\n{used}\n{body}")
        return self._text("\n\n".join(sections))


# ---------------------------------------------------------------------------
# Worker side: one bounded turn in its own process
# ---------------------------------------------------------------------------

def run_envelope(envelope: dict) -> dict:
    """Run one fan-out task and describe the outcome; never raises."""
    from .bootstrap import make_builtin_clients
    from .memory import MemoryStore
    from .session import AgentSession
    from .tooling import PermissionState, inject_builtin_tools

    config = dict(envelope.get("config") or {})
    mode = str(envelope.get("permission_mode") or "prompt_all")
    permissions = PermissionState(
        agent_mode=mode == "yolo",
        permission_mode="safe_auto" if mode == "safe_auto" else "prompt_all",
    )
    wanted = set(envelope.get("tools") or ())
    session = AgentSession(
        config, interactive=False, permissions=permissions,
        cwd=envelope.get("cwd") or None,
        pause_controller=(
            PauseController(envelope["pause_path"])
            if envelope.get("pause_path") else None
        ),
    )
    try:
        clients = {
            name: client for name, client in make_builtin_clients(
                MemoryStore(), config, interactive=False, permissions=permissions,
            ).items() if name in wanted
        }
        shell = clients.get("local_shell")
        if shell is not None:
            shell.allow_prefixes(envelope.get("allow_prefixes") or [])
        session.budgets.max_tool_rounds = int(envelope.get("rounds") or DEFAULT_ROUNDS)
        session.attach_clients(clients)
        definitions: List[dict] = []
        tool_map: Dict[str, Any] = {}
        inject_builtin_tools(definitions, tool_map, clients)
        tools = [
            definition for definition in definitions
            if definition.get("function", {}).get("name") in clients
        ]
        task = str(envelope.get("task") or "")
        context = str(envelope.get("context") or "")
        messages = [
            {"role": "system", "content": WORKER_PROMPT},
            {"role": "user", "content": task if not context else f"{task}\n\nContext:\n{context}"},
        ]
        reply, usage = session.run_turn(messages, tools=tools or None, tool_map=tool_map)
    except AgentsCancelled:
        return {"ok": False, "error": "cancelled"}
    except Exception as exc:  # noqa: BLE001 - one bounded task per process
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        session.close()
    outcome = {
        "provider": session.provider,
        "model": str(usage.get("model") or session.model),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    summary = str(reply or "").strip()
    if not summary:
        return dict(outcome, ok=False,
                    error=str(usage.get("error") or "the worker produced no reply"))
    return dict(outcome, ok=True, summary=summary)


def worker_main() -> int:
    """Process entry: envelope JSON on stdin, result JSON on stdout."""
    # Tool previews and warnings print to stdout; keep the result channel clean.
    result_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        envelope = json.loads(sys.stdin.read())
        if not isinstance(envelope, dict):
            raise ValueError("envelope must be an object")
    except ValueError as exc:
        result = {"ok": False, "error": f"invalid fan-out envelope ({exc})"}
    else:
        result = run_envelope(envelope)
    with os.fdopen(result_fd, "w", encoding="utf-8") as channel:
        channel.write(json.dumps(result))
    return 0 if result.get("ok") else 1
