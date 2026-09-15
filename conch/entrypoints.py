"""Swarm console entrypoints.

``conch-edge`` is live as of Swarm Phase 1: the personal edge daemon that
owns the mission kernel and keeps missions running when the terminal
closes. It refuses to start unless ``edge_daemon=true`` is configured (or
``CONCH_EDGE_DAEMON=true``), so nothing changes for shell-only users.

``conch-hostctl`` and ``conch-worker`` are live as of Swarm Phase 2: the
on-host fleet control utility (install/probe/deploy/rollback/RPC relay)
and the bounded worker supervisor.

``conch-controller`` is live as of the fleet awakening: the supervised
fleet controller that drives the distributed task plane. It is gated on
``fleet_controller=true`` (or ``CONCH_FLEET_CONTROLLER=true``) exactly
the way conch-edge is gated. The interactive ``conch`` entrypoint is
untouched and never depends on any of these.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__


#: Exit status when the controller is not enabled in config (EX_CONFIG).
CONTROLLER_DISABLED_EXIT_CODE = 78


def controller_main(argv: Optional[List[str]] = None) -> int:
    """The fleet controller daemon (live: the fleet awakening)."""
    parser = argparse.ArgumentParser(
        prog="conch-controller",
        description=(
            "Conch fleet controller: owns the fleet kernel (worker "
            "registry, dispatches, grants) and drives the distributed "
            "task plane end to end — scheduling queued task envelopes "
            "onto eligible workers over SSH stdio RPC, lease/heartbeat "
            "sweeps, failure-class retries, cancellation propagation, "
            "and artifact pull. It coexists with conch-edge: separate "
            "kernel scope (~/.local/state/conch/fleet), separate lock "
            "and epoch, separate control socket. The documented default "
            "is to run it supervised: `conch-controller install` sets up "
            "launchd (macOS) or a systemd user unit (Linux)."
        ),
    )
    parser.add_argument(
        "--version", action="version",
        version=f"conch-controller {__version__}",
    )
    parser.add_argument(
        "--config", metavar="PATH", default="",
        help="Extra config file layered over the standard conch config.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single supervision tick and exit (smoke testing).",
    )
    parser.add_argument(
        "command", nargs="?", default="run", metavar="command",
        choices=("run", "install", "uninstall", "status"),
        help=(
            "run (default): run the controller in the foreground. "
            "install: set up and start the launchd/systemd-supervised "
            "daemon. uninstall: stop and remove it. status: supervisor "
            "state and controller health."
        ),
    )
    args = parser.parse_args(argv)
    from .config import get_bool, load_config

    config = load_config()
    if args.config:
        from pathlib import Path

        from .config import _parse_config_file

        config.update(_parse_config_file(Path(args.config)))
    if args.command == "status":
        from .fleet.controller import controller_status_cmd

        return controller_status_cmd(config)
    if args.command == "uninstall":
        from .fleet.controller import controller_uninstall_cmd

        return controller_uninstall_cmd(config)
    if not get_bool(config, "fleet_controller"):
        print(
            "conch-controller: the fleet controller is not enabled. Set "
            "`fleet_controller = true` in your conch config (or "
            "CONCH_FLEET_CONTROLLER=true) to let the controller drive "
            "fleet workers; the interactive shell is fully functional "
            "without it. See README \"Trusted SSH fleet\".",
            file=sys.stderr,
        )
        return CONTROLLER_DISABLED_EXIT_CODE
    if args.command == "install":
        from .fleet.controller import controller_install_cmd

        return controller_install_cmd(config)
    from .fleet.controller import run_controller_daemon

    return run_controller_daemon(config, once=args.once)


#: Exit status when the edge daemon is not enabled in config (EX_CONFIG).
EDGE_DISABLED_EXIT_CODE = 78


def edge_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conch-edge",
        description=(
            "Personal Conch edge daemon: owns the durable mission kernel, "
            "fires scheduled work sessions, answers channel messages, "
            "delivers notifications, and serves the shell's /missions "
            "attach surface over a local control socket. It keeps missions "
            "running when the terminal closes, sharing the shell's config "
            "and state. The documented default is to run it supervised: "
            "`conch-edge install` sets up launchd (macOS) or a systemd "
            "user unit (Linux) so it survives crashes and reboots."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"conch-edge {__version__}"
    )
    parser.add_argument(
        "--config", metavar="PATH", default="",
        help="Extra config file layered over the standard conch config.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single supervision tick and exit (smoke testing).",
    )
    parser.add_argument(
        "command", nargs="?", default="run", metavar="command",
        choices=("run", "install", "uninstall", "status"),
        help=(
            "run (default): run the daemon in the foreground. "
            "install: set up and start the launchd/systemd-supervised "
            "daemon (the default way to run it). uninstall: stop and "
            "remove the supervised daemon. status: supervisor state and "
            "daemon health."
        ),
    )
    args = parser.parse_args(argv)
    from .config import get_bool, load_config

    config = load_config()
    if args.config:
        from pathlib import Path

        from .config import _parse_config_file

        config.update(_parse_config_file(Path(args.config)))
    if args.command == "status":
        from .kernel.install import status_cmd

        return status_cmd(config)
    if args.command == "uninstall":
        from .kernel.install import uninstall_cmd

        return uninstall_cmd(config)
    if args.command in ("run", "install") and not get_bool(
        config, "edge_daemon"
    ):
        print(
            "conch-edge: the edge daemon is not enabled. Set "
            "`edge_daemon = true` in your conch config (or "
            "CONCH_EDGE_DAEMON=true) to let the daemon own scheduled "
            "tasks and missions; the interactive shell is fully "
            "functional without it. See README \"Edge daemon\".",
            file=sys.stderr,
        )
        return EDGE_DISABLED_EXIT_CODE
    if args.command == "install":
        from .kernel.install import install_cmd

        return install_cmd(config)
    from .kernel.daemon import run_edge_daemon

    return run_edge_daemon(config, once=args.once)


def worker_main(argv: Optional[List[str]] = None) -> int:
    """The bounded fleet worker supervisor (live as of Swarm Phase 2)."""
    from .fleet.worker import main as fleet_worker_main

    return fleet_worker_main(argv)


def hostctl_main(argv: Optional[List[str]] = None) -> int:
    """The on-host fleet control utility (live as of Swarm Phase 2).

    Delegates to :mod:`conch.fleet.hostctl`, which is stdlib-only and
    single-file so enrollment can bootstrap it onto bare hosts.
    """
    from .fleet.hostctl import main as fleet_hostctl_main

    return fleet_hostctl_main(argv)
