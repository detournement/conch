"""conch-edge install / uninstall / status (always-on daemon work item).

Proven here: the rendered launchd plist and systemd unit substitute the
resolved program, whitelist-only environment (never secrets), log paths,
and working directory; the deploy/ files are byte-identical to the
documentation-default render so hand-edited and installer-written copies
cannot drift; install/uninstall drive the right supervisor command
sequences with health verification (recorded runner — the real launchd
round-trip is a live acceptance step); and the conch-edge entrypoint
dispatches the subcommands with the edge_daemon config gate intact.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.kernel.install import (
    LAUNCHD_LABEL,
    deploy_template_text,
    install_cmd,
    render_launchd_plist,
    render_systemd_unit,
    resolve_program,
    status_cmd,
    uninstall_cmd,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Runner:
    """Records supervisor commands; scripted return codes by prefix."""

    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        import subprocess

        rc = 1 if cmd[1] in self.failures else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="err")

    def names(self):
        return [" ".join(call[:2]) for call in self.calls]


class InstallCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # Unit/plist writes land in the isolated tree, never the real home.
        home_patcher = patch(
            "conch.kernel.install.Path.home", lambda: self.root / "home"
        )
        home_patcher.start()
        self.addCleanup(home_patcher.stop)


class TestTemplateParity(unittest.TestCase):
    def test_deploy_files_match_documentation_render(self):
        for kind, name in (
            ("launchd", "com.conch.edge.plist"),
            ("systemd", "conch-edge.service"),
        ):
            rendered = deploy_template_text(kind)
            on_disk = (REPO_ROOT / "deploy" / name).read_text()
            self.assertEqual(
                rendered, on_disk,
                f"deploy/{name} drifted from the installer template —"
                " regenerate it from deploy_template_text",
            )


class TestRendering(unittest.TestCase):
    def test_launchd_substitution_and_escaping(self):
        text = render_launchd_plist(
            ["/opt/py & tools/python3", "-c", "run()"],
            {"PATH": "/a:/b", "PYTHONPATH": "/repo"},
            "/logs/out.log", "/logs/err.log",
            working_directory="/Users/someone",
        )
        self.assertIn("<string>/opt/py &amp; tools/python3</string>", text)
        self.assertIn("<string>-c</string>", text)
        self.assertIn("<key>PATH</key>", text)
        self.assertIn("<string>/a:/b</string>", text)
        self.assertIn("<key>PYTHONPATH</key>", text)
        self.assertIn("<key>WorkingDirectory</key>", text)
        self.assertIn("<string>/Users/someone</string>", text)
        self.assertIn("<string>/logs/out.log</string>", text)
        self.assertIn("<key>SuccessfulExit</key>", text)

    def test_systemd_substitution_and_quoting(self):
        text = render_systemd_unit(
            ["/usr/bin/python3", "-c", "import x; run()"],
            {"PATH": "/a:/b"},
        )
        self.assertIn(
            'ExecStart=/usr/bin/python3 -c "import x; run()"', text
        )
        self.assertIn('Environment="PATH=/a:/b"', text)
        self.assertIn("NoNewPrivileges=true", text)

    def test_environment_is_a_whitelist_never_ambient_secrets(self):
        with patch.dict(os.environ, {
            "SUPER_SECRET_TOKEN": "hunter2", "PATH": "/a:/b",
        }):
            program, environment = resolve_program()
        self.assertLessEqual(set(environment), {"PATH", "PYTHONPATH"})
        rendered = render_launchd_plist(program, environment, "/o", "/e")
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("SUPER_SECRET_TOKEN", rendered)

    def test_resolve_program_prefers_console_script(self):
        with patch("conch.kernel.install.shutil.which",
                   return_value="/venv/bin/conch-edge"):
            program, environment = resolve_program()
        self.assertEqual(program, ["/venv/bin/conch-edge"])
        self.assertNotIn("PYTHONPATH", environment)

    def test_resolve_program_falls_back_to_interpreter(self):
        import sys

        with patch("conch.kernel.install.shutil.which", return_value=None):
            program, environment = resolve_program()
        self.assertEqual(program[0], sys.executable)
        self.assertEqual(program[1], "-c")
        self.assertIn("edge_main", program[2])
        self.assertEqual(environment.get("PYTHONPATH"), str(REPO_ROOT))


class TestInstallDarwin(InstallCase):
    def test_install_sequence_writes_plist_and_verifies(self):
        runner = _Runner()
        messages = []
        code = install_cmd(
            {"edge_daemon": "true"}, platform_name="darwin", runner=runner,
            alive=lambda: True, sleep=lambda s: None, out=messages.append,
        )
        self.assertEqual(code, 0)
        self.assertEqual(runner.names(), [
            "launchctl bootout",     # idempotent pre-clean
            "launchctl bootstrap",
            "launchctl enable",
            "launchctl list",
        ])
        plist = (self.root / "home" / "Library" / "LaunchAgents"
                 / f"{LAUNCHD_LABEL}.plist")
        self.assertTrue(plist.exists())
        content = plist.read_text()
        program, _ = resolve_program()
        self.assertIn(f"<string>{program[0]}</string>", content)
        kernel_dir = self.root / "state" / "conch" / "kernel"
        self.assertIn(str(kernel_dir / "launchd.err"), content)
        self.assertTrue(
            any("launchd agent com.conch.edge running" in m
                for m in messages)
        )
        self.assertTrue(any("launchctl setenv" in m for m in messages))

    def test_install_fails_when_daemon_never_answers(self):
        runner = _Runner()
        messages = []
        code = install_cmd(
            {"edge_daemon": "true"}, platform_name="darwin", runner=runner,
            alive=lambda: False, sleep=lambda s: None, verify_seconds=0.1,
            out=messages.append,
        )
        self.assertEqual(code, 1)
        self.assertTrue(
            any("did not answer" in m for m in messages)
        )

    def test_install_falls_back_to_legacy_loader(self):
        runner = _Runner(failures={"bootstrap"})
        code = install_cmd(
            {"edge_daemon": "true"}, platform_name="darwin", runner=runner,
            alive=lambda: True, sleep=lambda s: None, out=lambda m: None,
        )
        self.assertEqual(code, 0)
        self.assertIn("launchctl load", runner.names())

    def test_uninstall_removes_plist_and_boots_out(self):
        runner = _Runner()
        install_cmd(
            {"edge_daemon": "true"}, platform_name="darwin", runner=runner,
            alive=lambda: True, sleep=lambda s: None, out=lambda m: None,
        )
        runner.calls.clear()
        messages = []
        code = uninstall_cmd(
            {}, platform_name="darwin", runner=runner,
            alive=lambda: False, sleep=lambda s: None, out=messages.append,
        )
        self.assertEqual(code, 0)
        self.assertEqual(runner.names(), ["launchctl bootout"])
        plist = (self.root / "home" / "Library" / "LaunchAgents"
                 / f"{LAUNCHD_LABEL}.plist")
        self.assertFalse(plist.exists())
        self.assertTrue(any("daemon stopped" in m for m in messages))
        # idempotent: uninstalling again reports the absence, still exits 0
        code = uninstall_cmd(
            {}, platform_name="darwin", runner=runner,
            alive=lambda: False, sleep=lambda s: None, out=messages.append,
        )
        self.assertEqual(code, 0)
        self.assertTrue(any("no launchd agent" in m for m in messages))

    def test_status_reports_health(self):
        runner = _Runner()
        messages = []

        def fake_request(op, args=None, **kwargs):
            return {
                "holder": "edge-host-1", "epoch": 3,
                "uptime_seconds": 12.0, "missions": {"ready": 1},
                "channel_intake": {"holder": "edge-host-1"},
            }

        with patch("conch.kernel.control.request", fake_request):
            code = status_cmd(
                {}, platform_name="darwin", runner=runner,
                alive=lambda: True, out=messages.append,
            )
        self.assertEqual(code, 0)
        joined = "\n".join(messages)
        self.assertIn("launchctl list: loaded", joined)
        self.assertIn("daemon: healthy", joined)
        self.assertIn("channel intake lease", joined)

    def test_status_unhealthy_exits_nonzero(self):
        runner = _Runner()
        code = status_cmd(
            {}, platform_name="darwin", runner=runner,
            alive=lambda: False, out=lambda m: None,
        )
        self.assertEqual(code, 1)


class TestInstallLinux(InstallCase):
    def test_install_sequence_systemd(self):
        runner = _Runner()
        code = install_cmd(
            {"edge_daemon": "true"}, platform_name="linux", runner=runner,
            alive=lambda: True, sleep=lambda s: None, out=lambda m: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(runner.names(), [
            "systemctl --user",  # daemon-reload
            "systemctl --user",  # enable --now
        ])
        self.assertEqual(runner.calls[0][2], "daemon-reload")
        self.assertEqual(runner.calls[1][2:], ["enable", "--now",
                                               "conch-edge"])
        unit = (self.root / "config" / "systemd" / "user"
                / "conch-edge.service")
        self.assertTrue(unit.exists())
        program, _ = resolve_program()
        self.assertIn(program[0], unit.read_text())

    def test_uninstall_systemd(self):
        runner = _Runner()
        install_cmd(
            {"edge_daemon": "true"}, platform_name="linux", runner=runner,
            alive=lambda: True, sleep=lambda s: None, out=lambda m: None,
        )
        runner.calls.clear()
        code = uninstall_cmd(
            {}, platform_name="linux", runner=runner,
            alive=lambda: False, sleep=lambda s: None, out=lambda m: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls[0][2:], ["disable", "--now",
                                               "conch-edge"])
        self.assertEqual(runner.calls[1][2], "daemon-reload")
        unit = (self.root / "config" / "systemd" / "user"
                / "conch-edge.service")
        self.assertFalse(unit.exists())

    def test_unsupported_platform(self):
        code = install_cmd(
            {"edge_daemon": "true"}, platform_name="win32",
            runner=_Runner(), alive=lambda: True, sleep=lambda s: None,
            out=lambda m: None,
        )
        self.assertEqual(code, 2)


class TestEntrypointDispatch(InstallCase):
    def _edge_main(self, argv, env=None):
        from conch.entrypoints import edge_main

        overrides = {"CONCH_EDGE_DAEMON": "true"}
        overrides.update(env or {})
        with patch.dict(os.environ, overrides):
            return edge_main(argv)

    def test_install_requires_edge_daemon_enabled(self):
        import io
        import sys

        stderr = io.StringIO()
        with patch.dict(os.environ, {}), patch.object(sys, "stderr", stderr):
            os.environ.pop("CONCH_EDGE_DAEMON", None)
            from conch.entrypoints import EDGE_DISABLED_EXIT_CODE, edge_main

            code = edge_main(["install"])
        self.assertEqual(code, EDGE_DISABLED_EXIT_CODE)
        self.assertIn("edge_daemon", stderr.getvalue())

    def test_subcommands_dispatch_to_installer(self):
        calls = []
        with patch("conch.kernel.install.install_cmd",
                   lambda config, **kw: calls.append("install") or 0), \
             patch("conch.kernel.install.uninstall_cmd",
                   lambda config, **kw: calls.append("uninstall") or 0), \
             patch("conch.kernel.install.status_cmd",
                   lambda config, **kw: calls.append("status") or 0):
            self.assertEqual(self._edge_main(["install"]), 0)
            self.assertEqual(self._edge_main(["uninstall"]), 0)
            # status and uninstall work without edge_daemon enabled
            os.environ.pop("CONCH_EDGE_DAEMON", None)
            self.assertEqual(self._edge_main(["status"], env={}), 0)
        self.assertEqual(calls, ["install", "uninstall", "status"])

    def test_unknown_command_rejected(self):
        from conch.entrypoints import edge_main

        with self.assertRaises(SystemExit):
            with patch("sys.stderr"):
                edge_main(["reinstall"])


if __name__ == "__main__":
    unittest.main()
