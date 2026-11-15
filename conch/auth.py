"""User-owned native login flows. Conch never reads or stores seat tokens."""

from __future__ import annotations

import argparse
import subprocess
import sys

from .config import set_config_values
from .seat_common import SeatError, environment
from .seats import DEFAULT_MODELS, PROVIDERS, status, transport

SETUP_HELP = """First-time setup (Node.js/npm required for these install commands):
  Claude seat:
    npm install -g @anthropic-ai/claude-code
    conch auth login claude-code
    conch auth status claude-code
  ChatGPT seat:
    npm install -g @openai/codex
    conch auth login codex
    conch auth status codex

Choose one provider; each successful login selects Conch's saved default.
Status verifies native login, not model access. Run conch "Reply with ready."
to exercise model access and Conch's startup probe. Plan limits still apply.
No API key is required for a seat. API-key providers are billed separately.
Native installer alternatives: https://code.claude.com/docs/en/setup
                             https://developers.openai.com/codex/cli
"""


def login(provider: str) -> int:
    """Let the provider own browser sign-in, then select this seat in Conch."""
    try:
        result = subprocess.run(transport(provider).auth_command("login"),
                                env=environment(), check=False)
    except FileNotFoundError:
        print(f"conch: the official CLI for {provider} is not installed or is not on PATH.\n"
              f"{SETUP_HELP}", file=sys.stderr)
        return 1
    except (OSError, SeatError) as exc:
        print(f"conch: login failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    if result.returncode:
        return result.returncode
    ok, detail = status(provider)
    if not ok:
        print(f"conch: {detail}", file=sys.stderr)
        return 1
    model = DEFAULT_MODELS[provider]
    path = set_config_values({"provider": provider, "model": model,
                              "chat_model": model, "api_key_env": ""})
    print(f"conch: selected {provider}/{model} in {path}; credentials remain with the provider CLI")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="conch auth", description=__doc__, epilog=SETUP_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", choices=("login", "status", "logout"))
    parser.add_argument("provider", choices=PROVIDERS, nargs="?")
    args = parser.parse_args(argv)
    if args.action != "status" and not args.provider:
        parser.error("login and logout require a provider: claude-code or codex")
    if args.action == "login":
        return login(args.provider)
    if args.action == "status":
        failed = False
        for provider in (args.provider,) if args.provider else PROVIDERS:
            ok, detail = status(provider)
            print(f"{provider}: {detail}")
            failed |= not ok
        return int(failed)
    try:
        return subprocess.run(transport(args.provider).auth_command("logout"),
                              env=environment(), check=False).returncode
    except (OSError, SeatError) as exc:
        print(f"conch: logout failed ({type(exc).__name__})", file=sys.stderr)
        return 1
