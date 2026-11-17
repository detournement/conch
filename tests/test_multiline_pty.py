"""Pty-driven integration tests for multiline input.

These spawn tests/multiline_pty_driver.py inside a real pseudo-terminal.
Legacy helpers exercise readline/libedit and the /paste reader; TerminalUI
coverage uses prompt_toolkit's real input lifecycle, including bracketed
paste, queued submissions, and isolated approval input.
"""

import ast
import os
import re
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
        # This byte-level harness has no screen emulator to answer cursor
        # position queries; real-emulator UI smoke coverage exercises CPR.
        env = dict(
            os.environ, TERM="xterm-256color", PYTHONUNBUFFERED="1",
            PROMPT_TOOLKIT_NO_CPR="1",
        )
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
        # prompt_toolkit may precede a committed output line with cursor or
        # erase controls. Strip terminal controls, not message whitespace.
        transcript = re.sub(
            r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]",
            "", self.transcript(),
        )
        for match in re.finditer(re.escape(tag) + r"([^\r\n]*)", transcript):
            values.append(ast.literal_eval(match.group(1)))
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


class TerminalUIQueueTests(PtyTestCase):
    def finish_queue(self, session):
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"quit\r")
        self.assertTrue(session.wait_for("DONE"), session.transcript())
        return session.parsed("QUEUED=")[0]

    def test_paste_during_stream_queues_one_block(self):
        session = self.session("typeahead")
        session.send(b"\x1b[200~x\ny\n\x1b[201~\r")
        time.sleep(0.15)
        session.send(b"solo\r")
        self.assertEqual(self.finish_queue(session), ["x\ny\n", "solo"])

    def test_unsubmitted_paste_remains_editable_after_busy_turn(self):
        session = self.session("typeahead")
        session.send(b"\x1b[200~a\nb\x1b[201~")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"-tail\r")
        self.assertEqual(self.finish_queue(session), ["a\nb-tail"])

    def test_long_queued_message_remains_exact_in_message_and_history(self):
        message = " ".join("word%03d" % i for i in range(50))
        session = self.session("typeahead")
        session.send(message.encode() + b"\r")
        self.assertEqual(self.finish_queue(session), [message])
        self.assertIn(message, session.parsed("HIST=")[0])

    def test_approval_does_not_consume_queued_message_or_draft(self):
        session = self.session("approval")
        session.send(b"chat-before\rdraft-before")
        self.assertTrue(session.wait_for("APPROVAL-ONLY:"), session.transcript())
        session.send(b"approved\r")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")  # Submit the draft restored after the overlay.
        self.assertEqual(
            self.finish_queue(session), ["chat-before", "draft-before"]
        )
        self.assertEqual(session.parsed("ANSWER="), ["approved"])
        self.assertNotIn("approved", session.parsed("HIST=")[0])

    def test_safe_boundary_consumes_only_steering(self):
        session = self.session("steering")
        session.send(b"guide\rnext-turn\x1bq/help\rexit\r")
        session.send(b"\x1b[200~/help\nquit\x1b[201~\r")
        self.assertEqual(self.finish_queue(session), ["next-turn", "/help", "exit"])
        self.assertEqual(session.parsed("STEERING="), [["guide", "/help\nquit"]])
        history = session.parsed("HIST=")[0]
        self.assertEqual(history.count("guide"), 1)
        self.assertEqual(history.count("/help\nquit"), 1)

    def test_queue_off_keeps_busy_draft_for_later_submission(self):
        session = self.session("queue-off")
        session.send(b"kept\r\x1bq")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")
        self.assertEqual(self.finish_queue(session), ["kept"])
        self.assertEqual(session.parsed("STEERING="), [[]])

    def test_reserved_edit_blocks_later_steering_and_preserves_draft(self):
        session = self.session("queue-edit")
        session.send(b"first\rsecond\rdraft\x11\r")
        self.assertTrue(session.wait_for("RESERVED"), session.transcript())
        session.send(b"\x01\x0bedited\x1b\rcontinued\r\x11")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")
        self.assertEqual(self.finish_queue(session), ["draft"])
        self.assertEqual(session.parsed("STEERING="), [[[], ["edited\ncontinued", "second"]]])
        history = session.parsed("HIST=")[0]
        self.assertIn("edited\ncontinued", history)
        self.assertNotIn("first", history)

    def test_escape_restores_reserved_message_and_original_order(self):
        session = self.session("queue-edit")
        session.send(b"first\rsecond\rdraft\x11\r")
        self.assertTrue(session.wait_for("RESERVED"), session.transcript())
        session.send(b"-discard\x1b")
        self.assertTrue(session.wait_for("EDIT-RELEASED"), session.transcript())
        session.send(b"\x11")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")
        self.assertEqual(self.finish_queue(session), ["draft"])
        self.assertEqual(session.parsed("STEERING="), [[[], ["first", "second"]]])
        self.assertNotIn("first-discard", session.parsed("HIST=")[0])

    def test_editing_steering_into_command_defers_it(self):
        session = self.session("queue-edit")
        session.send(b"first\rsecond\r\x11\r")
        self.assertTrue(session.wait_for("RESERVED"), session.transcript())
        session.send(b"\x01\x0b/help\r\x11")
        self.assertEqual(self.finish_queue(session), ["/help"])
        self.assertEqual(session.parsed("STEERING="), [[[], ["second"]]])

    def test_selection_keeps_identity_when_earlier_item_is_consumed(self):
        session = self.session("queue-selection")
        session.send(b"first\rsecond\x1bqthird\x1bqdraft\x11\x1b[B")
        self.assertTrue(session.wait_for("SELECTION-DRAINED"), session.transcript())
        session.send(b"\r")
        self.assertTrue(session.wait_for("EDITING"), session.transcript())
        session.send(b"\x01\x0bchanged\r\x11")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")
        self.assertEqual(self.finish_queue(session), ["changed", "third", "draft"])
        self.assertEqual(session.parsed("STEERING="), [["first"]])

    def test_delete_removes_only_selected_message_without_history(self):
        session = self.session("queue-delete")
        session.send(b"first\rremove-me\rthird\rdraft\x11")
        self.assertTrue(session.wait_for("LIST-OPEN"), session.transcript())
        session.send(b"\x1b[B\x1b[3~\x11")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")
        self.assertEqual(self.finish_queue(session), ["first", "third", "draft"])
        self.assertNotIn("remove-me", session.parsed("HIST=")[0])

    def test_turn_end_waits_for_reserved_edit_before_delivering(self):
        session = self.session("queue-read")
        session.send(b"first\rsecond\r\x11\r")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\x01\x0brevised\r\x11")
        self.assertEqual(self.finish_queue(session), ["revised", "second"])

    def test_close_unblocks_reader_with_reserved_message(self):
        session = self.session("queue-close")
        session.send(b"first\r\x11\r")
        self.assertTrue(session.wait_for("DONE"), session.transcript())
        self.assertEqual(session.parsed("CLOSED="), [True])
        self.assertNotIn("first", session.parsed("HIST=")[0])

    def test_approval_supersedes_edit_without_submitting_either_draft(self):
        session = self.session("approval-edit")
        session.send(b"first\rdraft\x11\r")
        self.assertTrue(session.wait_for("RESERVED"), session.transcript())
        session.send(b"-unsaved")
        self.assertTrue(session.wait_for("APPROVAL-ONLY:"), session.transcript())
        session.send(b"approved\r")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\r")
        self.assertEqual(self.finish_queue(session), ["draft"])
        self.assertEqual(session.parsed("STEERING="), [["first"]])
        self.assertEqual(session.parsed("ANSWER="), ["approved"])
        history = session.parsed("HIST=")[0]
        self.assertNotIn("approved", history)
        self.assertNotIn("first-unsaved", history)

    def test_handoff_discards_reserved_edit_and_composer(self):
        session = self.session("nested-handoff")
        session.send(b"secret-queued\rsecret-draft\x11\r-unsaved")
        self.assertTrue(session.wait_for("RESUMED-INPUT"), session.transcript())
        session.send(b"fresh\rquit\r")
        self.assertTrue(session.wait_for("DONE"), session.transcript())
        self.assertEqual(session.parsed("QUEUED="), [["fresh"]])
        self.assertEqual(session.parsed("HIST=")[0], ["quit", "fresh"])


