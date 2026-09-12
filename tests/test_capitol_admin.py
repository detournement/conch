"""CapitolAdmin contract tests against a scripted fake platform/workflow API.

The builder profile's gates (Phase 3 Deliverable 3): default-off config
flag, required-policy veto, mandatory idempotency keys and kernel
external-action ledger entries, version pins and rollback references on
every mutation, duplicate-mutation replay without a second wire call,
bearer values sunk into the registry file (never returned, ledgered, or
logged), and the full create→verify→rollback→cleanup drill — the same
drill the live disposable-asset test runs against the serving stack.
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.capitol.admin import CapitolAdmin
from conch.capitol.credentials import (
    bearer_fingerprint,
    parse_agents_yaml,
    resolve_admin_token,
)
from conch.capitol.errors import CapitolAuthError, CapitolError
from conch.kernel.store import MissionStore
from conch.policy import register_required_policy, unregister_required_policy

ORG = "org-admin"
TOKEN = "eyJADMIN.test.token"
MINTED = "cap_a2a_MINTEDBEARER0001"
ROTATED = "cap_a2a_ROTATEDBEARER002"


class FakeAdminGateway(BaseHTTPRequestHandler):
    """One fake serving both the platform-api and workflow-api paths."""

    agents = {}
    bearers = {}          # agent_id -> [{id, label}]
    workflows = {}        # workflow_id -> {payload, versions: [...]}
    schedules = {}        # workflow_id -> [{...}]
    collections = {}      # collection_id -> {...}
    requests = []         # (method, path, body)
    counter = 0

    @classmethod
    def reset(cls):
        cls.agents = {}
        cls.bearers = {}
        cls.workflows = {
            "wf-1": {
                "payload": {"id": "wf-1", "name": "Fake Flow",
                            "publish_to_api": False},
                "versions": [
                    {"id": "ver-1", "version_number": 1,
                     "version_type": "manual"},
                ],
            },
        }
        cls.schedules = {}
        cls.collections = {}
        cls.requests = []
        cls.counter = 0
        cls.version_counter = 1   # ver-1 already exists on wf-1

    def log_message(self, *_args):
        pass

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _handle(self):
        cls = self.__class__
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._json({"detail": "bad token"}, 401)
            return
        body = self._body() if self.command in ("POST", "PATCH", "PUT") \
            else {}
        cls.requests.append((self.command, self.path, body))
        path = self.path

        # -- platform: agents ------------------------------------------------
        if path == f"/agents/{ORG}" and self.command == "GET":
            self._json({"agents": list(cls.agents.values())})
            return
        if path == f"/agents/{ORG}" and self.command == "POST":
            cls.counter += 1
            agent_id = f"agent-{cls.counter}"
            agent = dict(body, id=agent_id, orgid=ORG)
            cls.agents[agent_id] = agent
            self._json({
                "success": True, "agent": agent,
                "a2a_bearer_token": MINTED,
                "a2a_endpoint_url": f"http://fake/a2a/{ORG}/{agent_id}",
                "a2a_agent_card_url": (
                    f"http://fake/a2a/{ORG}/{agent_id}/.well-known/"
                    "agent-card.json"
                ),
            })
            return
        if path.startswith(f"/agents/{ORG}/"):
            rest = path[len(f"/agents/{ORG}/"):]
            parts = rest.split("/")
            agent_id = parts[0]
            if len(parts) == 1:
                if self.command == "GET":
                    agent = cls.agents.get(agent_id)
                    if agent is None:
                        self._json({"detail": "no such agent"}, 404)
                        return
                    self._json(agent)
                    return
                if self.command == "PATCH":
                    agent = cls.agents.setdefault(agent_id, {})
                    agent.update(body)
                    self._json({"success": True, "agent": agent})
                    return
                if self.command == "DELETE":
                    cls.agents.pop(agent_id, None)
                    self._json({"success": True})
                    return
            if parts[1:] == ["rotate-a2a-token"]:
                self._json({"success": True,
                            "a2a_bearer_token": ROTATED})
                return
            if parts[1:] == ["bearers"] and self.command == "POST":
                cls.counter += 1
                bearer_id = f"brr-{cls.counter}"
                cls.bearers.setdefault(agent_id, []).append({
                    "id": bearer_id, "label": body.get("label"),
                })
                self._json({
                    "success": True,
                    "bearer": {"id": bearer_id,
                               "label": body.get("label")},
                    "a2a_bearer_token": MINTED,
                })
                return
            if len(parts) == 3 and parts[1] == "bearers" and (
                self.command == "DELETE"
            ):
                cls.bearers[agent_id] = [
                    row for row in cls.bearers.get(agent_id, [])
                    if row["id"] != parts[2]
                ]
                self._json({"success": True})
                return

        # -- workflow-api: workflows / versions / schedules -------------------
        base = f"/api/v1/orgs/{ORG}"
        if path == f"{base}/workflows" and self.command == "POST":
            workflow_id = str(body.get("id") or "wf-1")
            record = cls.workflows.setdefault(
                workflow_id, {"payload": {}, "versions": []}
            )
            record["payload"] = dict(body)
            cls.version_counter += 1
            record["versions"].insert(0, {
                "id": f"ver-{cls.version_counter}",
                "version_number": len(record["versions"]) + 1,
                "version_type": (
                    "published" if body.get("publish_to_api")
                    else "manual"
                ),
            })
            self._json({
                "success": True, "message": "persisted",
                "workflow_id": workflow_id, "orgid": ORG,
                "workflow": {"payload": record["payload"]},
            })
            return
        if path.startswith(f"{base}/workflows/"):
            rest = path[len(f"{base}/workflows/"):]
            workflow_id, _, tail = rest.partition("/")
            record = cls.workflows.get(workflow_id)
            if record is None:
                self._json({"detail": "no such workflow"}, 404)
                return
            if not tail and self.command == "GET":
                self._json({"workflow": {"payload": record["payload"]}})
                return
            if not tail and self.command == "DELETE":
                cls.workflows.pop(workflow_id, None)
                self._json({"success": True})
                return
            if tail == "versions" and self.command == "GET":
                self._json({"workflow_id": workflow_id,
                            "versions": record["versions"]})
                return
            if tail == "schedules" and self.command == "GET":
                self._json(cls.schedules.get(workflow_id, []))
                return
            if tail == "schedules" and self.command == "POST":
                cls.counter += 1
                schedule = dict(body, id=f"sch-{cls.counter}",
                                workflow_id=workflow_id)
                cls.schedules.setdefault(workflow_id, []).append(schedule)
                self._json(schedule)
                return
            if tail.startswith("schedules/"):
                schedule_id = tail.split("/", 1)[1]
                rows = cls.schedules.get(workflow_id, [])
                if self.command == "PUT":
                    for row in rows:
                        if row["id"] == schedule_id:
                            row.update(body)
                            self._json(row)
                            return
                    self._json({"detail": "no such schedule"}, 404)
                    return
                if self.command == "DELETE":
                    cls.schedules[workflow_id] = [
                        row for row in rows if row["id"] != schedule_id
                    ]
                    self._json({"success": True})
                    return

        # -- platform: collections ---------------------------------------------
        if path == f"/collections/{ORG}" and self.command == "GET":
            self._json(list(cls.collections.values()))
            return
        if path == f"/collections/{ORG}" and self.command == "POST":
            cls.counter += 1
            collection_id = f"col-{cls.counter}"
            row = dict(body, id=collection_id)
            cls.collections[collection_id] = row
            self._json(row)
            return
        if path.startswith(f"/collections/{ORG}/") and (
            self.command == "DELETE"
        ):
            collection_id = path.rsplit("/", 1)[1]
            row = cls.collections.get(collection_id)
            if row is not None:
                row["soft_deleted"] = True
            self._json({"success": True})
            return

        self._json({"detail": f"unhandled {self.command} {path}"}, 404)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle


class AdminCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAdminGateway)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeAdminGateway.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.registry = root / "agents.yaml"
        patcher = patch(
            "conch.capitol.credentials.REGISTRY_PATH", self.registry
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MissionStore(root / "kernel.db")
        self.addCleanup(self.store.close)
        self.mission_id = self.store.create_mission(
            {"goal": "provision capitol assets", "budgets": {}}
        )
        base = f"http://127.0.0.1:{self.port}"
        self.admin = CapitolAdmin(
            platform_url=base, workflow_url=base, org_id=ORG,
            token=TOKEN, store=self.store, mission_id=self.mission_id,
        )

    # -- gating ---------------------------------------------------------------

    def test_from_config_is_off_by_default(self):
        with self.assertRaises(CapitolError) as raised:
            CapitolAdmin.from_config({
                "capitol_base_url": "http://localhost:1",
                "capitol_platform_url": "http://localhost:2",
                "capitol_org": ORG,
            })
        self.assertIn("capitol_admin", str(raised.exception))

    def test_from_config_resolves_token_from_env(self):
        config = {
            "capitol_admin": "true",
            "capitol_base_url": f"http://127.0.0.1:{self.port}",
            "capitol_platform_url": f"http://127.0.0.1:{self.port}",
            "capitol_org": ORG,
        }
        import os

        with patch.dict(os.environ, {"CAPITOL_ADMIN_TOKEN": TOKEN}):
            admin = CapitolAdmin.from_config(
                config, store=self.store, mission_id=self.mission_id
            )
        self.assertEqual(admin.org_id, ORG)

    def test_admin_token_resolves_from_registry_x_user_token(self):
        self.registry.write_text(
            "agents:\n"
            "- name: local-admin\n"
            "  base_url: http://127.0.0.1:8300\n"
            f"  org_id: {ORG}\n"
            "  agent_id: agent-x\n"
            "  bearer: cap_a2a_X\n"
            f"  x_user_token: {TOKEN}\n"
        )
        import os

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAPITOL_ADMIN_TOKEN", None)
            token, source = resolve_admin_token({}, ORG)
        self.assertEqual(token, TOKEN)
        self.assertEqual(source, "registry:local-admin")

    def test_required_policy_vetoes_mutations(self):
        register_required_policy(
            "test-admin-deny",
            lambda event, payload: not event.startswith("capitol.admin."),
        )
        self.addCleanup(unregister_required_policy, "test-admin-deny")
        with self.assertRaises(CapitolError) as raised:
            self.admin.create_collection(
                "conch-phase3-x", idempotency_key="k-veto"
            )
        self.assertIn("denied by required policy", str(raised.exception))
        self.assertEqual(FakeAdminGateway.requests, [],
                         "a vetoed mutation must never reach the wire")

    def test_mutations_require_idempotency_key_and_ledger(self):
        with self.assertRaises(CapitolError):
            self.admin.create_collection("c", idempotency_key="")
        bare = CapitolAdmin(
            platform_url=self.admin.platform_url,
            workflow_url=self.admin.workflow_url,
            org_id=ORG, token=TOKEN,
        )
        with self.assertRaises(CapitolError) as raised:
            bare.create_collection("c", idempotency_key="k")
        self.assertIn("ledger", str(raised.exception))

    def test_auth_failure_is_typed_and_redacted(self):
        bad = CapitolAdmin(
            platform_url=self.admin.platform_url,
            workflow_url=self.admin.workflow_url,
            org_id=ORG, token="eyJWRONG",
            store=self.store, mission_id=self.mission_id,
        )
        with self.assertRaises(CapitolAuthError) as raised:
            bad.list_agents()
        self.assertNotIn("eyJWRONG", str(raised.exception))

    # -- create agent + bearer hygiene ----------------------------------------

    def test_create_agent_sinks_bearer_and_ledgers(self):
        result = self.admin.create_orchestrator_agent(
            "conch-phase3-orch", ["wf-1"], idempotency_key="k-create",
        )
        self.assertTrue(result["agent_id"])
        self.assertEqual(result["bearer_fingerprint"],
                         bearer_fingerprint(MINTED))
        self.assertNotIn(MINTED, json.dumps(result))
        # the bearer landed in the registry file, mode 0600
        entries = parse_agents_yaml(self.registry.read_text())
        self.assertEqual(entries[0]["bearer"], MINTED)
        self.assertEqual(entries[0]["agent_id"], result["agent_id"])
        self.assertEqual(
            self.registry.stat().st_mode & 0o777, 0o600
        )
        # ledger: committed with the rollback reference, no token bytes
        action = self.store.find_action(
            "capitol-admin:create_agent:k-create"
        )
        self.assertEqual(action["status"], "committed")
        self.assertNotIn(MINTED, action["detail"])
        detail = json.loads(action["detail"])
        self.assertEqual(detail["result"]["rollback_ref"]["kind"],
                         "delete_agent")

    def test_duplicate_mutation_replays_without_wire_call(self):
        self.admin.create_orchestrator_agent(
            "conch-phase3-orch", ["wf-1"], idempotency_key="k-dup",
        )
        wire_before = len(FakeAdminGateway.requests)
        replay = self.admin.create_orchestrator_agent(
            "conch-phase3-orch", ["wf-1"], idempotency_key="k-dup",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(FakeAdminGateway.requests), wire_before)

    def test_unknown_outcome_reconciles_by_adoption(self):
        """A re-run after an uncertain outcome adopts the same-name agent
        instead of creating a duplicate (query-before-retry)."""
        self.admin.create_orchestrator_agent(
            "conch-phase3-adopt", ["wf-1"], idempotency_key="k-a1",
        )
        result = self.admin.create_orchestrator_agent(
            "conch-phase3-adopt", ["wf-1"], idempotency_key="k-a2",
        )
        self.assertTrue(result["adopted_existing"])
        self.assertEqual(len(FakeAdminGateway.agents), 1)

    def test_rotate_and_mint_bearers_update_registry(self):
        created = self.admin.create_orchestrator_agent(
            "conch-phase3-rot", ["wf-1"], idempotency_key="k-rot0",
        )
        agent_id = created["agent_id"]
        rotated = self.admin.rotate_bearer(
            agent_id, idempotency_key="k-rot1"
        )
        self.assertEqual(rotated["bearer_fingerprint"],
                         bearer_fingerprint(ROTATED))
        entries = parse_agents_yaml(self.registry.read_text())
        mine = [e for e in entries if e.get("agent_id") == agent_id]
        self.assertEqual(mine[0]["bearer"], ROTATED,
                         "rotation replaces the bearer in place")
        minted = self.admin.mint_deployment_bearer(
            agent_id, "conch-phase3-deploy", idempotency_key="k-mint",
        )
        self.assertTrue(minted["bearer_id"])
        self.assertEqual(
            minted["rollback_ref"],
            {"kind": "revoke_bearer", "agent_id": agent_id,
             "bearer_id": minted["bearer_id"]},
        )
        revoked = self.admin.revoke_bearer(
            agent_id, minted["bearer_id"], idempotency_key="k-revoke",
        )
        self.assertTrue(revoked["revoked"])
        self.assertEqual(FakeAdminGateway.bearers[agent_id], [])

    def test_allowlist_pin_captures_prior_for_rollback(self):
        created = self.admin.create_orchestrator_agent(
            "conch-phase3-allow", ["wf-1"], idempotency_key="k-al0",
        )
        agent_id = created["agent_id"]
        result = self.admin.set_workflow_allowlist(
            agent_id, ["wf-1", "wf-2"], idempotency_key="k-al1",
        )
        self.assertEqual(
            result["rollback_ref"]["prior"]["workflow_allowlist"],
            ["wf-1"],
        )
        self.assertEqual(
            FakeAdminGateway.agents[agent_id]["workflow_allowlist"],
            ["wf-1", "wf-2"],
        )

    # -- publish / version pin / rollback ----------------------------------------

    def test_publish_pins_version_and_rollback_restores(self):
        published = self.admin.publish_workflow(
            "wf-1", idempotency_key="k-pub",
        )
        self.assertTrue(published["published"])
        self.assertTrue(published["version_pin"])
        self.assertEqual(
            published["rollback_ref"]["prior_version_id"], "ver-1"
        )
        self.assertTrue(
            FakeAdminGateway.workflows["wf-1"]["payload"]["publish_to_api"]
        )
        # publishing again is a recorded no-op
        again = self.admin.publish_workflow(
            "wf-1", idempotency_key="k-pub2",
        )
        self.assertTrue(again["already_published"])
        # rollback re-persists the pre-publish state (publish_to_api off)
        # through the workflow-api, minting a new version in the same
        # lineage — it does NOT touch the platform-api rollback endpoint.
        rolled = self.admin.rollback_workflow(
            "wf-1", idempotency_key="k-roll",
        )
        self.assertTrue(rolled["rolled_back"])
        self.assertEqual(rolled["rolled_back_from"],
                         published["version_pin"])
        # the pin advances to a fresh version recording the reverted state
        self.assertTrue(rolled["version_pin"])
        self.assertNotEqual(rolled["version_pin"], published["version_pin"])
        self.assertFalse(
            FakeAdminGateway.workflows["wf-1"]["payload"]["publish_to_api"]
        )
        # a second rollback is a recorded no-op (already unpublished)
        again_roll = self.admin.rollback_workflow(
            "wf-1", idempotency_key="k-roll2",
        )
        self.assertTrue(again_roll["already_unpublished"])

    # -- persist / delete workflows ------------------------------------------------

    def test_persist_workflow_create_pins_version(self):
        payload = {"id": "wf-new", "name": "Fresh Flow",
                   "nodes": [], "edges": [], "publish_to_api": True}
        result = self.admin.persist_workflow(
            payload, idempotency_key="k-pw1",
        )
        self.assertTrue(result["created"])
        self.assertTrue(result["version_pin"])
        # undoing a first persist removes the asset
        self.assertEqual(
            result["rollback_ref"],
            {"kind": "delete_workflow", "workflow_id": "wf-new"},
        )
        self.assertEqual(
            FakeAdminGateway.workflows["wf-new"]["payload"]["name"],
            "Fresh Flow",
        )
        action = self.store.find_action(
            "capitol-admin:persist_workflow:k-pw1"
        )
        self.assertEqual(action["status"], "committed")

    def test_persist_workflow_update_keeps_rollback_version(self):
        result = self.admin.persist_workflow(
            {"id": "wf-1", "name": "Fake Flow v2", "nodes": [],
             "edges": [], "publish_to_api": False},
            idempotency_key="k-pw2",
        )
        self.assertFalse(result["created"])
        self.assertEqual(
            result["rollback_ref"]["kind"], "persist_prior_version"
        )
        self.assertEqual(
            result["rollback_ref"]["prior_version_id"], "ver-1"
        )
        self.assertNotEqual(result["version_pin"], "ver-1")

    def test_persist_workflow_replays_without_new_version(self):
        payload = {"id": "wf-rep", "name": "Replayed", "nodes": [],
                   "edges": [], "publish_to_api": True}
        first = self.admin.persist_workflow(
            payload, idempotency_key="k-pw3",
        )
        versions_before = len(
            FakeAdminGateway.workflows["wf-rep"]["versions"]
        )
        replay = self.admin.persist_workflow(
            payload, idempotency_key="k-pw3",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["version_pin"], first["version_pin"])
        self.assertEqual(
            len(FakeAdminGateway.workflows["wf-rep"]["versions"]),
            versions_before,
        )

    def test_persist_workflow_requires_stable_id(self):
        with self.assertRaises(CapitolError):
            self.admin.persist_workflow(
                {"name": "No Id"}, idempotency_key="k-pw4",
            )

    def test_delete_workflow_ledgers(self):
        self.admin.persist_workflow(
            {"id": "wf-del", "name": "Doomed", "nodes": [], "edges": []},
            idempotency_key="k-pw5",
        )
        result = self.admin.delete_workflow(
            "wf-del", idempotency_key="k-pw6",
        )
        self.assertTrue(result["deleted"])
        action = self.store.find_action(
            "capitol-admin:delete_workflow:k-pw6"
        )
        self.assertEqual(action["status"], "committed")

    # -- schedules ----------------------------------------------------------------

    def test_schedule_create_update_delete_cycle(self):
        created = self.admin.create_schedule(
            "wf-1", "conch-phase3-sched", "0 9 * * 1-5",
            idempotency_key="k-s1",
        )
        schedule_id = created["schedule_id"]
        self.assertTrue(schedule_id)
        updated = self.admin.update_schedule(
            "wf-1", schedule_id, {"enabled": False},
            idempotency_key="k-s2",
        )
        self.assertEqual(updated["rollback_ref"]["prior"], {"enabled": True})
        deleted = self.admin.delete_schedule(
            "wf-1", schedule_id, idempotency_key="k-s3",
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(FakeAdminGateway.schedules["wf-1"], [])
        # same-name create adopts rather than duplicating
        self.admin.create_schedule(
            "wf-1", "conch-phase3-x", "0 9 * * *", idempotency_key="k-s4",
        )
        adopted = self.admin.create_schedule(
            "wf-1", "conch-phase3-x", "0 9 * * *", idempotency_key="k-s5",
        )
        self.assertTrue(adopted["adopted_existing"])

    # -- collections + full drill ---------------------------------------------------

    def test_collection_create_bind_cleanup(self):
        created = self.admin.create_collection(
            "conch-phase3-col", idempotency_key="k-c1",
        )
        collection_id = created["collection_id"]
        agent = self.admin.create_orchestrator_agent(
            "conch-phase3-colagent", ["wf-1"], idempotency_key="k-c2",
        )
        bound = self.admin.bind_agent_collections(
            agent["agent_id"], [collection_id], idempotency_key="k-c3",
        )
        self.assertEqual(
            bound["applied"]["data_collection_allowlist"],
            [collection_id],
        )
        cleaned = self.admin.delete_collection(
            collection_id, idempotency_key="k-c4",
        )
        self.assertTrue(cleaned["deleted"])
        self.assertTrue(
            FakeAdminGateway.collections[collection_id]["soft_deleted"]
        )

    def test_full_drill_create_verify_rollback_cleanup(self):
        """The fake twin of the live disposable-asset gate: every step
        ledgered, every mutation idempotent, everything cleaned up."""
        agent = self.admin.create_orchestrator_agent(
            "conch-phase3-drill", ["wf-1"], idempotency_key="drill-1",
        )
        published = self.admin.publish_workflow(
            "wf-1", idempotency_key="drill-2",
        )
        schedule = self.admin.create_schedule(
            "wf-1", "conch-phase3-drill", "0 8 * * *",
            idempotency_key="drill-3",
        )
        # verify
        self.assertIn(agent["agent_id"], FakeAdminGateway.agents)
        self.assertTrue(published["version_pin"])
        # rollback reverts the published state in the same version lineage
        rolled = self.admin.rollback_workflow(
            "wf-1", idempotency_key="drill-4",
        )
        self.assertTrue(rolled["rolled_back"])
        self.assertFalse(
            FakeAdminGateway.workflows["wf-1"]["payload"]["publish_to_api"]
        )
        # cleanup
        self.admin.delete_schedule(
            "wf-1", schedule["schedule_id"], idempotency_key="drill-5",
        )
        self.admin.delete_agent(
            agent["agent_id"], idempotency_key="drill-6",
        )
        self.assertNotIn(agent["agent_id"], FakeAdminGateway.agents)
        # every step is on the ledger, committed
        for step in range(1, 7):
            rows = [
                row for row in self.store._read_conn().execute(
                    "SELECT status FROM external_actions WHERE"
                    " idempotency_key LIKE ?",
                    (f"%drill-{step}",),
                ).fetchall()
            ]
            self.assertEqual(rows[0][0], "committed", f"drill-{step}")
        # and the journal replays to the same projections
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
