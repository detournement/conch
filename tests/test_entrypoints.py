"""Swarm console entrypoints.

``conch-edge`` is live (Swarm Phase 1) but gated on ``edge_daemon=true`` —
without it the command refuses with a clear pointer, so shell-only users
cannot start a daemon by accident. The still-dormant entrypoints
(controller, worker, hostctl) must exist, parse arguments, and refuse to
run with a pointer to the plan — exiting nonzero so scripts and supervisors
can't mistake a dormant mode for a working one.
"""

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import __version__
from conch.entrypoints import (
    DORMANT_EXIT_CODE,
    EDGE_DISABLED_EXIT_CODE,
    controller_main,
    edge_main,
    hostctl_main,
    worker_main,
)

DORMANT_MAINS = {
    "conch-controller": controller_main,
    "conch-worker": worker_main,
    "conch-hostctl": hostctl_main,
}

ALL_MAINS = dict(DORMANT_MAINS)
ALL_MAINS["conch-edge"] = edge_main


class TestDormantEntrypoints(unittest.TestCase):
    def test_dormant_mains_exit_nonzero_with_plan_pointer(self):
        for prog, main in DORMANT_MAINS.items():
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


class TestEdgeEntrypoint(unittest.TestCase):
    """conch-edge is live but hard-gated on edge_daemon=true."""

    def test_refuses_without_edge_daemon_enabled(self):
        stderr = io.StringIO()
        clean_env = {
            key: value for key, value in os.environ.items()
            if key != "CONCH_EDGE_DAEMON"
        }
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clean_env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            with patch.dict(os.environ, clean_env, clear=True):
                with contextlib.redirect_stderr(stderr):
                    code = edge_main([])
        self.assertEqual(code, EDGE_DISABLED_EXIT_CODE)
        message = stderr.getvalue()
        self.assertIn("edge_daemon", message)
        self.assertIn("fully", message)

    def test_help_describes_the_live_daemon(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                edge_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("mission kernel", text)
        self.assertNotIn("dormant", text)


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
