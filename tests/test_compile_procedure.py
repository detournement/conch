"""Existing Procedure capture is bounded, inert, exact, and review-only."""

import copy
import unittest

from conch.capitol.compiler.capture import CAPTURE_BLOCK_CAP
from conch.capitol.compiler.capture_procedure import (
    capture_from_procedure,
    workflow_input_override_key,
)
from conch.capitol.errors import CapitolError
from conch.secretguard import CredentialRejected
from tests.test_capitol_procedures import document, version


class FakeClient:
    def __init__(self, doc=None, workflow_version=None):
        self.org_id = "org-procedures"
        self.doc = doc or document()
        self.version = workflow_version or version()
        self.calls = []

    def get(self, workflow_id, **kwargs):
        self.calls.append(("procedure", workflow_id, kwargs))
        return copy.deepcopy(self.doc)

    def get_workflow_version(self, workflow_id, workflow_version_id):
        self.calls.append(("version", workflow_id, workflow_version_id))
        return copy.deepcopy(self.version)


class ProcedureCaptureTests(unittest.TestCase):
    def test_exact_pair_becomes_bounded_inert_evidence(self):
        payload = version()["payload"]
        payload["nodes"] = [
            {
                "id": "input-node",
                "data": {"struct": {"node_id": "json_input_node"}},
            }
        ]
        client = FakeClient(workflow_version=dict(version(), payload=payload))
        context = capture_from_procedure(
            {},
            document()["workflow_id"],
            2,
            client=client,
        )
        self.assertLessEqual(len(context["block"]), CAPTURE_BLOCK_CAP)
        self.assertIn("INERT EVIDENCE", context["block"])
        self.assertIn("NEVER AUTHORIZATION", context["block"])
        self.assertEqual(context["source_ref"]["relationship"], "adopt")
        self.assertEqual(
            context["discovery_workflow"]["input_override_key"],
            "input-node.value",
        )
        self.assertEqual(
            context["provenance"]["procedure_content_digest"],
            document()["content_digest"],
        )
        self.assertEqual(context["provenance"]["evidence"], context["block"])

    def test_explicit_goal_is_adaptation_not_direct_adoption(self):
        context = capture_from_procedure(
            {},
            document()["workflow_id"],
            2,
            goal="adapt this for another team",
            client=FakeClient(),
        )
        self.assertEqual(context["source_ref"]["relationship"], "adapt")
        self.assertEqual(
            context["default_goal"],
            "adapt this for another team",
        )

    def test_workflow_version_mismatch_fails_closed(self):
        bad = dict(version(), version_number=3)
        with self.assertRaisesRegex(CapitolError, "mismatch"):
            capture_from_procedure(
                {},
                document()["workflow_id"],
                2,
                client=FakeClient(workflow_version=bad),
            )

    def test_secretguard_rejects_entire_procedure_capture(self):
        bad = document()
        bad["markdown"] = "run with AKIAIOSFODNN7EXAMPLE"
        with self.assertRaises(CredentialRejected):
            capture_from_procedure(
                {},
                document()["workflow_id"],
                2,
                client=FakeClient(doc=bad),
            )

    def test_multiple_input_nodes_never_guess(self):
        payload = {
            "nodes": [
                {
                    "id": "a",
                    "data": {
                        "struct": {
                            "node_id": "json_input_node",
                        }
                    },
                },
                {
                    "id": "b",
                    "data": {
                        "struct": {
                            "node_id": "text_input_node",
                        }
                    },
                },
            ]
        }
        self.assertEqual(workflow_input_override_key(payload), "")


if __name__ == "__main__":
    unittest.main()
