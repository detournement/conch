"""Fleet authority gates: owner grants, ceilings, and the envelope clamp.

The rule under test everywhere: effective authority is
``requested ∩ worker ceiling ∩ caller authority`` — clamped when the
request left a dimension open, refused with the exact excess when it named
something outside the intersection, and NEVER widened. Grants are ledgered
worker_updated events, so replay == live is asserted too.
"""

import tempfile
import unittest
from pathlib import Path

from conch.fleet import authority
from conch.fleet.registry import FleetRegistry
from conch.kernel.store import MissionStore
from conch.swarm.protocol import ActionClass, DataClassification


def plain_worker(**overrides):
    worker = {"labels": {}, "data_ceiling": "internal"}
    worker.update(overrides)
    return worker


class TestCeilingDefaults(unittest.TestCase):
    def test_default_ceiling_is_narrow(self):
        ceiling = authority.worker_ceiling(plain_worker())
        self.assertEqual(
            ceiling["tools"], authority.DEFAULT_WORKER_TOOLS
        )
        self.assertEqual(
            ceiling["actions"], authority.DEFAULT_WORKER_ACTIONS
        )
        self.assertEqual(ceiling["data"], "internal")

    def test_hard_exclusions_survive_full_grant(self):
        worker = plain_worker(
            labels={"grants": {"tools": "full",
                               "actions": sorted(ActionClass.ALL)}}
        )
        ceiling = authority.worker_ceiling(worker)
        self.assertFalse(ceiling["tools"] & authority.HARD_EXCLUDED_TOOLS)
        # A crafted grant block naming an excluded tool is neutralized too.
        worker = plain_worker(
            labels={"grants": {"tools": ["ssh_remote", "conch_config",
                                         "local_shell"]}}
        )
        ceiling = authority.worker_ceiling(worker)
        self.assertNotIn("ssh_remote", ceiling["tools"])
        self.assertNotIn("conch_config", ceiling["tools"])
        self.assertIn("local_shell", ceiling["tools"])


class TestClamp(unittest.TestCase):
    def test_default_request_clamps_to_read_and_narrow_tools(self):
        clamped = authority.clamp_envelope(plain_worker())
        self.assertEqual(clamped["actions"], (ActionClass.READ,))
        self.assertEqual(
            set(clamped["tools"]), set(authority.DEFAULT_WORKER_TOOLS)
        )
        self.assertEqual(clamped["data"], "internal")

    def test_requested_tool_beyond_ceiling_is_refused(self):
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.clamp_envelope(
                plain_worker(), tools=["local_shell", "save_memory"]
            )
        self.assertIn("save_memory", str(ctx.exception))

    def test_requested_action_beyond_ceiling_is_refused(self):
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.clamp_envelope(
                plain_worker(), actions=["communicate"]
            )
        self.assertIn("communicate", str(ctx.exception))

    def test_grant_raises_the_ceiling(self):
        worker = plain_worker(labels={"grants": {
            "actions": ["communicate"], "tools": ["save_memory"],
        }})
        clamped = authority.clamp_envelope(
            worker, tools=["save_memory"], actions=["communicate"],
        )
        self.assertIn("save_memory", clamped["tools"])
        self.assertIn("communicate", clamped["actions"])
        self.assertIn(ActionClass.READ, clamped["actions"])

    def test_hard_excluded_tool_refused_even_with_full_grant(self):
        worker = plain_worker(labels={"grants": {"tools": "full"}})
        with self.assertRaises(authority.AuthorityError):
            authority.clamp_envelope(worker, tools=["ssh_remote"])

    def test_caller_authority_intersects(self):
        # Worker granted communicate, but the caller holds only defaults:
        # the request is refused (child ⊆ caller ∩ ceiling).
        worker = plain_worker(labels={"grants": {
            "actions": ["communicate"],
        }})
        caller = dict(authority.DEFAULT_DELEGATE_AUTHORITY)
        with self.assertRaises(authority.AuthorityError):
            authority.clamp_envelope(
                worker, actions=["communicate"], caller=caller
            )
        # Caller tool bound applies too.
        caller = {"tools": ["public_api"], "actions": [ActionClass.READ],
                  "data": "internal"}
        with self.assertRaises(authority.AuthorityError):
            authority.clamp_envelope(
                worker, tools=["local_shell"], caller=caller
            )
        clamped = authority.clamp_envelope(
            worker, tools=["public_api"], caller=caller
        )
        self.assertEqual(clamped["tools"], ("public_api",))

    def test_data_classification_takes_the_minimum(self):
        worker = plain_worker(data_ceiling="confidential")
        clamped = authority.clamp_envelope(
            worker, data="restricted",
            caller={"tools": None, "actions": ActionClass.ALL,
                    "data": "public"},
        )
        self.assertEqual(clamped["data"], DataClassification.PUBLIC)
        clamped = authority.clamp_envelope(worker, data="restricted")
        self.assertEqual(clamped["data"], "confidential")

    def test_token_budget_min_of_request_and_caller(self):
        clamped = authority.clamp_envelope(
            plain_worker(), token_budget=500000,
            caller={"tools": None, "actions": ActionClass.ALL,
                    "data": "internal", "token_budget": 100000},
        )
        self.assertEqual(clamped["token_budget"], 100000)


