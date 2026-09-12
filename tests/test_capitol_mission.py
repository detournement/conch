"""Mission↔Capitol supervision gates (Swarm Phase 3, Deliverables 2+5).

Real kernel + real supervisor + the recorded fake A2A gateway:

- a mission's bounded ``capitol_control`` tool starts an allowlisted run
  (ledger + binding + wire idempotency in one flow), authority coming
  from the spec envelope, never the prompt;
- the daemon-tick supervisor advances persisted cursors, wakes missions
  on terminal/failure/HITL, maps HITL checkpoints into origin-bound
  expiring kernel approvals, and relays the decision back as the HITL
  reply exactly once;
- mission abort maps onto ``stop_workflow`` with a ledgered cancel;
- Capitol-unavailable degradation: bindings back off (mission never
  fails), and a returned stack resumes cleanly from the persisted
  cursor — the tested Phase 3 gate;
- binding lifecycles replay identically from the journal (replay==live).
"""

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from conch.capitol.client import CapitolRuntime
from conch.capitol.supervisor import (
    DISCOVERY_INTERVAL_SECONDS,
    CapitolControlClient,
    CapitolSupervisor,
    cancel_idempotency_key,
    hitl_idempotency_key,
    run_start_idempotency_key,
)
from conch.kernel.daemon import EdgeDaemon
from conch.kernel.engine import MissionEngine
from conch.kernel.model import BindingStatus, MissionState
from conch.kernel.store import MissionStore
from conch.policy import register_required_policy, unregister_required_policy

from tests.test_capitol_client import AGENT, BEARER, ORG, FakeGateway, _event


def _hitl_event(sequence, request_id, *, kind="intervention",
                node_id="node-hitl", prompt="Continue this run?"):
    data = {"request_id": request_id, "prompt": prompt}
    if kind == "clarification":
        data["input_kind"] = "clarification"
    return _event(
        sequence, event_type="node.input_required",
        node={"node_id": node_id, "node_type": "human_intervention",
              "display_name": "Checkpoint"},
        data=data,
    )


