"""Single-shot ask entrypoint."""

from __future__ import annotations

import argparse
import sys

from .llm import ask


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        hint = ""
        if "unrecognized arguments" in message:
            hint = (" (put -- before a request whose first word starts"
                    " with a dash)")
        print(f"{self.prog}: error: {message}{hint}", file=sys.stderr)
        sys.exit(2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="conch-ask",
        usage="conch-ask [--] <request ...>",
        description=(
            "Turn a natural-language request into one shell command."
            " The command is\nprinted, never run; paste or edit it"
            " yourself. Uses the same provider and\nkey configuration as"
            " `conch` (~/.config/conch/config and env)."
        ),
        epilog=(
            "Options are recognised only before the request. Put -- before"
            " a request\nwhose first word starts with a dash."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "-h", "--help", action="store_true",
        help="Show this help and exit.",
    )
    parser.add_argument(
        "-V", "--version", action="store_true",
        help="Show the version and exit.",
    )
    return parser


def split_leading_options(argv):
    """(option tokens, request words); ``--`` ends the options."""
    for index, token in enumerate(argv):
        if token == "--":
            return list(argv[:index]), list(argv[index + 1:])
        if not (token.startswith("-") and len(token) > 1):
            return list(argv[:index]), list(argv[index:])
    return list(argv), []


def main(argv=None):
    """Return one shell command for the given request."""
    argv = list(sys.argv[1:] if argv is None else argv)
    leading, words = split_leading_options(argv)
    parser = build_arg_parser()
    options = parser.parse_args(leading)
    if options.version:
        from . import __version__
        print(f"conch-ask {__version__}")
        return
    if options.help:
        print(parser.format_help(), end="")
        return
    request = " ".join(words).strip()
    if not request:
        print("conch-ask: provide a request (try --help)", file=sys.stderr)
        sys.exit(1)
    try:
        cmd = ask(request)
    except Exception as exc:
        # The startup model check failed closed (conch-ask never prompts;
        # only a pre-approved fallback_models entry may take over). The
        # import stays lazy so the happy path never loads the shell wiring.
        from .bootstrap import StartupError

        if not isinstance(exc, StartupError):
            raise
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)
    if not cmd:
        print("conch: [no response]", file=sys.stderr)
        sys.exit(1)
    print(cmd)
