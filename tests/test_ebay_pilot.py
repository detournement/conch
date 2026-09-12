"""eBay pilot driver tests: caps policy boundaries and the full
photo→draft→clarify→approve→publish flow against the fake A2A gateway.

The fake "workflow engine" below re-implements the deterministic checks of
Capitol's ``ebay_approval_node`` (exact confirmation ``proceed to post``,
exact challenge ``POST r{rev} {hash[-12:]}``, exact idempotency key
``{app}:{session}:r{rev}:publish``, revision/hash/channel/thread staleness)
so the tests prove the driver constructs byte-exact publish requests, not
merely plausible ones.
"""

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from conch.capitol.client import CapitolRuntime
from conch.capitol.ebay import (
    CONFIRMATION_PHRASE,
    EbayPilot,
    PilotState,
    build_publish_request,
    challenge_for,
    evaluate_caps,
    extract_contracts,
    find_contract,
    publish_idempotency_key,
)
from conch.capitol.errors import CapitolError

from tests.test_capitol_client import BEARER, FakeGateway, _event

ORG = "org-ebay"
AGENT = "agent-ebay"

BASE_CONFIG = {
    "ebay_intake": "typed",
    "ebay_app_id": "conch-ebay",
    "ebay_account_ref": "org-ebay-sandbox",
    "ebay_actor_id": "9e000000-0000-0000-0000-000000000001",
    "ebay_fulfillment_policy_id": "6244379000",
    "ebay_payment_policy_id": "6244377000",
    "ebay_return_policy_id": "6244378000",
    "ebay_merchant_location_key": "sandbox-location",
}

SANDBOX_POLICIES = {
    "fulfillment_policy_id": "6244379000",
    "payment_policy_id": "6244377000",
    "return_policy_id": "6244378000",
    "merchant_location_key": "sandbox-location",
}


def chat_orchestrator(data, message):
    """Scripted stand-in for the agent's delegable draft_or_revise binding:
    promotes the message's FileParts and launches the draft workflow with
    the server-side defaults (session/thread bound to the context)."""
    file_parts = [
        part for part in (message.get("parts") or [])[1:]
        if isinstance(part, dict) and (part.get("file") or {}).get("bytes")
    ]
    request = {
        "schema": "ebay.draft_request.v1",
        "mode": "initial",
        "app_id": "ebay-oversight",
        "account_ref": "org-ebay-sandbox",
        "listing_session_id": "ctx-fake-1",
        "expected_revision": 1,
        "actor_principal_id": "server-actor-0001",
        "channel": "a2a",
        "thread_id": "ctx-fake-1",
        "media": [
            {"artifact_id": f"promoted-{index}", "order": index}
            for index, _part in enumerate(file_parts)
        ],
        "seller_policies": dict(SANDBOX_POLICIES),
        "item_context": str(data.get("message") or ""),
    }
    FakeGateway.run_counter += 1
    run_id = f"run-{FakeGateway.run_counter}"
    output = EbayEngine._draft(run_id, request)
    FakeGateway.runs[run_id] = {
        "status": "running", "output": output,
        "started_by_context": "ctx-fake-1",
    }
    return {
        "assistant_reply": "I drafted a listing from your photo.",
        "conversation_id": "ctx-fake-1",
        "run_id": run_id,
    }


def _revision(request, number, status, open_questions=(), parent=None):
    media = [
        {"artifact_id": item["artifact_id"],
         "digest": "sha256:" + "1" * 64,
         "order": item["order"]}
        for item in request["media"]
    ]
    body = {
        "schema": "ebay.listing_revision.v1",
        "app_id": request["app_id"],
        "account_ref": request["account_ref"],
        "listing_session_id": request["listing_session_id"],
        "actor_principal_id": request["actor_principal_id"],
        "channel": request["channel"],
        "thread_id": request["thread_id"],
        "environment": "sandbox",
        "marketplace_id": "EBAY_US",
        "revision": number,
        "status": status,
        "open_questions": list(open_questions),
        "parent_draft_hash": parent,
        "media": media,
        "warnings": [],
        "listing": {
            "title": "Used Reference Book with Light Shelf Wear",
            "description": "An honest sandbox test listing.",
            "category_id": "377",
            "condition": "USED_GOOD",
            "price": {"value": "19.95", "currency": "USD"},
            "quantity": 1,
            "aspects": {"Type": ["Book"]},
            "policies": {
                "fulfillment_policy_id": request["seller_policies"][
                    "fulfillment_policy_id"
                ],
                "payment_policy_id": request["seller_policies"][
                    "payment_policy_id"
                ],
                "return_policy_id": request["seller_policies"][
                    "return_policy_id"
                ],
                "merchant_location_key": request["seller_policies"][
                    "merchant_location_key"
                ],
            },
        } if status == "draft_review" else {},
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode()
    ).hexdigest()
    body["draft_hash"] = f"sha256:{digest}"
    return body


