#!/usr/bin/env python3
"""
Conch ask: read natural-language request from args or stdin, print one shell command.
Usage:
  conch-ask "find files containing n22 in the name"
  echo "list largest 10 files" | conch-ask
"""
import os
import signal
import sys

from .llm import ask

# Hard process timeout so we never hang indefinitely (e.g. DNS/SSL stall)
def _timeout_handler(signum: object, frame: object) -> None:
    print("conch-ask: timed out after 25s (API or network)", file=sys.stderr)
    sys.exit(124)


def main() -> None:
    if sys.platform != "win32":
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(25)
    try:
        _run()
    finally:
        if sys.platform != "win32":
            signal.alarm(0)


def _run() -> None:
    if len(sys.argv) > 1:
        request = " ".join(sys.argv[1:])
    else:
        request = sys.stdin.read().strip()
    if not request:
        print("conch-ask: give a request as args or stdin", file=sys.stderr)
        sys.exit(1)
    context = {}
    if os.environ.get("PWD"):
        context["cwd"] = os.environ["PWD"]
    if os.environ.get("CONCH_OS_SHELL"):
        context["os_shell"] = os.environ["CONCH_OS_SHELL"]
    if os.environ.get("CONCH_HISTORY"):
        context["history"] = os.environ["CONCH_HISTORY"]
    cmd = ask(request, context)
    if cmd:
        print(cmd, flush=True)
    else:
        print("conch-ask: no command in response", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
