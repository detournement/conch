"""Reusable startup wiring shared by the interactive shell and headless modes.

Swarm Phase 0: everything here used to live inside ``app.chat_loop``. The
interactive shell now composes these functions with its readline/typeahead
UI on top; headless entrypoints (controller, edge, worker — later phases)
call them directly without importing any terminal machinery.

Nothing in this module prompts, reads stdin, or requires a TTY. Startup
problems are reported by raising :class:`StartupError` (carrying a CLI exit
code) or by returning warning strings for the caller to display however it
wants.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

from .config import get_bool, local_only_enabled
from .memory import MemoryStore
from .session import AgentSession
from .tooling import (
    ApiLayerClient,
    ConchConfigClient,
    ConchIntrospectClient,
    DelegateTaskClient,
    InteractiveTerminalClient,
    LocalShellClient,
    LocalShellPolicy,
    ManageToolsClient,
    PersonalItemsClient,
    PublicApiClient,
    SSHRemoteClient,
    SaveMemoryClient,
    SearchConversationsClient,
    SkillManageClient,
    TodoListClient,
    ToolRuntimeState,
    apply_filter,
    auto_disable_oversized_groups,
    cap_tools,
    default_permissions,
    inject_builtin_tools,
    load_tool_prefs,
    save_tool_prefs,
    set_agent_mode,
)
from . import mcp as mcp_mod

DEFAULT_MAX_TOOL_ROUNDS = 25


class StartupError(Exception):
    """Startup cannot proceed. ``code`` is the process exit status for CLIs."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def apply_agent_mode_from_config(config: dict) -> bool:
    """Enable agent mode when the config file asks for it.

    Returns True only when agent mode was turned on by the config default,
    so the caller knows to show the startup notice (manual /agent toggles
    mid-session never go through here).
    """
    from .tooling import set_permission_mode
    set_permission_mode(config.get("permission_mode", ""))
    if get_bool(config, "agent_mode"):
        set_agent_mode(True)
        return True
    return False


def resolve_startup_provider(config: dict) -> tuple:
    """(provider, raw_fn) for a validated startup provider.

    Raises StartupError with the exact message and exit code the CLI has
    always used, so interactive and headless callers fail identically.
    """
    from .providers import RAW_FNS

    provider = (config.get("provider") or "openai").lower()
    if local_only_enabled(config, provider) and provider not in (
        "ollama",
        "custom",
    ):
        raise StartupError(
            "conch: local_only=true requires provider=ollama or provider=custom",
            code=2,
        )
    raw_fn = RAW_FNS.get(provider)
    if raw_fn is None:
        raise StartupError(f"conch: unknown provider {provider}", code=1)
    return provider, raw_fn


def warn_unknown_cloud_model(provider: str, model_name: str) -> str:
    """Startup scrutiny for config-file model values on cloud providers
    (warn-and-replace): "" when the model is known, otherwise a warning with
    close-match suggestions."""
    from .providers import (
        KNOWN_MODELS,
        get_fallback_model,
        suggest_models,
    )

    provider = (provider or "").lower()
    if provider in ("ollama", "custom"):
        return ""  # local providers are resolved from live services
    known = KNOWN_MODELS.get(provider)
    if not known or not model_name or model_name in known:
        return ""
    suggestions = suggest_models(model_name, known)
    hint = f" — did you mean {', '.join(suggestions)}?" if suggestions else ""
    replacement = get_fallback_model(provider)
    return (
        f"Configured model '{model_name}' isn't in conch's {provider} "
        f"catalog of tool-capable models{hint}; using '{replacement}' instead"
    )