class EbayEngine:
    """Scripted Capitol-side behavior for the two pilot workflows."""

    first_draft_needs_info = True
    reject_publish = False
    hitl_on_first_draft = False
    last_revision = {}
    publish_requests = []
    draft_requests = []

    @classmethod
    def reset(cls):
        cls.first_draft_needs_info = True
        cls.reject_publish = False
        cls.hitl_on_first_draft = False
        cls.last_revision = {}
        cls.publish_requests = []
        cls.draft_requests = []

    @classmethod
    def handle(cls, run_id, data):
        inputs = data.get("inputs") or {}
        request = inputs.get("node-json-input.value") or {}
        workflow_id = data.get("workflow_id")
        if workflow_id == "draft-wf":
            cls.draft_requests.append(request)
            output = cls._draft(run_id, request)
        elif workflow_id == "publish-wf":
            cls.publish_requests.append(request)
            output = cls._publish(request)
        else:
            output = {}
        FakeGateway.runs[run_id] = {"status": "running", "output": output}

    @classmethod
    def _draft(cls, run_id, request):
        assert request["schema"] == "ebay.draft_request.v1", request
        assert request["seller_policies"]["merchant_location_key"], request
        session = request["listing_session_id"]
        if request["mode"] == "initial":
            assert request["expected_revision"] == 1
            if cls.first_draft_needs_info:
                cls.first_draft_needs_info = False
                revision = _revision(
                    request, 1, "needs_info",
                    open_questions=["What brand is the item?"],
                )
            else:
                revision = _revision(request, 1, "draft_review")
            if cls.hitl_on_first_draft:
                cls.hitl_on_first_draft = False
                FakeGateway.stream_plans[run_id] = [{
                    "events": [
                        _event(1),
                        {
                            "run_id": run_id, "sequence": 2,
                            "event_type": "node.input_required",
                            "scope": "node",
                            "node": {"node_id": "n-agent",
                                     "node_type": "agent",
                                     "display_name": "Draft Agent"},
                            "data": {
                                "input_kind": "clarification",
                                "prompt": "Is the cover hardback?",
                                "request_id": "clar-1",
                            },
                        },
                        _event(3),
                    ],
                    "end": "terminal",
                }]
        else:
            current = request["current_revision"]
            assert request["expected_revision"] == int(current["revision"]) + 1
            assert request["revision_feedback"], "revise needs feedback"
            assert current["listing_session_id"] == session
            revision = _revision(
                request, int(current["revision"]) + 1, "draft_review",
                parent=current["draft_hash"],
            )
        cls.last_revision[session] = revision
        return {
            "outputs": {"review-node": {"value": revision}},
            "output_nodes": [{"node_id": "review-node"}],
        }

    @classmethod
    def _publish(cls, request):
        assert request["schema"] == "ebay.publish_request.v1", request
        revision = request["current_revision"]
        expected_challenge = (
            f"POST r{revision['revision']} {revision['draft_hash'][-12:]}"
        )
        expected_key = (
            f"{revision['app_id']}:{revision['listing_session_id']}:"
            f"r{revision['revision']}:publish"
        )
        exact = (
            request["confirmation"].strip().lower() == "proceed to post"
            and request["challenge"].strip() == expected_challenge
            and request["idempotency_key"] == expected_key
            and request["revision"] == revision["revision"]
            and request["draft_hash"] == revision["draft_hash"]
            and request["channel"] == revision["channel"]
            and request["thread_id"] == revision["thread_id"]
        )
        if not exact or cls.reject_publish:
            contract = {
                "schema": "ebay.approval_rejection.v1",
                "error_detail": {
                    "code": "exact_approval_rejected",
                    "message": "approval revision or draft hash is stale",
                },
            }
        else:
            contract = {
                "schema": "ebay.inventory_effect.v1",
                "state": "PUBLISHED",
                "idempotency_key": request["idempotency_key"],
                "listing": {
                    "listing_id": "110590300001",
                    "offer_id": "11448000001",
                    "sku": "cap-conch-ebay-test",
                    "listing_url": "https://sandbox.ebay.com/itm/110590300001",
                },
            }
        return {
            "outputs": {"effect-node": {"value": contract}},
            "output_nodes": [{"node_id": "effect-node"}],
        }


