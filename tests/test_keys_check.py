"""``conch keys`` end to end against fake services: inventory sources,
stale-duplicate detection, per-service classification (ok / rejected /
missing / unreachable / skipped), ``--json``, exit codes, ``set``
(refuses a rejected value, saves a verified one, 0600, managed),
``import``, ``rotate --from-env`` / ``--stdin``, ``required``, and the
daemon column against a live in-process edge daemon (``keys.status`` /
``keys.reload``). No network: providers are ``StubEndpoint``s, the rest
is ``FakeServices``."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod
from conch.keys import cli
from tests.keys_fakes import FakeServices
from tests.modelcheck_stubs import StubEndpoint, closed_port_url

GOOD_OR = "sk-or-good-0123456789abcdef"
STALE_OR = "sk-or-stale-0123456789abcdef"
GOOD_ANTHROPIC = "sk-ant-good-0123456789abcdef"
JIRA_TOKEN = "jira-good-token-0123456789"
COMPOSIO_KEY = "comp-good-key-0123456789"
MCP_TOKEN = "mcp-good-token-0123456789"
E2B_KEY = "e2b_good_key_0123456789"
SLACK_TOKEN = "xoxb-good-0123456789"


class KeysCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openrouter = StubEndpoint(["moonshotai/kimi-k3"], require_token=GOOD_OR).start()
        cls.anthropic = StubEndpoint(["claude-sonnet-5"], require_token=GOOD_ANTHROPIC).start()
        cls.services = FakeServices(
            jira_accounts={"me@example.com": JIRA_TOKEN}, composio_keys={COMPOSIO_KEY},
            mcp_tokens={MCP_TOKEN}, e2b_keys={E2B_KEY}, slack_tokens={SLACK_TOKEN},
        ).start()

    @classmethod
    def tearDownClass(cls):
        cls.openrouter.stop()
        cls.anthropic.stop()
        cls.services.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "conch"
        self.config_dir.mkdir(parents=True)
        self.state_dir = self.home / ".local" / "state"
        self.env_path = self.config_dir / "env"
        self.base_env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.state_dir),
            "CONCH_OPENROUTER_API_BASE": self.openrouter.v1,
            "CONCH_ANTHROPIC_API_BASE": self.anthropic.v1,
            "CONCH_COMPOSIO_API_BASE": self.services.base,
            "CONCH_SLACK_API_BASE": self.services.base,
            "NO_COLOR": "1",
        }
        self.write_config("provider = openrouter\nmodel = moonshotai/kimi-k3\napi_key_env = OPENROUTER_API_KEY\n")
        config_mod._ENV_LOAD_STATE["from_file"].clear()

    def write_config(self, text: str) -> None:
        (self.config_dir / "config").write_text(text)

    def write_env(self, text: str) -> None:
        self.env_path.write_text(text)
        os.chmod(self.env_path, 0o600)

    def write_mcp(self, servers: dict) -> None:
        (self.config_dir / "mcp.json").write_text(json.dumps({"mcpServers": servers}))

    def run_keys(self, args, env=None, stdin: str = ""):
        """Run ``conch keys`` in-process with an isolated environment;
        returns (exit code, stdout text)."""
        merged = dict(self.base_env)
        merged.update(env or {})
        out = io.StringIO()
        with patch.dict(os.environ, merged, clear=True), \
                patch("sys.stdin", io.StringIO(stdin)), \
                patch("conch.keys.identity.install_identity", return_value={
                    "version": "t", "commit": "abc1234", "path": "/x/conch", "root": "/x",
                    "shape": "checkout", "python": sys.executable, "argv0": "conch",
                    "dirty": False, "committed_at": 0, "installed_at": "", "source": "",
                }), \
                redirect_stdout(out):
            config_mod._ENV_LOAD_STATE["from_file"].clear()
            code = cli.main(list(args) + (["--timeout", "4"] if args and args[0] != "--help" else []))
        return code, out.getvalue()

    def run_json(self, args, env=None, stdin=""):
        code, text = self.run_keys(list(args) + ["--json"], env, stdin)
        return code, json.loads(text)

    @staticmethod
    def row(data, name):
        for item in data["credentials"]:
            if item["name"] == name:
                return item
        raise AssertionError(f"{name} not in {[c['name'] for c in data['credentials']]}")


class TestInventory(KeysCase):
    def test_sources_and_stale_duplicate(self):
        self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\nANTHROPIC_API_KEY={GOOD_ANTHROPIC}\n")
        code, data = self.run_json(["list"], {"OPENROUTER_API_KEY": STALE_OR, "COMPOSIO_API_KEY": COMPOSIO_KEY})
        self.assertEqual(code, 0)
        row = self.row(data, "OPENROUTER_API_KEY")
        self.assertTrue(row["required"])
        self.assertIn("active model moonshotai/kimi-k3", row["consumers"][0])
        # hand-added line, different shell export → the export wins and the file entry is flagged stale
        self.assertEqual(row["source"], "shell env")
        self.assertIn("NOT used", row["stale"])
        self.assertEqual(row["fingerprint"], config_mod.key_fingerprint(STALE_OR))
        self.assertEqual(row["env_file_fingerprint"], config_mod.key_fingerprint(GOOD_OR))
        self.assertEqual(self.row(data, "ANTHROPIC_API_KEY")["source"], "env file (hand-added)")
        self.assertFalse(self.row(data, "ANTHROPIC_API_KEY")["required"])
        self.assertEqual(self.row(data, "COMPOSIO_API_KEY")["source"], "shell env (daemon-blind)")
        for item in data["credentials"]:
            self.assertNotIn(GOOD_OR, json.dumps(item))
            self.assertNotIn(STALE_OR, json.dumps(item))

    def test_managed_key_wins_over_export(self):
        with patch.dict(os.environ, self.base_env, clear=True):
            config_mod.set_env_values({"OPENROUTER_API_KEY": GOOD_OR})
        _, data = self.run_json(["list"], {"OPENROUTER_API_KEY": STALE_OR})
        row = self.row(data, "OPENROUTER_API_KEY")
        self.assertEqual(row["source"], "env file")
        self.assertTrue(row["managed"])
        self.assertIn("saved key wins", row["stale"])

    def test_missing_required_key(self):
        _, data = self.run_json(["list"])
        row = self.row(data, "OPENROUTER_API_KEY")
        self.assertEqual(row["source"], "missing")
        self.assertEqual(row["fingerprint"], "-")

    def test_required_lists_active_config_needs(self):
        self.write_config("provider = anthropic\nstatus_page_url = https://status.example\n")
        self.write_mcp({"atlassian": {"type": "http", "url": self.services.mcp_url,
                                      "headers": {"Authorization": "Bearer ${MCP_ATLASSIAN_TOKEN}"}}})
        code, data = self.run_json(["required"])
        names = {item["name"] for item in data}
        self.assertEqual(code, 0)
        self.assertIn("ANTHROPIC_API_KEY", names)
        self.assertIn("CONCH_STATUS_WRITE_TOKEN", names)
        self.assertIn("MCP_ATLASSIAN_TOKEN", names)
        self.assertNotIn("OPENROUTER_API_KEY", names)

    def test_identity_line_first(self):
        code, text = self.run_keys(["list"])
        self.assertTrue(text.splitlines()[0].startswith("conch t (abc1234) from /x — install: checkout"), text)


class TestCheckClassification(KeysCase):
    def test_provider_ok_rejected_missing_unreachable(self):
        self.write_config(
            "provider = openrouter\nmodel = moonshotai/kimi-k3\nfallback_models = anthropic/claude-sonnet-5\n"
        )
        self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\nANTHROPIC_API_KEY=sk-ant-wrong-0123456789abcdef\n")
        code, data = self.run_json(["check"])
        self.assertEqual(code, 0, data["problems"])  # anthropic is optional (fallback)
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["status"], "ok")
        anthropic = self.row(data, "ANTHROPIC_API_KEY")
        self.assertEqual(anthropic["status"], "rejected")
        self.assertIn("ANTHROPIC_API_KEY was rejected by anthropic (401", anthropic["detail"])
        self.assertIn("run: conch keys set ANTHROPIC_API_KEY", anthropic["detail"])
        self.assertTrue(anthropic["last_verified"].endswith("(now)"))
        # missing required → exit 1
        self.write_env("")
        code, data = self.run_json(["check"])
        self.assertEqual(code, 1)
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["status"], "missing")
        self.assertTrue(any("OPENROUTER_API_KEY" in p for p in data["problems"]))
        # unreachable → reported, not red
        self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\n")
        code, data = self.run_json(["check"], {"CONCH_OPENROUTER_API_BASE": closed_port_url() + "/v1"})
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["status"], "unreachable")
        self.assertEqual(code, 0)

    def test_check_verifies_both_values_of_a_stale_duplicate(self):
        self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\n")
        _, data = self.run_json(["check"], {"OPENROUTER_API_KEY": STALE_OR})
        row = self.row(data, "OPENROUTER_API_KEY")
        self.assertEqual(row["status"], "rejected")  # the shell export is effective and stale
        self.assertEqual(row["alternatives"]["env-file"][0], "ok")
        self.assertEqual(row["alternatives"]["shell-env"][0], "rejected")

    def test_jira_composio_mcp_e2b_slack_status(self):
        self.write_config(
            "provider = openrouter\nmodel = moonshotai/kimi-k3\nstatus_page_url = https://status.example\n"
            "exec_backend = e2b\n"
            f"e2b_api_url = {self.services.base}\nslack_channel = C1\nremote_enabled = true\n"
        )
        self.write_env(
            f"OPENROUTER_API_KEY={GOOD_OR}\nJIRA_API_TOKEN={JIRA_TOKEN}\nCOMPOSIO_API_KEY={COMPOSIO_KEY}\n"
            f"MCP_HOSTED_TOKEN={MCP_TOKEN}\nE2B_API_KEY={E2B_KEY}\nSLACK_BOT_TOKEN={SLACK_TOKEN}\n"
            "CONCH_STATUS_WRITE_TOKEN=status-token-0123456789abcdef\n"
        )
        self.write_mcp({
            "atlassian": {"command": "true", "env": {"JIRA_URL": self.services.base,
                                                     "JIRA_USERNAME": "me@example.com",
                                                     "JIRA_API_TOKEN": "${JIRA_API_TOKEN}"}},
            "hosted": {"type": "http", "url": self.services.mcp_url,
                       "headers": {"Authorization": "Bearer ${MCP_HOSTED_TOKEN}"}},
            "composio": {"type": "http", "url": self.services.mcp_url,
                         "headers": {"Authorization": "Bearer ${MCP_HOSTED_TOKEN}", "x-api-key": "${COMPOSIO_API_KEY}"}},
        })
        names = ["JIRA_API_TOKEN", "COMPOSIO_API_KEY", "MCP_HOSTED_TOKEN", "E2B_API_KEY",
                 "SLACK_BOT_TOKEN", "CONCH_STATUS_WRITE_TOKEN", "mcp/hosted", "OPENROUTER_API_KEY"]
        code, data = self.run_json(["check", *names])
        statuses = {item["name"]: item["status"] for item in data["credentials"]}
        self.assertEqual(statuses["JIRA_API_TOKEN"], "ok")
        self.assertEqual(statuses["COMPOSIO_API_KEY"], "ok")
        self.assertEqual(statuses["MCP_HOSTED_TOKEN"], "ok")  # propagated from mcp/hosted
        self.assertEqual(statuses["mcp/hosted"], "ok")
        self.assertEqual(statuses["E2B_API_KEY"], "ok")
        self.assertEqual(statuses["SLACK_BOT_TOKEN"], "ok")
        self.assertEqual(statuses["CONCH_STATUS_WRITE_TOKEN"], "skipped")
        self.assertIn("write-only", self.row(data, "CONCH_STATUS_WRITE_TOKEN")["detail"])
        self.assertEqual(code, 0, data["problems"])
        # rotate the hosted bearer out from under it: the server row and its referenced NAME both go red
        self.write_env(self.env_path.read_text().replace(MCP_TOKEN, "mcp-wrong-token-0123456789"))
        code, data = self.run_json(["check", "mcp/hosted", "MCP_HOSTED_TOKEN"])
        self.assertEqual(code, 1)
        self.assertEqual(self.row(data, "mcp/hosted")["status"], "rejected")
        hosted = self.row(data, "MCP_HOSTED_TOKEN")
        self.assertEqual(hosted["status"], "rejected")
        self.assertIn("MCP_HOSTED_TOKEN was rejected by mcp/hosted (401", hosted["detail"])
        self.assertIn("conch keys set MCP_HOSTED_TOKEN", hosted["detail"])

    def test_jira_rejected_names_token(self):
        self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\nJIRA_API_TOKEN=wrong-jira-token-0123456789\n")
        self.write_mcp({"atlassian": {"command": "true", "env": {
            "JIRA_URL": self.services.base, "JIRA_USERNAME": "me@example.com",
            "JIRA_API_TOKEN": "${JIRA_API_TOKEN}"}}})
        code, data = self.run_json(["check", "JIRA_API_TOKEN"])
        self.assertEqual(code, 1)
        row = self.row(data, "JIRA_API_TOKEN")
        self.assertEqual(row["status"], "rejected")
        self.assertTrue(row["detail"].startswith("JIRA_API_TOKEN was rejected by Jira"), row["detail"])

    def test_unknown_name_is_a_usage_error(self):
        code, text = self.run_keys(["check", "NOPE_KEY"])
        self.assertEqual(code, 2)
        self.assertIn("unknown credential", text)


class TestSetImportRotate(KeysCase):
    def test_set_refuses_rejected_then_saves_verified(self):
        code, text = self.run_keys(["set", "OPENROUTER_API_KEY", "--stdin"], stdin=STALE_OR + "\n")
        self.assertEqual(code, 1)
        self.assertIn("not saved", text)
        self.assertFalse(self.env_path.exists())
        code, text = self.run_keys(["set", "OPENROUTER_API_KEY", "--stdin"],
                                   {"OPENROUTER_API_KEY": STALE_OR}, stdin=GOOD_OR + "\n")
        self.assertEqual(code, 0, text)
        self.assertIn("✓ saved OPENROUTER_API_KEY", text)
        self.assertIn("conch-managed", text)
        self.assertIn("your shell still exports an older OPENROUTER_API_KEY", text)
        self.assertNotIn(GOOD_OR, text)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        with patch.dict(os.environ, self.base_env, clear=True):
            self.assertEqual(config_mod.read_env_file()["OPENROUTER_API_KEY"], GOOD_OR)
            self.assertEqual(config_mod.managed_key_names(), {"OPENROUTER_API_KEY"})
        # format problems are refused
        code, text = self.run_keys(["set", "OPENROUTER_API_KEY", "--stdin"], stdin="<YOUR-KEY>\n")
        self.assertEqual(code, 1)
        self.assertIn("placeholder", text)
        code, text = self.run_keys(["set", "OPENROUTER_API_KEY", "--stdin"], stdin="sk-or-has space\n")
        self.assertEqual(code, 1)
        self.assertIn("whitespace", text)

    def test_set_without_tty_and_without_stdin_explains(self):
        code, text = self.run_keys(["set", "OPENROUTER_API_KEY"])
        self.assertEqual(code, 2)
        self.assertIn("--stdin", text)

    def test_set_inline_mcp_entry_points_at_migrate(self):
        self.write_mcp({"hosted": {"type": "http", "url": self.services.mcp_url,
                                   "headers": {"Authorization": f"Bearer {MCP_TOKEN}"}}})
        code, text = self.run_keys(["set", "hosted:headers.Authorization", "--stdin"], stdin="x\n")
        self.assertEqual(code, 2)
        self.assertIn("migrate-mcp", text)

    def test_import_all_configured_saves_verified_shell_keys(self):
        code, text = self.run_keys(["import", "--all-configured", "--yes"],
                                   {"OPENROUTER_API_KEY": GOOD_OR, "ANTHROPIC_API_KEY": "sk-ant-bad-0123456789abcdef"})
        self.assertEqual(code, 1, text)  # the bad anthropic value was refused
        self.assertIn("OPENROUTER_API_KEY: saved", text)
        self.assertIn("not imported", text)
        with patch.dict(os.environ, self.base_env, clear=True):
            entries = config_mod.read_env_file()
        self.assertEqual(entries.get("OPENROUTER_API_KEY"), GOOD_OR)
        self.assertNotIn("ANTHROPIC_API_KEY", entries)

    def test_rotate_from_env_imports_and_ends_green(self):
        self.write_env(f"OPENROUTER_API_KEY={STALE_OR}\n")
        code, data = self.run_json(["rotate", "--from-env", "--yes"], {"OPENROUTER_API_KEY": GOOD_OR})
        self.assertEqual(code, 0, data["problems"])
        self.assertTrue(data["ok"])
        self.assertEqual(data["changed"], ["OPENROUTER_API_KEY"])
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["status"], "ok")
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["source"], "env file")
        with patch.dict(os.environ, self.base_env, clear=True):
            self.assertEqual(config_mod.read_env_file()["OPENROUTER_API_KEY"], GOOD_OR)
            self.assertEqual(config_mod.read_env_manifest()["keys"]["OPENROUTER_API_KEY"]["set_by"],
                             "conch keys rotate")

    def test_rotate_stdin_dotenv(self):
        code, data = self.run_json(["rotate", "--stdin", "--yes"],
                                   stdin=f"# rotated\nOPENROUTER_API_KEY={GOOD_OR}\n")
        self.assertEqual(code, 0, data["problems"])
        self.assertEqual(data["changed"], ["OPENROUTER_API_KEY"])

    def test_rotate_without_values_names_remaining_steps(self):
        code, data = self.run_json(["rotate", "--from-env", "--yes"])
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertTrue(any("OPENROUTER_API_KEY" in line for line in data["remaining"]))
        self.assertTrue(any(f["code"] == "envfile.created" for f in data["doctor"]))
        self.assertTrue(self.env_path.exists())
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_unset_removes_and_reports(self):
        self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\n")
        code, text = self.run_keys(["unset", "OPENROUTER_API_KEY"])
        self.assertEqual(code, 0)
        self.assertIn("removed OPENROUTER_API_KEY", text)
        code, _ = self.run_keys(["unset", "OPENROUTER_API_KEY"])
        self.assertEqual(code, 1)

    def test_unknown_subcommand_hint(self):
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit) as caught:
            cli.main(["rotat"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("did you mean rotate", err.getvalue())


class TestDaemonColumnRendering(unittest.TestCase):
    """What the daemon column says for each daemon state (the daemon is a
    separate process in real life, so its view is synthesized here)."""

    def make(self, fp="sha256:aaaaaaaa", status="ok"):
        from conch.keys.inventory import Credential, Inventory

        shell = Credential(name="K", env_name="K", required=True, effective="env file",
                           effective_fp=fp, status=status)
        registry = Credential(name="capitol registry (x)", effective="capitol registry (x)",
                              effective_fp="sha256:bbbbbbbb", required=True)
        inventory = Inventory([shell, registry], {"edge_daemon": "true"}, "/e", True, "/m", {})
        return inventory, shell, registry

    def test_states(self):
        from conch.keys.verify import annotate_daemon_column

        inventory, shell, registry = self.make()
        annotate_daemon_column(inventory, {"state": "ok", "keys": {"K": {"present": True, "fingerprint": "sha256:aaaaaaaa"}}})
        self.assertEqual(shell.daemon, "ok (same key)")
        self.assertEqual(registry.daemon, "n/a")
        annotate_daemon_column(inventory, {"state": "ok", "keys": {"K": {"present": True, "fingerprint": "sha256:cccccccc"}}})
        self.assertEqual(shell.daemon, "stale (sha256:cccccccc) → conch keys reload")
        annotate_daemon_column(inventory, {"state": "ok", "keys": {"K": {"present": False}}})
        self.assertEqual(shell.daemon, "missing (daemon-blind)")
        annotate_daemon_column(inventory, {"state": "protocol"})
        self.assertEqual(shell.daemon, "? (old daemon: restart)")
        annotate_daemon_column(inventory, {"state": "not running"})
        self.assertEqual(shell.daemon, "not running")
        annotate_daemon_column(inventory, {"state": "disabled"})
        self.assertEqual(shell.daemon, "—")

    def test_exit_rule_covers_daemon(self):
        from conch.keys.cli import _required_ok

        inventory, shell, registry = self.make()
        shell.daemon = "stale (sha256:cccccccc) → conch keys reload"
        ok, problems = _required_ok([shell, registry], "ok")
        self.assertFalse(ok)
        self.assertEqual(problems, ["K: daemon stale (sha256:cccccccc)"])
        shell.daemon = "ok (same key)"
        self.assertTrue(_required_ok([shell, registry], "ok")[0])
        ok, problems = _required_ok([shell, registry], "protocol")
        self.assertFalse(ok)
        self.assertIn("predates conch keys", problems[0])
        shell.status = "unreachable"
        self.assertTrue(_required_ok([shell, registry], "disabled")[0])
        shell.status = "rejected"
        self.assertFalse(_required_ok([shell, registry], "disabled")[0])


class TestDaemonColumn(KeysCase):
    """The daemon column against a real in-process EdgeDaemon."""

    def start_daemon(self, env):
        from conch.kernel.daemon import EdgeDaemon

        def factory(mission, messages, control_client, caps):
            return "x", {}

        with patch.dict(os.environ, env, clear=True):
            config_mod._ENV_LOAD_STATE["from_file"].clear()
            config = config_mod.load_config()
            config.update({"mission_reviews": "false", "mission_consolidation": "false"})
            daemon = EdgeDaemon(config, session_factory=factory)
            daemon.start()

        def shutdown():
            # Under the same isolated environment the daemon was started in:
            # a shutdown must never resolve paths against the real HOME.
            with patch.dict(os.environ, env, clear=True):
                daemon.shutdown()

        self.addCleanup(shutdown)
        return daemon

    def test_daemon_sees_reload_and_column(self):
        self.write_config("provider = openrouter\nmodel = moonshotai/kimi-k3\nedge_daemon = true\n")
        self.write_env(f"OPENROUTER_API_KEY={STALE_OR}\n")
        env = dict(self.base_env)
        daemon = self.start_daemon(env)
        with patch.dict(os.environ, env, clear=True):
            config_mod._ENV_LOAD_STATE["from_file"].clear()
            config_mod.load_env_file()  # what the daemon process did at startup
            status = daemon.credential_status(["OPENROUTER_API_KEY", "NOPE"])
        self.assertTrue(status["keys"]["OPENROUTER_API_KEY"]["present"])
        self.assertEqual(status["keys"]["OPENROUTER_API_KEY"]["fingerprint"], config_mod.key_fingerprint(STALE_OR))
        self.assertEqual(status["keys"]["OPENROUTER_API_KEY"]["source"], "env file")
        self.assertFalse(status["keys"]["NOPE"]["present"])
        self.assertNotIn(STALE_OR, json.dumps(status))
        code, data = self.run_json(["check"])
        self.assertEqual(data["daemon"]["state"], "ok")
        self.assertEqual(data["daemon"]["pid"], os.getpid())
        self.assertEqual(code, 1)  # the stale key is rejected in both columns
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["daemon"], "rejected (same key)")
        # rotate --from-env saves it and nudges the daemon over the socket → green in both columns
        code, data = self.run_json(["rotate", "--from-env", "--yes"], {"OPENROUTER_API_KEY": GOOD_OR})
        self.assertEqual(code, 0, data["problems"])
        self.assertEqual(self.row(data, "OPENROUTER_API_KEY")["daemon"], "ok (same key)")
        # (same process as the CLI here, so the reload finds the value already live)
        self.assertIn("credentials reload (conch keys rotate)", daemon.log_path.read_text())

    def test_tick_reloads_on_env_file_change_and_sighup(self):
        self.write_config("provider = openrouter\nmodel = moonshotai/kimi-k3\nedge_daemon = true\n")
        self.write_env(f"OPENROUTER_API_KEY={STALE_OR}\n")
        env = dict(self.base_env)
        daemon = self.start_daemon(env)
        with patch.dict(os.environ, env, clear=True):
            config_mod.load_env_file()
            daemon.tick()
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], STALE_OR)
            # mtime/size change → reloaded on the next tick
            self.write_env(f"OPENROUTER_API_KEY={GOOD_OR}\n")
            os.utime(self.env_path, (1, 1))
            daemon.tick()
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], GOOD_OR)
            log = daemon.log_path.read_text()
            self.assertIn("credentials reloaded (env file changed): OPENROUTER_API_KEY sha256:", log)
            self.assertNotIn(GOOD_OR, log)
            # SIGHUP flag → reload on the tick thread
            daemon._reload_requested.set()
            daemon.tick()
            self.assertIn("credentials reload (SIGHUP): no change", daemon.log_path.read_text())


if __name__ == "__main__":
    unittest.main()
