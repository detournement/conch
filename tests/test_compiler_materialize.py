"""Materialization, drill gate, and rollback (C2) against the fake
gateway.

Proven here: the card's declared order (collections → workflows → agent →
schedules → pack write), end-to-end idempotency (re-materializing an
already-materialized compilation replays receipts and re-executes no
effect), partial failure recording receipts and offering rollback,
rollback reverting everything materialized so far in reverse via the
recorded rollback refs (adopted assets skipped), the drill gate's pass
path (materialized→verified→operating with a dry-run supervising
mission) and fail path (status stays materialized, failure attached), and
the safety invariants: no ``capitol_admin`` → materialize refuses, and
non-local base URLs are refused in v1.
"""

import json
import copy
import tempfile
import unittest
from pathlib import Path

from conch.capitol.compiler.card import normalize_card
from conch.capitol.compiler.graph import build_workflow_payload
from conch.capitol.compiler.materialize import (
    ensure_local_stack,
    materialize_compilation,
    rollback_compilation,
    verify_compilation,
)
from conch.capitol.errors import CapitolError
from conch.kernel.model import CompilationStatus, MissionState
from conch.kernel.store import MissionStore

from tests.compiler_fixtures import (
    AGENT_IDENTITY,
    LOCAL_CONFIG,
    PACK_NAME,
    SCHEDULE_IDENTITY,
    WORKFLOW_IDENTITY,
    FakeAdmin,
    FakeProcedureClient,
    FakeRunDriver,
    candidate_card,
    fake_catalog,
    fake_discovery,
)


class MaterializeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = MissionStore(root / "kernel" / "kernel.db")
        self.addCleanup(self.store.close)
        self.packs_dir = root / "packs"
        self.card = normalize_card(candidate_card(), fake_discovery())
        self.catalog = fake_catalog()
        self.config = dict(LOCAL_CONFIG)

    def compile_and_approve(self):
        compilation = self.store.create_compilation(self.card)
        cid = compilation["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="tester")
        return cid

    def materialize(self, cid, admin):
        self.procedure_client = FakeProcedureClient(admin)
        return materialize_compilation(
            self.store, self.config, cid, admin=admin,
            procedure_client=self.procedure_client,
            catalog=self.catalog, packs_dir=self.packs_dir,
            log=lambda line: None,
        )


class TestMaterializationOrder(MaterializeCase):
    def test_declared_order_and_receipts(self):
        cid = self.compile_and_approve()
        admin = FakeAdmin()
        state = self.materialize(cid, admin)
        self.assertEqual(admin.effects, [
            "persist_workflow", "create_agent", "create_schedule",
        ])
        steps = [step["step"] for step in state["steps"]]
        self.assertEqual(steps, [
            f"workflow:{WORKFLOW_IDENTITY}",
            f"agent:{AGENT_IDENTITY}",
            f"schedule:{SCHEDULE_IDENTITY}",
        ])
        for step in state["steps"]:
            self.assertTrue(step["rollback_ref"].get("kind"),
                            f"{step['step']} lacks a rollback ref")
        compilation = self.store.get_compilation(cid)
        self.assertEqual(
            compilation["status"], CompilationStatus.MATERIALIZED
        )
        self.assertEqual(
            compilation["materialization"]["documentation"]["status"],
            "linked",
        )
        self.assertEqual(len(compilation["procedures"]["links"]), 1)
        self.assertTrue(
            Path(compilation["materialization"]["lock_path"]).is_file()
        )
        self.assertTrue(
            compilation["materialization"]["lock_digest"].startswith(
                "sha256:"
            )
        )
        # the pack + drill fixtures landed on disk and load fail-closed
        pack_dir = self.packs_dir / PACK_NAME
        manifest = json.loads((pack_dir / "pack.json").read_text())
        self.assertEqual(manifest["pack"]["name"], PACK_NAME)
        fixtures = json.loads(
            (pack_dir / "assets" / "drill.json").read_text()
        )
        self.assertEqual(
            fixtures[0]["workflow_id"],
            self.card["assets"]["create"]["workflows"][0]["workflow_id"],
        )
        from conch.capitol.packs.registry import load_pack_dir

        pack = load_pack_dir(pack_dir)
        self.assertEqual(
            pack.raw["acceptance"]["kind"], "workflow_drill"
        )
        ok, message = self.store.replay_matches_live()
        self.assertTrue(ok, message)

    def test_collections_created_first_and_substituted(self):
        raw = candidate_card()
        raw["assets"]["create"]["collections"] = [{
            "identity": "conch-compile-report-archive",
            "name": "conch-compile-report-archive",
            "description": "compiled archive",
        }]
        raw["assets"]["create"]["workflows"][0]["stages"][1][
            "system_prompt"
        ] += " Archive into $collection:conch-compile-report-archive."
        self.card = normalize_card(raw, fake_discovery())
        cid = self.compile_and_approve()
        admin = FakeAdmin()
        state = self.materialize(cid, admin)
        self.assertEqual(admin.effects[0], "create_collection")
        self.assertEqual(
            state["collections"]["conch-compile-report-archive"],
            "col-conch-compile-report-archive",
        )

    def test_rematerialize_replays_without_new_effects(self):
        cid = self.compile_and_approve()
        admin = FakeAdmin()
        first = self.materialize(cid, admin)
        effects_after_first = list(admin.effects)
        second = self.materialize(cid, admin)
        self.assertEqual(admin.effects, effects_after_first)
        self.assertEqual(
            [step["step"] for step in first["steps"]],
            [step["step"] for step in second["steps"]],
        )
        self.assertEqual(
            self.store.get_compilation(cid)["status"],
            CompilationStatus.MATERIALIZED,
        )
        self.assertEqual(
            len(self.store.get_compilation(cid)["procedures"]["links"]), 1,
            "identical reconciliation appends no second link event",
        )

    def test_legacy_receipt_reconciles_read_only_when_exactly_provable(self):
        cid = self.compile_and_approve()
        workflow = self.card["assets"]["create"]["workflows"][0]
        payload = build_workflow_payload(
            self.catalog,
            workflow,
            collection_ids={"col-ledger": "col-ledger"},
        )
        admin = FakeAdmin()
        client = FakeProcedureClient(admin)
        client.seed(workflow["workflow_id"], payload)
        self.store.record_compilation_materialization(
            cid,
            {"steps": [{
                "step": f"workflow:{workflow['identity']}",
                "receipt": {
                    "workflow_id": workflow["workflow_id"],
                    "version_pin": client.version_id(
                        workflow["workflow_id"]
                    ),
                    "version_number": 1,
                    # Legacy receipts had no payload_digest.
                },
                "rollback_ref": {
                    "kind": "delete_workflow",
                    "workflow_id": workflow["workflow_id"],
                },
                "adopted": False,
            }]},
            complete=True,
        )
        state = materialize_compilation(
            self.store, self.config, cid, admin=admin,
            procedure_client=client, catalog=self.catalog,
            packs_dir=self.packs_dir, log=lambda line: None,
        )
        self.assertNotIn("persist_workflow", admin.effects)
        receipt = state["steps"][0]["receipt"]
        self.assertTrue(receipt["payload_digest"].startswith("sha256:"))
        self.assertEqual(
            len(self.store.get_compilation(cid)["procedures"]["links"]), 1,
        )


class TestPartialFailureAndRollback(MaterializeCase):
    def test_partial_failure_records_and_offers_rollback(self):
        cid = self.compile_and_approve()
        admin = FakeAdmin(fail_at="create_schedule")
        with self.assertRaisesRegex(CapitolError, "compile rollback"):
            self.materialize(cid, admin)
        compilation = self.store.get_compilation(cid)
        self.assertEqual(
            compilation["status"], CompilationStatus.APPROVED
        )
        state = compilation["materialization"]
        self.assertIn("injected failure", state["error"])
        steps = [step["step"] for step in state["steps"]]
        self.assertEqual(steps, [
            f"workflow:{WORKFLOW_IDENTITY}", f"agent:{AGENT_IDENTITY}",
        ])
        # the pack was never written (it is the last step)
        self.assertFalse((self.packs_dir / PACK_NAME).exists())

        # rollback reverts everything materialized so far, in reverse
        outcome = rollback_compilation(
            self.store, self.config, cid, admin=admin,
            log=lambda line: None,
        )
        self.assertEqual(outcome["reverted"], [
            f"agent:{AGENT_IDENTITY}", f"workflow:{WORKFLOW_IDENTITY}",
        ])
        self.assertEqual(admin.effects[-2:], [
            "delete_agent", "delete_workflow",
        ])
        self.assertEqual(
            self.store.get_compilation(cid)["status"],
            CompilationStatus.ROLLED_BACK,
        )
        ok, message = self.store.replay_matches_live()
        self.assertTrue(ok, message)

    def test_full_rollback_removes_pack_and_aborts_mission(self):
        cid = self.compile_and_approve()
        admin = FakeAdmin()
        self.materialize(cid, admin)
        driver = FakeRunDriver()
        verify_compilation(
            self.store, self.config, cid, driver=driver,
            procedure_client=self.procedure_client,
            packs_dir=self.packs_dir, log=lambda line: None,
        )
        compilation = self.store.get_compilation(cid)
        mission_id = compilation["mission_id"]
        self.assertTrue(mission_id)
        outcome = rollback_compilation(
            self.store, self.config, cid, admin=admin,
            log=lambda line: None,
        )
        self.assertIn(f"mission:{mission_id}", outcome["reverted"])
        self.assertFalse((self.packs_dir / PACK_NAME).exists())
        self.assertEqual(
            self.store.get_mission(mission_id)["status"],
            MissionState.CANCELLED,
        )
        self.assertEqual(
            self.store.get_compilation(cid)["status"],
            CompilationStatus.ROLLED_BACK,
        )

    def test_adopted_assets_never_deleted(self):
        cid = self.compile_and_approve()
        admin = FakeAdmin()
        # pre-existing agent adopted by an unknown-outcome reconcile
        state = self.materialize(cid, admin)
        for step in state["steps"]:
            if step["step"].startswith("agent:"):
                step["adopted"] = True
        self.store.record_compilation_materialization(
            cid, state, complete=True,
        )
        outcome = rollback_compilation(
            self.store, self.config, cid, admin=admin,
            log=lambda line: None,
        )
        self.assertTrue(any(
            entry.startswith(f"agent:{AGENT_IDENTITY}")
            for entry in outcome["skipped"]
        ))
        self.assertNotIn("delete_agent", admin.effects)

    def test_exact_procedure_workflow_adoption_is_never_deleted(self):
        admin = FakeAdmin()
        procedure_client = FakeProcedureClient(admin)
        workflow_id = "wf-procedure-adopt"
        payload = {
            "id": workflow_id,
            "name": "Existing Procedure Workflow",
            "nodes": [{
                "id": "input-node",
                "data": {"struct": {"node_id": "json_input_node"}},
            }],
            "edges": [],
        }
        procedure_client.seed(workflow_id, payload)
        version = procedure_client.get_workflow_version(
            workflow_id, procedure_client.version_id(workflow_id),
        )
        document = procedure_client.get(
            workflow_id,
            workflow_version_id=version["id"],
        )
        raw = copy.deepcopy(candidate_card())
        raw["schema"] = "conch.architecture_card.v2"
        raw["assets"] = {
            "reuse": [{
                "kind": "workflow", "id": workflow_id,
                "name": "Existing Procedure Workflow",
                "reason": "adopt the exact existing version",
            }],
            "create": {
                "workflows": [], "agent": None, "schedules": [],
                "collections": [],
            },
        }
        raw["drill"]["fixtures"][0]["workflow"] = workflow_id
        raw["mission"]["capitol"]["workflows"] = [workflow_id]
        raw["procedure_sources"] = [{
            "relationship": "adopt",
            "org_id": admin.org_id,
            "procedure_document_id": document["id"],
            "workflow_id": workflow_id,
            "workflow_version_id": version["id"],
            "workflow_version_number": version["version_number"],
            "workflow_payload_digest": version["payload_digest"],
            "procedure_content_digest": document["content_digest"],
            "compiler_version": document["compiler_version"],
        }]
        discovery = fake_discovery()
        discovery["org_id"] = admin.org_id
        discovery["workflows"].append({
            "id": workflow_id,
            "name": "Existing Procedure Workflow",
            "workflow_version_id": version["id"],
            "version_number": version["version_number"],
            "workflow_payload_digest": version["payload_digest"],
            "input_override_key": "input-node.value",
        })
        card = normalize_card(raw, discovery)
        cid = self.store.create_compilation(card)["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="tester")
        state = materialize_compilation(
            self.store, self.config, cid, admin=admin,
            procedure_client=procedure_client,
            catalog=self.catalog, packs_dir=self.packs_dir,
            log=lambda line: None,
        )
        adopted = next(
            step for step in state["steps"]
            if step["step"].startswith("workflow-adopt:")
        )
        self.assertTrue(adopted["adopted"])
        self.assertNotIn("persist_workflow", admin.effects)
        outcome = rollback_compilation(
            self.store, self.config, cid, admin=admin,
            log=lambda line: None,
        )
        self.assertTrue(any(
            "workflow-adopt:" in entry for entry in outcome["skipped"]
        ))
        self.assertIn(workflow_id, admin.workflow_payloads)
        self.assertNotIn("delete_workflow", admin.effects)

    def test_rollback_without_receipts_refused(self):
        cid = self.compile_and_approve()
        with self.assertRaisesRegex(CapitolError, "no recorded"):
            rollback_compilation(
                self.store, self.config, cid, admin=FakeAdmin(),
                log=lambda line: None,
            )


class TestDrillGate(MaterializeCase):
    def materialize_ok(self):
        cid = self.compile_and_approve()
        self.materialize(cid, FakeAdmin())
        return cid

    def test_pass_path_advances_to_operating(self):
        cid = self.materialize_ok()
        driver = FakeRunDriver()
        outcome = verify_compilation(
            self.store, self.config, cid, driver=driver,
            procedure_client=self.procedure_client,
            packs_dir=self.packs_dir, log=lambda line: None,
        )
        self.assertEqual(outcome["status"], CompilationStatus.OPERATING)
        # the drill drove the real override key with the fixture input
        workflow = self.card["assets"]["create"]["workflows"][0]
        self.assertEqual(
            driver.triggers[0]["workflow_id"], workflow["workflow_id"]
        )
        self.assertIn(
            workflow["input_override_key"], driver.triggers[0]["overrides"]
        )
        compilation = self.store.get_compilation(cid)
        self.assertTrue(compilation["drill"]["passed"])
        # supervising mission exists, dry-run, bound per the card
        mission = self.store.get_mission(compilation["mission_id"])
        self.assertIsNotNone(mission)
        self.assertTrue(mission["spec"]["dry_run"])
        self.assertEqual(
            mission["spec"]["capitol"]["workflows"],
            [workflow["workflow_id"]],
        )
        ok, message = self.store.replay_matches_live()
        self.assertTrue(ok, message)

    def test_fail_path_stays_materialized_with_failure(self):
        cid = self.materialize_ok()
        driver = FakeRunDriver(status="failed")
        with self.assertRaisesRegex(CapitolError, "drill FAILED"):
            verify_compilation(
                self.store, self.config, cid, driver=driver,
                procedure_client=self.procedure_client,
                packs_dir=self.packs_dir, log=lambda line: None,
            )
        compilation = self.store.get_compilation(cid)
        self.assertEqual(
            compilation["status"], CompilationStatus.MATERIALIZED
        )
        self.assertFalse(compilation["drill"]["passed"])
        self.assertEqual(compilation["mission_id"], "")

    def test_missing_marker_fails_the_gate(self):
        cid = self.materialize_ok()
        driver = FakeRunDriver(output_marker="the wrong output")
        with self.assertRaisesRegex(CapitolError, "marker"):
            verify_compilation(
                self.store, self.config, cid, driver=driver,
                procedure_client=self.procedure_client,
                packs_dir=self.packs_dir, log=lambda line: None,
            )

    def test_fail_closed_pack_load_blocks_the_drill(self):
        cid = self.materialize_ok()
        pack_path = self.packs_dir / PACK_NAME / "pack.json"
        broken = json.loads(pack_path.read_text())
        broken["surprise"] = True
        pack_path.write_text(json.dumps(broken))
        from conch.capitol.packs.manifest import PackError

        with self.assertRaises(PackError):
            verify_compilation(
                self.store, self.config, cid, driver=FakeRunDriver(),
                procedure_client=self.procedure_client,
                packs_dir=self.packs_dir, log=lambda line: None,
            )

    def test_documentation_pending_may_drill_in_shadow(self):
        cid = self.compile_and_approve()
        admin = FakeAdmin()
        self.procedure_client = FakeProcedureClient(admin, missing=True)
        materialize_compilation(
            self.store, self.config, cid, admin=admin,
            procedure_client=self.procedure_client,
            catalog=self.catalog, packs_dir=self.packs_dir,
            log=lambda line: None,
        )
        compilation = self.store.get_compilation(cid)
        self.assertEqual(
            compilation["materialization"]["documentation"]["status"],
            "documentation_pending",
        )
        outcome = verify_compilation(
            self.store, self.config, cid, driver=FakeRunDriver(),
            procedure_client=self.procedure_client,
            packs_dir=self.packs_dir, log=lambda line: None,
        )
        self.assertEqual(outcome["status"], CompilationStatus.OPERATING)

    def test_stale_latest_workflow_version_fails_closed(self):
        cid = self.materialize_ok()
        self.procedure_client.latest = False
        with self.assertRaisesRegex(CapitolError, "latest version"):
            verify_compilation(
                self.store, self.config, cid, driver=FakeRunDriver(),
                procedure_client=self.procedure_client,
                packs_dir=self.packs_dir, log=lambda line: None,
            )

    def test_procedure_digest_drift_fails_closed(self):
        cid = self.materialize_ok()
        self.procedure_client.markdown_suffix = "\nChanged projection."
        with self.assertRaisesRegex(CapitolError, "Procedure drift"):
            verify_compilation(
                self.store, self.config, cid, driver=FakeRunDriver(),
                procedure_client=self.procedure_client,
                packs_dir=self.packs_dir, log=lambda line: None,
            )


class TestSafetyInvariants(MaterializeCase):
    def test_materialize_requires_capitol_admin_authority(self):
        cid = self.compile_and_approve()
        config = dict(self.config)
        config.pop("capitol_admin")
        with self.assertRaisesRegex(CapitolError, "capitol_admin"):
            materialize_compilation(
                self.store, config, cid, catalog=self.catalog,
                packs_dir=self.packs_dir, log=lambda line: None,
            )
        # nothing was recorded, status untouched
        compilation = self.store.get_compilation(cid)
        self.assertEqual(compilation["status"],
                         CompilationStatus.APPROVED)
        self.assertEqual(compilation["materialization"], {})

    def test_non_local_org_refused(self):
        for key in ("capitol_base_url", "capitol_platform_url"):
            config = dict(self.config)
            config[key] = "https://api.capitol.example.com"
            with self.assertRaisesRegex(CapitolError, "local"):
                ensure_local_stack(config)
        cid = self.compile_and_approve()
        config = dict(self.config,
                      capitol_base_url="https://prod.capitol.ai")
        with self.assertRaisesRegex(CapitolError, "local"):
            materialize_compilation(
                self.store, config, cid, admin=FakeAdmin(),
                catalog=self.catalog, packs_dir=self.packs_dir,
                log=lambda line: None,
            )

    def test_materialize_requires_approval(self):
        compilation = self.store.create_compilation(self.card)
        with self.assertRaisesRegex(CapitolError, "approved"):
            self.materialize(
                compilation["compilation_id"], FakeAdmin()
            )

    def test_stale_approval_refused_after_revision(self):
        cid = self.compile_and_approve()
        # a revision after approval re-arms review; approve is void
        self.store.record_compilation_card(
            cid, dict(self.card, narrative="changed"), actor="tester",
        )
        with self.assertRaisesRegex(CapitolError, "approved"):
            self.materialize(cid, FakeAdmin())


if __name__ == "__main__":
    unittest.main()
