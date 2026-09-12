"""Pty child used by test_multiline_pty: exercises the real readline/tty
paths of conch.multiline and prints machine-parseable markers on stdout.

Not a test module (unittest discovery only picks up test_*.py); it is
spawned inside a pseudo-terminal so readline/libedit and the termios drain
run exactly as they do in an interactive session.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conch import multiline  # noqa: E402


def emit(tag, value=""):
    sys.stdout.write("%s%s\n" % (tag, value))
    sys.stdout.flush()


def _acquire_controlling_tty():
    """Adopt the test pty as this session's controlling terminal.

    The harness spawns the driver with start_new_session=True so that pty
    keyboard signals (^C) stay inside this session instead of leaking into
    the test runner's process group. A fresh session has no controlling
    terminal, so claim the pty explicitly — otherwise its line discipline
    has no foreground process group to deliver SIGINT to.
    """
    try:
        import fcntl
        import termios

        if hasattr(termios, "TIOCSCTTY"):
            fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
        else:  # BSD fallback: first tty open by a session leader acquires it
            os.close(os.open(os.ttyname(sys.stdin.fileno()), os.O_RDWR))
        os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())
    except OSError:
        pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    _acquire_controlling_tty()
    emit("BACKEND=", multiline.readline_backend())
    emit("BRACKETED=", str(multiline.enable_bracketed_paste()))

    if mode == "main":
        import readline

        readline.clear_history()
        emit("READY")
        while True:
            try:
                msg = multiline.read_user_message("you: ")
            except EOFError:
                break
            except KeyboardInterrupt:
                emit("KBDINT")
                continue
            if msg is None:
                emit("CANCELLED")
                continue
            emit("MSG=", repr(msg))
            if msg == "quit":
                break
        n = readline.get_current_history_length()
        emit("HIST=", repr([readline.get_history_item(i) for i in range(1, n + 1)]))

    elif mode == "paste":
        emit("READY")
        block = multiline.read_paste_block()
        emit("PASTE=", repr(block))

    elif mode == "typeahead":
        import time

        from conch.app import TypeaheadBuffer

        buf = TypeaheadBuffer()
        buf.start()
        emit("READY")
        time.sleep(1.0)
        partial = buf.stop()
        emit("QUEUED=", repr(buf.get_queued()))
        emit("PARTIAL=", repr(partial))

    emit("DONE")


if __name__ == "__main__":
    main()
