"""Architecture Card + compilation kernel aggregate (C1).

Proven here: fail-closed card validation (unknown fields, bad stages,
non-catalog tools, dangling references, bad cron, prefix discipline),
code-enforced reuse-first, synthetic-only drill fixtures, secret-guarded
cards, deterministic uuid5 identities and payload generation, generated
pack manifests passing the fail-closed loader, clean version diffing, and
the compilation aggregate's kernel discipline: hash-chained events →
projection with replay == live, the status machine, origin-bound
local-only approval pinning the exact card digest, revision invalidating
approval, and materialization/drill records advancing status.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from conch.capitol.compiler.card import (
    CardError,
    card_digest,
    diff_cards,
    normalize_card,
    render_card_markdown,
)
from conch.capitol.compiler.graph import (
    build_workflow_payload,
    payload_digest,
    workflow_uuid,
)
from conch.capitol.errors import CapitolError
from conch.kernel.model import (
    COMPILATION_EVENT_KINDS,
    EVENT_KINDS,
    ApprovalError,
    CompilationStatus,
    ConflictError,
    KernelError,
    check_compilation_transition,
    kernel_id,
    parse_kernel_id,
)
from conch.kernel.store import MissionStore
from conch.secretguard import CredentialRejected

from tests.compiler_fixtures import (
    WORKFLOW_IDENTITY,
    candidate_card,
    fake_catalog,
    fake_discovery,
)


class TestCompilationModel(unittest.TestCase):
    def test_events_registered_and_id_kind_parses(self):
        self.assertTrue(COMPILATION_EVENT_KINDS <= EVENT_KINDS)
        self.assertEqual(parse_kernel_id(kernel_id("cmp")), "cmp")

    def test_status_machine(self):
        check_compilation_transition("compiled", "approved")
        check_compilation_transition("approved", "materialized")
        check_compilation_transition("materialized", "verified")
        check_compilation_transition("verified", "operating")
        check_compilation_transition("operating", "rolled_back")
        for current, target in (
            ("compiled", "materialized"),   # approval cannot be skipped
            ("rejected", "approved"),        # a rejected card needs revision
            ("rolled_back", "compiled"),     # rolled_back is terminal
            ("materialized", "operating"),   # the drill cannot be skipped
        ):
            with self.assertRaises(KernelError):
                check_compilation_transition(current, target)


class TestCardValidation(unittest.TestCase):
    def setUp(self):
        self.discovery = fake_discovery()
        self.card = candidate_card()

    def normalize(self, card=None, discovery=None):
        return normalize_card(
            card if card is not None else self.card,
            discovery if discovery is not None else self.discovery,
        )

    def test_happy_path_resolves_deterministic_identities(self):
        normalized = self.normalize()
        workflow = normalized["assets"]["create"]["workflows"][0]
        self.assertEqual(
            workflow["workflow_id"], workflow_uuid(WORKFLOW_IDENTITY)
        )
        self.assertTrue(
            workflow["input_override_key"].endswith(".text_input")
        )
        fixture = normalized["drill"]["fixtures"][0]
        self.assertEqual(fixture["workflow_id"], workflow["workflow_id"])
        self.assertEqual(
            fixture["override_key"], workflow["input_override_key"]
        )
        self.assertEqual(
            normalized["mission"]["capitol"]["workflows"],
            [workflow["workflow_id"]],
        )
        # normalization is idempotent and digest-stable
        self.assertEqual(
            card_digest(self.normalize()), card_digest(normalized)
        )

    def test_mission_dry_run_forced(self):
        self.card["mission"]["dry_run"] = False
        self.assertTrue(self.normalize()["mission"]["dry_run"])

    def test_unknown_top_field_fails_closed(self):
        self.card["surprise"] = True
        with self.assertRaisesRegex(CardError, "unknown fields"):
            self.normalize()

    def test_wrong_schema_fails_closed(self):
        self.card["schema"] = "conch.architecture_card.v3"
        with self.assertRaisesRegex(CardError, "unsupported card schema"):
            self.normalize()

    def test_v1_remains_digest_stable_and_readable(self):
        normalized = self.normalize()
        self.assertEqual(normalized["schema"], "conch.architecture_card.v1")
        self.assertNotIn("procedure_sources", normalized)

    def test_v2_adopts_exact_discovered_workflow(self):
        source_workflow = {
            "id": "wf-source",
            "name": "Existing Exact Process",
            "workflow_version_id": "ver-source-3",
            "version_number": 3,
            "workflow_payload_digest": "sha256:" + "a" * 64,
            "input_override_key": "node-input.value",
        }
        discovery = fake_discovery()
        discovery["org_id"] = "org-test"
        discovery["workflows"].append(source_workflow)
        card = candidate_card()
        card["schema"] = "conch.architecture_card.v2"
        card["assets"] = {
            "reuse": [{
                "kind": "workflow",
                "id": "wf-source",
                "name": "Existing Exact Process",
                "reason": "adopt this exact workflow version",
            }],
            "create": {
                "workflows": [], "agent": None, "schedules": [],
                "collections": [],
            },
        }
        card["drill"]["fixtures"][0]["workflow"] = "wf-source"
        card["mission"]["capitol"]["workflows"] = ["wf-source"]
        card["procedure_sources"] = [{
            "relationship": "adopt",
            "org_id": "org-test",
            "procedure_document_id": "proc-source",
            "workflow_id": "wf-source",
            "workflow_version_id": "ver-source-3",
            "workflow_version_number": 3,
            "workflow_payload_digest": "sha256:" + "a" * 64,
            "procedure_content_digest": "sha256:" + "b" * 64,
            "compiler_version": "1.0.0",
        }]
        normalized = normalize_card(card, discovery)
        self.assertEqual(
            normalized["drill"]["fixtures"][0]["override_key"],
            "node-input.value",
        )
        bindings = normalized["pack"]["manifest"]["capitol"]["workflows"]
        self.assertEqual(
            list(bindings.values()), [{"id": "wf-source"}],
        )

    def test_v2_cross_org_direct_adopt_is_refused(self):
        card = candidate_card()
        card["schema"] = "conch.architecture_card.v2"
        card["procedure_sources"] = [{
            "relationship": "adopt",
            "org_id": "other-org",
            "procedure_document_id": "proc-source",
            "workflow_id": "wf-ingest",
            "workflow_version_id": "ver-source",
            "workflow_version_number": 1,
            "workflow_payload_digest": "sha256:" + "a" * 64,
            "procedure_content_digest": "sha256:" + "b" * 64,
            "compiler_version": "1.0.0",
        }]
        card["assets"]["reuse"].append({
            "kind": "workflow", "id": "wf-ingest",
            "name": "together-funding-ingest", "reason": "source",
        })
        discovery = fake_discovery()
        discovery["org_id"] = "org-test"
        with self.assertRaisesRegex(CardError, "cross-org"):
            normalize_card(card, discovery)

    def test_missing_required_sections(self):
        for key in ("goal", "narrative", "drill", "rollback", "mission"):
            broken = copy.deepcopy(candidate_card())
            del broken[key]
            with self.assertRaises(CardError):
                normalize_card(broken, self.discovery)

    def test_identity_prefix_enforced(self):
        self.card["assets"]["create"]["workflows"][0]["identity"] = (
            "rogue-daily-report"
        )
        with self.assertRaisesRegex(CardError, "prefix"):
            self.normalize()

    def test_unknown_stage_kind_fails_closed(self):
        self.card["assets"]["create"]["workflows"][0]["stages"][1][
            "kind"
        ] = "python_exec"
        with self.assertRaisesRegex(CapitolError, "not supported"):
            self.normalize()

    def test_unknown_stage_field_fails_closed(self):
        self.card["assets"]["create"]["workflows"][0]["stages"][1][
            "shell"
        ] = "rm -rf /"
        with self.assertRaisesRegex(CapitolError, "unknown fields"):
            self.normalize()

    def test_tool_outside_catalog_fails_closed(self):
        self.card["assets"]["create"]["workflows"][0]["stages"][1][
            "tools"
        ] = ["GMAIL_SEND_EMAIL"]
        with self.assertRaisesRegex(CapitolError, "node catalog"):
            self.normalize()

    def test_first_stage_must_be_input(self):
        stages = self.card["assets"]["create"]["workflows"][0]["stages"]
        stages[0], stages[1] = stages[1], stages[0]
        with self.assertRaises(CapitolError):
            self.normalize()

    def test_dangling_workflow_reference(self):
        self.card["drill"]["fixtures"][0]["workflow"] = (
            "$create:conch-compile-nonexistent"
        )
        with self.assertRaisesRegex(CardError, "unknown created workflow"):
            self.normalize()

    def test_invented_reused_workflow_id_refused(self):
        self.card["mission"]["capitol"]["workflows"] = ["wf-invented"]
        with self.assertRaisesRegex(CardError, "discovery did"):
            self.normalize()

    def test_bad_cron(self):
        self.card["assets"]["create"]["schedules"][0]["cron"] = "17:00"
        with self.assertRaisesRegex(CardError, "cron"):
            self.normalize()

    def test_unknown_collection_placeholder(self):
        self.card["assets"]["create"]["workflows"][0]["stages"][1][
            "system_prompt"
        ] = "Read $collection:col-unknown."
        with self.assertRaisesRegex(CapitolError, "unknown collection"):
            self.normalize()

    def test_mission_spec_fails_closed(self):
        self.card["mission"]["surprise_field"] = 1
        with self.assertRaisesRegex(CardError, "not a valid spec"):
            self.normalize()

    # -- reuse-first (the hard rule, enforced in code) -------------------

    def test_reuse_first_rejects_duplicate_workflow(self):
        self.card["assets"]["create"]["workflows"][0]["name"] = (
            "together-funding-ingest"
        )
        with self.assertRaisesRegex(CardError, "reuse-first"):
            self.normalize()

    def test_reuse_first_rejects_duplicate_collection(self):
        self.card["assets"]["create"]["collections"] = [{
            "identity": "conch-compile-together-funding-requests",
            "name": "together-funding-requests",
        }]
        with self.assertRaisesRegex(CardError, "reuse-first"):
            self.normalize()

    def test_reuse_first_rejects_duplicate_agent(self):
        self.card["assets"]["create"]["agent"]["name"] = "together-funding"
        with self.assertRaisesRegex(CardError, "reuse-first"):
            self.normalize()

    def test_reusing_existing_assets_is_the_sanctioned_path(self):
        normalized = self.normalize()
        reused = normalized["assets"]["reuse"]
        self.assertEqual(reused[0]["id"], "col-ledger")

    # -- drill fixture safety ------------------------------------------------

    def test_fixture_with_real_account_refused(self):
        self.card["drill"]["fixtures"][0]["input"] = (
            "email from ada@lumenrobotics.ai about a raise"
        )
        with self.assertRaisesRegex(CardError, "real-looking account"):
            self.normalize()

    def test_fixture_synthetic_domains_allowed(self):
        self.card["drill"]["fixtures"][0]["input"] = json.dumps([
            {"from": "founder@example.com"},
            {"from": "ops@synthetic.test"},
            {"from": "x@nowhere.invalid"},
        ])
        self.normalize()  # does not raise

    def test_empty_fixtures_refused(self):
        self.card["drill"]["fixtures"] = []
        with self.assertRaisesRegex(CardError, "non-empty"):
            self.normalize()

    # -- secretguard -----------------------------------------------------------

    def test_credentialed_card_refused_whole(self):
        self.card["narrative"] = (
            "use AKIAIOSFODNN7EXAMPLE / aws secret in the workflow"
        )
        with self.assertRaises(CredentialRejected):
            self.normalize()

    # -- generated pack --------------------------------------------------------

    def test_generated_pack_manifest_loads_fail_closed(self):
        from conch.capitol.packs.manifest import load_pack_data

        normalized = self.normalize()
        manifest = normalized["pack"]["manifest"]
        pack = load_pack_data(manifest, source="test")
        self.assertEqual(pack.name, normalized["pack"]["name"])
        self.assertEqual(
            manifest["acceptance"]["kind"], "workflow_drill"
        )
        alias = normalized["assets"]["create"]["workflows"][0]["identity"]
        self.assertEqual(
            manifest["capitol"]["workflows"][alias]["id"],
            workflow_uuid(WORKFLOW_IDENTITY),
        )

    # -- payload generation ------------------------------------------------------

    def test_payload_generation_is_deterministic(self):
        normalized = self.normalize()
        workflow = normalized["assets"]["create"]["workflows"][0]
        catalog = fake_catalog()
        first = build_workflow_payload(
            catalog, workflow, collection_ids={"col-ledger": "col-ledger"},
        )
        second = build_workflow_payload(
            catalog, workflow, collection_ids={"col-ledger": "col-ledger"},
        )
        self.assertEqual(payload_digest(first), payload_digest(second))
        self.assertEqual(first["id"], workflow["workflow_id"])
        self.assertTrue(first["publish_to_api"])
        # the reused collection id landed in the agent prompt
        agent_nodes = [
            node for node in first["nodes"]
            if node["data"]["struct"]["node_id"] == "agent_node"
        ]
        prompt = next(
            param["value"]
            for param in agent_nodes[0]["data"]["struct"]["params"]
            if param["field_id"] == "system_prompt"
        )
        self.assertIn("col-ledger", prompt)
        self.assertNotIn("$collection:", prompt)

    # -- rendering + diffing --------------------------------------------------------

    def test_render_markdown(self):
        normalized = self.normalize()
        text = render_card_markdown(normalized, card_version=1)
        self.assertIn("# Architecture Card", text)
        self.assertIn("Assets — reuse", text)
        self.assertIn(WORKFLOW_IDENTITY, text)
        self.assertIn("DISABLED", text)
        self.assertIn("Rollback plan", text)

    def test_versions_diff_cleanly(self):
        first = self.normalize()
        revised_raw = copy.deepcopy(candidate_card())
        revised_raw["success_criteria"].append(
            "notification names the report document"
        )
        second = normalize_card(revised_raw, self.discovery)
        diff = diff_cards(first, second)
        self.assertIn("+", diff)
        self.assertIn("notification names the report document", diff)
        self.assertEqual(diff_cards(first, first), "")


class TestCompilationAggregate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MissionStore(
            Path(self._tmp.name) / "kernel" / "kernel.db"
        )
        self.addCleanup(self.store.close)
        self.card = normalize_card(candidate_card(), fake_discovery())

    def create(self):
        return self.store.create_compilation(self.card, actor="tester")

    def test_create_projects_and_replays(self):
        compilation = self.create()
        self.assertEqual(compilation["status"],
                         CompilationStatus.COMPILED)
        self.assertEqual(compilation["card_version"], 1)
        self.assertEqual(compilation["goal"], self.card["goal"])
        row = self.store.compilation_card(compilation["compilation_id"])
        self.assertEqual(row["card"], self.card)
        self.assertEqual(row["digest"], "sha256:" + __import__(
            "hashlib"
        ).sha256(json.dumps(
            self.card, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest())
        ok, message = self.store.replay_matches_live()
        self.assertTrue(ok, message)
        self.assertGreater(self.store.verify_chain(), 0)

    def test_full_lifecycle_replays(self):
        compilation = self.create()
        cid = compilation["compilation_id"]
        self.store.record_compilation_card(
            cid, dict(self.card, narrative=self.card["narrative"] + " v2"),
            actor="tester", guidance="tighter",
        )
        self.store.decide_compilation(cid, "approve", decided_by="tester")
        self.store.record_compilation_materialization(
            cid, {"steps": [{"step": "workflow:x", "receipt": {},
                             "rollback_ref": {"kind": "delete_workflow",
                                              "workflow_id": "w"},
                             "adopted": False}]},
            complete=True,
        )
        self.store.record_compilation_drill(
            cid, {"runs": [{"run_id": "r1", "status": "success"}]},
            passed=True,
        )
        self.store.transition_compilation(
            cid, CompilationStatus.OPERATING, mission_id="msn-test",
        )
        final = self.store.get_compilation(cid)
        self.assertEqual(final["status"], CompilationStatus.OPERATING)
        self.assertEqual(final["mission_id"], "msn-test")
        self.assertTrue(final["drill"]["passed"])
        ok, message = self.store.replay_matches_live()
        self.assertTrue(ok, message)

    def test_revision_bumps_version_and_invalidates_approval(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="me")
        approved = self.store.get_compilation(cid)
        self.assertEqual(approved["status"], CompilationStatus.APPROVED)
        self.assertEqual(approved["approved_version"], 1)
        new_version = self.store.record_compilation_card(
            cid, dict(self.card, narrative="revised"), actor="me",
            guidance="change it",
        )
        self.assertEqual(new_version, 2)
        revised = self.store.get_compilation(cid)
        self.assertEqual(revised["status"], CompilationStatus.COMPILED)
        self.assertEqual(revised["approved_version"], 0)
        self.assertEqual(revised["approved_digest"], "")
        versions = self.store.compilation_card_versions(cid)
        self.assertEqual([row["card_version"] for row in versions], [1, 2])
        self.assertEqual(versions[1]["guidance"], "change it")

    def test_reject_then_revise(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(
            cid, "reject", decided_by="me", reason="too broad",
        )
        rejected = self.store.get_compilation(cid)
        self.assertEqual(rejected["status"], CompilationStatus.REJECTED)
        self.assertEqual(rejected["decision_reason"], "too broad")
        self.store.record_compilation_card(
            cid, dict(self.card, narrative="narrower"), actor="me",
        )
        self.assertEqual(
            self.store.get_compilation(cid)["status"],
            CompilationStatus.COMPILED,
        )

    def test_approval_pins_current_digest(self):
        cid = self.create()["compilation_id"]
        result = self.store.decide_compilation(
            cid, "approve", decided_by="me",
        )
        row = self.store.compilation_card(cid)
        self.assertEqual(result["digest"], row["digest"])
        compilation = self.store.get_compilation(cid)
        self.assertEqual(compilation["approved_digest"], row["digest"])
        self.assertEqual(compilation["decided_by"], "me")
        self.assertEqual(compilation["decision_origin"], "local::")

    def test_non_local_origin_refused(self):
        cid = self.create()["compilation_id"]
        for origin in ("slack", "session", "sms", ""):
            with self.assertRaisesRegex(ApprovalError, "local-only"):
                self.store.decide_compilation(
                    cid, "approve", decided_by="model",
                    origin_channel=origin,
                )
        # nothing changed
        self.assertEqual(
            self.store.get_compilation(cid)["status"],
            CompilationStatus.COMPILED,
        )

    def test_double_decision_refused(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="me")
        with self.assertRaises(KernelError):
            self.store.decide_compilation(cid, "approve", decided_by="me")

    def test_materialization_requires_approval(self):
        cid = self.create()["compilation_id"]
        with self.assertRaisesRegex(KernelError, "approved"):
            self.store.record_compilation_materialization(
                cid, {"steps": []}, complete=True,
            )

    def test_drill_requires_materialized(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="me")
        with self.assertRaisesRegex(KernelError, "materialized"):
            self.store.record_compilation_drill(cid, {}, passed=True)

    def test_drill_failure_stays_materialized(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="me")
        self.store.record_compilation_materialization(
            cid, {"steps": []}, complete=True,
        )
        self.store.record_compilation_drill(
            cid, {"error": "gate failed"}, passed=False,
        )
        compilation = self.store.get_compilation(cid)
        self.assertEqual(
            compilation["status"], CompilationStatus.MATERIALIZED
        )
        self.assertFalse(compilation["drill"]["passed"])

    def test_no_revision_after_materialization(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="me")
        self.store.record_compilation_materialization(
            cid, {"steps": []}, complete=True,
        )
        with self.assertRaisesRegex(KernelError, "materialization"):
            self.store.record_compilation_card(
                cid, dict(self.card, narrative="too late"), actor="me",
            )

    def test_resolve_by_seq_and_prefix(self):
        compilation = self.create()
        cid = compilation["compilation_id"]
        by_seq = self.store.resolve_compilation(
            f"#{compilation['compilation_seq']}"
        )
        self.assertEqual(by_seq["compilation_id"], cid)
        by_prefix = self.store.resolve_compilation(cid[:16])
        self.assertEqual(by_prefix["compilation_id"], cid)
        self.assertIsNone(self.store.resolve_compilation("cmp-nope"))

    def test_event_history_on_own_chain(self):
        cid = self.create()["compilation_id"]
        self.store.decide_compilation(cid, "approve", decided_by="me")
        kinds = [
            event["kind"]
            for event in self.store.compilation_events(cid)
        ]
        self.assertEqual(
            kinds, ["compilation_created", "compilation_decided"]
        )

    def test_procedure_link_is_idempotent_and_conflicts_fail_closed(self):
        cid = self.create()["compilation_id"]
        decision = self.store.decide_compilation(
            cid, "approve", decided_by="me",
        )
        link = {
            "workflow_id": "wf-1",
            "workflow_version_id": "ver-1",
            "workflow_version_number": 1,
            "workflow_payload_digest": "sha256:" + "a" * 64,
            "procedure_document_id": "proc-1",
            "procedure_content_digest": "sha256:" + "b" * 64,
            "compiler_version": "1.0.0",
            "verification": "draft",
            "card_version": 1,
            "card_digest": decision["digest"],
            "lineage": {"compilation_id": cid},
            "linked_at": 1.0,
        }
        self.assertTrue(
            self.store.record_compilation_procedure_link(cid, link)
        )
        replayed = dict(link, linked_at=2.0)
        self.assertFalse(
            self.store.record_compilation_procedure_link(cid, replayed)
        )
        with self.assertRaises(ConflictError):
            self.store.record_compilation_procedure_link(
                cid,
                dict(
                    link,
                    procedure_content_digest="sha256:" + "c" * 64,
                    linked_at=3.0,
                ),
            )
        self.assertEqual(
            len(self.store.get_compilation(cid)["procedures"]["links"]), 1,
        )
        ok, message = self.store.replay_matches_live()
        self.assertTrue(ok, message)

    def test_card_secretguard_at_store_boundary(self):
        with self.assertRaises(CredentialRejected):
            self.store.create_compilation({
                "goal": "leak",
                "note": "aws key AKIAIOSFODNN7EXAMPLE",
            })


if __name__ == "__main__":
    unittest.main()