def resolve_ollama_startup_model(config: dict, model_name: str) -> tuple:
    """Verify the configured Ollama model at startup, substituting one that
    exists on the server when possible.

    Returns (model_name, warnings). Never raises and never dead-ends: an
    unreachable server, a missing configured model, or an empty server all
    degrade to a warning so the session still starts.
    """
    from .providers import (
        get_fallback_model,
        get_ollama_base_url,
        list_ollama_models,
        ollama_model_matches,
    )

    live_models = list_ollama_models(config)
    if live_models is None:
        return model_name, [
            f"Ollama server unreachable at {get_ollama_base_url(config)} — "
            f"model '{model_name}' unverified"
        ]
    if ollama_model_matches(model_name, live_models):
        return model_name, []
    replacement = get_fallback_model("ollama", config)
    if replacement:
        return replacement, [
            f"Model '{model_name}' is not available/tool-capable on the "
            f"Ollama server — using '{replacement}' instead"
        ]
    installed = list_ollama_models(config, tool_capable_only=False) or []
    if installed:
        warning = (
            "No tool-capable models installed on the Ollama server — chat "
            "needs tool support (try `ollama pull qwen2.5`, or /provider to "
            "switch)"
        )
    else:
        warning = (
            "No models installed on the Ollama server — pull one (e.g. "
            "`ollama pull qwen2.5`) or /provider to switch"
        )
    return model_name, [warning]


def resolve_startup_model(config: dict, provider: str) -> tuple:
    """Validate/normalize the configured model for *provider* at startup.

    Returns (model_name, warnings) and updates config in place exactly the
    way the interactive shell always has (model/chat_model/custom_model).
    """
    model_name = config.get("chat_model", config.get("model", ""))
    warnings: list = []
    if provider == "ollama":
        resolved, warnings = resolve_ollama_startup_model(config, model_name)
        if resolved != model_name:
            model_name = resolved
            config["model"] = resolved
            config["chat_model"] = resolved
    elif provider == "custom":
        from .providers import get_custom_base_url, list_custom_models

        available = list_custom_models(config, timeout=3.0)
        if available is None:
            warnings.append(
                f"Custom endpoint unreachable at "
                f"{get_custom_base_url(config)}; model is unverified"
            )
        elif model_name not in available and available:
            replacement = available[0]
            warnings.append(
                f"Custom model '{model_name}' is unavailable or "
                f"failed tool conformance; using '{replacement}'"
            )
            model_name = replacement
            config["model"] = replacement
            config["chat_model"] = replacement
            config["custom_model"] = replacement
        elif not available:
            warnings.append(
                "No custom endpoint models passed native tool-call conformance"
            )
    else:
        # Cloud config values are subject to the same tools-only catalog gate
        # as interactive switches.
        model_warning = warn_unknown_cloud_model(provider, model_name)
        if model_warning:
            warnings.append(model_warning)
            from .providers import get_fallback_model

            model_name = get_fallback_model(provider, config)
            config["model"] = model_name
            config["chat_model"] = model_name
    return model_name, warnings


