"""``${NAME}`` references in mcp.json (env, headers, url, args): expanded
from the environment; a missing variable is a loud per-server error that
names the server and the variable and never ships a blank header; 401s
name the referenced key; ``migrate-mcp`` moves inline secrets into the
env file (dry run vs apply, backup, 0600)."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import mcp
from conch.keys import mcpmigrate
from conch.keys.inventory import inline_secrets, load_mcp_servers


class McpRefsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "conch"
        self.config_dir.mkdir(parents=True)
        self.mcp_path = self.config_dir / "mcp.json"
        self._env = patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in ("REF_TOKEN", "REF_URL_KEY", "REF_ARG", "JIRA_API_TOKEN", "MCP_HOSTED_TOKEN"):
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)
        patcher = patch.object(mcp, "CONFIG_PATH", self.mcp_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_mcp(self, servers: dict) -> None:
        self.mcp_path.write_text(json.dumps({"mcpServers": servers}, indent=2))


class TestExpansion(McpRefsCase):
    def test_env_headers_url_args_expand(self):
        os.environ.update({"REF_TOKEN": "tok", "REF_URL_KEY": "urlkey", "REF_ARG": "argval"})
        self.assertEqual(mcp._expand_env({"Authorization": "Bearer ${REF_TOKEN}"}, server="s"),
                         {"Authorization": "Bearer tok"})
        self.assertEqual(mcp.expand_references("https://h/x?k=${REF_URL_KEY}", server="s", field="url"),
                         "https://h/x?k=urlkey")
        self.assertEqual(mcp._expand_args(["--token", "$REF_ARG", 3], server="s"),
                         ["--token", "argval", "3"])

    def test_missing_variable_names_server_field_and_fix(self):
        with self.assertRaises(mcp.McpConfigError) as caught:
            mcp._expand_env({"Authorization": "Bearer ${REF_TOKEN}"}, server="hosted", field="headers")
        text = str(caught.exception)
        self.assertIn("'hosted'", text)
        self.assertIn("headers.Authorization", text)
        self.assertIn("${REF_TOKEN}", text)
        self.assertIn("conch keys set REF_TOKEN", text)
        self.assertEqual(caught.exception.server, "hosted")

    def test_empty_variable_counts_as_missing(self):
        os.environ["REF_TOKEN"] = "   "
        with self.assertRaises(mcp.McpConfigError):
            mcp.expand_references("${REF_TOKEN}", server="s", field="url")

    def test_referenced_names_in_order(self):
        names = mcp.referenced_env_names({
            "headers": {"a": "${ONE} $TWO"}, "args": ["${THREE}", "${ONE}"], "url": "x$FOUR",
        })
        self.assertEqual(names, ["ONE", "TWO", "THREE", "FOUR"])

    def test_create_clients_marks_only_the_broken_server(self):
        os.environ["REF_TOKEN"] = "tok"
        self.write_mcp({
            "good": {"type": "http", "url": "http://127.0.0.1:9/mcp",
                     "headers": {"Authorization": "Bearer ${REF_TOKEN}"}},
            "broken": {"type": "http", "url": "http://127.0.0.1:9/mcp",
                       "headers": {"Authorization": "Bearer ${JIRA_API_TOKEN}"}},
            "stdio": {"command": "true", "env": {"JIRA_API_TOKEN": "${JIRA_API_TOKEN}"}},
        })
        clients = mcp.create_clients()
        self.assertIsInstance(clients["good"], mcp.HttpMcpClient)
        self.assertEqual(clients["good"].headers["Authorization"], "Bearer tok")
        self.assertEqual(clients["good"].credential_names, ["REF_TOKEN"])
        broken = clients["broken"]
        self.assertIsInstance(broken, mcp.MisconfiguredMcpClient)
        self.assertEqual(broken.list_tools(), [])
        self.assertIn("JIRA_API_TOKEN", broken.last_error)
        self.assertIn("conch keys set JIRA_API_TOKEN", broken.last_error)
        self.assertFalse(broken.unreachable)
        stdio = clients["stdio"]
        self.assertIsInstance(stdio, mcp.StdioMcpClient)
        self.assertEqual(stdio.list_tools(), [])
        self.assertIn("'stdio' env.JIRA_API_TOKEN references ${JIRA_API_TOKEN}", stdio.last_error)
        stdio.close()
        statuses = mcp.server_statuses(clients, {})
        self.assertEqual(statuses["broken"].state, "error")
        self.assertIn("JIRA_API_TOKEN", statuses["broken"].detail)
        for client in clients.values():
            client.close()

    def test_http_401_names_the_referenced_secret(self):
        text = mcp.name_rejected_credential("atlassian", "HTTP 401: nope",
                                            ["JIRA_URL", "JIRA_API_TOKEN"])
        self.assertTrue(text.startswith("JIRA_API_TOKEN was rejected by mcp/atlassian (401"))
        self.assertIn("run: conch keys set JIRA_API_TOKEN", text)
        self.assertIn("also referenced: JIRA_URL", text)
        plain = mcp.name_rejected_credential("q", "HTTP 403: forbidden", [])
        self.assertIn("mcp/q rejected its credential (403", plain)
        self.assertIn("migrate-mcp", plain)
        self.assertEqual(mcp.name_rejected_credential("q", "timed out", ["X"]), "timed out")


class TestMigrateMcp(McpRefsCase):
    def servers(self):
        return {
            "atlassian": {"command": "uvx", "args": ["mcp-atlassian"],
                          "env": {"JIRA_URL": "https://x.atlassian.net", "JIRA_USERNAME": "me@x.io",
                                  "JIRA_API_TOKEN": "jira-token-value-0123456789"}},
            "hosted": {"type": "http", "url": "https://h/mcp?api_key=urlsecret0123456789",
                       "headers": {"Authorization": "Bearer hosted-bearer-0123456789"}},
            "cli": {"command": "srv", "args": ["--token", "argsecret0123456789", "--verbose"]},
            "clean": {"type": "http", "url": "https://c/mcp", "headers": {"Authorization": "Bearer ${MCP_HOSTED_TOKEN}"}},
        }

    def test_inline_detection_skips_non_secrets_and_references(self):
        found = {s.location: s for s in inline_secrets(self.servers())}
        self.assertEqual(set(found), {
            "atlassian env.JIRA_API_TOKEN", "hosted headers.Authorization",
            "hosted url?api_key", "cli args[1]",
        })
        self.assertEqual(found["hosted headers.Authorization"].value, "hosted-bearer-0123456789")
        self.assertEqual(found["hosted headers.Authorization"].suggested_name, "MCP_HOSTED_TOKEN")
        self.assertEqual(found["atlassian env.JIRA_API_TOKEN"].suggested_name, "JIRA_API_TOKEN")

    def test_dry_run_writes_nothing(self):
        self.write_mcp(self.servers())
        before = self.mcp_path.read_text()
        plan = mcpmigrate.plan_migration(self.mcp_path)
        self.assertEqual(len(plan.secrets), 4)
        self.assertEqual(self.mcp_path.read_text(), before)
        self.assertFalse((self.config_dir / "env").exists())
        for line in plan.lines():
            self.assertNotIn("0123456789", line)  # fingerprints, never values

    def test_apply_backs_up_rewrites_and_saves_managed(self):
        self.write_mcp(self.servers())
        os.chmod(self.mcp_path, 0o644)
        plan = mcpmigrate.plan_migration(self.mcp_path)
        result = mcpmigrate.apply_migration(plan)
        backup = Path(result["backup"])
        self.assertTrue(backup.exists())
        self.assertIn("jira-token-value", backup.read_text())
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        rewritten = json.loads(self.mcp_path.read_text())["mcpServers"]
        self.assertEqual(rewritten["atlassian"]["env"]["JIRA_API_TOKEN"], "${JIRA_API_TOKEN}")
        self.assertEqual(rewritten["atlassian"]["env"]["JIRA_USERNAME"], "me@x.io")
        self.assertEqual(rewritten["hosted"]["headers"]["Authorization"], "Bearer ${MCP_HOSTED_TOKEN}")
        self.assertIn("api_key=${MCP_HOSTED_API_KEY}", rewritten["hosted"]["url"])
        self.assertEqual(rewritten["cli"]["args"], ["--token", "${MCP_CLI_TOKEN}", "--verbose"])
        self.assertEqual(rewritten["clean"]["headers"]["Authorization"], "Bearer ${MCP_HOSTED_TOKEN}")
        self.assertEqual(stat.S_IMODE(self.mcp_path.stat().st_mode), 0o600)
        self.assertNotIn("0123456789", self.mcp_path.read_text())
        from conch.config import read_env_file, read_env_manifest

        entries = read_env_file()
        self.assertEqual(entries["JIRA_API_TOKEN"], "jira-token-value-0123456789")
        self.assertEqual(entries["MCP_HOSTED_TOKEN"], "hosted-bearer-0123456789")
        self.assertEqual(entries["MCP_HOSTED_API_KEY"], "urlsecret0123456789")
        self.assertEqual(entries["MCP_CLI_TOKEN"], "argsecret0123456789")
        self.assertEqual(read_env_manifest()["keys"]["JIRA_API_TOKEN"]["set_by"], "conch keys migrate-mcp")
        # The loader now resolves them by reference.
        clients = mcp.create_clients()
        self.assertEqual(clients["hosted"].headers["Authorization"], "Bearer hosted-bearer-0123456789")
        self.assertIn("api_key=urlsecret0123456789", clients["hosted"].url)
        for client in clients.values():
            client.close()
        servers, error = load_mcp_servers(self.mcp_path)
        self.assertEqual(error, "")
        self.assertEqual(inline_secrets(servers), [])

    def test_apply_refuses_on_name_conflict(self):
        self.write_mcp(self.servers())
        from conch.config import set_env_values

        set_env_values({"JIRA_API_TOKEN": "a-different-value-0123456789"})
        plan = mcpmigrate.plan_migration(self.mcp_path)
        self.assertTrue(plan.conflicts)
        with self.assertRaises(ValueError):
            mcpmigrate.apply_migration(plan)
        self.assertIn("jira-token-value-0123456789", self.mcp_path.read_text())
        self.assertFalse(list(self.config_dir.glob("mcp.json.bak-*")))


if __name__ == "__main__":
    unittest.main()