class TerminalUIDetailTests(PtyTestCase):
    def test_hiding_details_retains_updates_until_next_turn(self):
        session = self.session("details")
        self.assertTrue(session.wait_for("DETAILS-READY"), session.transcript())
        self.assertTrue(session.wait_for("visible-reasoning-continued"), session.transcript())
        session.send(b"\x1br")
        self.assertTrue(session.wait_for("DETAILS-HIDDEN"), session.transcript())
        self.assertNotIn("hidden-reasoning", session.transcript())
        session.send(b"\x1br")
        self.assertTrue(session.wait_for("hidden-reasoning"), session.transcript())
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"next-detail-turn\rquit\r")
        self.assertTrue(session.wait_for("DONE"), session.transcript())
        details = session.parsed("DETAILS=")[0]
        self.assertIn("visible-reasoning-continued", details)
        self.assertIn("visible-progress", details)
        self.assertIn("hidden-reasoning", details)
        self.assertEqual(session.parsed("DETAILS-CLEARED="), [True])

    def test_output_publication_keeps_raw_input_and_preserves_draft(self):
        session = self.session("publication")
        session.send(b"fast-startup-draft")
        self.assertTrue(session.wait_for("DRAINING"), session.transcript())
        session.send(b"\rquit\r")
        self.assertTrue(session.wait_for("DONE"), session.transcript())
        modes = session.parsed("OUTPUT-MODES=")[0]
        self.assertEqual(modes, [(False, False)] * 12)
        self.assertEqual(session.parsed("QUEUED="), [["fast-startup-draft"]])
        self.assertIn("FINAL-RESPONSE", session.transcript())
        for index in range(12):
            self.assertIn("DIAGNOSTIC-" + str(index), session.transcript())


if __name__ == "__main__":
    unittest.main()
