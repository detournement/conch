"""The compilation session (C1): deterministic scaffolding around a
scripted model.

Proven here against the fake catalog/discovery fixtures: an emitted card
is validated fail-closed inside the session (a rejection returns to the
model as the tool result, naming the fix), reuse-first steers a model
that tries to duplicate an existing workflow into reusing it, a session
that never emits an accepted card fails (nothing partial is stored), the
read-only ``capitol_control`` wrapper refuses every effectful and admin
op, and the discovery digest is bounded and complete.
"""

import copy
import json
import unittest

from conch.capitol.compiler.card import CardError
from conch.capitol.compiler.session import (
    READ_OPS,
    CompilerWorkspaceClient,
    ReadOnlyCapitolClient,
    card_requirements,
    discovery_digest,
    run_compile_session,
)

from tests.compiler_fixtures import (
    WORKFLOW_IDENTITY,
    candidate_card,
    fake_discovery,
)


def scripted_factory(emissions):
    """A session factory that plays the given cards into the workspace
    in order, mimicking a model retrying after rejections. Returns the
    list of tool results for assertions."""
    results = []

    def factory(messages, workspace, capitol_client, caps):
        for card in emissions:
            result = workspace.call_tool(
                "compiler_workspace", {"op": "emit_card", "card": card}
            )
            results.append(result["content"][0]["text"])
        return "done", {"input_tokens": 10, "output_tokens": 10}
    return factory, results


class TestCompileSession(unittest.TestCase):
    def setUp(self):
        self.discovery = fake_discovery()

    def test_valid_card_accepted_and_normalized(self):
        factory, results = scripted_factory([candidate_card()])
        card = run_compile_session(
            {}, "weekday 5pm funding report",
            discovery=self.discovery, session_factory=factory,
        )
        self.assertIn("Card accepted", results[0])
        # the returned card is the NORMALIZED one (ids resolved)
        workflow = card["assets"]["create"]["workflows"][0]
        self.assertTrue(workflow["workflow_id"])
        self.assertTrue(card["mission"]["dry_run"])

    def test_reuse_first_steers_the_model(self):
        duplicate = copy.deepcopy(candidate_card())
        # the model's first attempt recreates an existing workflow
        duplicate["assets"]["create"]["workflows"][0]["name"] = (
            "together-funding-ingest"
        )
        corrected = candidate_card()
        factory, results = scripted_factory([duplicate, corrected])
        card = run_compile_session(
            {}, "goal", discovery=self.discovery, session_factory=factory,
        )
        self.assertIn("reuse-first", results[0])
        self.assertIn("together-funding-ingest", results[0])
        self.assertIn("Card accepted", results[1])
        # the accepted card creates only the genuinely novel workflow
        created = [
            workflow["name"]
            for workflow in card["assets"]["create"]["workflows"]
        ]
        self.assertEqual(created, [WORKFLOW_IDENTITY])

    def test_open_questions_surface_in_acceptance(self):
        parked = candidate_card()
        parked["open_questions"] = ["which channel gets the report?"]
        factory, results = scripted_factory([parked])
        card = run_compile_session(
            {}, "goal", discovery=self.discovery, session_factory=factory,
        )
        self.assertIn("open question", results[0])
        self.assertEqual(len(card["open_questions"]), 1)

    def test_no_accepted_card_fails_the_session(self):
        broken = candidate_card()
        del broken["narrative"]
        factory, results = scripted_factory([broken])
        with self.assertRaisesRegex(CardError, "without an accepted card"):
            run_compile_session(
                {}, "goal", discovery=self.discovery,
                session_factory=factory,
            )
        self.assertIn("emit_card rejected", results[0])

    def test_model_error_fails_the_session(self):
        def factory(messages, workspace, capitol_client, caps):
            return "", {"error": "backend unreachable"}
        with self.assertRaisesRegex(CardError, "backend unreachable"):
            run_compile_session(
                {}, "goal", discovery=self.discovery,
                session_factory=factory,
            )

    def test_empty_goal_refused(self):
        with self.assertRaisesRegex(CardError, "non-empty goal"):
            run_compile_session(
                {}, "  ", discovery=self.discovery,
                session_factory=lambda *a: ("", {}),
            )

    def test_prompt_carries_goal_discovery_and_revision_context(self):
        seen = {}

        def factory(messages, workspace, capitol_client, caps):
            seen["messages"] = messages
            seen["caps"] = caps
            workspace.call_tool(
                "compiler_workspace",
                {"op": "emit_card", "card": candidate_card()},
            )
            return "ok", {}
        prior = {"schema": "conch.architecture_card.v1", "goal": "old"}
        run_compile_session(
            {}, "the goal text", discovery=self.discovery,
            session_factory=factory, prior_card=prior,
            guidance="make it weekly",
        )
        system = seen["messages"][0]["content"]
        user = seen["messages"][1]["content"]
        self.assertIn("REUSE-FIRST", system)
        self.assertIn("ARCHITECTURE CARD SHAPE", system)
        self.assertIn("the goal text", user)
        self.assertIn("DISCOVERY DIGEST", user)
        self.assertIn("together-funding-ingest", user)
        self.assertIn("REVISION", user)
        self.assertIn("make it weekly", user)
        # mission-session-style budgets
        self.assertEqual(seen["caps"]["max_tool_rounds"], 15)
        self.assertEqual(seen["caps"]["token_budget"], 200000)
        self.assertEqual(seen["caps"]["wall_seconds"], 600)