class ScriptedUI:
    def __init__(self, answers=None, confirms=None):
        self.said = []
        self.prompts = []
        self.answers = list(answers or [])
        self.confirms = list(confirms or [])

    def say(self, text):
        self.said.append(str(text))

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def confirm(self, prompt):
        self.prompts.append(prompt)
        return self.confirms.pop(0) if self.confirms else False


class EbayFlowTests(unittest.TestCase):
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
        FakeGateway.on_call_workflow = None

    def setUp(self):
        FakeGateway.reset(self.port)
        EbayEngine.reset()
        FakeGateway.on_call_workflow = EbayEngine.handle
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photos = []
        for name in ("front.jpg", "back.jpg"):
            path = Path(self.tmp.name) / name
            path.write_bytes(b"\xff\xd8\xff" + name.encode())
            self.photos.append(str(path))
        self.state = PilotState(Path(self.tmp.name) / "state.json")
        self.runtime = CapitolRuntime(
            f"http://127.0.0.1:{self.port}", ORG, AGENT, BEARER,
            caller_system="conch-tests", caller_version="0.0",
        )

    def _pilot(self, ui, config_extra=None):
        config = dict(BASE_CONFIG)
        if config_extra:
            config.update(config_extra)
        return EbayPilot(
            self.runtime, config, state=self.state,
            say=ui.say, ask=ui.ask, confirm=ui.confirm,
        ), config

    # -- full flow ------------------------------------------------------------

    def test_full_flow_clarify_then_auto_publish(self):
        ui = ScriptedUI(answers=["Acme Press"], confirms=[True])
        pilot, _config = self._pilot(ui)
        result = pilot.sell(self.photos, notes="paperback, light wear")
        self.assertTrue(result["published"])
        self.assertEqual(result["listing"]["listing_id"], "110590300001")

        # Two draft turns (initial needs_info, then revise) + one publish.
        self.assertEqual(len(EbayEngine.draft_requests), 2)
        initial, revise = EbayEngine.draft_requests
        self.assertEqual(initial["mode"], "initial")
        self.assertEqual(initial["item_context"], "paperback, light wear")
        self.assertEqual(
            [media["order"] for media in initial["media"]], [0, 1]
        )
        self.assertEqual(revise["mode"], "revise")
        self.assertIn("Acme Press", revise["revision_feedback"])
        self.assertEqual(
            revise["current_revision"]["status"], "needs_info"
        )

        # The publish request is byte-exact against the immutable revision.
        self.assertEqual(len(EbayEngine.publish_requests), 1)
        publish = EbayEngine.publish_requests[0]
        revision = publish["current_revision"]
        self.assertEqual(publish["confirmation"], CONFIRMATION_PHRASE)
        self.assertEqual(
            publish["challenge"],
            challenge_for(revision["revision"], revision["draft_hash"]),
        )
        self.assertEqual(
            publish["idempotency_key"],
            publish_idempotency_key(
                revision["app_id"], revision["listing_session_id"],
                revision["revision"],
            ),
        )
        self.assertEqual(revision["revision"], 2)
        self.assertEqual(revision["status"], "draft_review")

        # Photos actually landed as private artifacts via presigned PUT.
        self.assertEqual(len(FakeGateway.uploads), 2)
        uploaded_ids = {
            media["artifact_id"] for media in initial["media"]
        }
        self.assertEqual(uploaded_ids, set(FakeGateway.uploads.keys()))

        # Run linkage persisted: runs, revisions, listing id.
        sessions = self.state.sessions()
        self.assertEqual(len(sessions), 1)
        session = next(iter(sessions.values()))
        kinds = [run["kind"] for run in session["runs"]]
        self.assertEqual(kinds, ["draft", "draft", "publish"])
        self.assertTrue(
            all(run.get("status") == "success" for run in session["runs"])
        )
        self.assertEqual(
            [entry["revision"] for entry in session["revisions"]], [1, 2]
        )
        self.assertEqual(session["listing"]["listing_id"], "110590300001")
        self.assertEqual(session["context_id"], "ctx-fake-1")

    def test_caps_breach_requires_exact_phrase_and_publishes(self):
        EbayEngine.first_draft_needs_info = False
        ui = ScriptedUI(answers=[CONFIRMATION_PHRASE])
        pilot, _config = self._pilot(
            ui, {"ebay_max_price_usd": "10"}  # 19.95 breaches the cap
        )
        result = pilot.sell(self.photos[:1])
        self.assertTrue(result["published"])
        self.assertIn(
            "exact approval required",
            " ".join(ui.said),
        )

    def test_caps_breach_wrong_phrase_never_publishes(self):
        EbayEngine.first_draft_needs_info = False
        ui = ScriptedUI(answers=["yes please"])
        pilot, _config = self._pilot(ui, {"ebay_max_price_usd": "10"})
        result = pilot.sell(self.photos[:1])
        self.assertFalse(result["published"])
        self.assertEqual(EbayEngine.publish_requests, [])

    def test_auto_path_still_needs_operator_confirmation(self):
        EbayEngine.first_draft_needs_info = False
        ui = ScriptedUI(confirms=[False])
        pilot, _config = self._pilot(ui)
        result = pilot.sell(self.photos[:1])
        self.assertFalse(result["published"])
        self.assertEqual(EbayEngine.publish_requests, [])

    def test_publish_rejection_surfaces_as_error(self):
        EbayEngine.first_draft_needs_info = False
        EbayEngine.reject_publish = True
        ui = ScriptedUI(confirms=[True])
        pilot, _config = self._pilot(ui)
        with self.assertRaises(CapitolError) as raised:
            pilot.sell(self.photos[:1])
        self.assertIn("rejected", str(raised.exception))

    def test_hitl_clarification_mid_run(self):
        EbayEngine.first_draft_needs_info = False
        EbayEngine.hitl_on_first_draft = True
        ui = ScriptedUI(answers=["hardback"], confirms=[True])
        pilot, _config = self._pilot(ui)
        result = pilot.sell(self.photos[:1])
        self.assertTrue(result["published"])
        clarifications = [
            (skill, data) for skill, data, _env in FakeGateway.calls
            if skill == "submit_clarification_response"
        ]
        self.assertEqual(len(clarifications), 1)
        self.assertEqual(clarifications[0][1]["response"], "hardback")
        self.assertEqual(clarifications[0][1]["request_id"], "clar-1")

    def test_rejects_missing_photo(self):
        ui = ScriptedUI()
        pilot, _config = self._pilot(ui)
        with self.assertRaises(CapitolError):
            pilot.sell([str(Path(self.tmp.name) / "missing.jpg")])

    def test_rejects_unconfigured_actor(self):
        EbayEngine.first_draft_needs_info = False
        ui = ScriptedUI()
        pilot, _config = self._pilot(ui, {"ebay_actor_id": ""})
        with self.assertRaises(CapitolError) as raised:
            pilot.sell(self.photos[:1])
        self.assertIn("ebay_actor_id", str(raised.exception))

    def test_chat_intake_clarify_then_publish(self):
        """Default intake: photos as chat FileParts; the orchestrator's
        launch is supervised, the revise + publish turns stay typed and
        echo the server-bound identity (session = context) verbatim."""
        FakeGateway.chat_script = chat_orchestrator
        ui = ScriptedUI(answers=["Acme Press"], confirms=[True])
        pilot, _config = self._pilot(ui, {"ebay_intake": "chat"})
        result = pilot.sell(self.photos, notes="paperback, light wear")
        self.assertTrue(result["published"])

        chat_calls = [
            (data, env) for skill, data, env in FakeGateway.calls
            if skill == "chat"
        ]
        self.assertEqual(len(chat_calls), 1)
        data, envelope = chat_calls[0]
        self.assertIn("paperback, light wear", data["message"])
        parts = envelope["params"]["message"]["parts"]
        self.assertEqual(len(parts), 3)  # data part + two FileParts
        self.assertTrue(all(p.get("file", {}).get("bytes")
                            for p in parts[1:]))

        # Revise + publish echo the server-side session/actor, not config.
        revise = EbayEngine.draft_requests[-1]
        self.assertEqual(revise["mode"], "revise")
        self.assertEqual(revise["listing_session_id"], "ctx-fake-1")
        self.assertEqual(revise["actor_principal_id"], "server-actor-0001")
        self.assertEqual(
            [m["artifact_id"] for m in revise["media"]],
            ["promoted-0", "promoted-1"],
        )
        publish = EbayEngine.publish_requests[0]
        self.assertEqual(publish["listing_session_id"], "ctx-fake-1")
        self.assertEqual(publish["actor_principal_id"], "server-actor-0001")
        self.assertEqual(
            publish["idempotency_key"],
            "ebay-oversight:ctx-fake-1:r2:publish",
        )
        # No typed upload happened on the chat path.
        self.assertEqual(FakeGateway.uploads, {})


