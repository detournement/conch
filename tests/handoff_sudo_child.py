"""Sudo-like pty child for handoff tests: prompt, read with echo off, repeat.

Not a test module; it is spawned by the terminal-handoff runner inside a
pseudo-terminal and reports exactly which bytes reached it, so tests can
assert the child receives clean credential entries and nothing else.
"""

import sys
import termios


def main():
    fd = sys.stdin.fileno()
    print("SUDOCHILD-START", flush=True)
    for attempt in range(3):
        sys.stdout.write("Password:")
        sys.stdout.flush()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        try:
            line = sys.stdin.readline()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        entry = line.rstrip("\n")
        sys.stdout.write(f"\nGOT{attempt}={entry!r}\n")
        sys.stdout.flush()
    print("SUDOCHILD-END", flush=True)


if __name__ == "__main__":
    main()
