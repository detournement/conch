"""/capitol command-family tests against the fake A2A + admin gateways.

The runtime subcommands map onto CapitolRuntime (reads never touch the
kernel ledger), watch cursors persist in versioned CLI state so a bare
re-invoke resumes, skill-gated features surface missing card skills as
"does not advertise", the gated admin subcommands refuse without
capitol_admin=true and anchor every mutation in the kernel ledger under
the find-or-create capitol-admin-cli ops mission, and the pack surface
lists/validates/verifies flow packs through the loader.
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.capitol.commands import run_capitol_command, watch_cursor
from conch.commands import SLASH_COMMANDS, slash_command_names

from tests.test_capitol_admin import (
    MINTED,
    ORG as ADMIN_ORG,
    TOKEN,
    FakeAdminGateway,
)
from tests.test_capitol_client import (
    AGENT,
    BEARER,
    ORG,
    FakeGateway,
    _event,
)


def _run(arg, config):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        run_capitol_command(arg, config)
    return buffer.getvalue()


class CapitolCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
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
        FakeGateway.reset(self.port)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self.tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config"),
            "CAPITOL_A2A_BEARER": BEARER,
        })
        env.start()
        self.addCleanup(env.stop)
        self.config = {
            "capitol_base_url": f"http://127.0.0.1:{self.port}",
            "capitol_org": ORG,
            "capitol_agent": AGENT,
        }

    # -- registration -----------------------------------------------------

    def test_registered_in_slash_commands(self):
        from conch.commands import all_slash_commands

        self.assertIn("/capitol", slash_command_names())
        entry = next(
            e for e in all_slash_commands() if e[0].startswith("/capitol")
        )
        self.assertIn("A2Actrl", entry[1])

    def test_help_lists_subcommand_groups(self):
        out = _run("help", self.config)
        for token in ("card", "start", "watch", "respond", "admin",
                      "pack verify", "up", "chat"):
            self.assertIn(token, out)

    # -- read / observe -----------------------------------------------------

    def test_card_workflows_describe(self):
        out = _run("card", self.config)
        self.assertIn("Fake eBay Sales Operator", out)
        self.assertIn("wire schema:  1.0.11", out)
        self.assertIn("subscribe_workflow_events", out)
        out = _run("workflows", self.config)
        self.assertIn("draft-wf", out)
        self.assertIn("Draft or Revise Listing", out)
        out = _run("describe draft-wf", self.config)
        self.assertIn("node-json-input", out)

    def test_agents_directory(self):
        out = _run("agents", self.config)
        self.assertIn(AGENT, out)
        self.assertIn("2 workflow(s)", out)

    def test_suggest_and_capability_gating(self):
        out = _run("suggest sell a lamp -n 2", self.config)
        self.assertIn("draft-wf", out)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "suggest_workflow")
        self.assertEqual(data["max_suggestions"], 2)
        # A card without the skill surfaces the gap, not a crash.
        FakeGateway.card = dict(
            FakeGateway.card,
            skills=[{"id": "handshake"}, {"id": "call_workflow"}],
        )
        out = _run("suggest anything", self.config)
        self.assertIn("does not advertise", out)
        self.assertIn("suggest_workflow", out)

    def test_stats_versions_runs(self):
        out = _run("stats draft-wf --days 7", self.config)
        self.assertIn('"run_count": 4', out)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(data, {"workflow_id": "draft-wf", "days": 7})
        out = _run("versions draft-wf", self.config)
        self.assertIn('"version_id": "v2"', out)
        out = _run("runs", self.config)
        self.assertIn("per-workflow", out, "org-wide run listing gap named")

    def test_status_events_output_evals(self):
        FakeGateway.runs["run-x"] = {
            "status": "success",
            "output": {"eval_rollup": {
                "has_evals": True,
                "summary": {"total": 1, "passed": 1, "failed": 0, "na": 0,
                            "errors": 0, "success_rate": 1.0,
                            "suite_passed": True},
                "evals": [], "eval_nodes": [],
            }},
        }
        FakeGateway.run_events["run-x"] = [_event(1), _event(2)]
        self.assertIn('"status": "success"', _run("status run-x",
                                                  self.config))
        out = _run("events run-x --since 2", self.config)
        self.assertIn("seq    2", out)
        self.assertNotIn("seq    1", out)
        out = _run("output run-x", self.config)
        self.assertIn("eval_rollup", out)
        out = _run("evals run-x", self.config)
        self.assertIn('"suite_passed": true', out)

    def test_procedure_search_and_exact_show_use_read_client(self):
        from tests.test_capitol_procedures import document, result_item

        client = unittest.mock.Mock()
        client.search.return_value = {
            "schema_version": "capitol.procedure_collection.v1",
            "results": [result_item()],
            "limit": 10, "offset": 0, "total": 1,
        }
        client.get.return_value = document()
        with patch(
            "conch.capitol.procedures.CapitolProcedureClient.from_config",
            return_value=client,
        ):
            out = _run('procedure search "governed thing"', self.config)
            self.assertIn("Governed Thing", out)
            self.assertIn("reviewed", out)
            out = _run(
                f"procedure show {document()['workflow_id']} --version 2",
                self.config,
            )
        self.assertIn("untrusted Procedure prose", out)
        self.assertIn(document()["content_digest"], out)
        client.get.assert_called_once_with(
            document()["workflow_id"], version_number=2,
        )

    def test_procedure_endpoint_gap_fails_closed_without_traceback(self):
        from conch.capitol.errors import CapitolProtocolError

        client = unittest.mock.Mock()
        client.search.side_effect = CapitolProtocolError(
            "unsupported Procedure search schema (failing closed)"
        )
        with patch(
            "conch.capitol.procedures.CapitolProcedureClient.from_config",
            return_value=client,
        ):
            out = _run('procedure search "warehouse"', self.config)
        self.assertIn("failing closed", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn(BEARER, out)

    # -- start / watch -------------------------------------------------------

    def test_start_discovers_inputs_key_and_prints_replay_key(self):
        out = _run("start draft-wf --input '{\"n\":1}'", self.config)
        self.assertIn("inputs key: node-json-input.value", out)
        self.assertIn("run run-1 started", out)
        self.assertIn("idempotency key: capitol-cli:", out)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "call_workflow")
        self.assertEqual(data["inputs"], {"node-json-input.value": {"n": 1}})
        # replay with --key returns the same run
        key = [k for k in FakeGateway.idempotency][0]
        out = _run(f"start draft-wf --input '{{\"n\":1}}' --key {key}",
                   self.config)
        self.assertIn("run run-1 started (replayed)", out)

    def test_start_raw_inputs_and_input_file(self):
        payload = Path(self.tmp.name) / "inputs.json"
        payload.write_text(json.dumps({"custom.key": {"a": 1}}))
        out = _run(f"start draft-wf --input @{payload} --raw", self.config)
        self.assertIn("started", out)
        _skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(data["inputs"], {"custom.key": {"a": 1}})

    def test_watch_is_resumable_from_persisted_cursor(self):
        run_id = "run-w"
        FakeGateway.runs[run_id] = {"status": "running", "output": {}}
        FakeGateway.stream_plans[run_id] = [
            {"events": [_event(1), _event(2)], "end": "terminal"},
        ]
        out = _run(f"watch {run_id}", self.config)
        self.assertIn("seq    1", out)
        self.assertEqual(watch_cursor(run_id), 2,
                         "the cursor persisted per delivered event")
        # A bare re-invoke resumes from the persisted cursor + 1.
        FakeGateway.stream_plans[run_id] = [
            {"events": [_event(2), _event(3)], "end": "terminal"},
        ]
        out = _run(f"watch {run_id}", self.config)
        self.assertIn("resuming run run-w from sequence 3", out)
        self.assertIn("seq    3", out)
        self.assertNotIn("seq    2", out, "no replay below the cursor")
        self.assertIn("ended: TASK_STATE_COMPLETED", out)
        self.assertEqual(FakeGateway.stream_requests[-1]["since"], 3)

    def test_start_watch_combo(self):
        FakeGateway.stream_plans["run-1"] = [
            {"events": [_event(1)], "end": "terminal"},
        ]
        out = _run("start draft-wf --input '{\"n\":2}' --watch",
                   self.config)
        self.assertIn("run run-1 started", out)
        self.assertIn("seq    1", out)

    # -- steer ----------------------------------------------------------------

    def test_respond_clarification_and_intervention(self):
        _run("respond run-9 req-1 the blue variant", self.config)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_clarification_response")
        self.assertEqual(data, {
            "run_id": "run-9", "request_id": "req-1",
            "response": "the blue variant", "declined": False,
        })
        _run("respond run-9 req-2 --decline", self.config)
        _skill, data, _env = FakeGateway.calls[-1]
        self.assertTrue(data["declined"])
        _run("respond run-9 req-3 --continue --node n-7", self.config)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_intervention_response")
        self.assertEqual(data, {
            "run_id": "run-9", "node_id": "n-7",
            "request_id": "req-3", "response": "continue",
        })

    def test_pause_stop_resume_cancel(self):
        FakeGateway.runs["run-p"] = {"status": "paused", "output": {}}
        _run("pause run-p --reason maintenance", self.config)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual((skill, data["reason"]),
                         ("pause_workflow", "maintenance"))
        _run("resume run-p --payload '{\"go\":true}' --edited-nodes n1,n2",
             self.config)
        skill, data, _env = FakeGateway.calls[-1]
        self.assertEqual(skill, "resume_workflow")
        self.assertEqual(data["payload"], {"go": True})
        self.assertEqual(data["edited_node_ids"], ["n1", "n2"])
        _run("stop run-p", self.config)
        self.assertEqual(FakeGateway.calls[-1][0], "stop_workflow")
        out = _run("cancel run-p", self.config)
        self.assertIn("TASK_STATE_CANCELED", out)

    def test_chat_displays_untrusted_prose(self):
        out = _run("chat what can you do", self.config)
        self.assertIn("untrusted assistant prose", out)
        self.assertIn("hello", out)

    # -- artifacts ---------------------------------------------------------------

    def test_up_down_url(self):
        blob = Path(self.tmp.name) / "item.bin"
        blob.write_bytes(b"artifact-bytes")
        out = _run(f"up {blob}", self.config)
        self.assertIn("artifact_id: art-1", out)
        self.assertIn("digest:      sha256:", out)
        FakeGateway.blobs["file-7"] = b"label pdf bytes"
        dest = Path(self.tmp.name) / "out" / "label.pdf"
        out = _run(f"down file-7 {dest}", self.config)
        self.assertIn("downloaded to", out)
        self.assertEqual(dest.read_bytes(), b"label pdf bytes")
        out = _run("url file-7", self.config)
        self.assertIn("/blob/file-7", out)
        self.assertIn("use immediately", out)

    # -- failure surfaces ----------------------------------------------------------

    def test_auth_failure_parks_with_credential_message(self):
        with patch.dict(os.environ,
                        {"CAPITOL_A2A_BEARER": "cap_a2a_WRONG"}):
            out = _run("workflows", self.config)
        self.assertIn("credential needed", out)
        self.assertIn("no automatic re-auth", out)
        self.assertNotIn("cap_a2a_WRONG", out)

    def test_unknown_subcommand_prints_usage(self):
        out = _run("frobnicate", self.config)
        self.assertIn("Unknown subcommand", out)
        self.assertIn("/capitol — generic Capitol control", out)

    def test_unconfigured_capitol_is_a_clear_error(self):
        out = _run("workflows", {})
        self.assertIn("capitol_base_url", out)


class CapitolPackSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self.tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_pack_list_and_show(self):
        out = _run("packs", {})
        self.assertIn("ebay-listing", out)
        self.assertIn("sha256:", out)
        out = _run("pack show ebay-listing", self.config if hasattr(
            self, "config") else {})
        self.assertIn("workflows: draft, publish", out)
        self.assertIn("approvals: ebay_publish", out)
        self.assertIn("acceptance: golden_scenarios", out)

    def test_pack_show_unknown_fails_closed(self):
        out = _run("pack show nope", {})
        self.assertIn("no pack named", out)

    def test_pack_list_reports_invalid_user_pack(self):
        from conch.capitol.packs import user_packs_dir

        bad = user_packs_dir() / "broken-pack"
        bad.mkdir(parents=True)
        (bad / "pack.json").write_text("{not json")
        out = _run("packs", {})
        self.assertIn("broken-pack: INVALID", out)
        self.assertIn("ebay-listing", out, "other packs still listed")

    def test_pack_verify_runs_the_golden_acceptance_drill(self):
        out = _run("pack verify ebay-listing", {})
        self.assertIn("manifest: valid (sha256:", out)
        self.assertIn("ok: shell_typed_clarify_auto_publish", out)
        self.assertIn("scenario(s) equivalent", out)


class CapitolAdminCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         FakeAdminGateway)
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "CAPITOL_ADMIN_TOKEN": TOKEN,
        })
        env.start()
        self.addCleanup(env.stop)
        registry = patch(
            "conch.capitol.credentials.REGISTRY_PATH",
            root / "agents.yaml",
        )
        registry.start()
        self.addCleanup(registry.stop)
        base = f"http://127.0.0.1:{self.port}"
        self.config = {
            "capitol_admin": "true",
            "capitol_base_url": base,
            "capitol_platform_url": base,
            "capitol_org": ADMIN_ORG,
        }

    def _kernel_store(self):
        from conch.kernel.store import MissionStore, default_kernel_db_path

        return MissionStore(default_kernel_db_path())

    def test_admin_gate_fails_closed_without_flag(self):
        config = dict(self.config)
        config.pop("capitol_admin")
        out = _run("admin agents", config)
        self.assertIn("capitol_admin", out)
        self.assertEqual(FakeAdminGateway.requests, [])

    def test_create_agent_ledgers_and_prints_fingerprint_only(self):
        out = _run(
            "admin create-agent conch-cli-orch --workflows wf-1 "
            "--key k-cli-1",
            self.config,
        )
        self.assertIn("bearer_fingerprint", out)
        self.assertNotIn(MINTED, out, "bearer bytes never printed")
        self.assertIn("A2Actrl registry", out)
        store = self._kernel_store()
        try:
            action = store.find_action("capitol-admin:create_agent:k-cli-1")
            self.assertEqual(action["status"], "committed")
            missions = [
                m for m in store.list_missions()
                if str((m.get("spec") or {}).get("goal") or "").startswith(
                    "capitol-admin-cli"
                )
            ]
            self.assertEqual(len(missions), 1,
                             "one find-or-create anchor mission")
        finally:
            store.close()
        # A second admin op reuses the anchor mission (no duplicates).
        _run("admin collections create conch-cli-col --key k-cli-2",
             self.config)
        store = self._kernel_store()
        try:
            missions = [
                m for m in store.list_missions()
                if str((m.get("spec") or {}).get("goal") or "").startswith(
                    "capitol-admin-cli"
                )
            ]
            self.assertEqual(len(missions), 1)
        finally:
            store.close()

    def test_default_key_is_digest_derived_and_replayable(self):
        first = _run("admin collections create digest-col", self.config)
        self.assertIn("idempotency key: capitol-cli:collections-create:",
                      first)
        wire_before = len(FakeAdminGateway.requests)
        second = _run("admin collections create digest-col", self.config)
        self.assertIn('"replayed": true', second)
        self.assertEqual(len(FakeAdminGateway.requests), wire_before,
                         "the digest key replays without a wire call")

    def test_publish_and_rollback_note(self):
        out = _run("admin publish wf-1 --key k-pub", self.config)
        self.assertIn('"published": true', out)
        self.assertIn('"version_pin"', out)
        out = _run("admin rollback wf-1 --key k-roll", self.config)
        self.assertIn('"rolled_back": true', out)
        self.assertIn("persist-based inverse", out)

    def test_allowlist_and_schedules(self):
        _run("admin create-agent orch --workflows wf-1 --key k-a",
             self.config)
        agents = json.loads(_run("admin agents", self.config))
        agent_id = agents[0]["id"]
        out = _run(f"admin allowlist {agent_id} wf-1,wf-2 --key k-b",
                   self.config)
        self.assertIn('"rollback_ref"', out)
        out = _run(
            "admin schedule-add wf-1 nightly '0 3 * * *' --tz UTC "
            "--key k-c",
            self.config,
        )
        self.assertIn('"schedule_id"', out)
        out = _run("admin schedules wf-1", self.config)
        self.assertIn("nightly", out)

    def test_required_policy_veto_reaches_cli(self):
        from conch.policy import (
            register_required_policy,
            unregister_required_policy,
        )

        register_required_policy(
            "test-cli-admin-deny",
            lambda event, payload: not event.startswith("capitol.admin."),
        )
        self.addCleanup(unregister_required_policy, "test-cli-admin-deny")
        out = _run("admin collections create vetoed --key k-veto",
                   self.config)
        self.assertIn("denied by required policy", out)


if __name__ == "__main__":
    unittest.main()
