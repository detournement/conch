"""Pty-driven integration tests for multiline input.

These spawn tests/multiline_pty_driver.py inside a real pseudo-terminal so
readline/libedit, the termios paste drain, and the raw /paste reader run
exactly as in an interactive session. The bracketed-paste test only runs
where the backend actually supports it (GNU readline 8.1+) and skips
cleanly on libedit (macOS system Pythons).
"""

import ast
import os
import select
import struct
import subprocess
import sys
import threading
import time
import unittest

try:
    import fcntl
    import termios

    HAVE_PTY = hasattr(os, "openpty")
except ImportError:  # pragma: no cover - non-POSIX platforms
    HAVE_PTY = False

DRIVER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "multiline_pty_driver.py"
)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Typed-line sends must be spaced past the paste probe window so they are
# not mistaken for paste chunks; paste sends go out in a single write.
TYPE_GAP_S = 0.15


class PtySession:
    def __init__(self, mode):
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", PYTHONUNBUFFERED="1")
        # start_new_session keeps pty keyboard signals (^C) inside the
        # driver's own session; the driver then claims the pty as its
        # controlling terminal so SIGINT delivery works like a real shell.
        self.proc = subprocess.Popen(
            [sys.executable, DRIVER, mode],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            cwd=REPO_ROOT,
            start_new_session=True,
        )
        os.close(slave)
        self._out = bytearray()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        # Drain the master continuously, like a real terminal emulator does.
        # libedit engages the terminal with a drain-style tcsetattr that
        # blocks until pending pty output is consumed; a harness that stops
        # reading between sends would wedge the child mid-prompt (verified).
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        while not self._closed.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._out.extend(data)

    @property
    def out(self):
        with self._lock:
            return bytes(self._out)

    def wait_for(self, marker, timeout=10.0, count=1):
        m = marker.encode() if isinstance(marker, str) else marker
        end = time.time() + timeout
        while True:
            if self.out.count(m) >= count:
                return True
            if time.time() >= end:
                return False
            time.sleep(0.02)

    def send(self, data):
        os.write(self.master, data if isinstance(data, bytes) else data.encode())

    def transcript(self):
        return self.out.decode("utf-8", "replace")

    def parsed(self, tag):
        values = []
        for line in self.transcript().splitlines():
            if line.startswith(tag):
                values.append(ast.literal_eval(line[len(tag):]))
        return values

    def close(self):
        try:
            self.proc.terminate()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait()
        self._closed.set()
        self._reader.join(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass


@unittest.skipUnless(HAVE_PTY, "POSIX pty support required")
class PtyTestCase(unittest.TestCase):
    def session(self, mode):
        s = PtySession(mode)
        self.addCleanup(s.close)
        if not s.wait_for("READY"):
            self.fail("driver never became ready:\n" + s.transcript())
        return s

    def finish(self, s):
        """End a main-mode session and return its MSG= values."""
        s.send(b"quit\r")
        self.assertTrue(s.wait_for("DONE"), s.transcript())
        return s.parsed("MSG=")


class PromptFlowTests(PtyTestCase):
    def test_typed_single_line(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"hello\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs, ["hello", "quit"])

    def test_pasted_two_lines_yield_one_message(self):
        # Regression: a two-line paste used to submit on the first newline
        # and fire the second line as a separate input.
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"alpha one\rbeta two\r")  # one write, like a terminal paste
        self.assertTrue(s.wait_for("... "), s.transcript())
        s.send(b"\r")  # the single Enter that sends the block
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs, ["alpha one\nbeta two", "quit"])

    def test_paste_without_trailing_newline(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"one\rtwo three")
        self.assertTrue(s.wait_for("... "), s.transcript())
        s.send(b"\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs[0], "one\ntwo three")

    def test_tabs_survive_paste(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"def f():\r\treturn 1\r")
        self.assertTrue(s.wait_for("... "), s.transcript())
        s.send(b"\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs[0], "def f():\n\treturn 1")

    def test_pasted_slash_lines_stay_one_literal_message(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"/help\r/paste\rjust text\r")
        self.assertTrue(s.wait_for("... "), s.transcript())
        s.send(b"\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs, ["/help\n/paste\njust text", "quit"])

    def test_typed_fence_submits_on_close(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"```py\r")
        self.assertTrue(s.wait_for("... "), s.transcript())
        time.sleep(TYPE_GAP_S)
        s.send(b"x = 1\r")
        time.sleep(TYPE_GAP_S)
        s.send(b"```\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs[0], "```py\nx = 1\n```")

    def test_backslash_continuation(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"one \\\r")
        self.assertTrue(s.wait_for("... "), s.transcript())
        time.sleep(TYPE_GAP_S)
        s.send(b"two\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs[0], "one \ntwo")

    def test_ctrl_c_cancels_block_cleanly(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"aaa\rbbb\r")
        self.assertTrue(s.wait_for("... "), s.transcript())
        time.sleep(TYPE_GAP_S)
        s.send(b"\x03")
        self.assertTrue(s.wait_for("CANCELLED"), s.transcript())
        time.sleep(TYPE_GAP_S)
        s.send(b"hello\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        # Nothing partial was submitted; the next message works normally.
        self.assertEqual(msgs, ["hello", "quit"])

    def test_history_keeps_single_sanitized_entry(self):
        s = self.session("main")
        self.assertTrue(s.wait_for("you:"))
        s.send(b"h1\rh2\r")
        self.assertTrue(s.wait_for("... "), s.transcript())
        s.send(b"\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        self.finish(s)
        hist = s.parsed("HIST=")[0]
        from conch.multiline import HISTORY_NEWLINE_MARK

        self.assertEqual(hist, ["h1" + HISTORY_NEWLINE_MARK + "h2", "quit"])


class BracketedPasteTests(PtyTestCase):
    def test_bracketed_paste_single_message(self):
        s = self.session("main")
        if "BRACKETED=True" not in s.transcript():
            self.skipTest(
                "backend can't do bracketed paste here (libedit or GNU "
                "readline < 8.1); drain fallback covers paste on this "
                "platform — see PromptFlowTests"
            )
        self.assertTrue(s.wait_for("you:"))
        s.send(b"\x1b[200~line1\nline2\x1b[201~\r")
        self.assertTrue(s.wait_for("MSG="), s.transcript())
        msgs = self.finish(s)
        self.assertEqual(msgs, ["line1\nline2", "quit"])


class PasteCommandTests(PtyTestCase):
    def test_sentinel_ends_block(self):
        s = self.session("paste")
        s.send(b"hello\r\tworld\r.\r")
        self.assertTrue(s.wait_for("PASTE="), s.transcript())
        self.assertEqual(s.parsed("PASTE=")[0], "hello\n\tworld")

    def test_ctrl_d_ends_block(self):
        s = self.session("paste")
        s.send(b"x\r\x04")
        self.assertTrue(s.wait_for("PASTE="), s.transcript())
        self.assertEqual(s.parsed("PASTE=")[0], "x")

    def test_ctrl_c_cancels(self):
        s = self.session("paste")
        s.send(b"x\r")
        time.sleep(TYPE_GAP_S)
        s.send(b"\x03")
        self.assertTrue(s.wait_for("PASTE="), s.transcript())
        self.assertIsNone(s.parsed("PASTE=")[0])


class TypeaheadCoalesceTests(PtyTestCase):
    def test_paste_during_stream_queues_one_block(self):
        s = self.session("typeahead")
        s.send(b"x\ry\r")  # pasted block, trailing newline
        time.sleep(0.3)
        s.send(b"solo\r")  # separately typed line
        self.assertTrue(s.wait_for("PARTIAL="), s.transcript())
        self.assertEqual(s.parsed("QUEUED=")[0], ["x\ny", "solo"])
        self.assertEqual(s.parsed("PARTIAL=")[0], "")

    def test_unterminated_paste_stays_partial_block(self):
        s = self.session("typeahead")
        s.send(b"a\rb")  # pasted block without trailing newline
        self.assertTrue(s.wait_for("PARTIAL="), s.transcript())
        self.assertEqual(s.parsed("QUEUED=")[0], [])
        self.assertEqual(s.parsed("PARTIAL=")[0], "a\nb")


if __name__ == "__main__":
    unittest.main()