class CapsPolicyTests(unittest.TestCase):
    def _revision(self, price="19.95", category="377"):
        return {
            "revision": 1,
            "listing": {
                "category_id": category,
                "price": {"value": price, "currency": "USD"},
            },
        }

    def test_default_sandbox_policy_allows_auto(self):
        decision = evaluate_caps({}, self._revision())
        self.assertTrue(decision.auto)
        self.assertEqual(decision.reasons, [])

    def test_auto_publish_disabled(self):
        decision = evaluate_caps(
            {"ebay_auto_publish": "false"}, self._revision()
        )
        self.assertFalse(decision.auto)

    def test_price_ceiling_boundary(self):
        config = {"ebay_max_price_usd": "19.95"}
        self.assertTrue(evaluate_caps(config, self._revision("19.95")).auto)
        self.assertFalse(evaluate_caps(config, self._revision("19.96")).auto)

    def test_no_min_price_rule(self):
        # The clamp is a ceiling only; low prices are the workflow's call.
        config = {"ebay_max_price_usd": "50"}
        self.assertTrue(evaluate_caps(config, self._revision("0.99")).auto)

    def test_category_allowlist(self):
        config = {"ebay_allowed_category_ids": "377, 261186"}
        self.assertTrue(evaluate_caps(config, self._revision()).auto)
        self.assertFalse(
            evaluate_caps(config, self._revision(category="9999")).auto
        )

    def test_missing_fields_fail_closed(self):
        config = {"ebay_max_price_usd": "50"}
        revision = {"revision": 1, "listing": {}}
        self.assertFalse(evaluate_caps(config, revision).auto)
        config = {"ebay_allowed_category_ids": "377"}
        self.assertFalse(evaluate_caps(config, revision).auto)

    def test_malformed_cap_fails_closed(self):
        config = {"ebay_max_price_usd": "lots"}
        self.assertFalse(evaluate_caps(config, self._revision()).auto)


