"""Fleet's registrations against the foundation plugin seams.

Imported only by :func:`conch.plugins.load_builtin_plugins`; the fleet
runtime itself (client, delegate, authority) stays behind lazy imports
inside the registered callables, so non-fleet installs never load
``conch.fleet`` beyond this module.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..plugins import (
    SlashCommand,
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


register_session_tool_provider("fleet_delegate", FleetSessionToolProvider())
register_mission_tool_provider("fleet_delegate", FleetMissionToolProvider())
register_slash_command(SlashCommand(
    "/fleet",
    "/fleet <workers|run|task|tasks|cancel|artifacts|drain|enable|grant"
    "|enroll|status> ...",
    "Drive the fleet: dispatch tasks to trusted SSH workers (/fleet help)",
    _run_fleet,
))
