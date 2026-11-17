"""The startup conch: shown on a real terminal with room for it, never in
pipes or redirected output, and never crowding a small window."""

import io
import os
import unittest
from unittest import mock

from conch.app import CONCH_SHELL_ART, _print_conch_shell_art


class _Terminal(io.StringIO):
    def isatty(self):
        return True


class StartupArtTests(unittest.TestCase):
    def printed(self, stdout, columns=120, rows=50):
        with mock.patch("sys.stdout", stdout), \
                mock.patch("shutil.get_terminal_size",
                           return_value=os.terminal_size((columns, rows))):
            _print_conch_shell_art()
        return stdout.getvalue()

    def test_large_terminal_shows_the_whole_conch(self):
        out = self.printed(_Terminal())
        for line in CONCH_SHELL_ART:
            self.assertIn(line, out)

    def test_pipes_and_small_windows_get_nothing(self):
        self.assertEqual(self.printed(io.StringIO()), "")
        self.assertEqual(self.printed(_Terminal(), columns=30), "")
        self.assertEqual(self.printed(_Terminal(), rows=20), "")

    def test_no_color_prints_plain_text(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            out = self.printed(_Terminal())
        self.assertNotIn("\033[", out)


if __name__ == "__main__":
    unittest.main()
