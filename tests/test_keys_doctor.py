"""``conch keys doctor`` and the ``/keys`` slash command: install/PATH
shadowing, env-file repair, stale profile exports (file:line, incl.
sourced files, fish, direnv), daemon state findings, the identity line,
and slash routing (/help lists it, unknown subcommand hint, reload_tools
when a saved key is referenced by an MCP server)."""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod
from conch.keys import doctor, identity
from conch.keys.inventory import build_inventory
from conch.keys.shellenv import scan_shell_exports


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "conch"
        self.config_dir.mkdir(parents=True)
        patcher = patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"), "PATH": "/usr/bin:/bin",
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        self.identity = {"version": "0.7.1", "commit": "abc1234", "path": "/repo/conch", "root": "/repo",
                         "shape": "checkout", "python": "/usr/bin/python3", "argv0": "./bin/conch",
                         "dirty": False, "committed_at": 1_800_000_000, "installed_at": "", "source": ""}

    def shim(self, directory: str, text: str = "#!/usr/bin/python3\nfrom conch.app import main\n") -> Path:
        path = self.home / directory / "conch"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path


class TestBinaries(DoctorCase):
    def test_multiple_binaries_named_with_versions(self):
        old = self.shim("brew/bin")
        mine = self.shim("repo/bin")
        descriptions = {
            str(old): {"path": str(old), "resolved": str(old), "python": "/usr/bin/python3",
                       "package": "/old/conch", "version": "0.4.0", "shape": "pip-editable → /old",
                       "commit": "", "error": ""},
            str(mine): {"path": str(mine), "resolved": str(mine), "python": "/usr/bin/python3",
                        "package": "/repo/conch", "version": "0.7.1", "shape": "checkout",
                        "commit": "abc1234", "error": ""},
        }
        findings = doctor.check_binaries(
            self.identity, path_value=f"{old.parent}:{mine.parent}", describe=lambda p: descriptions[str(p)],
        )
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.level, "warn")
        self.assertIn("2 conch binaries on PATH", finding.message)
        self.assertIn("you are running #2", finding.message)
        self.assertIn("`conch` resolves here", finding.details[0])
        self.assertIn("pip-editable → /old 0.4.0", finding.details[0])
        self.assertIn("running now", finding.details[1])
        self.assertIn("different versions", finding.fix)

    def test_single_other_binary_warns_colleague_gets_other_copy(self):
        other = self.shim("local/bin")
        findings = doctor.check_binaries(
            self.identity, path_value=str(other.parent),
            describe=lambda p: {"path": str(p), "resolved": str(p), "python": "p", "package": "/uv/conch",
                                "version": "0.7.0", "shape": "uv", "commit": "", "error": ""},
        )
        self.assertEqual(findings[0].level, "warn")
        self.assertIn("a colleague typing `conch` gets the other copy", findings[0].message)

    def test_none_on_path(self):
        findings = doctor.check_binaries(self.identity, path_value=str(self.home / "nothing"))
        self.assertEqual(findings[0].code, "path.none")
        self.assertIn("./bin/conch keys", findings[0].message)

    def test_installed_copy_older_than_checkout(self):
        uv = self.shim("local/bin")
        state = self.home / ".local" / "state" / "conch"
        state.mkdir(parents=True)
        (state / "install-source").write_text("installed_at=2026-10-09T23:35:25Z\nsource=https://github.com/x/conch/archive/refs/heads/edge.tar.gz\n")
        findings = doctor.check_installed_copies(
            self.identity, path_value=str(uv.parent),
            describe=lambda p: {"path": str(p), "resolved": str(p), "python": "p", "package": "/uv/conch",
                                "version": "0.7.0", "shape": "uv", "commit": "", "error": ""},
        )
        self.assertEqual(findings[0].code, "install.stale")
        self.assertIn("installed 2026-10-09", findings[0].message)
        self.assertIn("curl -fsSL https://conch-site-gamma.vercel.app/install | sh", findings[0].fix)


