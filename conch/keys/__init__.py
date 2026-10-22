"""``conch keys`` — one place to see, verify, rotate, and reload every
credential conch uses.

The problem it solves: after a key rotation, a user (or a colleague who
just ran ``git pull``) must be able to run **one command** and end up
green, no matter how their machine is set up — the interactive shell,
``conch-ask``, the edge daemon under launchd/systemd, workers, and MCP
server launches all have to see the new key, and anything still stale
has to be named with its exact location and fix.

    conch keys                 # inventory: name, consumer, source, fingerprint, status
    conch keys check           # live-verify every key (shell + daemon columns)
    conch keys set NAME        # prompt (never echoed), verify, save 0600, reload
    conch keys rotate          # doctor → check → prompt for what failed → verify → reload
    conch keys doctor          # install/PATH/shell-profile/daemon weirdness, explained
    conch keys migrate-mcp     # move inline mcp.json secrets into the env file as ${NAME}

Design rules (house style): fail closed, fail loud, credentials by
reference. Nothing in this package prints, logs, or stores a secret
value — names, locations, and sha256 fingerprints only. The single
source of truth is ``~/.config/conch/env`` (see
:func:`conch.config.load_env_file` for the precedence rule); this
package is stdlib-only and never imports a product package.
"""

from __future__ import annotations

from .cli import main

__all__ = ["main"]
