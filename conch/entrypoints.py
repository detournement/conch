"""Dormant swarm console entrypoints (Swarm Phase 0).

``conch-controller``, ``conch-edge``, ``conch-worker``, and ``conch-hostctl``
are installed from day one so packaging, docs, and deployment scripts can
reference stable command names, but every one of them currently prints a
clear "not yet enabled" message and exits nonzero. Later phases replace the
``_dormant`` call with the real daemon/utility main while keeping the
argparse surface built here.

The interactive ``conch`` entrypoint is untouched and never depends on any
of these.
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


def edge_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        "conch-edge",
        "Personal Conch edge daemon: keeps missions running when the "
        "terminal closes, sharing the shell's config and state (dormant).",
    )
    parser.parse_args(argv)
    return _dormant("conch-edge", "the personal edge daemon")


def worker_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        "conch-worker",
        "Bounded Conch fleet worker: executes versioned task envelopes "
        "received over SSH stdin/stdout (dormant).",
    )
    parser.parse_args(argv)
    return _dormant("conch-worker", "the fleet worker runtime")


def hostctl_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        "conch-hostctl",
        "Conch host control utility: enrollment, capability probing, "
        "artifact deployment, and rollback on fleet hosts (dormant).",
    )
    parser.parse_args(argv)
    return _dormant("conch-hostctl", "the host control utility")
