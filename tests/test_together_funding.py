"""Together Fund funding-intake pipeline gates.

The builders and driver behind the live ``together-funding-*`` assets:

- graph payloads are structurally sound (every edge endpoint resolves;
  every bound param rides a ``port_to_param`` edge with its
  ``target_param_id`` — the EY local-stack gotcha), deterministic across
  rebuilds, and published;
- the ingest agent's authority is configuration, not prose: the packet
  workflow is the single delegable entry, version-pinned, launch budget
  unbounded, dedupe key format pinned;
- provisioning drives every mutation through CapitolAdmin's ledger
  discipline against the scripted fake gateway (create → replay), plus
  the qdrant ledger-collection ensure through the same
  record→effect→resolve shape against a fake MCP server;
- onboarding provisions per-member Composio pipes and surfaces auth URLs;
- the run driver reshapes ``<node>.<field>`` overrides into the runs
  API's ``InputNodeOverride`` list.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conch.capitol import together_funding as tf
from conch.capitol.errors import CapitolError

from tests.test_capitol_admin import FakeAdminGateway, ORG, TOKEN


# ---------------------------------------------------------------------------
# Minimal catalog fixture: exactly the structs the builders consume.
# ---------------------------------------------------------------------------

def _param(field_id, value="", *, bindable=False, cardinality="one"):
    param = {
        "field_id": field_id,
        "display_name": field_id,
        "valid_types": ["str"],
        "required": False,
        "value": value,
        "default": value,
        "hidden": False,
    }
    if bindable:
        param["bindable_config"] = {
            "is_bound": False,
            "binding_type": "param",
            "input_port": {
                "id": "",
                "name": field_id,
                "category": "input",
                "display_name": field_id,
                "cardinality": cardinality,
                "port_type": {"accepted_types": ["str", "dict", "list"]},
                "optional": True,
                "incoming_connections": [],
            },
        }
    return param


def _entry(node_id, *, node_type="tool", params=(), out_ports=("tool",),
           in_ports=()):
    return {
        "node_id": node_id,
        "display_name": node_id,
        "description": node_id,
        "node_type": node_type,
        "node_sub_type": "fixture",
        "class_name": node_id.title().replace("_", "") + "Node",
        "module": "abc",
        "full_name": f"abc.{node_id}",
        "name": node_id,
        "version": "0.1.0",
        "show": True,
        "beta": False,
        "can_be_tool": node_type == "tool",
        "has_chat": False,
        "icon": "CogIcon",
        "input_ports": [
            {
                "id": f"fixture-in-{name}",
                "name": name,
                "category": "input",
                "display_name": name,
                "cardinality": "many",
                "port_type": {"accepted_types": ["tools"]},
                "optional": True,
                "incoming_connections": [],
            }
            for name in in_ports
        ],
        "output_ports": [
            {
                "id": f"fixture-out-{name}",
                "name": name,
                "category": "output",
                "display_name": name,
                "cardinality": "one",
                "port_type": {"accepted_types": ["str"]},
                "optional": False,
            }
            for name in out_ports
        ],
        "params": [json.loads(json.dumps(p)) for p in params],
    }


def fixture_catalog():
    agent_params = [
        _param("name"), _param("model", "claude-sonnet-5"),
        _param("system_prompt"),
        _param("user_prompt", bindable=True),
        _param("temperature", 0.7), _param("timeout", 120),
        _param("retries", 3), _param("tool_choice", "auto"),
        _param("max_tokens", None),
        _param("delegable_workflows", []),
        _param("delegable_workflow_launch_budget", "at_most_one"),
    ]
    entries = [
        _entry("text_input_node", node_type="input",
               params=[_param("text_input")], out_ports=("text",)),
        _entry("json_input_node", node_type="input",
               params=[_param("mode", "value"), _param("value"),
                       _param("on_invalid", "error")],
               out_ports=("value", "digest", "valid", "provenance")),
        _entry("agent_node", node_type="agent", params=agent_params,
               out_ports=("text",), in_ports=("tool",)),
        _entry("EXECUTE_COMPOSIO_TOOL",
               params=[_param("composio_apps", [])]),
        _entry("markdown_output_node", node_type="output",
               params=[_param("markdown_content", bindable=True)],
               out_ports=("markdown_output",)),
        _entry("docx_chat_node", node_type="output",
               params=[_param("model", "claude-opus-4-6"),
                       _param("request", bindable=True),
                       _param("ground_with_provided_info", False)],
               out_ports=("docx_output",)),
        _entry("notify_node", node_type="output",
               params=[_param("title"),
                       _param("message", bindable=True),
                       _param("event_subtype")],
               out_ports=("delivered", "event_key", "digest")),
        _entry("union_collect_node", node_type="logic",
               params=[_param("mode", "collect"),
                       _param("values", bindable=True,
                              cardinality="many")],
               out_ports=("items", "digest", "input_count")),
        _entry("reduce_node", node_type="logic",
               params=[_param("operation", "concat"),
                       _param("separator", ""),
                       _param("items", bindable=True)],
               out_ports=("value", "digest")),
        _entry("unstructured_collection_ingest_node", node_type="data",
               params=[_param("files", None, bindable=True),
                       _param("collection", []),
                       _param("documents", None, bindable=True),
                       _param("conflict_policy", "skip"),
                       _param("wait_for_index", True),
                       _param("index_timeout_seconds", 600),
                       _param("request_timeout_seconds", 120),
                       _param("max_concurrency", 3)],
               out_ports=("summary", "receipts", "failed", "clean",
                          "digest")),
    ]
    for tool in (tf.LEDGER_TOOL_IDS + tf.RESEARCH_TOOL_IDS):
        entries.append(_entry(tool))
    return {entry["node_id"]: entry for entry in entries}


LEDGER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def build_packet(catalog, **kwargs):
    kwargs.setdefault("ledger_collection_id", LEDGER_ID)
    return tf.build_packet_payload(catalog, **kwargs)


def build_ingest(catalog, **kwargs):
    kwargs.setdefault("ledger_collection_id", LEDGER_ID)
    return tf.build_ingest_payload(catalog, **kwargs)


def assert_graph_integrity(case, payload):
    nodes = {n["id"]: n["data"]["struct"] for n in payload["nodes"]}
    case.assertEqual(
        len(nodes), len(payload["nodes"]), "duplicate node ids"
    )
    for edge in payload["edges"]:
        case.assertIn(edge["source"], nodes, edge["id"])
        case.assertIn(edge["target"], nodes, edge["id"])
        source_ports = {
            p["id"] for p in nodes[edge["source"]]["output_ports"]
        }
        case.assertIn(edge["sourceHandle"], source_ports, edge["id"])
        target = nodes[edge["target"]]
        target_ports = {p["id"] for p in target.get("input_ports", [])}
        for param in target.get("params", []):
            port = (param.get("bindable_config") or {}).get("input_port")
            if port and port.get("id"):
                target_ports.add(port["id"])
        case.assertIn(edge["targetHandle"], target_ports, edge["id"])
        if edge["edge_type"] == "port_to_param":
            case.assertTrue(edge["target_param_id"], edge["id"])
    # every bound param's edge carries port_to_param + target_param_id
    for node_id, struct in nodes.items():
        for param in struct.get("params", []):
            bindable = param.get("bindable_config") or {}
            if not bindable.get("is_bound"):
                continue
            port_id = (bindable.get("input_port") or {}).get("id")
            matching = [
                e for e in payload["edges"]
                if e["target"] == node_id and e["targetHandle"] == port_id
            ]
            case.assertTrue(
                matching,
                f"bound param {param['field_id']} on {node_id} has no edge",
            )
            for edge in matching:
                case.assertEqual(edge["edge_type"], "port_to_param")
                case.assertEqual(
                    edge["target_param_id"], param["field_id"]
                )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

class BuilderCase(unittest.TestCase):
    def setUp(self):
        self.catalog = fixture_catalog()

    def test_dedupe_key_format(self):
        self.assertEqual(
            tf.dedupe_key("18c2f4a9"), "together:funding:18c2f4a9"
        )
        with self.assertRaises(CapitolError):
            tf.dedupe_key("  ")

    def test_packet_payload_structure(self):
        payload = build_packet(self.catalog)
        assert_graph_integrity(self, payload)
        self.assertEqual(payload["name"], tf.PACKET_WORKFLOW_NAME)
        self.assertTrue(payload["publish_to_api"])
        self.assertFalse(payload["allow_clarifications"])
        by_name = {
            n["data"]["struct"].get("name"): n["data"]["struct"]
            for n in payload["nodes"]
        }
        self.assertIn("Company Researcher", by_name)
        self.assertIn("Packet Recorder", by_name)
        researcher = by_name["Company Researcher"]
        prompts = {
            p["field_id"]: p.get("value")
            for p in researcher["params"]
        }
        self.assertEqual(
            prompts["system_prompt"], tf.RESEARCH_PROMPT_PREFIX
        )
        # the request input is the single overridable entry point
        self.assertEqual(
            payload["nodes"][0]["id"], tf.PACKET_INPUT_NODE_ID
        )
        self.assertTrue(
            tf.PACKET_INPUT_KEY.startswith(tf.PACKET_INPUT_NODE_ID)
        )

    def test_packet_rebuild_is_deterministic(self):
        first = build_packet(self.catalog)
        second = build_packet(self.catalog)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )

    def test_ingest_payload_delegable_entry_is_pinned(self):
        payload = build_ingest(
            self.catalog,
            packet_workflow_id="11111111-2222-3333-4444-555555555555",
            packet_version_id="99999999-8888-7777-6666-555555555555",
        )
        assert_graph_integrity(self, payload)
        self.assertTrue(payload["publish_to_api"])
        triage = next(
            n["data"]["struct"] for n in payload["nodes"]
            if n["data"]["struct"].get("name") == "Funding Triage"
        )
        params = {p["field_id"]: p.get("value") for p in triage["params"]}
        entries = params["delegable_workflows"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(
            entry["workflow_id"], "11111111-2222-3333-4444-555555555555"
        )
        self.assertEqual(
            entry["version_id"], "99999999-8888-7777-6666-555555555555"
        )
        self.assertEqual(entry["alias"], tf.PACKET_ALIAS)
        self.assertEqual(entry["launch_budget"], "unbounded")
        self.assertEqual(
            params["delegable_workflow_launch_budget"], "unbounded"
        )
        # the system prompt names the exact input key, dedupe prefix, and
        # ledger collection id — never a placeholder
        prompt = params["system_prompt"]
        self.assertIn(tf.PACKET_INPUT_KEY, prompt)
        self.assertIn("together:funding:", prompt)
        self.assertIn(LEDGER_ID, prompt)
        self.assertNotIn("<PACKET_INPUT_KEY>", prompt)
        self.assertNotIn("<LEDGER_COLLECTION_ID>", prompt)

    def test_ingest_requires_packet_workflow_id(self):
        with self.assertRaises(CapitolError):
            build_ingest(self.catalog, packet_workflow_id="")

    def test_prompts_carry_no_secret_material(self):
        for text in (tf.TRIAGE_SYSTEM_PROMPT, tf.RESEARCH_PROMPT_PREFIX,
                     tf.RECORDER_SYSTEM_PROMPT):
            lowered = text.lower()
            for marker in ("cap_a2a_", "x-api-key", "bearer ",
                           "api key:", "jwt"):
                self.assertNotIn(marker, lowered)

    def test_gmail_surface_is_read_only(self):
        self.assertEqual(
            tf.GMAIL_TOOLS,
            ["GMAIL_GET_PROFILE", "GMAIL_FETCH_EMAILS",
             "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"],
        )
        payload = build_ingest(
            self.catalog,
            packet_workflow_id="11111111-2222-3333-4444-555555555555",
        )
        composio = next(
            n["data"]["struct"] for n in payload["nodes"]
            if n["data"]["struct"]["node_id"] == "EXECUTE_COMPOSIO_TOOL"
        )
        apps = next(
            p["value"] for p in composio["params"]
            if p["field_id"] == "composio_apps"
        )
        for slug in apps:
            self.assertNotIn("SEND", slug)
            self.assertNotIn("DELETE", slug)
            self.assertNotIn("MODIFY", slug)

    def test_synthetic_fixtures_cover_both_classes(self):
        fixtures = tf.synthetic_fixtures("m1")
        self.assertEqual(len(fixtures), 2)
        ids = {f["gmail_message_id"] for f in fixtures}
        self.assertEqual(len(ids), 2)
        self.assertTrue(any("raising" in f["subject"] for f in fixtures))
        self.assertTrue(
            any("raising" not in f["subject"] for f in fixtures)
        )
        # marker keeps drills isolated; same marker replays the same keys
        self.assertEqual(
            {f["gmail_message_id"] for f in tf.synthetic_fixtures("m1")},
            ids,
        )


# ---------------------------------------------------------------------------
# Fake onboarding endpoints
# ---------------------------------------------------------------------------

class FakeOnboardGateway(BaseHTTPRequestHandler):
    """The composio-configs surfaces: provision (first call pending with
    an auth URL; 'broken' members reproduce the v3.32 short-local-part
    server-name bug), config-row list/create."""

    connected_members = set()
    broken_members = set()
    config_rows = {}
    requests = []

    @classmethod
    def reset(cls):
        cls.connected_members = set()
        cls.broken_members = set()
        cls.config_rows = {}
        cls.requests = []

    def log_message(self, *_args):
        pass

    def _send(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        cls = self.__class__
        cls.requests.append((self.path, None))
        if "/users/" in self.path:
            email = self.path.rsplit("/users/", 1)[1].split("/")[0]
            self._send({"items": cls.config_rows.get(email, [])})
            return
        self._send({"detail": "unhandled"}, 404)

    def do_POST(self):
        cls = self.__class__
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        cls.requests.append((self.path, body))
        email = self.path.rsplit("/users/", 1)[1].split("/")[0]
        if self.path.endswith("/provision"):
            if email in cls.broken_members:
                self._send({
                    "success": False,
                    "error": "Failed to provision Composio MCP server",
                    "mcp_server_id": None, "connected": [],
                    "missing": [], "auth_urls": {},
                })
                return
            connected = email in cls.connected_members
            self._send({
                "success": True,
                "mcp_server_id": f"mcp-{email.split('@')[0]}",
                "connected": ["gmail"] if connected else [],
                "missing": [] if connected else ["gmail"],
                "auth_urls": (
                    {} if connected
                    else {"gmail": f"https://connect.example/{email}"}
                ),
            })
            return
        # config-row create
        cls.config_rows.setdefault(email, []).append(body)
        self._send(dict(body, id="row-1"), 201)


class FakeComposio(BaseHTTPRequestHandler):
    """Composio v3: servers, auth configs, connected accounts."""

    servers = []
    accounts = []
    requests = []

    @classmethod
    def reset(cls):
        cls.servers = []
        cls.accounts = []
        cls.requests = []

    def log_message(self, *_args):
        pass

    def _send(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        cls = self.__class__
        cls.requests.append(("GET", self.path, None))
        if self.path.startswith("/api/v3/mcp/servers"):
            self._send({"items": cls.servers})
            return
        if self.path.startswith("/api/v3/auth_configs"):
            self._send({"items": [{
                "id": "ac_fake_gmail", "status": "ENABLED",
                "is_composio_managed": True, "connections_count": 5,
            }]})
            return
        if self.path.startswith("/api/v3/connected_accounts"):
            user = ""
            if "user_ids=" in self.path:
                user = self.path.split("user_ids=")[1].split("&")[0]
            self._send({"items": [
                account for account in cls.accounts
                if account.get("user_id") == user
            ]})
            return
        self._send({"detail": "unhandled"}, 404)

    def do_POST(self):
        cls = self.__class__
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        cls.requests.append(("POST", self.path, body))
        if self.path.startswith("/api/v3/mcp/servers"):
            server = {"id": f"srv-{len(cls.servers) + 1}",
                      "name": body.get("name"),
                      "toolkits": body.get("no_auth_apps") or []}
            cls.servers.append(server)
            self._send(server, 201)
            return
        if self.path.startswith("/api/v3/connected_accounts"):
            account = {
                "id": f"ca-{len(cls.accounts) + 1}",
                "user_id": (body.get("connection") or {}).get("user_id"),
                "status": "INITIATED",
                "redirect_url": "https://composio.example/oauth/start",
            }
            cls.accounts.append(account)
            self._send(account, 201)
            return
        self._send({"detail": "unhandled"}, 404)


class OnboardingCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeOnboardGateway
        )
        cls.port = cls.server.server_address[1]
        threading.Thread(
            target=cls.server.serve_forever, daemon=True
        ).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOnboardGateway.reset()
        self.base = f"http://127.0.0.1:{self.port}"

    def test_pending_member_gets_auth_url(self):
        row = tf.provision_member(self.base, TOKEN, ORG, "Ada@Fund.VC")
        self.assertEqual(row["email"], "ada@fund.vc")  # normalized
        self.assertEqual(row["missing"], ["gmail"])
        self.assertEqual(
            row["auth_url"], "https://connect.example/ada@fund.vc"
        )

    def test_connected_member_reports_live_pipe(self):
        FakeOnboardGateway.connected_members.add("tom@capitol.ai")
        row = tf.provision_member(self.base, TOKEN, ORG, "tom@capitol.ai")
        self.assertEqual(row["connected"], ["gmail"])
        self.assertEqual(row["auth_url"], "")

    def test_onboard_members_is_per_member_and_total(self):
        FakeOnboardGateway.connected_members.add("live@fund.vc")
        lines = []
        rows = tf.onboard_members(
            self.base, TOKEN, ORG,
            ["live@fund.vc", "new@fund.vc"], log=lines.append,
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("CONNECTED" in line for line in lines))
        self.assertTrue(any("PENDING" in line for line in lines))
        # one provision call per member (idempotent server-side)
        self.assertEqual(len(FakeOnboardGateway.requests), 2)

    def test_rejects_non_email(self):
        with self.assertRaises(CapitolError):
            tf.provision_member(self.base, TOKEN, ORG, "not-an-email")


class OnboardingFallbackCase(unittest.TestCase):
    """The direct v3 path around the platform's server-name bug."""

    @classmethod
    def setUpClass(cls):
        cls.gateway = ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeOnboardGateway
        )
        cls.composio = ThreadingHTTPServer(("127.0.0.1", 0), FakeComposio)
        for server in (cls.gateway, cls.composio):
            threading.Thread(
                target=server.serve_forever, daemon=True
            ).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.gateway, cls.composio):
            server.shutdown()
            server.server_close()

    def setUp(self):
        import os
        from unittest.mock import patch

        FakeOnboardGateway.reset()
        FakeComposio.reset()
        self.base = f"http://127.0.0.1:{self.gateway.server_address[1]}"
        self.composio_base = (
            f"http://127.0.0.1:{self.composio.server_address[1]}"
        )
        patcher = patch.dict(
            os.environ, {"COMPOSIO_API_KEY": "test-key"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fallback_provisions_and_returns_auth_url(self):
        FakeOnboardGateway.broken_members.add("ada@fund.vc")
        row = tf.provision_member(
            self.base, TOKEN, ORG, "ada@fund.vc",
            composio_base=self.composio_base,
        )
        self.assertEqual(row["via"], "conch-fallback")
        self.assertEqual(
            row["auth_url"], "https://composio.example/oauth/start"
        )
        # a safe server name (no '@'), created once
        self.assertEqual(len(FakeComposio.servers), 1)
        self.assertEqual(
            FakeComposio.servers[0]["name"], "capitol-ada-fund-vc"
        )
        # the config row landed through the platform API
        self.assertEqual(
            FakeOnboardGateway.config_rows["ada@fund.vc"][0][
                "composio_mcp_config_id"
            ],
            "srv-1",
        )
        # idempotent: second call reuses the server and config row
        row2 = tf.provision_member(
            self.base, TOKEN, ORG, "ada@fund.vc",
            composio_base=self.composio_base,
        )
        self.assertEqual(len(FakeComposio.servers), 1)
        self.assertEqual(
            len(FakeOnboardGateway.config_rows["ada@fund.vc"]), 1
        )
        self.assertTrue(row2["auth_url"])

    def test_fallback_reports_active_connection(self):
        FakeOnboardGateway.broken_members.add("sam@fund.vc")
        FakeComposio.accounts.append({
            "id": "ca-live", "user_id": "sam@fund.vc",
            "status": "ACTIVE",
        })
        row = tf.provision_member(
            self.base, TOKEN, ORG, "sam@fund.vc",
            composio_base=self.composio_base,
        )
        self.assertEqual(row["connected"], ["gmail"])
        self.assertEqual(row["auth_url"], "")
        self.assertEqual(row["connected_account_id"], "ca-live")

    def test_fallback_requires_composio_key(self):
        import os
        from unittest.mock import patch

        FakeOnboardGateway.broken_members.add("kim@fund.vc")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPOSIO_API_KEY", None)
            with self.assertRaises(CapitolError) as raised:
                tf.provision_member(
                    self.base, TOKEN, ORG, "kim@fund.vc",
                    composio_base=self.composio_base,
                )
        self.assertIn("COMPOSIO_API_KEY", str(raised.exception))


# ---------------------------------------------------------------------------
# Provisioning drill against the scripted fakes
# ---------------------------------------------------------------------------

class ProvisionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeAdminGateway
        )
        cls.admin_port = cls.admin_server.server_address[1]
        threading.Thread(
            target=cls.admin_server.serve_forever, daemon=True
        ).start()

    @classmethod
    def tearDownClass(cls):
        cls.admin_server.shutdown()
        cls.admin_server.server_close()

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from conch.capitol.admin import CapitolAdmin
        from conch.kernel.store import MissionStore

        FakeAdminGateway.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patcher = patch(
            "conch.capitol.credentials.REGISTRY_PATH",
            root / "agents.yaml",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MissionStore(root / "kernel.db")
        self.addCleanup(self.store.close)
        self.mission_id = self.store.create_mission(
            {"goal": "together-funding provisioning", "budgets": {}}
        )
        base = f"http://127.0.0.1:{self.admin_port}"
        self.admin = CapitolAdmin(
            platform_url=base, workflow_url=base, org_id=ORG,
            token=TOKEN, store=self.store, mission_id=self.mission_id,
        )
        self.catalog = fixture_catalog()

    def test_full_provision_then_replay(self):
        logs = []
        outcome = tf.provision_pipeline(
            self.admin, catalog=self.catalog,
            schedule_enabled=False, log=logs.append,
        )
        ledger_id = outcome["ledger_collection"]["collection_id"]
        self.assertTrue(ledger_id)
        self.assertEqual(
            FakeAdminGateway.collections[ledger_id]["name"],
            tf.LEDGER_COLLECTION,
        )
        packet = outcome["packet"]
        ingest = outcome["ingest"]
        self.assertTrue(packet["created"])
        self.assertTrue(packet["version_pin"])
        self.assertTrue(ingest["created"])
        # the persisted ingest definition pins the packet version
        record = FakeAdminGateway.workflows[ingest["workflow_id"]]
        triage = next(
            node["data"]["struct"]
            for node in record["payload"]["nodes"]
            if node["data"]["struct"].get("name") == "Funding Triage"
        )
        entry = next(
            p["value"] for p in triage["params"]
            if p["field_id"] == "delegable_workflows"
        )[0]
        self.assertEqual(entry["workflow_id"], packet["workflow_id"])
        self.assertEqual(entry["version_id"], packet["version_pin"])
        # the persisted prompts point at the REAL collection id
        triage_prompt = next(
            p["value"] for p in triage["params"]
            if p["field_id"] == "system_prompt"
        )
        self.assertIn(ledger_id, triage_prompt)
        # orchestrator allowlists exactly the two workflows
        agent = FakeAdminGateway.agents[outcome["agent"]["agent_id"]]
        self.assertEqual(
            sorted(agent["workflow_allowlist"]),
            sorted([ingest["workflow_id"], packet["workflow_id"]]),
        )
        self.assertTrue(outcome["schedule"]["schedule_id"])
        created_schedule = FakeAdminGateway.schedules[
            ingest["workflow_id"]
        ][0]
        self.assertEqual(
            created_schedule["cron_expression"], tf.SCHEDULE_CRON
        )
        self.assertFalse(created_schedule["enabled"])

        # replay: same definitions, zero new platform effects
        requests_before = len(FakeAdminGateway.requests)
        versions_before = len(
            FakeAdminGateway.workflows[packet["workflow_id"]]["versions"]
        )
        replay = tf.provision_pipeline(
            self.admin, catalog=self.catalog,
            schedule_enabled=False, log=logs.append,
        )
        self.assertTrue(replay["packet"]["replayed"])
        self.assertTrue(replay["ingest"]["replayed"])
        self.assertTrue(replay["ledger_collection"]["replayed"])
        self.assertEqual(
            len(FakeAdminGateway.workflows[
                packet["workflow_id"]
            ]["versions"]),
            versions_before,
            "replay must not mint a new version",
        )
        self.assertEqual(
            len(FakeAdminGateway.requests), requests_before,
            "replay must not touch the platform",
        )


# ---------------------------------------------------------------------------
# Run driver reshaping
# ---------------------------------------------------------------------------

class RunDriverCase(unittest.TestCase):
    class _Capture(BaseHTTPRequestHandler):
        bodies = []

        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.__class__.bodies.append(
                json.loads(self.rfile.read(length) or b"{}")
            )
            data = json.dumps(
                {"run_id": "run-1", "session_id": "s", "status": "queued"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def test_trigger_reshapes_overrides(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._Capture)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self._Capture.bodies = []
        driver = tf.RunDriver(f"http://127.0.0.1:{port}", TOKEN, ORG)
        driver.trigger(
            "wf-1",
            {f"{tf.INGEST_WINDOW_NODE_ID}.text_input": "incremental"},
        )
        body = self._Capture.bodies[0]
        self.assertEqual(
            body["inputs"],
            [{
                "node_instance_id": tf.INGEST_WINDOW_NODE_ID,
                "fields": [{
                    "field_id": "text_input",
                    "current_value": "incremental",
                }],
            }],
        )
        with self.assertRaises(CapitolError):
            driver.trigger("wf-1", {"no-dot-key": 1})

    def test_find_delegated_run_id(self):
        detail = {"node_results": [
            {"output_data": {"text": "nothing here"}},
            {"output_data": {"launches": [{
                "alias": "funding_packet",
                "child_run_id": "12345678-1234-5234-9234-123456789abc",
            }]}},
        ]}
        self.assertEqual(
            tf.find_delegated_run_id(detail),
            "12345678-1234-5234-9234-123456789abc",
        )
        self.assertEqual(tf.find_delegated_run_id({"node_results": []}), "")


if __name__ == "__main__":
    unittest.main()
