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

import os
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
    LlamaidxRegistryClient,
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
    missing = missing_api_key_startup_error(provider, config)
    if missing is not None:
        raise missing
    return provider, raw_fn


def missing_api_key_startup_error(provider: str,
                                  config: dict) -> Optional[StartupError]:
    """A StartupError naming the unset key variable and the remedy when
    *provider* needs an API key that is not in the environment; None when
    the provider is keyless or the key is present. Checked at startup so a
    misconfigured install fails with one clear message instead of a bare
    `[no response]` on every turn."""
    from .providers import api_key_env_for, missing_api_key_message

    key_env = api_key_env_for(provider, config)
    if not key_env or os.environ.get(key_env, "").strip():
        return None
    return StartupError(f"conch: {missing_api_key_message(provider, key_env)}",
                        code=2)


def warn_unknown_cloud_model(provider: str, model_name: str) -> str:
    """Catalog scrutiny for a cloud model name: "" when the model is in
    conch's verified tool-capable catalog, otherwise a diagnosis with
    close-match suggestions and the catalog default. Purely advisory —
    nothing is substituted here; the startup model check
    (:mod:`conch.modelcheck`) decides what happens next, with the user."""
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
    default = get_fallback_model(provider)
    return (
        f"Configured model '{model_name}' isn't in conch's {provider} "
        f"catalog of tool-capable models{hint} (catalog default: '{default}')"
    )


def resolve_startup_model(config: dict, provider: str) -> tuple:
    """The configured model name for *provider* at startup.

    Returns (model_name, warnings). This is pure normalization: it makes
    no network calls and never substitutes a different model. Whether
    the model actually works is decided by the startup model check in
    :mod:`conch.modelcheck`, which either verifies it silently or puts
    the alternatives in front of the user (interactive) / walks the
    pre-approved ``fallback_models`` list (non-interactive). The silent
    "model X unavailable; using Y" swap that used to live here is gone
    on purpose: nothing switches without approval.
    """
    model_name = (config.get("chat_model") or config.get("model") or "").strip()
    return model_name, []


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
    # Product tools plug in through the session-tool seam: each provider
    # owns its config gate (capitol_control needs capitol_base_url,
    # fleet_delegate needs fleet_controller) and lazy-imports its product
    # code, so unconfigured sessions never load the adapters. Mission
    # sessions swap these clients out for envelope-scoped ones (kernel
    # engine); remote turns swap capitol_control for an origin-bound
    # variant whose start proposes an approval.
    from .plugins import load_builtin_plugins, session_tool_providers

    load_builtin_plugins()
    for name, provider in session_tool_providers():
        client = provider.build_session_client(config)
        if client is not None:
            clients[name] = client
    backend_kind = str(config.get("exec_backend") or "").strip().lower()
    if backend_kind and backend_kind != "local":
        # Startup default for the sandbox exec backend (/sandbox flips it
        # per session). Construction only validates config (docker on
        # PATH, API key env present); the sandbox itself starts lazily on
        # the first command. Fail soft: a broken backend config degrades
        # to local execution with a visible warning, never a dead shell.
        from .execbackend import SandboxError, build_exec_backend

        try:
            backend = build_exec_backend(backend_kind, config)
        except SandboxError as exc:
            print(f"  \033[33m\u26a0 exec_backend disabled: {exc}\033[0m")
            backend = None
        if backend is not None:
            local_shell.set_exec_backend(backend)
    if str(config.get("llamaidx_url") or "").strip():
        # The llama-idx registry as a chat-queryable data source (fleet
        # status + selectable models). Read-only against the registry;
        # unset llamaidx_url = tool absent, zero new traffic. Stays
        # foundation-wired (not a product plugin): llamaidx.py is a
        # foundation module like public_apis.
        clients["llamaidx_registry"] = LlamaidxRegistryClient(config)
    import os

    api_layer_key = config.get("API_LAYER_KEY", "") or os.environ.get("API_LAYER_KEY", "")
    if api_layer_key:
        api_layer = ApiLayerClient()
        api_layer.bind(api_layer_key)
        clients["api_layer"] = api_layer
    return clients


def load_runtime_tools(builtin_clients: Dict[str, Any], use_cache: bool = False,
                       config: Optional[dict] = None):
    """Create MCP clients and assemble the session tool state.

    ``config`` is the conch config; it contributes config-keyed MCP
    endpoints (``capitol_docs_url``) on top of ``mcp.json``.
    """
    if use_cache:
        cached = mcp_mod.load_cached_tools()
        if cached is not None:
            mcp_clients = mcp_mod.mount_config_servers(mcp_mod.create_clients(), config)
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
    mcp_clients = mcp_mod.mount_config_servers(mcp_mod.create_clients(), config)
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
    mcp_clients, state = load_runtime_tools(
        builtins, use_cache=use_cached_tools, config=config
    )
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
