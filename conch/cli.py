"""Single-shot ask entrypoint."""

from __future__ import annotations

import sys

from .app import main as chat_main


def main():
    """Fallback ask implementation.

    For now this routes through chat's one-shot mode, which keeps installs simple
    until a dedicated ask runtime is split out.
    """
    if len(sys.argv) <= 1:
        print("conch-ask: provide a prompt", file=sys.stderr)
        sys.exit(1)
    chat_main()

