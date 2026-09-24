"""Strict Procedure REST client contracts and credential hygiene."""

import json
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conch.capitol.errors import CapitolAuthError, CapitolProtocolError
from conch.capitol.procedures import (
    CapitolProcedureClient,
    procedure_content_digest,
    workflow_payload_digest,
)

ORG = "org-procedures"
TOKEN = "eyJPROCEDURE.secret.token"
WORKFLOW_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
DOCUMENT_ID = str(uuid.uuid4())


def document():
    markdown = "# Procedure\n\nDo the governed thing."
    structured = {"name": "Governed Thing", "steps": []}
    return {
        "schema_version": "capitol.procedure_document.v1",
        "id": DOCUMENT_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_name": "Governed Thing",
        "workflow_description": "A test workflow",
        "workflow_version_id": VERSION_ID,
        "version_number": 2,
        "content_digest": procedure_content_digest(markdown, structured),
        "compiler_version": "1.0.0",
        "verification": "reviewed",
        "verified_by_id": "user-1",
        "verified_at": "2026-09-24T01:00:00Z",
        "health": None,
        "publication": {
            "basis": "workflow_version",
            "workflow_version_type": "published",
            "is_published": True,
            "published_at": "2026-09-24T00:00:00Z",
        },
        "exposure": {
            "basis": "current_workflow_state",
            "version_specific": False,
            "publish_to_api": True,
            "publish_to_mcp": False,
            "publish_to_template": False,
            "workflow_updated_at": "2026-09-24T01:00:00Z",
        },
        "compiled_at": "2026-09-24T00:00:00Z",
        "markdown": markdown,
        "doc_json": structured,
        "created_at": "2026-09-24T00:00:00Z",
        "updated_at": "2026-09-24T01:00:00Z",
    }


def result_item():
    doc = document()
    return {
        key: value
        for key, value in doc.items()
        if key not in ("markdown", "doc_json", "health")
    }


def version():
    payload = {
        "id": WORKFLOW_ID,
        "name": "Governed Thing",
        "nodes": [],
        "edges": [],
    }
    return {
        "schema_version": "capitol.workflow_version.v1",
        "id": VERSION_ID,
        "workflow_id": WORKFLOW_ID,
        "version_number": 2,
        "version_type": "published",
        "payload": payload,
        "payload_digest": workflow_payload_digest(payload),
        "is_latest": True,
        "created_by_id": "user-1",
        "created_at": "2026-09-24T00:00:00Z",
    }


class FakeProcedureGateway(BaseHTTPRequestHandler):
    requests = []
    extra_field = False
    bad_schema = False

    @classmethod
    def reset(cls):
        cls.requests = []
        cls.extra_field = False
        cls.bad_schema = False

    def log_message(self, *_args):
        pass

    def _json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cls = self.__class__
        cls.requests.append((self.command, self.path))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._json(
                {"detail": f"bad token {self.headers.get('Authorization')}"}, 401
            )
            return
        base = f"/api/v1/orgs/{ORG}"
        if self.path.startswith(f"{base}/procedures/search?"):
            payload = {
                "schema_version": "capitol.procedure_collection.v1",
                "results": [result_item()],
                "limit": 5,
                "offset": 0,
                "total": 1,
            }
        elif self.path.startswith(f"{base}/procedures?"):
            payload = {
                "schema_version": "capitol.procedure_collection.v1",
                "procedures": [result_item()],
                "limit": 10,
                "offset": 0,
                "total": 1,
            }
        elif self.path.startswith(f"{base}/workflows/{WORKFLOW_ID}/procedure"):
            payload = document()
        elif self.path == (f"{base}/workflows/{WORKFLOW_ID}/versions/{VERSION_ID}"):
            payload = version()
        else:
            self._json({"detail": "not found"}, 404)
            return
        if cls.bad_schema:
            payload["schema_version"] = "future.v99"
        if cls.extra_field:
            payload["surprise"] = True
        self._json(payload)


class ProcedureClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            FakeProcedureGateway,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeProcedureGateway.reset()
        self.client = CapitolProcedureClient(
            f"http://127.0.0.1:{self.port}",
            ORG,
            TOKEN,
        )

    def test_side_effect_free_search_list_and_exact_reads(self):
        found = self.client.search("governed thing", limit=5)
        listed = self.client.list(limit=10)
        shown = self.client.get(WORKFLOW_ID, version_number=2)
        exact = self.client.get_workflow_version(WORKFLOW_ID, VERSION_ID)
        self.assertEqual(found["results"][0]["id"], DOCUMENT_ID)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(shown["content_digest"], document()["content_digest"])
        self.assertEqual(exact["payload_digest"], version()["payload_digest"])
        self.assertTrue(
            all(method == "GET" for method, _path in FakeProcedureGateway.requests)
        )
        self.assertIn(
            "version_number=2",
            FakeProcedureGateway.requests[2][1],
        )

    def test_unknown_field_and_schema_fail_closed(self):
        FakeProcedureGateway.extra_field = True
        with self.assertRaisesRegex(CapitolProtocolError, "contract drift"):
            self.client.get(WORKFLOW_ID, version_number=2)
        FakeProcedureGateway.extra_field = False
        FakeProcedureGateway.bad_schema = True
        with self.assertRaisesRegex(CapitolProtocolError, "unsupported"):
            self.client.search("x")

    def test_digest_mismatch_fails_closed(self):
        original = FakeProcedureGateway.do_GET

        def bad(handler):
            if handler.path.startswith(
                f"/api/v1/orgs/{ORG}/workflows/{WORKFLOW_ID}/procedure"
            ):
                value = document()
                value["content_digest"] = "sha256:" + "0" * 64
                handler._json(value)
                return
            original(handler)

        FakeProcedureGateway.do_GET = bad
        self.addCleanup(
            setattr,
            FakeProcedureGateway,
            "do_GET",
            original,
        )
        with self.assertRaisesRegex(CapitolProtocolError, "does not match"):
            self.client.get(WORKFLOW_ID, version_number=2)

    def test_token_never_appears_in_repr_or_auth_error(self):
        self.assertNotIn(TOKEN, repr(self.client))
        bad = CapitolProcedureClient(
            self.client.workflow_url,
            ORG,
            "wrong-secret-token",
        )
        with self.assertRaises(CapitolAuthError) as raised:
            bad.search("x")
        self.assertNotIn("wrong-secret-token", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
