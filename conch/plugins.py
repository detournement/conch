"""Plugin seams: how product packages plug into the foundation.

The import direction is the whole point. The foundation (shell, session,
tooling, bootstrap, commands) and the mission kernel never import the
product packages (``conch.capitol``, ``conch.fleet``); instead each
product ships a ``plugin`` module that registers its integration points
here, and the foundation iterates the registries at its seams:

- **slash commands** — products contribute ``/capitol``-style commands
  to the dispatcher, completion, and ``/help``;
- **session tool providers** — config-gated model-callable tools for
  local sessions (``capitol_control`` when Capitol is configured,
  ``fleet_delegate`` when ``fleet_controller`` is on);
- **mission tool providers** — spec-derived, envelope-scoped tools for
  kernel mission sessions (interactive builtins are dropped and only
  re-added when the mission spec grants the authority);
- **daemon services** — supervision passes run inside the edge-daemon
  tick on their own cadence (Capitol run supervision).

Registration is cheap: plugin modules import nothing heavy at module
scope and lazy-import their product code inside the registered
callables, preserving the shell-first invariant (unconfigured installs
never load the adapters, and the classic shell never imports
``conch.kernel``).

This module must never import ``conch.kernel`` or any product package
at module scope: plugin modules are named as strings and imported only
by :func:`load_builtin_plugins`.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional, Tuple

# Product plugin modules, in registration (= tool/command listing) order.
# A missing module is a missing product, not an error: when the packages
# split into separate distributions, absence simply means the feature
# set is not installed.
BUILTIN_PLUGIN_MODULES = (
    "conch.capitol.plugin",
    "conch.fleet.plugin",
)

_loaded = False


def load_builtin_plugins() -> None:
    """Import the product plugin modules (idempotent, fail-soft).

    Importing a plugin module runs its registrations. Each module keeps
    its product imports lazy, so this is cheap and never pulls in the
    kernel or the product adapters themselves.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in BUILTIN_PLUGIN_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            continue


# ---------------------------------------------------------------------------
# Slash commands: products contribute commands instead of being imported
# by the shell dispatcher.
# ---------------------------------------------------------------------------

class SlashCommand:
    """One product-contributed slash command.

    ``spec``/``description`` feed the command registry (completion and
    the conch_introspect capabilities report); ``help_lines`` are
    preformatted lines for the ``/help`` screen; ``handler(arg, config)``
    follows the dispatcher protocol (return ``None`` or an action).
    """

    def __init__(self, name: str, spec: str, description: str,
                 handler: Callable[[str, dict], Any],
                 help_lines: Tuple[str, ...] = ()):
        self.name = name
        self.spec = spec
        self.description = description
        self.handler = handler
        self.help_lines = help_lines


_slash_commands: "Dict[str, SlashCommand]" = {}


def register_slash_command(command: SlashCommand) -> None:
    _slash_commands[command.name] = command


def slash_commands() -> List[SlashCommand]:
    return list(_slash_commands.values())


def slash_handler(name: str) -> Optional[Callable[[str, dict], Any]]:
    command = _slash_commands.get(name)
    return command.handler if command else None


# ---------------------------------------------------------------------------
# Session tool providers: config-gated builtin tools for local sessions.
# ---------------------------------------------------------------------------

_session_tool_providers: "Dict[str, Any]" = {}


def register_session_tool_provider(name: str, provider: Any) -> None:
    """``provider.build_session_client(config)`` returns a client or
    ``None`` (the config gate); ``provider.tool_def()`` returns the tool
    schema (lazy import inside)."""
    _session_tool_providers[name] = provider


def session_tool_providers() -> List[Tuple[str, Any]]:
    return list(_session_tool_providers.items())


# ---------------------------------------------------------------------------
# Mission tool providers: spec-derived tools for kernel mission sessions.
# ---------------------------------------------------------------------------

_mission_tool_providers: "Dict[str, Any]" = {}


def register_mission_tool_provider(name: str, provider: Any) -> None:
    """``name`` is the tool name the provider owns: the mission engine
    drops any interactive builtin with that name before asking
    ``provider.build_mission_tool(mission, config, control)`` for the
    envelope-scoped replacement (``None`` when the spec grants nothing).
    ``provider.attach_mission_control(control, store, mission,
    session_id, config)`` runs earlier, when the engine builds the
    mission control client."""
    _mission_tool_providers[name] = provider


def mission_tool_providers() -> List[Tuple[str, Any]]:
    return list(_mission_tool_providers.items())


# ---------------------------------------------------------------------------
# Daemon services: supervision passes inside the edge-daemon tick.
# ---------------------------------------------------------------------------

_daemon_service_factories: "List[Callable[..., Any]]" = []


def register_daemon_service(factory: Callable[..., Any]) -> None:
    """``factory(store, config, log=..., clock=...)`` returns a service
    whose ``tick(stats)`` runs one pass (the service owns its own config
    gate, cadence, and error handling — a failing service degrades its
    product, never the daemon)."""
    _daemon_service_factories.append(factory)


def daemon_service_factories() -> List[Callable[..., Any]]:
    return list(_daemon_service_factories)
