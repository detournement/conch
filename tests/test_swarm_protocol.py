"""Swarm protocol foundations (Swarm Phase 0).

Gates proven here: canonical round-trips for every shape, byte-stable
canonical JSON/digests, and fail-closed rejection of unknown fields,
unknown/newer versions, wrong types, bad enum values, malformed IDs, and
inconsistent cross-field combinations.
"""

import json
import time
import unittest

from conch.swarm.protocol import (
    EVENT_KINDS,
    MAX_WIRE_BYTES,
    PROTOCOL_VERSION,
    ActionClass,
    DataClassification,
    FailureClass,
    Lease,
    ProtocolError,
    TaskEnvelope,
    TaskEvent,
    TaskReceipt,
    canonical_json,
    classification_rank,
    new_id,
    parse_id,
)


def _envelope(**overrides):
    values = dict(
        task_id=new_id("task"),
        mission_id=new_id("msn"),
        principal="user:thom",
        task="summarize the repository",
        idempotency_key="idem-1",
        issued_at=time.time(),
        tools=("local_shell",),
        skills=("digest",),
        action_classes=(ActionClass.READ, ActionClass.COMPUTE),
        data_classification=DataClassification.INTERNAL,
    )
    values.update(overrides)
    return TaskEnvelope(**values)


def _event(**overrides):
    values = dict(
        event_id=new_id("evt"),
        task_id=new_id("task"),
        attempt=1,
        sequence=0,
        kind="progress",
        created_at=time.time(),
        payload={"note": "halfway"},
    )
    values.update(overrides)
    return TaskEvent(**values)


def _receipt(**overrides):
    values = dict(
        receipt_id=new_id("rcpt"),
        task_id=new_id("task"),
        attempt=1,
        action_class=ActionClass.READ,
        outcome="success",
        created_at=time.time(),
        details={"url": "https://example.com"},
    )
    values.update(overrides)
    return TaskReceipt(**values)


def _lease(**overrides):
    now = time.time()
    values = dict(
        lease_id=new_id("lease"),
        task_id=new_id("task"),
        worker_id=new_id("wrk"),
        controller_epoch=3,
        fencing_token=17,
        granted_at=now,
        expires_at=now + 60,
    )
    values.update(overrides)
    return Lease(**values)


ALL_BUILDERS = {
    TaskEnvelope: _envelope,
    TaskEvent: _event,
    TaskReceipt: _receipt,
    Lease: _lease,
}


class TestCanonicalIds(unittest.TestCase):
    def test_new_id_shape_and_kind(self):
        for kind in ("msn", "task", "evt", "rcpt", "lease", "wrk"):
            value = new_id(kind)
            self.assertEqual(parse_id(value), kind)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ProtocolError):
            new_id("banana")

    def test_malformed_ids_rejected(self):
        for bad in ("", "task-", "task-xyz", "task-123", "evil-abc",
                    "task-0123456789abc-shortrand", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ProtocolError):
                    parse_id(bad)

    def test_ids_sort_by_creation_within_kind(self):
        first = new_id("task")
        time.sleep(0.002)
        second = new_id("task")
        self.assertLess(first, second)


class TestCanonicalJson(unittest.TestCase):
    def test_sorted_compact_ascii(self):
        text = canonical_json({"b": 1, "a": {"z": True, "y": "é"}})
        self.assertEqual(text, '{"a":{"y":"\\u00e9","z":true},"b":1}')

    def test_equal_values_serialize_identically(self):
        task_id = new_id("task")
        mission_id = new_id("msn")
        first = _envelope(task_id=task_id, mission_id=mission_id,
                          issued_at=1000.0)
        second = _envelope(task_id=task_id, mission_id=mission_id,
                           issued_at=1000.0)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.digest(), second.digest())

    def test_digest_changes_with_content(self):
        base = _envelope(issued_at=1000.0)
        other = TaskEnvelope.from_dict(
            {**base.to_dict(), "task": "a different instruction"}
        )
        self.assertNotEqual(base.digest(), other.digest())

    def test_non_serializable_rejected(self):
        with self.assertRaises(ProtocolError):
            canonical_json({"x": {1, 2}})
        with self.assertRaises(ProtocolError):
            canonical_json({"x": float("nan")})


