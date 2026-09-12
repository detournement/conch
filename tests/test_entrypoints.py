"""Dormant swarm entrypoints (Swarm Phase 0).

Each console script must exist, parse arguments, and refuse to run with a
clear pointer to the plan — exiting nonzero so scripts and supervisors can't
mistake a dormant mode for a working one.
"""

import contextlib
import io
import unittest
from pathlib import Path

from conch import __version__
from conch.entrypoints import (
    DORMANT_EXIT_CODE,
    controller_main,
    edge_main,
    hostctl_main,
    worker_main,
)

ALL_MAINS = {
    "conch-controller": controller_main,
    "conch-edge": edge_main,
    "conch-worker": worker_main,
    "conch-hostctl": hostctl_main,
}


class TestDormantEntrypoints(unittest.TestCase):
    def test_all_exit_nonzero_with_plan_pointer(self):
        for prog, main in ALL_MAINS.items():
            with self.subTest(prog=prog):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = main([])
                self.assertEqual(code, DORMANT_EXIT_CODE)
                self.assertNotEqual(code, 0)
                message = stderr.getvalue()
                self.assertIn(prog, message)
                self.assertIn("not yet enabled", message)
                self.assertIn("PLAN.md", message)

    def test_dormant_message_never_goes_to_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            controller_main([])
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(stderr.getvalue())

    def test_version_flag_works_now(self):
        for prog, main in ALL_MAINS.items():
            with self.subTest(prog=prog):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as ctx:
                        main(["--version"])
                self.assertEqual(ctx.exception.code, 0)
                self.assertIn(__version__, stdout.getvalue())
                self.assertIn(prog, stdout.getvalue())

    def test_help_flag_works_now(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                edge_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("dormant", stdout.getvalue())

    def test_unknown_arguments_are_a_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                worker_main(["--definitely-not-a-flag"])
        self.assertEqual(ctx.exception.code, 2)

    def test_config_override_accepted(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = hostctl_main(["--config", "/tmp/some.conf"])
        self.assertEqual(code, DORMANT_EXIT_CODE)


class TestPyprojectScripts(unittest.TestCase):
    def test_console_scripts_registered(self):
        pyproject = (
            Path(__file__).resolve().parent.parent / "pyproject.toml"
        ).read_text()
        for line in (
            'conch = "conch.app:main"',
            'conch-controller = "conch.entrypoints:controller_main"',
            'conch-edge = "conch.entrypoints:edge_main"',
            'conch-worker = "conch.entrypoints:worker_main"',
            'conch-hostctl = "conch.entrypoints:hostctl_main"',
        ):
            self.assertIn(line, pyproject)

    def test_interactive_conch_entrypoint_untouched(self):
        pyproject = (
            Path(__file__).resolve().parent.parent / "pyproject.toml"
        ).read_text()
        self.assertIn('conch = "conch.app:main"', pyproject)


if __name__ == "__main__":
    unittest.main()