def make_builtin_clients(
    memory: MemoryStore,
    config: dict,
    interactive: bool = True,
    permissions=None,
) -> Dict[str, Any]:
    """Construct the built-in tool clients for one session."""
    perms = permissions if permissions is not None else default_permissions()
    local_shell = LocalShellClient(permissions=perms)
    local_shell.set_policy(LocalShellPolicy(interactive=interactive, allow_auto_execute=perms.get_agent_mode()))
    # Scale shell-output budget to the active model's context window (plan 1.5)
    from .runtime import tool_result_char_budget

    result_budget = tool_result_char_budget(
        (config.get("provider") or "").lower(), config
    )
    local_shell.set_result_budget(result_budget)
    interactive_terminal = InteractiveTerminalClient()
    interactive_terminal.set_policy(
        LocalShellPolicy(interactive=interactive, allow_auto_execute=False)
    )
    try:
        ssh_persist = int(config.get("ssh_control_persist", 600) or 600)
    except (TypeError, ValueError):
        ssh_persist = 600
    from .ssh_control import SSHControlManager

    ssh_remote = SSHRemoteClient(
        manager=SSHControlManager(persist_seconds=ssh_persist)
    )
    ssh_remote.bind_permissions(perms)
    ssh_remote.set_policy(
        LocalShellPolicy(
            interactive=interactive,
            allow_auto_execute=perms.get_agent_mode(),
        )
    )
    ssh_remote.set_result_budget(result_budget)
    # Seed the always-allow prefix list from config (plan 2.1)
    allow = (config.get("allow_prefixes") or "").strip()
    if allow:
        prefixes = [p.strip() for p in allow.split(",") if p.strip()]
        local_shell.allow_prefixes(prefixes)
        ssh_remote.allow_prefixes(prefixes)
    manage_tools = ManageToolsClient()
    save_memory = SaveMemoryClient()
    save_memory.bind(memory)
    conch_config = ConchConfigClient()
    conch_config.bind_permissions(perms)
    public_api = PublicApiClient()
    search_convos = SearchConversationsClient()
    clients: Dict[str, Any] = {
        "local_shell": local_shell,
        "interactive_terminal": interactive_terminal,
        "ssh_remote": ssh_remote,
        "manage_tools": manage_tools,
        "save_memory": save_memory,
        "conch_config": conch_config,
        "public_api": public_api,
        "search_conversations": search_convos,
        "todo_list": TodoListClient(),
        # The user's durable personal store (todo/recipes/papers spaces).
        # Deliberately available to remote/channel sessions too — channel
        # capture is a design goal; the sender allowlists gate access.
        "personal_items": PersonalItemsClient(),
        "delegate_task": DelegateTaskClient(),
        "skill_manage": SkillManageClient(),
        "conch_introspect": ConchIntrospectClient(),
    }
    clients["skill_manage"].configure(interactive=interactive)
    if str(config.get("capitol_base_url") or "").strip():
        # The model-callable Capitol runtime surface (never admin).
        # Import stays lazy: sessions without Capitol config never load
        # conch.capitol. Mission sessions swap this client out for the
        # envelope-scoped one (kernel engine); remote turns swap in an
        # origin-bound variant whose start proposes an approval.
        from .capitol.tool import CapitolSessionClient

        clients["capitol_control"] = CapitolSessionClient(config)
    if get_bool(config, "fleet_controller"):
        # The model-callable fleet delegation surface (local sessions
        # only: remote turns exclude it, workers deny-list it, delegated
        # sub-turns don't inherit it implicitly). Import stays lazy so
        # non-fleet installs never load conch.fleet.
        from .fleet.delegate import FleetDelegateClient

        clients["fleet_delegate"] = FleetDelegateClient(config)
    import os

    api_layer_key = config.get("API_LAYER_KEY", "") or os.environ.get("API_LAYER_KEY", "")
    if api_layer_key:
        api_layer = ApiLayerClient()
        api_layer.bind(api_layer_key)
        clients["api_layer"] = api_layer
    return clients


def load_runtime_tools(builtin_clients: Dict[str, Any], use_cache: bool = False):
    """Create MCP clients and assemble the session tool state."""
    if use_cache:
        cached = mcp_mod.load_cached_tools()
        if cached is not None:
            mcp_clients = mcp_mod.create_clients()
            all_tools = list(cached)
            tool_map: Dict[str, Any] = {}
            # Map cached tools to clients by server name
            for client in mcp_clients.values():
                try:
                    live = client.list_tools()
                except Exception:
                    live = []
                for t in live:
                    tool_name = t.get("function", {}).get("name", "")
                    if tool_name and tool_name not in tool_map:
                        tool_map[tool_name] = client
            all_tools = [
                tool
                for tool in all_tools
                if tool.get("function", {}).get("name", "") in tool_map
            ]
            inject_builtin_tools(all_tools, tool_map, builtin_clients)
            prefs = load_tool_prefs()
            prefs, _ = auto_disable_oversized_groups(all_tools, tool_map, prefs)
            tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
            state = ToolRuntimeState(all_tools=all_tools, tool_map=tool_map, tools=tools)
            builtin_clients["manage_tools"].bind(state)
            return mcp_clients, state
    mcp_clients = mcp_mod.create_clients()
    all_tools, tool_map = mcp_mod.collect_tools(mcp_clients)
    mcp_mod.save_tool_cache(all_tools)
    inject_builtin_tools(all_tools, tool_map, builtin_clients)
    prefs = load_tool_prefs()
    prefs, auto_disabled = auto_disable_oversized_groups(all_tools, tool_map, prefs)
    if auto_disabled:
        save_tool_prefs(prefs)
        for group_name, count in auto_disabled:
            print(
                f"\033[33m  Auto-disabled {group_name} ({count} tools — exceeds 200 limit). Use /enable {group_name} to override.\033[0m",
                file=sys.stderr,
            )
    tools = cap_tools(apply_filter(all_tools, tool_map, prefs))
    state = ToolRuntimeState(all_tools=all_tools, tool_map=tool_map, tools=tools)
    builtin_clients["manage_tools"].bind(state)
    return mcp_clients, state


