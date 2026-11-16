"""PTY child exercising legacy multiline helpers and the live TerminalUI.

Not a test module; tests spawn it on a real controlling terminal so input,
approval overlays, and credential handoff use the actual editor lifecycle.
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


def run_ui_mode(mode):
    import tempfile
    import time

    from prompt_toolkit.history import FileHistory

    from conch.terminal_ui import TerminalUI, get_active_ui

    # Partial stdout must be flushed by handoff, not by line buffering.
    if mode == "handoff":
        sys.stdout = open(1, "w", buffering=1 << 20, closefd=False)

    results = {}
    with tempfile.TemporaryDirectory(prefix="conch-ui-pty-") as directory:
        history_file = os.path.join(directory, "history")
        ui = TerminalUI(commands=lambda: [], history_file=history_file).start()
        try:
            ui.set_queue_enabled(True)
            ui.set_busy(True)
            emit("READY")
            time.sleep(1.0)  # The harness submits queued text and/or a draft.

            if mode == "handoff":
                from conch.secure_terminal import (
                    DirectTerminalRunner,
                    TerminalHandoffPolicy,
                )

                sys.stdout.write("ERRFRAG:connect interactively first.")
                runner = DirectTerminalRunner(
                    TerminalHandoffPolicy(
                        local_session=True,
                        input_fn=input,
                        handoff_context=lambda: ui.handoff(discard=True),
                    )
                )
                child = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "handoff_sudo_child.py",
                )
                result = runner.run(
                    [sys.executable, child], description="Hand off?", timeout=30
                )
                results["RC="] = result.returncode
            elif mode == "nested-handoff":
                active = [get_active_ui() is ui]
                try:
                    with ui.handoff(discard=False):
                        active.append(get_active_ui() is not None)
                        with ui.handoff(discard=True):
                            active.append(get_active_ui() is not None)
                        active.append(get_active_ui() is not None)
                        raise RuntimeError("test handoff exception")
                except RuntimeError as error:
                    if str(error) != "test handoff exception":
                        raise
                active.append(get_active_ui() is ui)
                results["ACTIVE="] = active
            elif mode == "approval":
                results["ANSWER="] = ui.ask("APPROVAL-ONLY: ")

            ui.set_busy(False)
            emit("RESUMED-INPUT" if "handoff" in mode else "DRAINING")
            messages = []
            while True:
                message = ui.read_message()
                if message == "quit":
                    break
                messages.append(message)
            results["QUEUED="] = messages
        finally:
            ui.close()
        results["HIST="] = list(FileHistory(history_file + ".composer").load_history_strings())
    for tag, value in results.items():
        emit(tag, repr(value))


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

    elif mode in ("typeahead", "handoff", "nested-handoff", "approval"):
        run_ui_mode(mode)

    emit("DONE")


if __name__ == "__main__":
    main()
