"""``python3 -m conch`` — the interactive shell, or a subcommand such as
``python3 -m conch keys rotate`` (the zero-assumption way to reach the
credential manager when no ``conch`` is on PATH)."""

from conch.app import main

if __name__ == "__main__":
    main()
