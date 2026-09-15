"""Capitol's registrations against the foundation plugin seams.

This module is what the foundation actually imports (via
:func:`conch.plugins.load_builtin_plugins`); everything heavy —
``CapitolRuntime``, the supervisor, the drivers — stays behind lazy
imports inside the registered callables, so unconfigured installs never
load the adapter and the shell-first invariant holds.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..plugins import (
    Component,
    SlashCommand,
    register_component,
    register_daemon_service,
    register_mission_tool_provider,
    register_session_tool_provider,
    register_slash_command,
)


def _configured(config: dict) -> bool:
    return bool(str(config.get("capitol_base_url") or "").strip())


# ---------------------------------------------------------------------------
# Session tool: capitol_control for local sessions (bootstrap/tooling seam).
# ---------------------------------------------------------------------------

class CapitolSessionToolProvider:
    """The model-callable Capitol runtime surface (never admin).

    Config-gated on ``capitol_base_url``. Mission sessions swap this
    client out for the envelope-scoped one (mission provider below);
    remote turns swap in an origin-bound variant whose start proposes an
    approval.
    """

    def build_session_client(self, config: dict):
        if not _configured(config):
            return None
        from .tool import CapitolSessionClient

        return CapitolSessionClient(config)

    def tool_def(self) -> dict:
        from .tool import CAPITOL_SESSION_TOOL

        return CAPITOL_SESSION_TOOL


# ---------------------------------------------------------------------------
# Mission tool: envelope-scoped capitol_control (kernel engine seam).
# ---------------------------------------------------------------------------

class CapitolMissionToolProvider:
    """Mission authority is spec-derived: the interactive builtin is
    dropped by the engine, and the envelope-scoped tool exists only when
    the mission spec grants a capitol envelope at all."""

    def attach_mission_control(self, control, store, mission,
                               session_id: str, config: dict) -> None:
        spec = mission.get("spec") or {}
        capitol_spec = spec.get("capitol") or {}
        if capitol_spec.get("allow_start") or capitol_spec.get(
            "allow_respond"
        ):
            # Import stays lazy: missions without Capitol authority never
            # load the adapter (shell-first invariant).
            from .supervisor import CapitolControlClient

            control.capitol = CapitolControlClient(
                store, mission, session_id, config
            )

    def build_mission_tool(
        self, mission: Dict[str, Any], config: dict, control
    ) -> Optional[Tuple[dict, Any]]:
        client = getattr(control, "capitol", None)
        if client is None:
            return None
        from .supervisor import CAPITOL_CONTROL_TOOL

        return CAPITOL_CONTROL_TOOL, client


# ---------------------------------------------------------------------------
# Daemon service: Capitol run supervision on its own cadence.
# ---------------------------------------------------------------------------

class CapitolDaemonService:
    """One Capitol supervision pass per cadence inside the daemon tick.

    Unconfigured installs never import the adapter; a failing pass logs
    once per distinct error and retries on cadence — Capitol being down
    degrades bindings, never the daemon.
    """

    def __init__(self, store, config: dict, log=print, clock=None):
        import time

        self.store = store
        self.config = config
        self.log = log
        self.clock = clock or time.time
        self.supervisor = None
        self.last_poll = 0.0
        self._error = ""

    def _fully_configured(self) -> bool:
        return bool(
            str(self.config.get("capitol_base_url") or "").strip()
            and str(self.config.get("capitol_org") or "").strip()
            and str(self.config.get("capitol_agent") or "").strip()
        )

    def tick(self, stats: Dict[str, int]) -> None:
        if not self._fully_configured():
            return
        now = float(self.clock())
        try:
            poll_seconds = float(
                self.config.get("capitol_poll_seconds") or 10.0
            )
        except (TypeError, ValueError):
            poll_seconds = 10.0
        if now - self.last_poll < max(poll_seconds, 1.0):
            return
        self.last_poll = now
        try:
            if self.supervisor is None:
                from .supervisor import CapitolSupervisor

                self.supervisor = CapitolSupervisor(
                    self.store, self.config, log=self.log,
                    clock=self.clock,
                )
            capitol_stats = self.supervisor.tick()
            self._error = ""
            for key, value in capitol_stats.items():
                if value:
                    stats[f"capitol_{key}"] = (
                        stats.get(f"capitol_{key}", 0) + value
                    )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self._error:
                self._error = message
                self.log(f"capitol supervision error: {message}")


# ---------------------------------------------------------------------------
# Slash commands: /ebay, /capitol, /compile (dispatcher seam).
# ---------------------------------------------------------------------------

def _run_ebay(arg: str, config: dict):
    # eBay pilot (Capitol A2A): deterministic driver, no model in the
    # loop — imported lazily so sessions without Capitol config pay
    # nothing at startup. The flow itself is the ebay-listing flow pack
    # run by the generic engine (conch.capitol.packs).
    from .ebay import run_ebay_command

    run_ebay_command(arg, config)
    return None


def _run_capitol(arg: str, config: dict):
    # Generic Capitol control surface (A2Actrl parity): runtime reads
    # and steering over CapitolRuntime, gated admin over CapitolAdmin,
    # and the flow-pack surface — imported lazily like /ebay.
    from .commands import run_capitol_command

    run_capitol_command(arg, config)
    return None


def _run_compile(arg: str, config: dict):
    # ProcessCompiler (process-compiler plan C1/C2): goal → reviewed
    # Architecture Card → materialized, drilled, supervised process.
    # Interactive-only in v1; imported lazily like /capitol.
    from .compiler.commands import run_compile_command

    run_compile_command(arg, config, origin="local")
    return None


# ---------------------------------------------------------------------------
# Component: /install works (component seam).
# ---------------------------------------------------------------------------

def _component_status(config: dict) -> str:
    if not _configured(config):
        return "not configured — /install works to connect a Capitol org"
    return (f"configured — {config.get('capitol_org') or '?'} at"
            f" {config.get('capitol_base_url')}")


def _component_setup(config: dict) -> None:
    import getpass

    from ..config import set_config_values, set_env_values

    print(
        "\n  \033[1mWorks\033[0m — governed business workflows over the"
        " Capitol A2A\n  gateway: /capitol drives runs, /compile turns"
        " goals into reviewed\n  processes, and missions supervise runs"
        " from the edge daemon."
    )
    current = str(config.get("capitol_base_url") or "").strip()
    prompt = ("Capitol gateway base URL"
              + (f" [{current}]" if current else "") + ": ")
    try:
        base_url = input(f"  {prompt}").strip() or current
        if not base_url:
            print("  \033[2mNo gateway URL — leaving works unconfigured."
                  "\033[0m\n")
            return
        org = input("  Organization id: ").strip()
        agent = input("  Agent id: ").strip()
        # The bearer is a secret: hidden input, stored 0600 in the conch
        # env file (never echoed, never in the config file).
        bearer = getpass.getpass(
            "  A2A bearer token (Enter to keep/skip): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  \033[2mWorks setup cancelled.\033[0m\n")
        return
    updates = {"capitol_base_url": base_url}
    if org:
        updates["capitol_org"] = org
    if agent:
        updates["capitol_agent"] = agent
    path = set_config_values(updates)
    config.update(updates)
    if bearer:
        set_env_values({"CAPITOL_A2A_BEARER": bearer})
        print("  \033[1;32m✓ bearer stored\033[0m \033[2m(0600 env file)"
              "\033[0m")
    print(f"\n  \033[1;32m✓ Works configured.\033[0m \033[2m({path})"
          "\033[0m \033[2mTry /capitol card, /capitol workflows, or"
          " /compile <goal>.\033[0m\n")


register_component(Component(
    "works", "Works",
    "governed Capitol workflows: /capitol control, /compile, run"
    " supervision",
    _component_status, _component_setup,
))

register_session_tool_provider(
    "capitol_control", CapitolSessionToolProvider()
)
register_mission_tool_provider(
    "capitol_control", CapitolMissionToolProvider()
)
register_daemon_service(CapitolDaemonService)
register_slash_command(SlashCommand(
    "/ebay",
    "/ebay <photo...> [-- notes]",
    "eBay pilot: draft + publish a sandbox listing from photos (Capitol A2A)",
    _run_ebay,
    help_lines=(
        "  \033[1m/ebay <photo...> [-- notes]\033[0m  Draft + publish a sandbox eBay listing (Capitol A2A)\n",
    ),
))
register_slash_command(SlashCommand(
    "/capitol",
    "/capitol <subcommand> [...]",
    "Control Capitol workflows, runs, artifacts, and flow packs (A2Actrl parity; /capitol help)",
    _run_capitol,
    help_lines=(
        "  \033[1m/capitol <subcommand>\033[0m  Control Capitol workflows/runs/artifacts/packs (/capitol help)\n",
    ),
))
register_slash_command(SlashCommand(
    "/compile",
    "/compile <goal|subcommand> [...]",
    "ProcessCompiler: compile a goal into a reviewed Architecture Card, then materialize it (/compile help)",
    _run_compile,
    help_lines=(
        "  \033[1m/compile <goal>\033[0m      Compile a goal into a reviewed, materialized process (/compile help)\n",
    ),
))