def build_agent_session(
    config: dict,
    *,
    interactive: bool = False,
    permissions=None,
    memory: Optional[MemoryStore] = None,
    use_cached_tools: bool = False,
    cwd=None,
) -> AgentSession:
    """Construct a fully wired AgentSession without any UI.

    Builds tool clients, loads MCP/tool state, and binds delegate_task and
    conch_introspect. The caller owns the session and must ``close()`` it.
    """
    if memory is None:
        memory = MemoryStore()
    session = AgentSession(
        config,
        interactive=interactive,
        permissions=permissions,
        memory=memory,
        cwd=cwd,
    )
    builtins = make_builtin_clients(
        memory, config, interactive=interactive,
        permissions=session.permissions,
    )
    mcp_clients, state = load_runtime_tools(builtins, use_cache=use_cached_tools)
    session.attach_clients(builtins, chat_state=state, mcp_clients=mcp_clients)
    builtins["delegate_task"].bind_session(session)
    builtins["conch_introspect"].bind(
        session.provider, session.model, config, state
    )
    return session


def route_scheduled_output(config: dict, task, reply: str, usage: dict) -> None:
    """Deliver a scheduled task's result over the notify channel (plan 4.3).
    No configured channel = old behavior (output discarded). Never raises."""
    try:
        if not (config.get("notify_channel") or "").strip():
            return
        from .channels import ChannelManager
        from .runtime import truncate_middle
        if usage.get("error"):
            body = ("the model backend was unreachable "
                    f"({truncate_middle(str(usage['error']), 200)})")
        else:
            body = truncate_middle(reply or "(no output)", 2500)
        task_id = getattr(task, "id", "?")
        task_prompt = getattr(task, "prompt", "")
        ok, detail = ChannelManager(config).notify(
            f"[conch scheduled #{task_id}] {task_prompt}\n\n{body}"
        )
        if not ok:
            print(f"  \033[33m⚠ scheduled notify failed: {detail}\033[0m", file=sys.stderr)
    except Exception as exc:
        print(f"  \033[33m⚠ scheduled notify failed: {exc}\033[0m", file=sys.stderr)


def make_scheduled_executor(
    config: dict,
    get_system_prompt,
    *,
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    route_output=route_scheduled_output,
):
    """Executor for the task scheduler: each run gets a fresh session.

    ``get_system_prompt`` is called per run so mid-session provider/model
    switches keep flowing into scheduled turns (the interactive shell passes
    a closure over its live system prompt). Scheduled runs share the
    process-default permission state, so a /agent toggle applies to them
    exactly as it always has.
    """

    def _scheduled_executor(prompt: str, task):
        session = build_agent_session(
            config, interactive=False, permissions=default_permissions()
        )
        try:
            messages = [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": prompt},
            ]
            reply, usage = session.run_turn(
                messages, max_tool_rounds=max_tool_rounds
            )
            # Route scheduled output over the notify channel (plan 4.3)
            # instead of discarding it.
            if route_output is not None:
                route_output(config, task, reply, usage)
            return reply, usage
        finally:
            session.close()

    return _scheduled_executor


def start_scheduler(executor):
    """Start the recurring-task scheduler with *executor*."""
    from .scheduler import Scheduler

    sched = Scheduler()
    sched.set_executor(executor)
    sched.start()
    return sched