class TestRoundTrips(unittest.TestCase):
    def test_all_shapes_round_trip_via_json(self):
        for cls, builder in ALL_BUILDERS.items():
            with self.subTest(shape=cls.__name__):
                original = builder()
                restored = cls.from_json(original.to_json())
                self.assertEqual(original, restored)
                self.assertEqual(original.digest(), restored.digest())

    def test_round_trip_from_bytes(self):
        original = _event()
        restored = TaskEvent.from_json(original.to_json().encode("ascii"))
        self.assertEqual(original, restored)

    def test_wire_carries_versions(self):
        for cls, builder in ALL_BUILDERS.items():
            with self.subTest(shape=cls.__name__):
                data = json.loads(builder().to_json())
                self.assertEqual(data["schema_version"], cls.SCHEMA_VERSION)
                self.assertEqual(data["protocol_version"], PROTOCOL_VERSION)


class TestFailClosed(unittest.TestCase):
    def test_unknown_field_rejected_everywhere(self):
        for cls, builder in ALL_BUILDERS.items():
            with self.subTest(shape=cls.__name__):
                data = builder().to_dict()
                data["surprise_field"] = "future semantics"
                with self.assertRaises(ProtocolError) as ctx:
                    cls.from_dict(data)
                self.assertIn("unknown field", str(ctx.exception))

    def test_missing_field_rejected_everywhere(self):
        for cls, builder in ALL_BUILDERS.items():
            with self.subTest(shape=cls.__name__):
                data = builder().to_dict()
                data.pop("task_id")
                with self.assertRaises(ProtocolError):
                    cls.from_dict(data)

    def test_newer_schema_version_rejected(self):
        for cls, builder in ALL_BUILDERS.items():
            with self.subTest(shape=cls.__name__):
                data = builder().to_dict()
                data["schema_version"] = cls.SCHEMA_VERSION + 1
                with self.assertRaises(ProtocolError) as ctx:
                    cls.from_dict(data)
                self.assertIn("schema_version", str(ctx.exception))

    def test_newer_protocol_version_rejected(self):
        data = _lease().to_dict()
        data["protocol_version"] = PROTOCOL_VERSION + 1
        with self.assertRaises(ProtocolError) as ctx:
            Lease.from_dict(data)
        self.assertIn("protocol_version", str(ctx.exception))

    def test_non_integer_versions_rejected(self):
        for bad in ("1", 1.0, True, None):
            with self.subTest(bad=bad):
                data = _event().to_dict()
                data["schema_version"] = bad
                with self.assertRaises(ProtocolError):
                    TaskEvent.from_dict(data)

    def test_wrong_types_rejected(self):
        cases = [
            (_envelope, "task", 42),
            (_envelope, "tools", "local_shell"),
            (_envelope, "max_tool_rounds", "10"),
            (_envelope, "max_tool_rounds", True),
            (_event, "payload", ["not", "a", "dict"]),
            (_event, "attempt", 1.5),
            (_receipt, "details", None),
            (_lease, "granted_at", "now"),
        ]
        for builder, field, bad in cases:
            with self.subTest(field=field, bad=bad):
                data = builder().to_dict()
                data[field] = bad
                cls = type(builder())
                with self.assertRaises(ProtocolError):
                    cls.from_dict(data)

    def test_non_dict_and_invalid_json_rejected(self):
        with self.assertRaises(ProtocolError):
            TaskEnvelope.from_json("[1,2,3]")
        with self.assertRaises(ProtocolError):
            TaskEnvelope.from_json("{not json")
        with self.assertRaises(ProtocolError):
            TaskEnvelope.from_json(None)

    def test_oversized_wire_rejected(self):
        huge = '{"x":"' + "a" * MAX_WIRE_BYTES + '"}'
        with self.assertRaises(ProtocolError):
            TaskEvent.from_json(huge)

    def test_oversized_payload_rejected_at_construction(self):
        with self.assertRaises(ProtocolError):
            _event(payload={"blob": "a" * MAX_WIRE_BYTES})


