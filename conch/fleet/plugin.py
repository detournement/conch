"""Fleet's registrations against the foundation plugin seams.

Imported only by :func:`conch.plugins.load_builtin_plugins`; the fleet
runtime itself (client, delegate, authority) stays behind lazy imports
inside the registered callables, so non-fleet installs never load
``conch.fleet`` beyond this module.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..plugins import (
    Component,
    SlashCommand,
    register_component,
    register_mission_tool_provider,
    register_session_tool_provider,
    register_slash_command,
)


# ---------------------------------------------------------------------------
# Session tool: fleet_delegate for local sessions (bootstrap/tooling seam).
# ---------------------------------------------------------------------------

class FleetSessionToolProvider:
    """The model-callable fleet delegation surface (local sessions only:
    remote turns exclude it, workers deny-list it, delegated sub-turns
    don't inherit it implicitly). Gated on ``fleet_controller``."""

    def build_session_client(self, config: dict):
        from ..config import get_bool

        if not get_bool(config, "fleet_controller"):
            return None
        from .delegate import FleetDelegateClient

        return FleetDelegateClient(config)

    def tool_def(self) -> dict:
        from .delegate import FLEET_DELEGATE_TOOL

        return FLEET_DELEGATE_TOOL


# ---------------------------------------------------------------------------
# Mission tool: envelope-clamped fleet_delegate (kernel engine seam).
# ---------------------------------------------------------------------------

class FleetMissionToolProvider:
    """Fleet authority is spec-derived: the engine drops the interactive
    fleet_delegate (if any), and an envelope-scoped client — clamped to
    the mission's fleet block — replaces it only when the spec grants
    one."""

    def attach_mission_control(self, control, store, mission,
                               session_id: str, config: dict) -> None:
        return None

    def build_mission_tool(
        self, mission: Dict[str, Any], config: dict, control
    ) -> Optional[Tuple[dict, Any]]:
        spec = mission.get("spec") or {}
        fleet_spec = spec.get("fleet") or {}
        if not fleet_spec:
            return None
        from ..config import get_bool

        if not get_bool(config, "fleet_controller"):
            return None
        from .authority import mission_fleet_authority
        from .delegate import FLEET_DELEGATE_TOOL, FleetDelegateClient

        client = FleetDelegateClient(
            config,
            caller=mission_fleet_authority(fleet_spec),
            principal=str(spec.get("principal") or "user"),
            allowed_workers=fleet_spec.get("workers"),
            allowed_skills=fleet_spec.get("skills"),
        )
        return FLEET_DELEGATE_TOOL, client


# ---------------------------------------------------------------------------
# Slash command: /fleet (dispatcher seam).
# ---------------------------------------------------------------------------

def _run_fleet(arg: str, config: dict):
    from .commands import handle_fleet_command

    handle_fleet_command(arg, config)
    return None


# ---------------------------------------------------------------------------
# Component: /install fleet (component seam).
# ---------------------------------------------------------------------------

def _component_status(config: dict) -> str:
    from ..config import get_bool

    if not get_bool(config, "fleet_controller"):
        return "disabled — /install fleet to enable trusted SSH workers"
    return "enabled — /fleet status shows the plane"


def _component_setup(config: dict) -> None:
    from ..config import get_bool, set_config_values

    print(
        "\n  \033[1mFleet\033[0m — trusted SSH hosts running bounded,"
        " replaceable\n  workers: enroll a host once, then dispatch tasks"
        " with /fleet run\n  or the fleet_delegate tool."
    )

    def _confirm(prompt: str) -> bool:
        try:
            answer = input(f"  {prompt} [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("", "y", "yes")

    if not get_bool(config, "fleet_controller"):
        if not _confirm("Enable fleet_controller?"):
            print("  \033[2mLeft disabled.\033[0m\n")
            return
        path = set_config_values({"fleet_controller": "true"})
        config["fleet_controller"] = "true"
        print(f"  \033[1;32m✓ fleet_controller = true\033[0m"
              f" \033[2m({path})\033[0m")
    if _confirm("Install the supervised conch-controller daemon"
                " (recommended)?"):
        from .controller import controller_install_cmd

        code = controller_install_cmd(config)
        if code != 0:
            print(f"\n  \033[31mController install exited {code}\033[0m"
                  " \033[2m— run `conch-controller install` directly for"
                  " details.\033[0m")
    print("\n  \033[1;32m✓ Fleet ready.\033[0m \033[2mNext: /fleet enroll"
          " <name> <user@host> to add a trusted worker, then /fleet run."
          "\033[0m\n")


register_component(Component(
    "fleet", "Fleet",
    "trusted SSH workers: signed builds, bounded tasks, task plane",
    _component_status, _component_setup,
))


register_session_tool_provider("fleet_delegate", FleetSessionToolProvider())
register_mission_tool_provider("fleet_delegate", FleetMissionToolProvider())
register_slash_command(SlashCommand(
    "/fleet",
    "/fleet <workers|run|task|tasks|cancel|artifacts|drain|enable|grant"
    "|enroll|status> ...",
    "Drive the fleet: dispatch tasks to trusted SSH workers (/fleet help)",
    _run_fleet,
))
