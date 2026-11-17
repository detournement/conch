"""Scribe importer (capture plan, feature 3): Scribe's MCP server as a
capture source, encoded against a fake server carrying the published
contract (hosted MCP endpoint, OAuth bearer, tools discovered per
session — Scribe does not publish tool names, so selection is by
capability).

Proven here: a real HTTP round-trip through conch's MCP client into a
draft card with scribe provenance, bearer header carried from the env
var by reference, unknown result shapes fail closed, missing search
tools fail closed naming what was offered, imported credentials reject
the capture whole, and the unset-config default means the source is
absent (zero traffic, gated command refusal).

Live verification against https://mcp.scribe.com/mcp needs a Scribe
workspace + OAuth token; none exists in this environment (marked in the
module under test as well).
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from conch.capitol.compiler.capture_scribe import (
    capture_from_scribe,
    scribe_unconfigured_reason,
)
from conch.capitol.compiler.card import normalize_card
from conch.capitol.compiler.commands import run_compile_command
from conch.capitol.errors import CapitolError
from conch.kernel.store import MissionStore
from conch.secretguard import CredentialRejected

from tests.compiler_fixtures import candidate_card, fake_discovery

GUIDE_TEXT = (
    "Guide: Weekly invoice run\n"
    "1. Export the ledger from the billing app\n"
    "2. Reconcile totals against the bank feed\n"
    "3. Email the summary to accounting\n"
)


class FakeScribeHandler(BaseHTTPRequestHandler):
    """JSON-RPC MCP surface: tools/list + tools/call."""

    tools = [{
        "name": "search_documents",
        "description": "Search Scribe documents and workflows",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }]
    result_factory = staticmethod(lambda query: {
        "content": [{"type": "text",
                     "text": GUIDE_TEXT + f"(query: {query})"}],
    })
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(
            int(self.headers.get("Content-Length", 0))
        ))
        type(self).seen.append({
            "method": body.get("method"),
            "auth": self.headers.get("Authorization", ""),
            "params": body.get("params"),
        })
        method = body.get("method")
        if method == "tools/list":
            result = {"tools": type(self).tools}
        elif method == "tools/call":
            arguments = (body.get("params") or {}).get("arguments") or {}
            result = type(self).result_factory(
                arguments.get("query", "")
            )
        else:
            result = {}
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class ScribeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "XDG_CONFIG_HOME": os.path.join(self._tmp.name, "config"),
            "SCRIBE_MCP_TOKEN": "synthetic-oauth-access",
        })
        env.start()
        self.addCleanup(env.stop)
        FakeScribeHandler.seen = []
        FakeScribeHandler.tools = list(FakeScribeHandler.__dict__.get(
            "tools", FakeScribeHandler.tools
        ))
        self.server = HTTPServer(("127.0.0.1", 0), FakeScribeHandler)
        thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = {
            "capture_enabled": "true",
            "scribe_mcp_url":
                f"http://127.0.0.1:{self.server.server_port}/mcp",
        }


class TestScribeCapture(ScribeCase):
    def test_round_trip_with_bearer(self):
        context = capture_from_scribe(self.config, "weekly invoice run")
        self.assertEqual(context["kind"], "scribe")
        self.assertIn("Export the ledger", context["block"])
        self.assertIn("search_documents",
                      context["provenance"]["tool"])
        # the bearer rode by reference on every request
        auths = {entry["auth"] for entry in FakeScribeHandler.seen}
        self.assertEqual(auths, {"Bearer synthetic-oauth-access"})
        self.assertIn("invoice", context["default_goal"].lower())

    def test_unknown_result_shape_fails_closed(self):
        FakeScribeHandler.result_factory = staticmethod(
            lambda query: {"documents": ["opaque"]}
        )
        self.addCleanup(lambda: setattr(
            FakeScribeHandler, "result_factory",
            staticmethod(lambda query: {
                "content": [{"type": "text", "text": GUIDE_TEXT}],
            }),
        ))
        with self.assertRaises(CapitolError) as caught:
            capture_from_scribe(self.config, "x")
        self.assertIn("failing closed", str(caught.exception))

    def test_no_search_tool_names_the_offering(self):
        FakeScribeHandler.tools = [{
            "name": "get_insights",
            "inputSchema": {"type": "object", "properties": {}},
        }]
        self.addCleanup(lambda: setattr(
            FakeScribeHandler, "tools", [{
                "name": "search_documents",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }],
        ))
        with self.assertRaises(CapitolError) as caught:
            capture_from_scribe(self.config, "x")
        self.assertIn("get_insights", str(caught.exception))

    def test_imported_credential_rejects_whole(self):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
        FakeScribeHandler.result_factory = staticmethod(
            lambda query: {"content": [{
                "type": "text",
                "text": f"step 1: export TOKEN={token}",
            }]}
        )
        self.addCleanup(lambda: setattr(
            FakeScribeHandler, "result_factory",
            staticmethod(lambda query: {
                "content": [{"type": "text", "text": GUIDE_TEXT}],
            }),
        ))
        with self.assertRaises(CredentialRejected):
            capture_from_scribe(self.config, "x")

    def test_unset_config_means_absent(self):
        self.assertIn("scribe_mcp_url",
                      scribe_unconfigured_reason({}))
        with self.assertRaises(CapitolError):
            capture_from_scribe({"capture_enabled": "true"}, "x")


class TestFromScribeCommand(ScribeCase):
    def run_cmd(self, arg, config=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_compile_command(
                arg, dict(self.config if config is None else config),
            )
        return buffer.getvalue()

    def test_command_end_to_end(self):
        def fake_session(config, goal, **kwargs):
            assert "Export the ledger" in kwargs["capture_context"]
            return normalize_card(candidate_card(), fake_discovery())

        with patch(
            "conch.capitol.compiler.session.run_compile_session",
            side_effect=fake_session,
        ):
            output = self.run_cmd('from-scribe "weekly invoice run"')
        self.assertIn("Compiled", output)
        store = MissionStore()
        self.addCleanup(store.close)
        rows = store.list_compilations()
        capture = store.compilation_capture(rows[0]["compilation_id"])
        self.assertEqual(capture["kind"], "scribe")
        self.assertEqual(capture["source"], "weekly invoice run")
        status = self.run_cmd(f"status {rows[0]['compilation_id']}")
        self.assertIn("captured from Scribe", status)

    def test_gates(self):
        gated = self.run_cmd('from-scribe "x"', config={})
        self.assertIn("/install capture", gated)
        unset = self.run_cmd(
            'from-scribe "x"', config={"capture_enabled": "true"},
        )
        self.assertIn("scribe_mcp_url", unset)


if __name__ == "__main__":
    unittest.main()