class TestEnvFileRepair(DoctorCase):
    def test_missing_created_with_confirmation(self):
        asked = []
        findings = doctor.check_env_file(fix=True, confirm=lambda q: asked.append(q) or True)
        self.assertEqual(findings[0].code, "envfile.created")
        path = self.config_dir / "env"
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(asked and "Create" in asked[0])

    def test_missing_without_fix_is_a_warning_with_remedy(self):
        findings = doctor.check_env_file()
        self.assertEqual(findings[0].level, "warn")
        self.assertIn("conch keys import --all-configured", findings[0].fix)

    def test_bad_mode_fixed(self):
        path = self.config_dir / "env"
        path.write_text("A=b\n")
        path.chmod(0o644)
        findings = doctor.check_env_file(fix=True, confirm=lambda q: True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(findings[0].level, "ok")
        path.chmod(0o644)
        findings = doctor.check_env_file(fix=False)
        self.assertEqual(findings[0].code, "envfile.mode")
        self.assertIn("chmod 600", findings[0].fix)


class TestProfiles(DoctorCase):
    def test_scan_follows_sources_fish_and_direnv(self):
        (self.home / ".zshrc").write_text(
            'export CONCH_DIR="$HOME/conch"\n[[ -f "${CONCH_DIR}/.env" ]] && source "${CONCH_DIR}/.env"\n'
            "source ~/.secrets\n"
        )
        (self.home / "conch").mkdir()
        (self.home / "conch" / ".env").write_text("export OPENAI_API_KEY=sk-from-dot-env\n")
        (self.home / ".secrets").write_text("ANTHROPIC_API_KEY=$(op read x)\n")
        (self.home / ".zshenv").write_text("# c\nexport OPENROUTER_API_KEY='sk-or-zshenv'\n")
        fish = self.home / ".config" / "fish"
        fish.mkdir(parents=True)
        (fish / "config.fish").write_text("set -gx CEREBRAS_API_KEY csk-fish\nset -g NOT_EXPORTED x\n")
        work = self.home / "proj"
        work.mkdir()
        (work / ".envrc").write_text("export E2B_API_KEY=e2b_envrc\n")
        scan = scan_shell_exports(
            ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY", "E2B_API_KEY"],
            home=self.home, cwd=work,
        )
        openai = scan.for_name("OPENAI_API_KEY")[0]
        self.assertEqual(openai.location, f"{self.home}/conch/.env:1")
        self.assertEqual(openai.via, f"{self.home}/.zshrc")
        self.assertEqual(openai.fingerprint, config_mod.key_fingerprint("sk-from-dot-env"))
        anthropic = scan.for_name("ANTHROPIC_API_KEY")[0]
        self.assertFalse(anthropic.literal)
        self.assertEqual(scan.for_name("OPENROUTER_API_KEY")[0].location, f"{self.home}/.zshenv:2")
        self.assertEqual(scan.for_name("CEREBRAS_API_KEY")[0].location, f"{fish}/config.fish:1")
        self.assertEqual(scan.for_name("E2B_API_KEY")[0].via, "direnv")

    def test_stale_export_named_with_precedence(self):
        (self.config_dir / "config").write_text("provider = openrouter\n")
        (self.home / ".zshenv").write_text("export OPENROUTER_API_KEY=sk-or-stale-export\n")
        config_mod.set_env_values({"OPENROUTER_API_KEY": "sk-or-saved"})
        inventory = build_inventory({"provider": "openrouter", "api_key_env": "OPENROUTER_API_KEY"},
                                    {"OPENROUTER_API_KEY": "sk-or-stale-export"}, home=self.home, with_plugins=False)
        row = inventory.get("OPENROUTER_API_KEY")
        self.assertEqual(row.effective, "env file")
        self.assertEqual(len(row.export_notes), 1)
        self.assertIn(f"{self.home}/.zshenv:1: STALE", row.export_notes[0])
        self.assertIn("conch overrides it", row.export_notes[0])
        findings = doctor.check_profiles(inventory)
        self.assertEqual(findings[0].code, "profile.stale")
        self.assertIn(".zshenv:1", findings[0].details[0])
        # unmanaged line: the export wins and the note says so
        config_mod.forget_managed_key("OPENROUTER_API_KEY")
        inventory = build_inventory({"provider": "openrouter", "api_key_env": "OPENROUTER_API_KEY"},
                                    {"OPENROUTER_API_KEY": "sk-or-stale-export"}, home=self.home, with_plugins=False)
        row = inventory.get("OPENROUTER_API_KEY")
        self.assertEqual(row.effective, "shell env")
        self.assertIn("it WINS over it", row.export_notes[0])


class TestDaemonFindings(DoctorCase):
    def inventory(self):
        (self.config_dir / "config").write_text("provider = openrouter\nedge_daemon = true\n")
        return build_inventory({"provider": "openrouter", "api_key_env": "OPENROUTER_API_KEY", "edge_daemon": "true"},
                               {}, home=self.home, with_plugins=False)

    def test_old_protocol_daemon_gets_restart_line(self):
        view = {"state": "protocol", "status": {"holder": "edge-host-424242"}}
        with patch("conch.keys.identity.process_start_time", return_value=0.0):
            findings = doctor.check_daemon(self.inventory(), self.identity, view, program={})
        codes = [f.code for f in findings]
        self.assertIn("daemon.old-protocol", codes)
        finding = [f for f in findings if f.code == "daemon.old-protocol"][0]
        self.assertIn("pid 424242", finding.message)
        self.assertIn(identity.restart_daemon_command(), finding.fix)

    def test_daemon_running_older_commit(self):
        view = {"state": "ok", "pid": 7, "version": "0.7.0", "started_at": 1_700_000_000,
                "identity": {"commit": "0ldc0mm", "shape": "checkout"}, "keys": {}}
        findings = doctor.check_daemon(self.inventory(), self.identity, view, program={})
        stale = [f for f in findings if f.code == "daemon.stale-code"]
        self.assertTrue(stale)
        self.assertIn("runs commit 0ldc0mm", stale[0].message)
        self.assertIn("restart it", stale[0].fix)

    def test_daemon_program_points_elsewhere(self):
        program = {"supervisor": "launchd", "unit_path": "/u/com.conch.edge.plist",
                   "argv": ["/opt/py", "-c", "x"], "env": {"PYTHONPATH": "/other"},
                   "runs": {"package": "/other/conch", "shape": "checkout", "version": "0.7.0", "commit": "deadbee"}}
        view = {"state": "not running"}
        findings = doctor.check_daemon(self.inventory(), self.identity, view, program=program)
        self.assertEqual(findings[0].code, "daemon.program")
        self.assertEqual(findings[0].level, "warn")
        self.assertIn("not the copy running this command", findings[0].message)
        self.assertEqual(findings[1].code, "daemon.down")

    def test_disabled(self):
        inventory = build_inventory({"provider": "openrouter"}, {}, home=self.home, with_plugins=False)
        findings = doctor.check_daemon(inventory, self.identity, {"state": "disabled"}, program={})
        self.assertEqual(findings[0].code, "daemon.disabled")


class TestIdentity(unittest.TestCase):
    def test_identity_line_shapes(self):
        line = identity.identity_line({"version": "0.7.1", "commit": "abc1234", "dirty": True, "root": "/r",
                                       "shape": "checkout", "argv0": "./bin/conch"})
        self.assertEqual(line, "conch 0.7.1 (abc1234+dirty) from /r — install: checkout")
        line = identity.identity_line({"version": "0.7.0", "commit": "", "installed_at": "2026-10-09T23:35:25Z",
                                       "source": "https://github.com/detournement/conch/archive/refs/heads/edge.tar.gz",
                                       "root": "/uv/site", "shape": "uv", "argv0": "/x/__main__.py"})
        self.assertEqual(line, "conch 0.7.0 (commit unknown; installed 2026-10-09 from github detournement/conch edge)"
                               " from /uv/site — install: uv via python -m conch")

    def test_this_checkout_identifies_itself(self):
        info = identity.install_identity()
        self.assertTrue(info["shape"].startswith("checkout"), info)
        self.assertTrue(info["commit"], info)
        self.assertTrue(Path(info["root"], "conch", "__init__.py").is_file())

    def test_describe_binary_static_only(self):
        """A shim is never executed; its interpreter imports the package."""
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "conch"
            shim.write_text("#!/bin/sh\necho EXECUTED; exit 99\n")
            shim.chmod(0o755)
            info = identity.describe_binary(shim)
            self.assertEqual(info["error"], "not a Python launcher")
            root = Path(__file__).resolve().parent.parent
            info = identity.describe_binary(root / "bin" / "conch")
            self.assertEqual(info["shape"], "checkout")
            self.assertEqual(Path(info["package"]).resolve(), (root / "conch").resolve())


class TestSlash(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "conch"
        self.config_dir.mkdir(parents=True)
        patcher = patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"), "PATH": "/usr/bin:/bin", "NO_COLOR": "1",
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        config_mod._ENV_LOAD_STATE["from_file"].clear()
        (self.config_dir / "config").write_text("provider = anthropic\n")

    def slash(self, line: str, stdin: str = ""):
        from conch.commands import handle_slash_command

        out = io.StringIO()
        with redirect_stdout(out), patch("sys.stdin", io.StringIO(stdin)):
            result = handle_slash_command(line, {"provider": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
                                          "anthropic", "claude-sonnet-5", lambda *_: None)
        return result, out.getvalue()

    def test_registered_everywhere(self):
        from conch.commands import BUILTIN_SLASH_COMMANDS, slash_command_names

        self.assertIn("/keys", BUILTIN_SLASH_COMMANDS)
        self.assertIn("/keys", slash_command_names())
        _, text = self.slash("/help")
        self.assertIn("/keys", text)

    def test_list_and_unknown_subcommand(self):
        result, text = self.slash("/keys")
        self.assertIsNone(result)
        self.assertIn("ANTHROPIC_API_KEY", text)
        self.assertIn("missing", text)
        err = io.StringIO()
        with patch("sys.stderr", err):
            result, _ = self.slash("/keys rotat")
        self.assertIsNone(result)
        self.assertIn("did you mean rotate", err.getvalue())
        _, text = self.slash("/keys help")
        self.assertIn("/keys set NAME", text)

    def test_set_in_session_updates_env_and_restarts_mcp_when_referenced(self):
        (self.config_dir / "mcp.json").write_text(json.dumps({"mcpServers": {
            "hosted": {"type": "http", "url": "http://127.0.0.1:9/mcp",
                       "headers": {"Authorization": "Bearer ${MCP_HOSTED_TOKEN}"}}}}))
        result, text = self.slash("/keys set MCP_HOSTED_TOKEN --stdin --no-verify", stdin="tok-0123456789abcdef\n")
        self.assertEqual(result, "reload_tools")
        self.assertIn("saved MCP_HOSTED_TOKEN", text)
        self.assertEqual(os.environ["MCP_HOSTED_TOKEN"], "tok-0123456789abcdef")
        self.assertIn("mcp/hosted", text)
        result, text = self.slash("/keys set ANTHROPIC_API_KEY --stdin --no-verify", stdin="sk-ant-0123456789abcdef\n")
        self.assertIsNone(result)  # no MCP server references it: no tool reload needed
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-0123456789abcdef")
        self.assertNotIn("sk-ant-0123456789abcdef", text)
        self.assertEqual(config_mod.managed_key_names(), {"MCP_HOSTED_TOKEN", "ANTHROPIC_API_KEY"})


if __name__ == "__main__":
    unittest.main()