def start_task_backend(config: dict, get_system_prompt, *,
                       max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS):
    """Select the scheduled-task backend for one interactive session.

    Returns ``(sched, kind)`` where kind is ``"legacy"`` or ``"kernel"``.

    The no-daemon invariant is enforced here: with ``edge_daemon`` unset or
    false, this starts the classic in-process scheduler over tasks.json and
    never imports ``conch.kernel`` at all — interactive behavior is exactly
    what it has always been. With ``edge_daemon=true``, `/schedule` is
    backed by the mission kernel through the same Scheduler-shaped surface
    (over the daemon's socket when it runs, else directly against the
    kernel database), and execution belongs to the ``conch-edge`` daemon —
    the shell never runs scheduled work in kernel mode.
    """
    if get_bool(config, "edge_daemon"):
        from .kernel.client import KernelSchedulerAdapter

        return KernelSchedulerAdapter(config), "kernel"
    sched = start_scheduler(
        make_scheduled_executor(
            config, get_system_prompt, max_tool_rounds=max_tool_rounds
        )
    )
    return sched, "legacy"


class _ShellIntakeGate:
    """Channel-intake lease for a shell-hosted remote loop in kernel mode.

    The same ``channel_intake`` lease the daemon's intake takes: whoever
    holds it is the single channel consumer, no matter what each process's
    config says. Kernel imports stay inside the methods so nothing kernel-
    related loads before the loop actually polls. Fails closed: no
    reachable kernel means no polling.
    """

    def __init__(self, config: dict):
        import os
        import socket as socket_mod

        self._config = config
        self.holder = f"shell-{socket_mod.gethostname()}-{os.getpid()}"

    def _lease_seconds(self) -> float:
        from .kernel.intake import intake_lease_seconds

        return intake_lease_seconds(
            self._config.get("remote_poll_interval", 60) or 60
        )

    def _with_client(self, fn):
        from .kernel.client import attach_kernel
        from .kernel.model import KernelError

        try:
            client = attach_kernel(self._config)
        except KernelError:
            return None
        try:
            return fn(client)
        except KernelError:
            return None
        finally:
            client.close()

    def acquire(self) -> bool:
        granted = self._with_client(
            lambda client: client.intake_acquire(
                self.holder, self._lease_seconds()
            )
        )
        return bool(granted)

    def release(self) -> None:
        self._with_client(
            lambda client: client.intake_release(self.holder)
        )

    def mission_input(self, mission_id: str, text: str, message) -> str:
        source = f"{message.channel}:{message.sender}"
        result = self._with_client(
            lambda client: client.provide_input(
                mission_id, text, source=source
            )
        )
        if result is None:
            return f"Mission input failed: no kernel answered for {mission_id}."
        if result.get("woken"):
            return (
                f"Input delivered to {mission_id} — it wakes now and will"
                " run at the next session slot."
            )
        return (
            f"Input recorded for {mission_id}; its state is unchanged and"
            " the input will be visible at its next session."
        )


def start_remote_loop(config: dict, conv_mgr=None, session=None) -> tuple:
    """Start the opt-in remote channel loop.

    Returns (loop, "") when running, (None, "disabled") when remote_enabled
    is off, (None, "daemon-hosted") when the edge daemon owns channel
    intake (the default whenever ``edge_daemon`` is enabled — set
    ``remote_host=shell`` to keep the loop in the shell), and (None, "no
    channel configured") when it is on but no channel is usable — the
    caller decides how (or whether) to surface each case.
    """
    if not get_bool(config, "remote_enabled"):
        return None, "disabled"
    kernel_mode = get_bool(config, "edge_daemon")
    remote_host = str(config.get("remote_host") or "daemon").strip().lower()
    if kernel_mode and remote_host != "shell":
        # Daemon-when-enabled default: the daemon answers channels 24/7;
        # a second consumer here would double-answer every message.
        return None, "daemon-hosted"
    from .remote import RemoteLoop

    intake_gate = None
    mission_input = None
    if kernel_mode:
        # remote_host=shell: the shell hosts the loop but holds the same
        # kernel channel-intake lease the daemon would, so exactly one
        # consumer polls either way; mission-addressed input routes into
        # the kernel and wakes the mission immediately.
        gate = _ShellIntakeGate(config)
        intake_gate = gate
        mission_input = gate.mission_input
    loop = RemoteLoop(
        config, conv_mgr=conv_mgr, session=session,
        intake_gate=intake_gate, mission_input=mission_input,
    )
    if loop.start():
        return loop, ""
    return None, "no channel configured"