class SupervisionCase(unittest.TestCase):
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
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = [1_000_000.0]
        self.store = MissionStore(
            Path(self._tmp.name) / "kernel.db", clock=lambda: self.now[0]
        )
        self.addCleanup(self.store.close)
        self.config = {
            "capitol_base_url": f"http://127.0.0.1:{self.port}",
            "capitol_org": ORG,
            "capitol_agent": AGENT,
        }
        self.engine = MissionEngine(
            self.store, self.config, holder="test",
            kernel_dir=Path(self._tmp.name),
        )
        self.supervisor = CapitolSupervisor(
            self.store, self.config,
            runtime_factory=self._runtime,
            clock=lambda: self.now[0],
        )

    def _runtime(self):
        return CapitolRuntime(
            f"http://127.0.0.1:{self.port}", ORG, AGENT, BEARER,
            caller_system="conch-tests", caller_version="0.0",
        )

    def _mission(self, *, dry_run=False, allow_start=True,
                 allow_respond=False, workflows=("draft-wf",),
                 max_runs=2, park=True, bind_scheduled=False):
        spec = {
            "goal": "supervise a capitol run",
            "budgets": {"sessions": 5},
            "dry_run": dry_run,
            "cadence_seconds": 3600,
            "capitol": {
                "workflows": list(workflows),
                "allow_start": allow_start,
                "allow_respond": allow_respond,
                "max_runs": max_runs,
                "bind_scheduled": bind_scheduled,
            },
        }
        mission_id = self.engine.create_mission(spec, activate=True)
        if park:
            self.store.transition_mission(
                mission_id, MissionState.WAITING_TIMER, reason="parked"
            )
        return mission_id

    def _tool(self, mission_id):
        mission = self.store.get_mission(mission_id)
        return CapitolControlClient(
            self.store, mission, "ses-test", self.config,
            runtime_factory=self._runtime,
        )

    def _start_run(self, mission_id):
        result = self._tool(mission_id).call_tool("capitol_control", {
            "op": "start_capitol_run", "workflow_id": "draft-wf",
            "inputs": {"value": {"n": 1}},
        })
        text = result["content"][0]["text"]
        self.assertIn("started", text, text)
        binding = self.store.find_bindings(
            kind="capitol_run", mission_id=mission_id
        )[0]
        return binding

    # -- the bounded mission tool ------------------------------------------

    def test_start_run_binds_ledgers_and_uses_idempotency_key(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        resource = binding["resource"]
        expected_key = run_start_idempotency_key(
            mission_id, "draft-wf", 1
        )
        self.assertEqual(resource["idempotency_key"], expected_key)
        self.assertEqual(resource["org_id"], ORG)
        self.assertEqual(resource["workflow_id"], "draft-wf")
        self.assertTrue(resource["run_id"])
        # the ledger entry committed with the run linkage
        action = self.store.find_action(expected_key)
        self.assertEqual(action["status"], "committed")
        # the same key rode the wire
        skill, data, _env = next(
            call for call in FakeGateway.calls
            if call[0] == "call_workflow"
        )
        self.assertEqual(data["idempotency_key"], expected_key)
        # a second call in the same envelope starts attempt 2 only if
        # max_runs allows; here it does (max_runs=2)
        second = self._tool(mission_id).call_tool("capitol_control", {
            "op": "start_capitol_run", "workflow_id": "draft-wf",
            "inputs": {"value": {"n": 2}},
        })
        self.assertIn("started", second["content"][0]["text"])
        third = self._tool(mission_id).call_tool("capitol_control", {
            "op": "start_capitol_run", "workflow_id": "draft-wf",
        })
        self.assertIn("max_runs", third["content"][0]["text"])

    def test_envelope_authority_is_spec_not_prompt(self):
        for kwargs, marker in (
            (dict(dry_run=True), "dry_run"),
            (dict(allow_start=False), "allow_start"),
            (dict(workflows=("other-wf",)), "allowlist"),
        ):
            mission_id = self._mission(**kwargs)
            result = self._tool(mission_id).call_tool("capitol_control", {
                "op": "start_capitol_run", "workflow_id": "draft-wf",
            })
            text = result["content"][0]["text"]
            self.assertIn("denied", text)
            self.assertIn(marker, text)
        self.assertFalse(
            [c for c in FakeGateway.calls if c[0] == "call_workflow"],
            "denied starts must never reach the wire",
        )

    def test_required_policy_denies_start(self):
        register_required_policy(
            "test-capitol-deny",
            lambda event, payload: event != "capitol.run.start",
        )
        self.addCleanup(unregister_required_policy, "test-capitol-deny")
        mission_id = self._mission()
        result = self._tool(mission_id).call_tool("capitol_control", {
            "op": "start_capitol_run", "workflow_id": "draft-wf",
        })
        self.assertIn("denied by required policy",
                      result["content"][0]["text"])

    # -- supervision to terminal ---------------------------------------------

    def test_supervisor_advances_cursor_and_finishes_run(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [_event(1), _event(2)]
        FakeGateway.runs[run_id]["status"] = "running"
        stats = self.supervisor.tick()
        self.assertEqual(stats["polled"], 1)
        binding = self.store.get_binding(binding["binding_id"])
        self.assertEqual(binding["cursor"], 2)
        self.assertEqual(binding["status"], BindingStatus.ACTIVE)
        # run finishes; the mission wakes and learns of it
        FakeGateway.run_events[run_id].append(_event(3))
        FakeGateway.runs[run_id]["status"] = "success"
        stats = self.supervisor.tick()
        self.assertEqual(stats["terminal"], 1)
        self.assertEqual(stats["woken"], 1)
        binding = self.store.get_binding(binding["binding_id"])
        self.assertEqual(binding["status"], BindingStatus.COMPLETED)
        self.assertEqual(binding["cursor"], 3)
        self.assertEqual(binding["detail"]["final_state"], "success")
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        inbox = [
            row for row in self.store._read_conn().execute(
                "SELECT idempotency_key FROM inbox WHERE mission_id=?",
                (mission_id,),
            ).fetchall()
        ]
        self.assertTrue(
            any("terminal" in row[0] for row in inbox), inbox
        )
        # a second pass is a no-op: terminal bindings are not supervised
        stats = self.supervisor.tick()
        self.assertEqual(stats["polled"], 0)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_run_failure_marks_binding_failed_and_wakes(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [
            _event(1, event_type="workflow.run_failed",
                   data={"error": "node exploded"}),
        ]
        FakeGateway.runs[run_id]["status"] = "failed"
        self.supervisor.tick()
        binding = self.store.get_binding(binding["binding_id"])
        self.assertEqual(binding["status"], BindingStatus.FAILED)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)

    # -- HITL ↔ approvals -------------------------------------------------------

    def test_intervention_maps_to_approval_and_reply_flows_back(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [
            _event(1), _hitl_event(2, "req-1"),
        ]
        FakeGateway.runs[run_id]["status"] = "running"
        stats = self.supervisor.tick()
        self.assertEqual(stats["hitl"], 1)
        binding = self.store.get_binding(binding["binding_id"])
        self.assertEqual(binding["status"], BindingStatus.WAITING_HITL)
        pending = binding["detail"]["pending_hitl"]
        self.assertEqual(pending["request_id"], "req-1")
        approvals = self.store.pending_approvals()
        self.assertEqual(len(approvals), 1)
        approval = approvals[0]
        self.assertEqual(approval["action_kind"],
                         "capitol.hitl.intervention")
        self.assertEqual(approval["mission_id"], mission_id)
        # the notification rode the outbox
        outbox = self.store.list_outbox()
        self.assertTrue(
            any("waiting on a intervention" in item["payload"]
                or "waiting on" in item["payload"] for item in outbox)
        )
        # approve → the reply is the literal continue token
        self.store.decide_approval(
            approval["approval_id"], "approve",
            nonce=approval["nonce"], origin_channel="local",
            decided_by="test",
        )
        self.supervisor.tick()
        sent = [
            (skill, data) for skill, data, _env in FakeGateway.calls
            if skill == "submit_intervention_response"
        ]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], {
            "run_id": run_id, "node_id": "node-hitl",
            "request_id": "req-1", "response": "continue",
        })
        binding = self.store.get_binding(binding["binding_id"])
        self.assertEqual(binding["status"], BindingStatus.ACTIVE)
        self.assertNotIn("pending_hitl", binding["detail"])
        action = self.store.find_action(
            hitl_idempotency_key(run_id, "req-1")
        )
        self.assertEqual(action["status"], "committed")
        # replaying the whole story reproduces the projections
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_denied_intervention_sends_stop(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [_hitl_event(1, "req-2")]
        FakeGateway.runs[run_id]["status"] = "running"
        self.supervisor.tick()
        approval = self.store.pending_approvals()[0]
        self.store.decide_approval(
            approval["approval_id"], "deny",
            nonce=approval["nonce"], origin_channel="local",
        )
        self.supervisor.tick()
        sent = [
            data for skill, data, _env in FakeGateway.calls
            if skill == "submit_intervention_response"
        ]
        self.assertEqual(sent[-1]["response"], "stop")
        del mission_id

    def test_mission_tool_answers_clarification_once(self):
        mission_id = self._mission(allow_respond=True)
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [
            _hitl_event(1, "req-3", kind="clarification",
                        prompt="Which color variant?"),
        ]
        FakeGateway.runs[run_id]["status"] = "running"
        self.supervisor.tick()
        # the envelope allows answering clarifications from the mission
        result = self._tool(mission_id).call_tool("capitol_control", {
            "op": "respond_hitl", "run_id": run_id,
            "response": "Blue, per the mission notes.",
        })
        self.assertIn("clarification answered",
                      result["content"][0]["text"])
        sent = [
            data for skill, data, _env in FakeGateway.calls
            if skill == "submit_clarification_response"
        ]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["response"], "Blue, per the mission notes.")
        # a later operator approval must NOT double-send: the ledger key
        # is shared between the tool and the supervisor
        approval = self.store.pending_approvals()[0]
        self.store.decide_approval(
            approval["approval_id"], "approve",
            nonce=approval["nonce"], origin_channel="local",
        )
        self.supervisor.tick()
        sent = [
            data for skill, data, _env in FakeGateway.calls
            if skill == "submit_clarification_response"
        ]
        self.assertEqual(len(sent), 1, "the reply must be delivered once")

    def test_intervention_refused_to_mission_tool(self):
        mission_id = self._mission(allow_respond=True)
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [_hitl_event(1, "req-4")]
        FakeGateway.runs[run_id]["status"] = "running"
        self.supervisor.tick()
        result = self._tool(mission_id).call_tool("capitol_control", {
            "op": "respond_hitl", "run_id": run_id,
            "response": "continue",
        })
        text = result["content"][0]["text"]
        self.assertIn("denied", text)
        self.assertIn("approval", text)

    # -- abort → cancel -----------------------------------------------------------

    def test_mission_abort_stops_bound_run(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.runs[run_id]["status"] = "running"
        self.engine.abort_mission(mission_id)
        stats = self.supervisor.tick()
        self.assertEqual(stats["cancelled"], 1)
        stopped = [
            data for skill, data, _env in FakeGateway.calls
            if skill == "stop_workflow"
        ]
        self.assertEqual(stopped[0]["run_id"], run_id)
        binding = self.store.get_binding(binding["binding_id"])
        self.assertEqual(binding["status"], BindingStatus.CANCELLED)
        action = self.store.find_action(cancel_idempotency_key(run_id))
        self.assertEqual(action["status"], "committed")
        # replay still matches with the cancel in the journal
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    # -- Capitol-unavailable degradation (the tested gate) ---------------------

    def test_degrades_with_backoff_and_resumes_from_cursor(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        run_id = binding["resource"]["run_id"]
        FakeGateway.run_events[run_id] = [_event(1), _event(2)]
        FakeGateway.runs[run_id]["status"] = "running"
        self.supervisor.tick()
        self.assertEqual(
            self.store.get_binding(binding["binding_id"])["cursor"], 2
        )
        # Capitol goes away: an unreachable-port runtime replaces the fake
        dead = CapitolSupervisor(
            self.store, self.config,
            runtime_factory=lambda: CapitolRuntime(
                "http://127.0.0.1:1", ORG, AGENT, BEARER,
            ),
            clock=lambda: self.now[0],
        )
        stats = dead.tick()
        self.assertEqual(stats["degraded"], 1)
        degraded = self.store.get_binding(binding["binding_id"])
        self.assertEqual(degraded["status"], BindingStatus.DEGRADED)
        self.assertEqual(degraded["cursor"], 2, "cursor survives outages")
        first_backoff = degraded["detail"]["backoff_seconds"]
        self.assertGreater(first_backoff, 0)
        # the mission is parked, never failed — no cloud fallback exists
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        # within the backoff window nothing polls
        stats = dead.tick()
        self.assertEqual(stats["degraded"], 0)
        self.assertEqual(stats["polled"], 0)
        # past the backoff the outage deepens: exponential growth
        self.now[0] += first_backoff + 1
        dead.tick()
        deeper = self.store.get_binding(binding["binding_id"])
        self.assertGreater(
            deeper["detail"]["backoff_seconds"], first_backoff
        )
        # exactly one outage note reached the mission inbox
        outage_notes = [
            row[0] for row in self.store._read_conn().execute(
                "SELECT idempotency_key FROM inbox WHERE mission_id=?"
                " AND idempotency_key LIKE '%degraded%'",
                (mission_id,),
            ).fetchall()
        ]
        self.assertEqual(len(outage_notes), 1)
        # the stack returns: events continue past the persisted cursor
        FakeGateway.run_events[run_id].append(_event(3))
        self.now[0] += deeper["detail"]["backoff_seconds"] + 1
        stats = self.supervisor.tick()
        self.assertEqual(stats["polled"], 1)
        recovered = self.store.get_binding(binding["binding_id"])
        self.assertEqual(recovered["status"], BindingStatus.ACTIVE)
        self.assertEqual(recovered["cursor"], 3)
        self.assertNotIn("backoff_seconds", recovered["detail"])
        self.assertNotIn("outage_started", recovered["detail"])
        # the recovery note is on the record; supervision simply resumed
        recovery_notes = [
            row[0] for row in self.store._read_conn().execute(
                "SELECT idempotency_key FROM inbox WHERE mission_id=?"
                " AND idempotency_key LIKE '%recovered%'",
                (mission_id,),
            ).fetchall()
        ]
        self.assertEqual(len(recovery_notes), 1)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_auth_failure_degrades_without_retry_storm(self):
        mission_id = self._mission()
        binding = self._start_run(mission_id)
        del mission_id
        bad = CapitolSupervisor(
            self.store, self.config,
            runtime_factory=lambda: CapitolRuntime(
                f"http://127.0.0.1:{self.port}", ORG, AGENT,
                "cap_a2a_WRONG",
            ),
            clock=lambda: self.now[0],
        )
        stats = bad.tick()
        self.assertEqual(stats["degraded"], 1)
        degraded = self.store.get_binding(binding["binding_id"])
        self.assertEqual(degraded["status"], BindingStatus.DEGRADED)
        self.assertTrue(degraded["detail"]["auth_needed"])

    # -- scheduled-run discovery (capitol.bind_scheduled) --------------------

    def _seed_run(self, run_id, status="running"):
        FakeGateway.runs[run_id] = {"status": status, "output": {}}
        FakeGateway.run_events[run_id] = []

    def test_bind_scheduled_discovers_dedupes_and_caps(self):
        mission_id = self._mission(
            allow_start=False, bind_scheduled=True, max_runs=2,
        )
        self._seed_run("sched-a")
        self._seed_run("sched-b")
        self._seed_run("sched-c")
        stats = self.supervisor.tick()
        self.assertEqual(stats["discovered"], 2, "max_runs caps discovery")
        bindings = self.store.find_bindings(
            kind="capitol_run", mission_id=mission_id
        )
        self.assertEqual(len(bindings), 2)
        for binding in bindings:
            self.assertEqual(binding["resource"]["source"], "scheduled")
            self.assertEqual(
                binding["resource"]["workflow_id"], "draft-wf"
            )
        # within the discovery interval nothing re-lists
        stats = self.supervisor.tick()
        self.assertEqual(stats["discovered"], 0)
        # past the interval: still capped, and never a duplicate binding
        self.now[0] += DISCOVERY_INTERVAL_SECONDS + 1
        stats = self.supervisor.tick()
        self.assertEqual(stats["discovered"], 0)
        bound_ids = sorted(
            binding["resource"]["run_id"]
            for binding in self.store.find_bindings(
                kind="capitol_run", mission_id=mission_id
            )
        )
        self.assertEqual(len(bound_ids), 2)
        self.assertEqual(len(set(bound_ids)), 2)
        # a supervised run reaching terminal frees discovery budget
        finished = bound_ids[0]
        FakeGateway.runs[finished]["status"] = "success"
        self.now[0] += DISCOVERY_INTERVAL_SECONDS + 1
        self.supervisor.tick()   # completes the finished binding
        self.now[0] += DISCOVERY_INTERVAL_SECONDS + 1
        stats = self.supervisor.tick()
        self.assertEqual(stats["discovered"], 1)
        all_ids = {
            binding["resource"]["run_id"]
            for binding in self.store.find_bindings(
                kind="capitol_run", mission_id=mission_id
            )
        }
        self.assertEqual(all_ids, {"sched-a", "sched-b", "sched-c"})
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_discovery_requires_opt_in(self):
        self._mission(allow_start=False, bind_scheduled=False)
        self._seed_run("sched-x")
        stats = self.supervisor.tick()
        self.assertEqual(stats["discovered"], 0)
        self.assertEqual(
            self.store.find_bindings(kind="capitol_run"), []
        )

    def test_discovery_respects_required_policy(self):
        register_required_policy(
            "test-bind-deny",
            lambda event, payload: event != "capitol.run.bind",
        )
        self.addCleanup(unregister_required_policy, "test-bind-deny")
        mission_id = self._mission(
            allow_start=False, bind_scheduled=True,
        )
        self._seed_run("sched-veto")
        stats = self.supervisor.tick()
        self.assertEqual(stats["discovered"], 0)
        self.assertEqual(
            self.store.find_bindings(
                kind="capitol_run", mission_id=mission_id
            ),
            [],
        )


class DaemonWiringTests(unittest.TestCase):
    """The conch-edge daemon runs the supervisor on its own cadence."""

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

    def test_daemon_tick_supervises_capitol_bindings(self):
        import os
        from unittest.mock import patch

        FakeGateway.reset(self.port)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "capitol_base_url": f"http://127.0.0.1:{self.port}",
                "capitol_org": ORG,
                "capitol_agent": AGENT,
                "capitol_poll_seconds": 1,
            }
            daemon = EdgeDaemon(
                config, kernel_dir=root / "kernel", state_dir=root,
                socket_path=root / "run" / "edge.sock",
                session_factory=lambda *a: ("ok", {}),
            )
            daemon.start()
            try:
                mission_id = daemon.engine.create_mission({
                    "goal": "daemon capitol wiring",
                    "budgets": {},
                    "dry_run": False,
                    "capitol": {"workflows": ["draft-wf"],
                                "allow_start": True},
                }, activate=True)
                daemon.store.transition_mission(
                    mission_id, "waiting_timer", reason="parked"
                )
                mission = daemon.store.get_mission(mission_id)
                tool = CapitolControlClient(
                    daemon.store, mission, "ses-d", config,
                    runtime_factory=lambda: CapitolRuntime(
                        f"http://127.0.0.1:{self.port}", ORG, AGENT,
                        BEARER,
                    ),
                )
                tool.call_tool("capitol_control", {
                    "op": "start_capitol_run", "workflow_id": "draft-wf",
                    "inputs": {"value": {"n": 1}},
                })
                binding = daemon.store.find_bindings(
                    kind="capitol_run", mission_id=mission_id
                )[0]
                run_id = binding["resource"]["run_id"]
                FakeGateway.runs[run_id]["status"] = "success"
                with patch.dict(
                    os.environ, {"CAPITOL_A2A_BEARER": BEARER}
                ):
                    stats = daemon.tick()
                self.assertEqual(stats.get("capitol_terminal"), 1)
                binding = daemon.store.get_binding(binding["binding_id"])
                self.assertEqual(binding["status"],
                                 BindingStatus.COMPLETED)
            finally:
                daemon.shutdown()

    def test_mission_binds_supervises_approves_and_completes(self):
        """The end-to-end Phase 3 mission gate in one scenario: a mission
        binds a Capitol run, the daemon supervises it, a HITL checkpoint
        round-trips through a kernel approval, the run then completes, and
        replaying the binding journal reproduces the live projections."""
        import os
        from unittest.mock import patch

        FakeGateway.reset(self.port)
        now = [2_000_000.0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "capitol_base_url": f"http://127.0.0.1:{self.port}",
                "capitol_org": ORG,
                "capitol_agent": AGENT,
                "capitol_poll_seconds": 1,
            }
            daemon = EdgeDaemon(
                config, kernel_dir=root / "kernel", state_dir=root,
                socket_path=root / "run" / "edge.sock",
                clock=lambda: now[0],
                session_factory=lambda *a: ("ok", {}),
            )
            daemon.start()

            def tick():
                # force a Capitol pass every tick regardless of cadence
                daemon._capitol_last_poll = 0.0
                with patch.dict(os.environ, {"CAPITOL_A2A_BEARER": BEARER}):
                    return daemon.tick()

            try:
                mission_id = daemon.engine.create_mission({
                    "goal": "bind, supervise, approve, complete",
                    "budgets": {}, "dry_run": False,
                    "cadence_seconds": 3600,
                    "capitol": {"workflows": ["draft-wf"],
                                "allow_start": True},
                }, activate=True)
                daemon.store.transition_mission(
                    mission_id, "waiting_timer", reason="parked"
                )
                mission = daemon.store.get_mission(mission_id)
                tool = CapitolControlClient(
                    daemon.store, mission, "ses-int", config,
                    runtime_factory=lambda: CapitolRuntime(
                        f"http://127.0.0.1:{self.port}", ORG, AGENT, BEARER,
                    ),
                )
                # 1) the mission binds a run
                tool.call_tool("capitol_control", {
                    "op": "start_capitol_run", "workflow_id": "draft-wf",
                    "inputs": {"value": {"n": 1}},
                })
                binding = daemon.store.find_bindings(
                    kind="capitol_run", mission_id=mission_id
                )[0]
                run_id = binding["resource"]["run_id"]

                # 2) the daemon supervises to a HITL checkpoint
                FakeGateway.run_events[run_id] = [
                    _event(1), _hitl_event(2, "req-int"),
                ]
                FakeGateway.runs[run_id]["status"] = "running"
                now[0] += 2
                stats = tick()
                self.assertEqual(stats.get("capitol_hitl"), 1)
                binding = daemon.store.get_binding(binding["binding_id"])
                self.assertEqual(binding["status"],
                                 BindingStatus.WAITING_HITL)

                # 3) the checkpoint is a kernel approval that round-trips
                approvals = daemon.store.pending_approvals()
                self.assertEqual(len(approvals), 1)
                approval = approvals[0]
                self.assertEqual(approval["mission_id"], mission_id)
                self.assertEqual(approval["action_kind"],
                                 "capitol.hitl.intervention")
                daemon.store.decide_approval(
                    approval["approval_id"], "approve",
                    nonce=approval["nonce"], origin_channel="local",
                    decided_by="test",
                )
                now[0] += 2
                tick()  # relays the approved reply to Capitol, once
                sent = [
                    data for skill, data, _e in FakeGateway.calls
                    if skill == "submit_intervention_response"
                ]
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]["response"], "continue")
                binding = daemon.store.get_binding(binding["binding_id"])
                self.assertEqual(binding["status"], BindingStatus.ACTIVE)

                # 4) the run completes; the daemon drives it to terminal
                FakeGateway.run_events[run_id].append(
                    _event(3, event_type="workflow.run_completed")
                )
                FakeGateway.runs[run_id]["status"] = "success"
                now[0] += 2
                stats = tick()
                self.assertEqual(stats.get("capitol_terminal"), 1)
                binding = daemon.store.get_binding(binding["binding_id"])
                self.assertEqual(binding["status"], BindingStatus.COMPLETED)
                self.assertEqual(binding["detail"]["final_state"], "success")
                self.assertEqual(
                    daemon.store.get_mission(mission_id)["status"],
                    MissionState.READY,
                )

                # 5) binding-event replay equals live state
                ok, detail = daemon.store.replay_matches_live()
                self.assertTrue(ok, detail)
            finally:
                daemon.shutdown()


if __name__ == "__main__":
    unittest.main()