class TestGrantValidation(unittest.TestCase):
    def test_caller_without_class_cannot_grant_it(self):
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.validate_grant(
                ["write_local", "communicate"], None,
                caller_actions=["read", "write_local"],
            )
        self.assertIn("communicate", str(ctx.exception))

    def test_owner_caller_can_grant(self):
        grant = authority.validate_grant(
            ["communicate"], ["save_memory"], "confidential",
        )
        self.assertEqual(grant["actions"], ["communicate"])
        self.assertEqual(grant["tools"], ["save_memory"])
        self.assertEqual(grant["data"], "confidential")

    def test_hard_excluded_tools_never_grantable(self):
        for tool in sorted(authority.HARD_EXCLUDED_TOOLS):
            with self.subTest(tool=tool):
                with self.assertRaises(authority.AuthorityError):
                    authority.validate_grant(None, [tool])

    def test_unknown_shapes_fail_closed(self):
        with self.assertRaises(authority.AuthorityError):
            authority.validate_grant(["fly"], None)
        with self.assertRaises(authority.AuthorityError):
            authority.validate_grant(None, ["made_up_tool"])
        with self.assertRaises(authority.AuthorityError):
            authority.validate_grant(None, None, "topsecret")
        with self.assertRaises(authority.AuthorityError):
            authority.validate_grant(None, None, "")

    def test_caller_data_bound_applies_to_grants(self):
        with self.assertRaises(authority.AuthorityError):
            authority.validate_grant(
                None, None, "restricted", caller_data="internal"
            )


class TestGrantLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MissionStore(Path(self._tmp.name) / "kernel.db")
        self.addCleanup(self.store.close)
        self.registry = FleetRegistry(self.store)
        self.worker_id = self.registry.enroll(
            "w1", host="10.0.0.1", trust_level=1,
        )

    def test_grants_are_ledgered_and_replay_matches_live(self):
        grant = authority.validate_grant(
            ["communicate"], ["save_memory"], "confidential",
        )
        ceiling = authority.apply_grant(
            self.registry, self.worker_id, grant, granted_by="thom",
        )
        self.assertIn("communicate", ceiling["actions"])
        self.assertIn("save_memory", ceiling["tools"])
        self.assertEqual(ceiling["data"], "confidential")
        worker = self.registry.require(self.worker_id)
        self.assertEqual(
            worker["labels"]["grants"]["granted_by"], "thom"
        )
        # The grant rode a worker_updated kernel event on the "" chain.
        events = self.store.event_tail("", limit=50)
        kinds = [event["kind"] for event in events]
        self.assertIn("worker_updated", kinds)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_revoke_returns_to_defaults_and_replays(self):
        authority.apply_grant(
            self.registry, self.worker_id,
            authority.validate_grant(None, "full"),
        )
        ceiling = authority.revoke_grants(self.registry, self.worker_id)
        self.assertEqual(
            ceiling["tools"], authority.DEFAULT_WORKER_TOOLS
        )
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_scheduler_respects_the_ceiling(self):
        """An envelope naming an ungranted tool never places (eligible
        is ceiling-aware)."""
        from conch.swarm.protocol import TaskEnvelope, new_id

        self.registry.activate(self.worker_id)
        worker = self.registry.require(self.worker_id)
        mission_id = self.store.create_mission({"goal": "g", "budgets": {}})
        envelope = TaskEnvelope(
            task_id=new_id("task"), mission_id=mission_id,
            principal="user", task="t", idempotency_key=new_id("task"),
            issued_at=1.0, tools=("save_memory",),
        )
        self.assertFalse(self.registry.eligible(worker, envelope))
        authority.apply_grant(
            self.registry, self.worker_id,
            authority.validate_grant(None, ["save_memory"]),
        )
        worker = self.registry.require(self.worker_id)
        self.assertTrue(self.registry.eligible(worker, envelope))


if __name__ == "__main__":
    unittest.main()