class ContractHelperTests(unittest.TestCase):
    def test_challenge_and_key_formulas(self):
        digest = "sha256:" + "ab" * 32
        self.assertEqual(
            challenge_for(3, digest), f"POST r3 {digest[-12:]}"
        )
        self.assertEqual(
            publish_idempotency_key("app", "sess", 3), "app:sess:r3:publish"
        )

    def test_build_publish_request_copies_revision_verbatim(self):
        revision = {
            "schema": "ebay.listing_revision.v1",
            "app_id": "app", "listing_session_id": "sess",
            "revision": 2, "draft_hash": "sha256:" + "9" * 64,
            "channel": "a2a", "thread_id": "thread-1",
        }
        request = build_publish_request(
            revision, actor_principal_id="actor",
            presentation_id="pres-1",
        )
        self.assertEqual(request["confirmation"], "proceed to post")
        self.assertEqual(request["challenge"], "POST r2 999999999999")
        self.assertEqual(
            request["idempotency_key"], "app:sess:r2:publish"
        )
        self.assertIs(request["current_revision"], revision)

    def test_build_publish_request_fails_closed_on_missing_fields(self):
        with self.assertRaises(CapitolError):
            build_publish_request(
                {"app_id": "a"}, actor_principal_id="x",
                presentation_id="p",
            )

    def test_extract_contracts_recursive(self):
        payload = {
            "outputs": {
                "a": {"value": {"schema": "ebay.listing_revision.v1", "n": 1}},
                "b": [{"schema": "other.v1"},
                      {"nested": {"schema": "ebay.inventory_effect.v1"}}],
            }
        }
        schemas = sorted(
            contract["schema"] for contract in extract_contracts(payload)
        )
        self.assertEqual(
            schemas,
            ["ebay.inventory_effect.v1", "ebay.listing_revision.v1"],
        )
        self.assertIsNotNone(
            find_contract(payload, "ebay.inventory_effect.v1")
        )
        self.assertIsNone(find_contract(payload, "ebay.sale_event.v1"))


class PilotStateTests(unittest.TestCase):
    def test_state_roundtrip_and_run_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = PilotState(Path(tmp) / "sub" / "state.json")
            state.update_session("s1", app_id="conch-ebay")
            state.record_run("s1", "draft", "run-1", "key-1")
            state.update_run("s1", "run-1", last_sequence=7, status="success")
            state.append("s1", "revisions", {"revision": 1})
            loaded = state.sessions()
            self.assertEqual(loaded["s1"]["app_id"], "conch-ebay")
            run = loaded["s1"]["runs"][0]
            self.assertEqual(run["last_sequence"], 7)
            self.assertEqual(run["status"], "success")
            self.assertEqual(loaded["s1"]["revisions"], [{"revision": 1}])
            # Corrupt file degrades to empty, never crashes.
            (Path(tmp) / "sub" / "state.json").write_text("{broken")
            self.assertEqual(state.sessions(), {})


if __name__ == "__main__":
    unittest.main()