class TestWorkspaceClient(unittest.TestCase):
    def test_requirements_op(self):
        workspace = CompilerWorkspaceClient(
            fake_discovery(), "conch-compile"
        )
        result = workspace.call_tool(
            "compiler_workspace", {"op": "requirements"}
        )
        text = result["content"][0]["text"]
        self.assertIn("conch.architecture_card.v1", text)
        self.assertIn("$create:", text)
        self.assertEqual(text, card_requirements("conch-compile"))

    def test_emit_accepts_json_string(self):
        workspace = CompilerWorkspaceClient(
            fake_discovery(), "conch-compile"
        )
        result = workspace.call_tool("compiler_workspace", {
            "op": "emit_card", "card": json.dumps(candidate_card()),
        })
        self.assertIn("Card accepted", result["content"][0]["text"])
        self.assertIsNotNone(workspace.card)

    def test_unknown_op(self):
        workspace = CompilerWorkspaceClient(fake_discovery(), "x")
        result = workspace.call_tool(
            "compiler_workspace", {"op": "provision"}
        )
        self.assertIn("unknown compiler_workspace op",
                      result["content"][0]["text"])


class TestReadOnlyCapitol(unittest.TestCase):
    def test_effectful_and_admin_ops_refused(self):
        client = ReadOnlyCapitolClient({"capitol_base_url": "http://x"})
        for op in ("start", "respond", "upload", "download", "admin",
                   "persist", "publish", "schedule-add", "pack_verify",
                   "create-agent"):
            result = client.call_tool("capitol_control", {"op": op})
            text = result["content"][0]["text"]
            self.assertIn("refuses", text)
            self.assertIn("never provisions", text)

    def test_read_ops_pass_through(self):
        calls = []

        class Inner:
            def call_tool(self, name, arguments):
                calls.append(arguments["op"])
                return {"content": [{"type": "text", "text": "ok"}]}
        client = ReadOnlyCapitolClient({})
        client._inner = Inner()
        for op in sorted(READ_OPS):
            result = client.call_tool("capitol_control", {"op": op})
            self.assertEqual(result["content"][0]["text"], "ok")
        self.assertEqual(sorted(calls), sorted(READ_OPS))


class TestDiscoveryDigest(unittest.TestCase):
    def test_digest_sections(self):
        text = discovery_digest(fake_discovery())
        self.assertIn("Org workflows (2):", text)
        self.assertIn("together-funding-packet", text)
        self.assertIn("Collections (1):", text)
        self.assertIn("Flow packs: ebay-listing", text)
        self.assertIn("node catalog", text)
        self.assertIn("Conch-side capabilities", text)

    def test_digest_bounded(self):
        discovery = fake_discovery()
        discovery["workflows"] = [
            {"id": f"wf-{i}", "name": "x" * 80} for i in range(500)
        ]
        text = discovery_digest(discovery)
        self.assertLess(len(text), 20000)
        self.assertIn("[clipped]", text)


if __name__ == "__main__":
    unittest.main()
