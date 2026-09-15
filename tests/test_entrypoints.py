"""Swarm console entrypoints.

``conch-edge`` (Swarm Phase 1) and ``conch-controller`` (the fleet
awakening) are live but hard-gated on their config flags — without
``edge_daemon=true`` / ``fleet_controller=true`` each refuses with a clear
pointer and a nonzero exit, so shell-only users cannot start a daemon by
accident and supervisors can't mistake a disabled mode for a working one.
``conch-hostctl`` and ``conch-worker`` are live (Swarm Phase 2).
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import __version__
from conch.entrypoints import (
    CONTROLLER_DISABLED_EXIT_CODE,
    EDGE_DISABLED_EXIT_CODE,
    controller_main,
    edge_main,
    hostctl_main,
    worker_main,
)

ALL_MAINS = {
    "conch-controller": controller_main,
    "conch-edge": edge_main,
    "conch-hostctl": hostctl_main,
    "conch-worker": worker_main,
}


def _clean_env(tmp: str) -> dict:
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("CONCH_EDGE_DAEMON", "CONCH_FLEET_CONTROLLER")
    }
    env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
    env["XDG_STATE_HOME"] = str(Path(tmp) / "state")
    return env


class TestEntrypointBasics(unittest.TestCase):
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
                controller_main(["--definitely-not-a-flag"])
        self.assertEqual(ctx.exception.code, 2)

    def test_hostctl_without_subcommand_shows_help_and_fails(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = hostctl_main([])
        self.assertEqual(code, 2)
        self.assertIn("probe", stderr.getvalue())

    def test_worker_requires_home(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                worker_main([])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("home", stderr.getvalue().lower())


class TestEdgeEntrypoint(unittest.TestCase):
    """conch-edge is live but hard-gated on edge_daemon=true."""

    def test_refuses_without_edge_daemon_enabled(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
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


class TestControllerEntrypoint(unittest.TestCase):
    """conch-controller is live but hard-gated on fleet_controller=true."""

    def test_refuses_without_fleet_controller_enabled(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                with contextlib.redirect_stderr(stderr):
                    code = controller_main([])
        self.assertEqual(code, CONTROLLER_DISABLED_EXIT_CODE)
        message = stderr.getvalue()
        self.assertIn("fleet_controller", message)
        self.assertIn("fully", message)

    def test_disabled_message_never_goes_to_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    controller_main([])
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(stderr.getvalue())

    def test_help_describes_the_live_controller(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                controller_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("task plane", text)
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
