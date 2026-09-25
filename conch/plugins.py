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
import os
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Product plugin modules, in registration (= tool/command listing) order.
# A missing module is a missing product, not an error: when the packages
# split into separate distributions, absence simply means the feature
# set is not installed.
BUILTIN_PLUGIN_MODULES = (
    "conch.fleet.plugin",
)
SOURCE_CHECKOUT_PLUGIN_MODULES = ("conch.capitol.plugin",)
PLUGIN_ENTRYPOINT_GROUP = "conch.plugins"

_loaded = False
_loaded_entrypoints: "set[str]" = set()
_plugin_errors: "Dict[str, str]" = {}


def _is_source_checkout() -> bool:
    """The monorepo keeps bundled products usable during the split.

    Wheels intentionally do not auto-activate that compatibility copy:
    installing the ``conch-works`` entry point is what enables Works.
    """
    root = Path(__file__).resolve().parents[1]
    disabled = str(
        os.environ.get("CONCH_DISABLE_BUNDLED_WORKS") or ""
    ).lower() in ("1", "true", "yes", "on")
    return not disabled and (root / ".git").exists()


def _entrypoints():
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=PLUGIN_ENTRYPOINT_GROUP))
    return list(discovered.get(PLUGIN_ENTRYPOINT_GROUP, ()))


def load_builtin_plugins(*, refresh: bool = False) -> None:
    """Import the product plugin modules (idempotent, fail-soft).

    Importing a plugin module runs its registrations. Each module keeps
    its product imports lazy, so this is cheap and never pulls in the
    kernel or the product adapters themselves.
    """
    global _loaded
    if _loaded and not refresh:
        return
    _loaded = True
    entrypoints = _entrypoints()
    modules = list(BUILTIN_PLUGIN_MODULES)
    if _is_source_checkout() and not any(
        entrypoint.name == "works" for entrypoint in entrypoints
    ):
        modules.extend(SOURCE_CHECKOUT_PLUGIN_MODULES)
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            _plugin_errors[name] = f"ImportError: {exc}"
            continue
    for entrypoint in entrypoints:
        identity = f"{entrypoint.name}={entrypoint.value}"
        if identity in _loaded_entrypoints:
            continue
        try:
            entrypoint.load()
        except Exception as exc:
            _plugin_errors[identity] = f"{type(exc).__name__}: {exc}"
            continue
        _loaded_entrypoints.add(identity)
        _plugin_errors.pop(identity, None)


def loaded_plugin_entrypoints() -> Tuple[str, ...]:
    return tuple(sorted(_loaded_entrypoints))


def plugin_errors() -> Dict[str, str]:
    return dict(_plugin_errors)


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
# Components: what /install lists, enables, and sets up.
# ---------------------------------------------------------------------------

class Component:
    """One installable conch component (edge, fleet, works, ...).

    v1 reality: every component ships inside the conch-shell
    distribution behind config gates, so "install" means enable +
    configure + set up its daemon or credentials. When the packages
    split into separate distributions, the registered callables swap to
    real per-package installs without the ``/install`` surface changing.

    ``status(config)`` must be one cheap line with no product imports;
    ``setup(config)`` is the interactive enable/configure/install flow
    (lazy product imports inside; prompts allowed). A setup accepting a
    second parameter receives the remaining ``/install`` tokens, so a
    component can offer sub-steps (``/install capture browser``).
    """

    def __init__(self, name: str, title: str, summary: str,
                 status: Callable[[dict], str],
                 setup: Callable[[dict], None]):
        self.name = name
        self.title = title
        self.summary = summary
        self.status = status
        self.setup = setup


_components: "Dict[str, Component]" = {}


def register_component(component: Component) -> None:
    _components[component.name] = component


def components() -> List[Component]:
    return list(_components.values())


# Skill package-data providers. Product distributions register directories;
# the foundation loader reads them before user skills (which still win).
_skill_directories: "Dict[str, Callable[[], Path]]" = {}


def register_skill_directory(
    name: str,
    provider: Callable[[], Path],
) -> None:
    _skill_directories[str(name)] = provider


def skill_directories() -> List[Path]:
    directories: List[Path] = []
    for name, provider in sorted(_skill_directories.items()):
        try:
            path = Path(provider())
        except Exception as exc:
            _plugin_errors[f"skills:{name}"] = (
                f"{type(exc).__name__}: {exc}"
            )
            continue
        directories.append(path)
    return directories


# ---------------------------------------------------------------------------
# Daemon services: supervision passes inside the edge-daemon tick.
# ---------------------------------------------------------------------------

_folder_handler_factories: "Dict[str, Callable[..., Any]]" = {}


def register_folder_handler(kind: str, factory: Callable[..., Any]) -> None:
    """Register a folder-watch handler binding (e.g. ``pack``).

    The generic folder-watch service (kernel-side) resolves a watch's
    ``handler = <kind>:<target>`` binding through this registry, so
    products supply drop handlers without the kernel importing them.
    A factory is called as ``factory(target, store, config, log)`` and
    returns a handler object with:

    - ``accepts()`` → dict(extensions, max_bytes, max_count,
      notes_sidecar) describing what the watch admits;
    - ``handle_drop(watch, drop_id, attachments, notes, notify)``
      → optional reply text;
    - optionally ``handle_verb(payload, notify)`` for continuation
      verbs (answers, approvals) routed from the shell.
    """
    _folder_handler_factories[str(kind)] = factory


def folder_handler_factory(kind: str) -> Optional[Callable[..., Any]]:
    return _folder_handler_factories.get(str(kind))


_daemon_service_factories: "List[Callable[..., Any]]" = []


def register_daemon_service(factory: Callable[..., Any]) -> None:
    """``factory(store, config, log=..., clock=...)`` returns a service
    whose ``tick(stats)`` runs one pass (the service owns its own config
    gate, cadence, and error handling — a failing service degrades its
    product, never the daemon)."""
    _daemon_service_factories.append(factory)


def daemon_service_factories() -> List[Callable[..., Any]]:
    return list(_daemon_service_factories)