class TestTaxonomies(unittest.TestCase):
    def test_failure_classes_exact(self):
        self.assertEqual(
            FailureClass.ALL,
            {
                "transient", "resource", "policy", "auth", "user_input",
                "bug", "unknown_external_outcome",
            },
        )

    def test_action_classes_cover_plan_taxonomy(self):
        for name in ("read", "communicate", "publish", "purchase",
                     "account_change", "delete", "provision"):
            self.assertIn(name, ActionClass.ALL)

    def test_data_classification_ordering(self):
        self.assertLess(
            classification_rank(DataClassification.PUBLIC),
            classification_rank(DataClassification.RESTRICTED),
        )
        with self.assertRaises(ProtocolError):
            classification_rank("top-secret")

    def test_event_kinds_include_lifecycle(self):
        for kind in ("started", "result", "failed", "cancelled"):
            self.assertIn(kind, EVENT_KINDS)


class TestShapeRules(unittest.TestCase):
    def test_envelope_rejects_unknown_action_class(self):
        with self.assertRaises(ProtocolError):
            _envelope(action_classes=("read", "launch_missiles"))

    def test_envelope_rejects_unknown_classification(self):
        with self.assertRaises(ProtocolError):
            _envelope(data_classification="ultra")

    def test_envelope_requires_positive_budgets(self):
        with self.assertRaises(ProtocolError):
            _envelope(max_tool_rounds=0)
        with self.assertRaises(ProtocolError):
            _envelope(token_budget=-1)
        with self.assertRaises(ProtocolError):
            _envelope(wall_clock_seconds=0)

    def test_envelope_requires_principal_task_idempotency(self):
        with self.assertRaises(ProtocolError):
            _envelope(principal="  ")
        with self.assertRaises(ProtocolError):
            _envelope(task="")
        with self.assertRaises(ProtocolError):
            _envelope(idempotency_key="")

    def test_envelope_id_kinds_enforced(self):
        with self.assertRaises(ProtocolError):
            _envelope(task_id=new_id("msn"))
        with self.assertRaises(ProtocolError):
            _envelope(mission_id=new_id("task"))
        # parent_task_id may be empty but never malformed
        _envelope(parent_task_id="")
        with self.assertRaises(ProtocolError):
            _envelope(parent_task_id="task-nonsense")

    def test_failed_event_requires_failure_class(self):
        with self.assertRaises(ProtocolError):
            _event(kind="failed")
        event = _event(kind="failed", failure_class=FailureClass.TRANSIENT)
        self.assertEqual(event.failure_class, "transient")

    def test_non_failed_event_rejects_failure_class(self):
        with self.assertRaises(ProtocolError):
            _event(kind="progress", failure_class=FailureClass.BUG)

    def test_unknown_event_kind_rejected(self):
        with self.assertRaises(ProtocolError):
            _event(kind="vibes")

    def test_receipt_outcome_failure_class_consistency(self):
        with self.assertRaises(ProtocolError):
            _receipt(outcome="success", failure_class=FailureClass.BUG)
        with self.assertRaises(ProtocolError):
            _receipt(outcome="failure")
        with self.assertRaises(ProtocolError):
            _receipt(outcome="unknown", failure_class=FailureClass.TRANSIENT)
        receipt = _receipt(
            outcome="unknown",
            failure_class=FailureClass.UNKNOWN_EXTERNAL_OUTCOME,
        )
        self.assertEqual(receipt.outcome, "unknown")

    def test_receipt_artifact_digest_shape(self):
        with self.assertRaises(ProtocolError):
            _receipt(artifact_digest="not-a-digest")
        digest_value = "0" * 64
        receipt = _receipt(artifact_digest=digest_value)
        self.assertEqual(receipt.artifact_digest, digest_value)

    def test_lease_time_and_fencing_rules(self):
        with self.assertRaises(ProtocolError):
            _lease(expires_at=0)
        with self.assertRaises(ProtocolError):
            _lease(controller_epoch=-1)
        with self.assertRaises(ProtocolError):
            _lease(fencing_token=-1)

    def test_shapes_are_immutable(self):
        envelope = _envelope()
        with self.assertRaises(Exception):
            envelope.task = "rewritten"


if __name__ == "__main__":
    unittest.main()
