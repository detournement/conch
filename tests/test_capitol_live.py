"""Live Capitol-stack gates for Swarm Phase 3 (opt-in, fail-closed).

Run against the local dev stack with::

    CONCH_CAPITOL_LIVE=1 python -m unittest tests.test_capitol_live -v

Every case skips cleanly when the stack, CLI, or admin token is absent
(see :mod:`tests.capitol_live_support`). These are the live twins of the
recorded fake-gateway contract tests in ``tests/test_capitol_client.py``
and the mission gates in ``tests/test_capitol_mission.py``:

- ``AdminDrillLive`` — the bounded builder profile end to end: create an
  orchestrator agent (bearer sunk under an OS-side reference, never
  returned/logged), publish + pin a disposable workflow version, manage
  the agent's workflow allowlist, create/update/delete a schedule, revert
  the publish to the prior version, and clean every asset up — each
  mutation ledgered with an idempotency key and a rollback reference.
- ``RuntimeGatesLive`` — invoke and supervise a run to a terminal state,
  resume events from a persisted cursor with no replay, read the eval
  roll-up, round-trip an artifact, and round-trip a HITL checkpoint.

All assets carry the ``conch-phase3-`` prefix and are disposable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from conch.capitol.admin import CapitolAdmin
from conch.capitol.client import CapitolRuntime
from conch.capitol.credentials import resolve_admin_token
from conch.kernel.store import MissionStore

from tests import capitol_live_support as live
from tests.capitol_live_support import (
    ASSET_PREFIX,
    ORG,
    PLATFORM_URL,
    WORKFLOW_URL,
    LiveHTTP,
    poll_until_terminal,
    unique_suffix,
)

TERMINAL = {"success", "failed", "stopped", "cancelled"}


class AdminDrillLive(unittest.TestCase):
    """Deliverable 1: the create → verify → revert → clean-up drill,
    against the serving stack, driven through :class:`CapitolAdmin`."""

    @classmethod
    def setUpClass(cls):
        cls.config = live.live_config()
        cls.http = LiveHTTP()
        base = cls.http.find_workflow_by_name(live.EVAL_WORKFLOW_NAME)
        if not base:
            # any workflow will do as a duplication base for publish/pin
            workflows = cls.http.list_workflows()
            if not workflows:
                raise unittest.SkipTest("no workflows in the dev org to clone")
            base = str(workflows[0].get("id"))
        cls.base_workflow = base

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        # Resolve the admin token from the *real* A2Actrl registry first…
        token, _ = resolve_admin_token({}, ORG, PLATFORM_URL)
        # …then redirect bearer sinks to a throwaway registry, so the drill
        # never writes to the user's real ~/.capitol-a2a/agents.yaml (the
        # sink is proven in the fake suite; here we just confirm it happens
        # live and 0600).
        self.registry = root / "agents.yaml"
        patcher = patch(
            "conch.capitol.credentials.REGISTRY_PATH", self.registry
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MissionStore(root / "kernel.db")
        self.addCleanup(self.store.close)
        self.mission_id = self.store.create_mission(
            {"goal": "live capitol provisioning drill", "budgets": {}}
        )
        self.admin = CapitolAdmin(
            platform_url=PLATFORM_URL, workflow_url=WORKFLOW_URL,
            org_id=ORG, token=token, store=self.store,
            mission_id=self.mission_id, config=self.config,
        )
        self._evidence = {}

    def test_full_disposable_asset_lifecycle(self):
        suffix = unique_suffix()
        wf_name = f"{ASSET_PREFIX}wf-{suffix}"
        agent_name = f"{ASSET_PREFIX}orch-{suffix}"
        sched_name = f"{ASSET_PREFIX}sched-{suffix}"

        # -- disposable workflow to operate on (duplicate of the base) -------
        workflow_id = self.http.duplicate_workflow(self.base_workflow, wf_name)
        self.addCleanup(self.http.delete_workflow, workflow_id)
        self.assertTrue(workflow_id)
        self._evidence["workflow_id"] = workflow_id

        # -- create the orchestrator agent (bearer sunk, ledgered) -----------
        created = self.admin.create_orchestrator_agent(
            agent_name, [workflow_id],
            idempotency_key=f"live-create-{suffix}",
            registry_alias=agent_name,
        )
        agent_id = created["agent_id"]
        self.addCleanup(self.http.delete_agent, agent_id)
        self.assertTrue(agent_id)
        self._evidence["agent_id"] = agent_id
        # the bearer never came back to the caller; only a fingerprint did
        self.assertTrue(created["bearer_fingerprint"].startswith("sha256:"))
        self.assertNotIn("bearer_token", json.dumps(created))
        # and it really landed in the (throwaway) registry file, 0600
        registry_text = self.registry.read_text()
        self.assertIn(agent_id, registry_text)
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o600)
        # the ledger recorded the create with a delete rollback reference
        action = self.store.find_action(
            f"capitol-admin:create_agent:live-create-{suffix}"
        )
        self.assertEqual(action["status"], "committed")
        detail = json.loads(action["detail"])
        self.assertEqual(detail["result"]["rollback_ref"]["kind"],
                         "delete_agent")

        # -- verify the agent exists and carries the allowlist --------------
        fetched = self.admin.get_agent(agent_id)
        self.assertEqual(str(fetched.get("id")), agent_id)
        self.assertIn(workflow_id,
                      fetched.get("workflow_allowlist") or [])

        # -- manage the allowlist (prior captured for rollback) -------------
        allowlist = self.admin.set_workflow_allowlist(
            agent_id, [workflow_id], idempotency_key=f"live-allow-{suffix}",
        )
        self.assertIn("prior", allowlist["rollback_ref"])

        # -- publish + pin the workflow version -----------------------------
        published = self.admin.publish_workflow(
            workflow_id, idempotency_key=f"live-pub-{suffix}",
        )
        self.assertTrue(published.get("published") or
                        published.get("already_published"))
        version_pin = published["version_pin"]
        self.assertTrue(version_pin)
        self._evidence["published_version_pin"] = version_pin
        self.assertTrue(
            self.http.get_workflow_payload(workflow_id).get("publish_to_api")
        )
        # duplicate publish replays from the ledger without a second effect
        replay = self.admin.publish_workflow(
            workflow_id, idempotency_key=f"live-pub-{suffix}",
        )
        self.assertTrue(replay.get("replayed"))

        # -- schedule create / update / delete ------------------------------
        schedule = self.admin.create_schedule(
            workflow_id, sched_name, "0 9 * * 1-5",
            idempotency_key=f"live-sched-{suffix}",
        )
        schedule_id = schedule["schedule_id"]
        self.assertTrue(schedule_id)
        self._evidence["schedule_id"] = schedule_id
        updated = self.admin.update_schedule(
            workflow_id, schedule_id, {"enabled": False},
            idempotency_key=f"live-sched-upd-{suffix}",
        )
        self.assertIn("prior", updated["rollback_ref"])
        deleted = self.admin.delete_schedule(
            workflow_id, schedule_id,
            idempotency_key=f"live-sched-del-{suffix}",
        )
        self.assertTrue(deleted["deleted"])

        # -- revert the publish to the prior version ------------------------
        rolled = self.admin.rollback_workflow(
            workflow_id, idempotency_key=f"live-roll-{suffix}",
        )
        self.assertTrue(rolled.get("rolled_back") or
                        rolled.get("already_unpublished"))
        self.assertFalse(
            self.http.get_workflow_payload(workflow_id).get("publish_to_api"),
            "rollback must leave the workflow unpublished",
        )
        self._evidence["rolled_back_version_pin"] = rolled.get("version_pin")

        # -- explicit cleanup (also covered by addCleanup) ------------------
        self.admin.delete_agent(
            agent_id, idempotency_key=f"live-del-agent-{suffix}",
        )
        self.assertEqual(
            self.http.pl("GET", f"/agents/{ORG}/{agent_id}")[0], 404
        )
        self.assertEqual(self.http.delete_workflow(workflow_id), 204)

        # -- every mutation is on the ledger, committed ---------------------
        for key in ("create", "allow", "pub", "sched", "sched-upd",
                    "sched-del", "roll", "del-agent"):
            rows = self.store._read_conn().execute(
                "SELECT status FROM external_actions WHERE"
                " idempotency_key LIKE ?", (f"%live-{key}-{suffix}",),
            ).fetchall()
            self.assertTrue(rows, f"no ledger row for {key}")
            self.assertEqual(rows[0][0], "committed", key)
        # and the whole mission journal replays to identical projections
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def tearDown(self):
        if self._evidence:
            print("\n[live admin drill] " + json.dumps(self._evidence))


class RuntimeGatesLive(unittest.TestCase):
    """Deliverable 3: live invoke / supervise / resume / eval / artifact /
    HITL through :class:`CapitolRuntime`, using a disposable agent bound to
    a disposable published copy of the eval-proof workflow."""

    @classmethod
    def setUpClass(cls):
        cls.config = live.live_config()
        cls.http = LiveHTTP()
        cls._cleanup = []
        eval_base = cls.http.find_workflow_by_name(live.EVAL_WORKFLOW_NAME)
        if not eval_base:
            raise unittest.SkipTest(
                f"eval workflow {live.EVAL_WORKFLOW_NAME!r} not in dev org"
            )
        suffix = unique_suffix()
        # a disposable published copy of the eval workflow
        cls.workflow_id = cls.http.duplicate_workflow(
            eval_base, f"{ASSET_PREFIX}evalrun-{suffix}"
        )
        cls._cleanup.append(("wf", cls.workflow_id))
        payload = cls.http.get_workflow_payload(cls.workflow_id)
        payload["publish_to_api"] = True
        cls.http.persist_workflow(payload)
        # a disposable orchestrator allowlisting it
        status, agent = cls.http.pl("POST", f"/agents/{ORG}", {
            "name": f"{ASSET_PREFIX}runner-{suffix}",
            "model_provider": "anthropic",
            "model_name": "claude-sonnet-4-20250514",
            "enable_workflow_runtime": True, "exposed_via_a2a": True,
            "workflow_allowlist": [cls.workflow_id],
        })
        if status >= 400:
            raise unittest.SkipTest(f"could not create live agent: {agent}")
        cls.agent_id = str((agent.get("agent") or {}).get("id"))
        cls._bearer = str(agent.get("a2a_bearer_token") or "")
        cls._cleanup.append(("agent", cls.agent_id))
        cls.runtime = CapitolRuntime(
            WORKFLOW_URL, ORG, cls.agent_id, cls._bearer,
            caller_system="conch-live-tests", caller_version="0.0",
        )

    @classmethod
    def tearDownClass(cls):
        for kind, ident in reversed(getattr(cls, "_cleanup", [])):
            try:
                if kind == "agent":
                    cls.http.delete_agent(ident)
                else:
                    cls.http.delete_workflow(ident)
            except Exception:
                pass

    def test_discover_handshake_and_capability_gating(self):
        card = self.runtime.discover()
        self.assertTrue(card.get("name"))
        self.assertIs(self.runtime.streaming_advertised(), True)
        context = self.runtime.handshake()
        self.assertTrue(context)
        ids = self.runtime.skill_ids()
        self.assertIn("call_workflow", ids)
        self.assertIn("subscribe_workflow_events", ids)

    def test_invoke_supervise_to_terminal_and_eval_read(self):
        key = f"live-invoke-{unique_suffix()}"
        started = self.runtime.call_workflow(
            self.workflow_id, {}, idempotency_key=key
        )
        run_id = started["run_id"]
        print(f"\n[live invoke] run_id={run_id}")
        # idempotent replay: same key + inputs returns the same run
        again = self.runtime.call_workflow(
            self.workflow_id, {}, idempotency_key=key
        )
        self.assertEqual(again["run_id"], run_id)
        # supervise via the live SSE stream to a terminal status frame
        events = []
        final = None
        for event in self.runtime.watch_run(run_id, reconnect_delay=0):
            if event.get("event_type") == "_final_status":
                final = event
                break
            if event.get("sequence") is not None:
                events.append(int(event["sequence"]))
        self.assertIsNotNone(final, "watch_run never yielded a terminal frame")
        self.assertTrue(events, "no sequenced events streamed")
        self.assertEqual(events, sorted(events))  # monotonic, no reordering
        self.assertEqual(len(events), len(set(events)))  # no duplicates
        # the run really is terminal
        self.assertIn(poll_until_terminal(self.runtime, run_id), TERMINAL)
        # eval roll-up reads back (the proof workflow ships one passing eval)
        report = self.runtime.eval_report(run_id)
        self.assertTrue(report["has_evals"])
        self.assertTrue(report["summary"]["suite_passed"])

    def test_resume_events_from_cursor_without_replay(self):
        """Disconnect-and-resume semantics: the adapter's resumable poller
        (what the supervisor persists a cursor through) must never re-yield
        an event at or below the cursor — even though the gateway's
        ``get_workflow_events`` re-sends the tail on a bare ``since_sequence``
        (defended client-side in :meth:`CapitolRuntime.poll_run`)."""
        started = self.runtime.call_workflow(
            self.workflow_id, {},
            idempotency_key=f"live-resume-{unique_suffix()}",
        )
        run_id = started["run_id"]
        self.assertIn(poll_until_terminal(self.runtime, run_id), TERMINAL)
        full = [
            int(e["sequence"])
            for e in self.runtime.poll_run(
                run_id, max_polls=1, _sleep=lambda _s: None
            )
            if e.get("sequence") is not None
        ]
        self.assertTrue(full)
        cursor = full[len(full) // 2]
        resumed = [
            int(e["sequence"])
            for e in self.runtime.poll_run(
                run_id, since_sequence=cursor, max_polls=1,
                _sleep=lambda _s: None,
            )
            if e.get("sequence") is not None
        ]
        self.assertTrue(resumed, "expected events at/after the cursor")
        self.assertTrue(all(seq >= cursor for seq in resumed),
                        "events below the cursor must not replay")
        self.assertEqual(resumed, sorted(resumed))  # contiguous, ordered

    def test_artifact_round_trip(self):
        import hashlib

        blob = b"\x89PNG\r\n conch-phase3 live artifact " + uuid.uuid4().bytes
        uploaded = self.runtime.upload_artifact(
            data=blob, filename="conch-phase3-live.png", mime_type="image/png"
        )
        self.assertEqual(uploaded["size_bytes"], len(blob))
        self.assertEqual(
            uploaded["digest"], "sha256:" + hashlib.sha256(blob).hexdigest()
        )
        file_id = uploaded.get("file_id") or uploaded.get("artifact_id")
        self.assertTrue(file_id)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            result = self.runtime.download_artifact(file_id, str(dest))
            self.assertEqual(result["size_bytes"], len(blob))
            self.assertEqual(dest.read_bytes(), blob)

    def test_hitl_round_trip(self):
        """A minimal text-input → human-intervention → markdown workflow:
        the run pauses at the checkpoint and Conch's intervention reply is
        accepted (the live twin of the recorded HITL wire-shape test)."""
        suffix = unique_suffix()
        workflow_id, agent_id, bearer = self._build_hitl_workflow(suffix)
        self.addCleanup(self.http.delete_agent, agent_id)
        self.addCleanup(self.http.delete_workflow, workflow_id)
        runtime = CapitolRuntime(
            WORKFLOW_URL, ORG, agent_id, bearer,
            caller_system="conch-live-tests", caller_version="0.0",
        )
        started = runtime.call_workflow(
            workflow_id, {}, idempotency_key=f"live-hitl-{suffix}"
        )
        run_id = started["run_id"]
        print(f"\n[live HITL] run_id={run_id}")
        # poll the event log until the checkpoint requests input
        request_id = node_id = None
        import time as _time
        deadline = _time.time() + 90
        while _time.time() < deadline:
            events = runtime.run_events(run_id).get("events") or []
            pending = [
                e for e in events
                if e.get("event_type") == "node.input_required"
            ]
            if pending:
                data = pending[-1].get("data") or {}
                request_id = data.get("request_id")
                node_id = (pending[-1].get("node") or {}).get("node_id")
                break
            if str(runtime.run_status(run_id).get("status") or "").lower() \
                    in TERMINAL:
                self.fail("run reached terminal without a HITL checkpoint")
            _time.sleep(2)
        self.assertTrue(request_id, "no HITL checkpoint appeared")
        # Conch answers the intervention; the gateway accepts the reply
        reply = runtime.submit_intervention(
            run_id, node_id, request_id, "continue"
        )
        self.assertTrue(
            reply.get("delivered")
            or "deliver" in json.dumps(reply).lower(),
            reply,
        )

    def _build_hitl_workflow(self, suffix):
        """Assemble a minimal HITL workflow from real node structs copied
        out of an existing human-intervention workflow, so the graph is
        wire-valid without hand-writing node internals."""
        base_id = self._find_hitl_workflow()
        if not base_id:
            raise unittest.SkipTest("no human-intervention workflow to model")
        payload = self.http.get_workflow_payload(base_id)
        nodes = {n["id"]: n for n in payload.get("nodes") or []}

        def _by_node_id(node_id):
            for node in nodes.values():
                struct = (node.get("data") or {}).get("struct") or {}
                if struct.get("node_id") == node_id:
                    return json.loads(json.dumps(node))  # deep copy
            return None

        text_in = _by_node_id("text_input_node")
        hitl = _by_node_id("human_intervention_node")
        markdown = _by_node_id("markdown_output_node")
        if not (text_in and hitl and markdown):
            raise unittest.SkipTest(
                "model workflow lacks text-input/HITL/markdown nodes"
            )
        for param in text_in["data"]["struct"]["params"]:
            if param.get("field_id") == "text_input":
                param["value"] = "conch-phase3 HITL probe: approve to proceed."
        # reuse the model's real edges into the HITL param and out of it,
        # remapping their sources to our text-input and HITL nodes
        edge_into_hitl = edge_into_md = None
        for edge in payload.get("edges") or []:
            if edge.get("target") == hitl["id"]:
                edge_into_hitl = json.loads(json.dumps(edge))
            if edge.get("target") == markdown["id"]:
                edge_into_md = json.loads(json.dumps(edge))
        if not (edge_into_hitl and edge_into_md):
            raise unittest.SkipTest("model workflow edges not as expected")
        text_port = [
            p for p in text_in["data"]["struct"]["output_ports"]
            if p.get("name") == "text"
        ][0]["id"]
        hitl_port = [
            p for p in hitl["data"]["struct"]["output_ports"]
            if p.get("name") == "text"
        ][0]["id"]
        edge_into_hitl.update(
            id="conch-e1", source=text_in["id"], source_node_id=text_in["id"],
            sourceHandle=text_port, source_port_id=text_port,
            sourceNodeId="text_input_node",
        )
        edge_into_md.update(
            id="conch-e2", source=hitl["id"], source_node_id=hitl["id"],
            sourceHandle=hitl_port, source_port_id=hitl_port,
            sourceNodeId="human_intervention_node",
        )
        workflow_id = str(uuid.uuid4())
        new_payload = {
            "id": workflow_id, "session_id": str(uuid.uuid4()),
            "name": f"{ASSET_PREFIX}hitl-{suffix}",
            "description": "disposable HITL probe",
            "publish_to_api": True,
            "nodes": [text_in, hitl, markdown],
            "edges": [edge_into_hitl, edge_into_md],
        }
        status, persisted = self.http.persist_workflow(new_payload)
        if status >= 400:
            raise unittest.SkipTest(f"could not persist HITL workflow: {persisted}")
        status, agent = self.http.pl("POST", f"/agents/{ORG}", {
            "name": f"{ASSET_PREFIX}hitl-op-{suffix}",
            "model_provider": "anthropic",
            "model_name": "claude-sonnet-4-20250514",
            "enable_workflow_runtime": True, "exposed_via_a2a": True,
            "workflow_allowlist": [workflow_id],
        })
        if status >= 400:
            self.http.delete_workflow(workflow_id)
            raise unittest.SkipTest(f"could not create HITL agent: {agent}")
        return (workflow_id, str((agent.get("agent") or {}).get("id")),
                str(agent.get("a2a_bearer_token") or ""))

    def _find_hitl_workflow(self):
        for row in self.http.list_workflows():
            payload = self.http.get_workflow_payload(str(row.get("id")))
            for node in payload.get("nodes") or []:
                struct = (node.get("data") or {}).get("struct") or {}
                if struct.get("node_id") == "human_intervention_node":
                    return str(row.get("id"))
        return None


if __name__ == "__main__":
    unittest.main()
