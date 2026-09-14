"""Pty-driven regression tests for the terminal-handoff input handshake.

Reproduces the reported failure: an interactive sudo-style handoff while the
typeahead reader is active. The child must receive only the bytes typed after
the handoff begins (three clean credential entries), Conch must print nothing
between the handoff banner and the resume banner, buffered Conch output must
be flushed before the child owns the terminal, and nothing captured around
the window may ever be queued or echoed.
"""

import time
import unittest

from tests.test_multiline_pty import HAVE_PTY, PtySession

HANDOFF_BANNER = "[Conch terminal handoff]"
RESUME_BANNER = "[Conch resumed]"


@unittest.skipUnless(HAVE_PTY, "POSIX pty support required")
class HandoffPtyTests(unittest.TestCase):
    def _run_handoff_session(self):
        s = PtySession("handoff")
        self.addCleanup(s.close)
        self.assertTrue(s.wait_for("READY"), s.transcript())
        # Typed while the typeahead reader is capturing, before the handoff:
        # must be discarded, never delivered to the child, never re-echoed.
        s.send(b"stolen-secret\r")
        passwords = [b"pw-first\r", b"pw-second\r", b"pw-third\r"]
        for count, password in enumerate(passwords, start=1):
            self.assertTrue(
                s.wait_for("Password:", timeout=20.0, count=count),
                s.transcript(),
            )
            time.sleep(0.05)
            s.send(password)
        self.assertTrue(s.wait_for("SUDOCHILD-END", timeout=20.0), s.transcript())
        self.assertTrue(s.wait_for("PARTIAL=", timeout=10.0), s.transcript())
        return s

    def test_sudo_like_child_receives_only_post_handoff_bytes(self):
        s = self._run_handoff_session()
        transcript = s.transcript()
        self.assertEqual(s.parsed("GOT0="), ["pw-first"], transcript)
        self.assertEqual(s.parsed("GOT1="), ["pw-second"], transcript)
        self.assertEqual(s.parsed("GOT2="), ["pw-third"], transcript)
        self.assertEqual(s.parsed("RC="), [0], transcript)

    def test_handoff_window_is_clean_and_captures_are_discarded(self):
        s = self._run_handoff_session()
        transcript = s.transcript()

        # Pre-handoff typeahead was discarded with a notice and never queued.
        self.assertIn("DISCARD-NOTICE", transcript)
        self.assertEqual(s.parsed("QUEUED="), [[]], transcript)
        self.assertEqual(s.parsed("PARTIAL="), [""], transcript)

        # The unflushed Conch fragment was quiesced before the child ran:
        # it appears before the handoff banner, and the window between the
        # banners contains only the child's own interaction — no Conch
        # output, no queued-preview echo, no pre-handoff keystrokes.
        self.assertIn("ERRFRAG:", transcript)
        banner_at = transcript.index(HANDOFF_BANNER)
        resume_at = transcript.index(RESUME_BANNER)
        self.assertLess(transcript.index("ERRFRAG:"), banner_at, transcript)
        window = transcript[banner_at:resume_at]
        self.assertNotIn("ERRFRAG:", window)
        self.assertNotIn("(queued:", window)
        self.assertNotIn("stolen-secret", window)
        self.assertNotIn("DISCARD-NOTICE", window)
        # The child's own markers are the only structured output inside.
        self.assertIn("SUDOCHILD-START", window)
        self.assertIn("SUDOCHILD-END", window)

        # No echo of the passwords: the child disabled ECHO before reading,
        # and Conch printed nothing, so the raw entries never appear outside
        # the child's explicit GOT reports.
        got_free = transcript.replace("GOT0='pw-first'", "").replace(
            "GOT1='pw-second'", ""
        ).replace("GOT2='pw-third'", "")
        self.assertNotIn("pw-first", got_free, transcript)
        self.assertNotIn("pw-second", got_free, transcript)
        self.assertNotIn("pw-third", got_free, transcript)


if __name__ == "__main__":
    unittest.main()
