"""User-owned native login flows. Conch never reads or stores seat tokens."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from typing import Tuple

from .config import set_config_values
from .seat_common import SeatError, environment
from .seats import DEFAULT_MODELS, PROVIDERS, status, transport

# provider -> (seat name, official CLI install command, native installer docs).
SEAT_SETUP = {
    "claude-code": ("Claude seat", "npm install -g @anthropic-ai/claude-code",
                    "https://code.claude.com/docs/en/setup"),
    "codex": ("ChatGPT seat", "npm install -g @openai/codex",
              "https://developers.openai.com/codex/cli"),
}

SETUP_HELP = """First-time setup (Node.js/npm required for these install commands):
{seats}
Choose one provider; each successful login selects Conch's saved default.
Status verifies native login, not model access. Run conch "Reply with ready."
to exercise model access and Conch's startup probe. Plan limits still apply.
No API key is required for a seat. API-key providers are billed separately.
Native installer alternatives: {docs}
""".format(
    seats="".join(
        f"  {name}:\n    {install}\n    conch auth login {provider}\n"
        f"    conch auth status {provider}\n"
        for provider, (name, install, _docs) in SEAT_SETUP.items()
    ),
    docs=("\n" + " " * 29).join(docs for _name, _install, docs in SEAT_SETUP.values()),
)


def install_hint(provider: str) -> Tuple[str, str]:
    """(install command, native installer docs URL) for one seat provider."""
    _name, install, docs = SEAT_SETUP[provider]
    return install, docs


def cli_installed(provider: str) -> bool:
    """True when the provider's official CLI is on the PATH its transport uses."""
    binary = transport(provider).auth_command("status")[0]
    return shutil.which(binary, path=environment().get("PATH")) is not None


def select(provider: str) -> Tuple[str, str]:
    """Make this seat Conch's saved default; returns (model, config path)."""
    model = DEFAULT_MODELS[provider]
    path = set_config_values({"provider": provider, "model": model,
                              "chat_model": model, "api_key_env": ""})
    return model, path


def sign_in(provider: str) -> Tuple[int, str]:
    """Let the provider own browser sign-in; selecting the seat is the caller's.

    Returns (0, "") once native status reports ready, otherwise (exit code,
    reason). The reason is empty when the native CLI exited non-zero and has
    already explained itself. FileNotFoundError propagates when the official
    CLI is not installed, so each caller can word its own install help.
    """
    try:
        result = subprocess.run(transport(provider).auth_command("login"),
                                env=environment(), check=False)
    except FileNotFoundError:
        raise
    except (OSError, SeatError) as exc:
        return 1, f"login failed ({type(exc).__name__})"
    if result.returncode:
        return result.returncode, ""
    ok, detail = status(provider)
    return (0, "") if ok else (1, detail)


def login(provider: str) -> int:
    """Let the provider own browser sign-in, then select this seat in Conch."""
    try:
        code, reason = sign_in(provider)
    except FileNotFoundError:
        print(f"conch: the official CLI for {provider} is not installed or is not on PATH.\n"
              f"{SETUP_HELP}", file=sys.stderr)
        return 1
    if code:
        if reason:
            print(f"conch: {reason}", file=sys.stderr)
        return code
    model, path = select(provider)
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
