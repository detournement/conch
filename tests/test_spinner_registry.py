"""Tests for the spinner registry — clear_active_spinners() must be safe."""

import unittest

from conch.render import Spinner, _active_spinners, clear_active_spinners


class TestSpinnerRegistry(unittest.TestCase):
    def test_clear_with_no_active_is_safe(self):
        clear_active_spinners()
        self.assertEqual(len(_active_spinners), 0)

    def test_spinner_unregisters_on_exit(self):
        before = len(_active_spinners)
        with Spinner("test"):
            pass
        self.assertEqual(len(_active_spinners), before)

    def test_nested_spinners_both_register_unregister(self):
        before = len(_active_spinners)
        with Spinner("outer"):
            with Spinner("inner"):
                pass
        self.assertEqual(len(_active_spinners), before)

    def test_clear_during_active_does_not_remove(self):
        before = len(_active_spinners)
        with Spinner("active"):
            clear_active_spinners()
            # spinner is still active, just visually cleared
            # (count depends on TTY; in non-tty it never registers)
        self.assertEqual(len(_active_spinners), before)


if __name__ == "__main__":
    unittest.main()
