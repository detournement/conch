"""Single-shot ask entrypoint."""

from __future__ import annotations

import sys

from .llm import ask


def main():
    """Return one shell command for the given request."""
    if len(sys.argv) <= 1:
        print("conch-ask: provide a prompt", file=sys.stderr)
        sys.exit(1)
    request = " ".join(sys.argv[1:]).strip()
    if not request:
        print("conch-ask: provide a prompt", file=sys.stderr)
        sys.exit(1)
    cmd = ask(request)
    if not cmd:
        print("conch: [no response]", file=sys.stderr)
        sys.exit(1)
    print(cmd)

