"""Swarm console entrypoints.

``conch-edge`` is live as of Swarm Phase 1: the personal edge daemon that
owns the mission kernel and keeps missions running when the terminal
closes. It refuses to start unless ``edge_daemon=true`` is configured (or
``CONCH_EDGE_DAEMON=true``), so nothing changes for shell-only users.

``conch-hostctl`` and ``conch-worker`` are live as of Swarm Phase 2: the
on-host fleet control utility (install/probe/deploy/rollback/RPC relay)
and the bounded worker supervisor. ``conch-controller`` remains dormant:
it prints a clear "not yet enabled" message and exits nonzero until its
phase lands. The interactive ``conch`` entrypoint is untouched and never
depends on any of these.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__

#: Exit status for a dormant entrypoint (EX_UNAVAILABLE from sysexits.h).
DORMANT_EXIT_CODE = 69

_DORMANT_MESSAGE = (
    "{prog}: not yet enabled in this build — {role} ships in a later phase "
    "of the Conch swarm roadmap. The interactive `conch` shell is fully "
    "functional without it; see PLAN.md (\"Swarm Phase 0\") for status."
)


def _build_parser(prog: str, description: str) -> argparse.ArgumentParser:
    """Shared argparse plumbing: later phases add subcommands to this."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog=(
            "This entrypoint is currently dormant; it parses arguments and "
            "exits. See PLAN.md for the roadmap."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{prog} {__version__}",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default="",
        help="Config file override (accepted now, used once enabled).",
    )
    return parser


def _dormant(prog: str, role: str) -> int:
    print(_DORMANT_MESSAGE.format(prog=prog, role=role), file=sys.stderr)
    return DORMANT_EXIT_CODE


def controller_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        "conch-controller",
        "Conch organizational controller: durable mission kernel, policy, "
        "budgets, approvals, and fleet scheduling (dormant).",
    )
    parser.parse_args(argv)
    return _dormant("conch-controller", "the mission controller daemon")


#: Exit status when the edge daemon is not enabled in config (EX_CONFIG).
EDGE_DISABLED_EXIT_CODE = 78


def edge_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conch-edge",
        description=(
            "Personal Conch edge daemon: owns the durable mission kernel, "
            "fires scheduled work sessions, delivers notifications, and "
            "serves the shell's /missions attach surface over a local "
            "control socket. It keeps missions running when the terminal "
            "closes, sharing the shell's config and state."
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
    args = parser.parse_args(argv)
    from .config import get_bool, load_config

    config = load_config()
    if args.config:
        from pathlib import Path

        from .config import _parse_config_file

        config.update(_parse_config_file(Path(args.config)))
    if not get_bool(config, "edge_daemon"):
        print(
            "conch-edge: the edge daemon is not enabled. Set "
            "`edge_daemon = true` in your conch config (or "
            "CONCH_EDGE_DAEMON=true) to let the daemon own scheduled "
            "tasks and missions; the interactive shell is fully "
            "functional without it. See README \"Edge daemon\".",
            file=sys.stderr,
        )
        return EDGE_DISABLED_EXIT_CODE
    from .kernel.daemon import run_edge_daemon

    return run_edge_daemon(config, once=args.once)


def worker_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        "conch-worker",
        "Bounded Conch fleet worker: executes versioned task envelopes "
        "received over SSH stdin/stdout (dormant).",
    )
    parser.parse_args(argv)
    return _dormant("conch-worker", "the fleet worker runtime")


def hostctl_main(argv: Optional[List[str]] = None) -> int:
    """The on-host fleet control utility (live as of Swarm Phase 2).

    Delegates to :mod:`conch.fleet.hostctl`, which is stdlib-only and
    single-file so enrollment can bootstrap it onto bare hosts.
    """
    from .fleet.hostctl import main as fleet_hostctl_main

    return fleet_hostctl_main(argv)
