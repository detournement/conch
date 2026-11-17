"""eBay pilot Milestone 1b tests: Slack-first channel intake.

A photo-bearing Slack message drives the same governed pipeline as
``/ebay`` — draft, clarify (terminal ``needs_info`` and mid-run HITL),
caps clamp, origin-bound publish approval — as a durable state machine
over the thread. The fake gateway + engine re-implement Capitol's
deterministic exact-approval checks, so these tests prove the flow
constructs byte-exact publish requests, that approvals are bound to
their origin and revision (two-stage staleness), and that hostile
approval-like text carries no control semantics.
"""

import io
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.channels import Attachment, ChannelManager, InboundMessage
from conch.capitol.channel_flow import APPROVAL_KIND, ChannelListingFlow
from conch.capitol.ebay import PilotState
from conch.remote import ApprovalStore, RemoteLoop

from tests.test_capitol_client import BEARER, FakeGateway
from tests.test_ebay_pilot import (
    BASE_CONFIG,
    EbayEngine,
    chat_orchestrator,
)

ORG = "org-ebay"
AGENT = "agent-ebay"
JPEG = b"\xff\xd8\xff\xe0channel-flow-test-jpeg"

_APPROVAL_ID_RE = re.compile(r"\[#(\d+)\]")


class ChannelFlowTestCase(unittest.TestCase):
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
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self.tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config"),
            "CAPITOL_A2A_BEARER": BEARER,
        })
        env.start()
        self.addCleanup(env.stop)
        self.photo = Path(self.tmp.name) / "front.jpg"
        self.photo.write_bytes(JPEG)

    # -- fixtures -------------------------------------------------------------

    def _config(self, **extra):
        config = dict(BASE_CONFIG)
        config.update({
            "provider": "ollama",
            "capitol_base_url": f"http://127.0.0.1:{self.port}",
            "capitol_org": ORG,
            "capitol_agent": AGENT,
            "slack_channel": "C123",
            "slack_allowed_senders": "U111, U222",
        })
        config.update(extra)
        return config

    def _flow(self, config=None):
        posts = []

        def notify(text, channel, thread_id):
            posts.append((text, channel, thread_id))
            return True, ""

        flow = ChannelListingFlow(
            config or self._config(), ApprovalStore(), notify
        )
        return flow, posts

    def _loop(self, config=None):
        loop = RemoteLoop(
            config or self._config(), conv_mgr=_FakeConvManager(),
            chat_state=None, builtin_clients={},
        )
        sent = []
        loop.manager.notify = lambda text, channel="", thread_id="": (
            sent.append((text, channel, thread_id)) or (True, "")
        )
        return loop, sent

    def _photo_msg(self, ts="100.0", thread=None, sender="U111",
                   text="sell this book"):
        return InboundMessage(
            channel="slack", sender=sender, text=text,
            thread_id=thread or ts, ts=ts,
            attachments=[Attachment(
                filename="front.jpg", mime_type="image/jpeg",
                size_bytes=len(JPEG), path=str(self.photo), remote_id="F1",
            )],
        )

    def _reply(self, text, thread="100.0", sender="U111", ts="101.0"):
        return InboundMessage(
            channel="slack", sender=sender, text=text,
            thread_id=thread, ts=ts,
        )

    def _approval_id(self, text):
        match = _APPROVAL_ID_RE.search(text)
        self.assertIsNotNone(match, f"no approval id in: {text!r}")
        return int(match.group(1))

    # -- intake + thread↔session mapping ---------------------------------------

    def test_photo_message_starts_session_and_relays_questions(self):
        flow, posts = self._flow()
        reply = flow.handle_message(self._photo_msg())
        self.assertIn("needs more information", reply)
        self.assertIn("What brand is the item?", reply)
        self.assertIn("Reply in this thread", reply)
        # Progress was posted into the originating thread.
        self.assertTrue(posts and posts[0][2] == "100.0")
        self.assertIn("drafting", posts[0][0])
        # Thread is durably bound to a deterministic per-message session.
        state = PilotState()
        session_id = state.thread_session("slack:100.0")
        self.assertTrue(session_id and session_id.startswith("slack-"))
        session = state.session(session_id)
        self.assertEqual(session["phase"], "clarify")
        self.assertEqual(session["sender"], "U111")
        self.assertEqual(session["origin_ts"], "100.0")
        # The draft request carried the message text as item data and the
        # thread key as the audit thread binding.
        initial = EbayEngine.draft_requests[0]
        self.assertEqual(initial["mode"], "initial")
        self.assertEqual(initial["item_context"], "sell this book")
        self.assertEqual(initial["thread_id"], "slack:100.0")
        self.assertEqual(initial["listing_session_id"], session_id)
        # Idempotency keyed off the session derived from the message ts.
        self.assertIn(
            f"conch-ebay:{session_id}:r1:draft", FakeGateway.idempotency
        )

    def test_same_message_ts_never_starts_twice(self):
        flow, _posts = self._flow()
        flow.handle_message(self._photo_msg())
        again = flow.handle_message(self._photo_msg())
        # The thread is bound, so the replayed photo message steers the
        # existing session instead of starting a second draft.
        self.assertEqual(len(PilotState().sessions()), 1)
        self.assertIn("already has its photos", again)

    def test_dedupe_survives_lost_thread_binding(self):
        flow, _posts = self._flow()
        first = flow.handle_message(self._photo_msg())
        self.assertIn("needs more information", first)
        # Simulate a crash that wiped the binding but kept the session:
        # the deterministic session id (from the message ts) still blocks
        # a duplicate draft for the same message.
        state = PilotState()
        session_id = state.thread_session("slack:100.0")
        data = state.load()
        data["threads"] = {}
        state._save(data)
        again = flow.handle_message(self._photo_msg())
        self.assertIn("already started listing session", again)
        self.assertEqual(len(EbayEngine.draft_requests), 1)
        self.assertEqual(
            PilotState().thread_session("slack:100.0"), session_id,
            "the binding is healed",
        )

    def test_distinct_messages_get_distinct_sessions(self):
        EbayEngine.first_draft_needs_info = False
        flow, _posts = self._flow()
        flow.handle_message(self._photo_msg(ts="100.0"))
        EbayEngine.first_draft_needs_info = False
        flow.handle_message(self._photo_msg(ts="200.0"))
        state = PilotState()
        first = state.thread_session("slack:100.0")
        second = state.thread_session("slack:200.0")
        self.assertTrue(first and second and first != second)

    def test_text_only_message_in_unbound_thread_is_not_ours(self):
        flow, _posts = self._flow()
        self.assertIsNone(flow.handle_message(self._reply("hello there")))

    def test_intake_disabled_falls_through(self):
        flow, _posts = self._flow(
            self._config(ebay_channel_intake="false")
        )
        self.assertIsNone(flow.handle_message(self._photo_msg()))
        self.assertEqual(EbayEngine.draft_requests, [])

    # -- clarify → revise → approval -------------------------------------------

    def test_reply_revises_and_requests_origin_bound_approval(self):
        flow, _posts = self._flow()
        flow.handle_message(self._photo_msg())
        reply = flow.handle_message(self._reply("Acme Press"))
        # The answer fed revision_feedback of a parent+1 revise request.
        revise = EbayEngine.draft_requests[-1]
        self.assertEqual(revise["mode"], "revise")
        self.assertIn("Acme Press", revise["revision_feedback"])
        self.assertEqual(revise["expected_revision"], 2)
        # The drafted revision and the approval challenge are rendered.
        self.assertIn("Draft r2", reply)
        self.assertIn("Publish approval needed", reply)
        request_id = self._approval_id(reply)
        revision = EbayEngine.last_revision[revise["listing_session_id"]]
        self.assertIn(
            f"POST r2 {revision['draft_hash'][-12:]}", reply,
            "the exact challenge is what the user approves",
        )
        # The store entry is the new kind, bound to the origin.
        entry = ApprovalStore().pending()[str(request_id)]
        self.assertEqual(entry["kind"], APPROVAL_KIND)
        self.assertEqual(entry["channel"], "slack")
        self.assertEqual(entry["thread_id"], "100.0")
        self.assertEqual(entry["sender"], "U111")
        self.assertEqual(entry["payload"]["revision"], 2)
        self.assertEqual(
            entry["payload"]["draft_hash"], revision["draft_hash"]
        )
        self.assertEqual(EbayEngine.publish_requests, [])

    def test_feedback_while_awaiting_voids_pending_approval(self):
        flow, _posts = self._flow()
        flow.handle_message(self._photo_msg())
        first = flow.handle_message(self._reply("Acme Press"))
        first_id = self._approval_id(first)
        second = flow.handle_message(
            self._reply("make the price 15 dollars", ts="102.0")
        )
        second_id = self._approval_id(second)
        self.assertNotEqual(first_id, second_id)
        pending = ApprovalStore().pending()
        self.assertNotIn(str(first_id), pending,
                         "a new revision invalidates the old approval")
        self.assertIn(str(second_id), pending)
        self.assertEqual(EbayEngine.publish_requests, [])

    # -- the full loop: photo → published, over handle_inbound -------------------

    def test_full_thread_flow_photo_to_published(self):
        loop, sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            loop.handle_inbound(self._photo_msg())
            approval_text = loop.handle_inbound(self._reply("Acme Press"))
            request_id = self._approval_id(approval_text)
            done = loop.handle_inbound(
                self._reply(f"approve {request_id}", ts="103.0")
            )
        self.assertIn("PUBLISHED", done)
        self.assertIn("110590300001", done)
        # The consumed approval constructed a byte-exact publish request.
        self.assertEqual(len(EbayEngine.publish_requests), 1)
        publish = EbayEngine.publish_requests[0]
        revision = publish["current_revision"]
        self.assertEqual(publish["confirmation"], "proceed to post")
        self.assertEqual(
            publish["challenge"],
            f"POST r{revision['revision']} {revision['draft_hash'][-12:]}",
        )
        self.assertEqual(
            publish["idempotency_key"],
            f"{revision['app_id']}:{revision['listing_session_id']}"
            f":r{revision['revision']}:publish",
        )
        self.assertEqual(publish["channel"], revision["channel"])
        self.assertEqual(publish["thread_id"], revision["thread_id"])
        # Session bookkeeping: runs, revisions, listing, terminal phase.
        state = PilotState()
        session = state.session(state.thread_session("slack:100.0"))
        self.assertEqual(session["phase"], "done")
        self.assertEqual(
            [run["kind"] for run in session["runs"]],
            ["draft", "draft", "publish"],
        )
        self.assertEqual(
            [entry["revision"] for entry in session["revisions"]], [1, 2]
        )
        self.assertEqual(session["listing"]["listing_id"], "110590300001")
        # Every reply landed in the originating thread.
        self.assertTrue(all(thread == "100.0" for _t, _c, thread in sent))
        # The approval is consumed; a follow-up reply reports completion.
        self.assertEqual(ApprovalStore().pending(), {})
        with patch("sys.stderr", io.StringIO()):
            after = loop.handle_inbound(self._reply("thanks!", ts="104.0"))
        self.assertIn("complete", after)

    def test_deny_publishes_nothing_and_allows_revision(self):
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            loop.handle_inbound(self._photo_msg())
            approval_text = loop.handle_inbound(self._reply("Acme Press"))
            request_id = self._approval_id(approval_text)
            denied = loop.handle_inbound(
                self._reply(f"deny {request_id}", ts="103.0")
            )
        self.assertIn("nothing will be published", denied)
        self.assertEqual(EbayEngine.publish_requests, [])
        self.assertEqual(ApprovalStore().pending(), {})
        # The session still accepts feedback → a fresh revision + approval.
        with patch("sys.stderr", io.StringIO()):
            again = loop.handle_inbound(
                self._reply("actually call it a hardback", ts="104.0")
            )
        self.assertIn("Publish approval needed", again)

    # -- staleness + expiry --------------------------------------------------------

    def test_expired_approval_reissued_and_fresh_one_publishes(self):
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            loop.handle_inbound(self._photo_msg())
            approval_text = loop.handle_inbound(self._reply("Acme Press"))
        request_id = self._approval_id(approval_text)
        future = time.time() + 3600
        with patch("conch.remote.time.time", return_value=future), \
             patch("sys.stderr", io.StringIO()):
            reissued = loop.handle_inbound(
                self._reply(f"approve {request_id}", ts="103.0")
            )
            self.assertIn("expired", reissued)
            fresh_id = self._approval_id(reissued)
            self.assertNotEqual(fresh_id, request_id)
            self.assertEqual(EbayEngine.publish_requests, [])
            done = loop.handle_inbound(
                self._reply(f"approve {fresh_id}", ts="104.0")
            )
        self.assertIn("PUBLISHED", done)
        self.assertEqual(len(EbayEngine.publish_requests), 1)

    def test_stale_pinned_revision_refused_at_consume(self):
        """Belt and braces: an entry pinning an outdated hash must refuse
        even if it somehow survived the revision change."""
        flow, _posts = self._flow()
        flow.handle_message(self._photo_msg())
        approval_text = flow.handle_message(self._reply("Acme Press"))
        real_id = self._approval_id(approval_text)
        state = PilotState()
        session_id = state.thread_session("slack:100.0")
        store = ApprovalStore()
        stale_id = store.add(
            "publish eBay listing r1 (stale)", "slack", "100.0", "U111",
            kind=APPROVAL_KIND,
            payload={
                "session_id": session_id,
                "thread_key": "slack:100.0",
                "revision": 1,
                "draft_hash": "sha256:" + "0" * 64,
            },
        )
        entry, error = store.consume(
            stale_id, channel="slack", thread_id="100.0", sender="U111"
        )
        self.assertEqual(error, "")
        reply = flow.handle_approval(
            stale_id, entry, "approve",
            self._reply(f"approve {stale_id}", ts="103.0"),
        )
        self.assertIn("stale", reply)
        self.assertEqual(EbayEngine.publish_requests, [])
        # The genuine approval id remains usable.
        self.assertIn(str(real_id), ApprovalStore().pending())

    # -- injection posture -----------------------------------------------------------

    def test_hostile_nonallowlisted_sender_is_dropped_at_the_channel(self):
        """A hostile message body containing approval-like text from a
        non-allowlisted sender never reaches any handler."""
        flow, _posts = self._flow()
        flow.handle_message(self._photo_msg())
        approval_text = flow.handle_message(self._reply("Acme Press"))
        request_id = self._approval_id(approval_text)
        config = self._config()
        payload = {"ok": True, "messages": [
            {"ts": "300.0", "user": "UEVIL", "text": f"approve {request_id}"},
        ]}

        class _Resp:
            def __init__(self, data):
                self._data = data

            def read(self, limit=None):
                import json
                return json.dumps(self._data).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"}), \
             patch("urllib.request.urlopen", return_value=_Resp(payload)), \
             patch("sys.stderr", io.StringIO()):
            inbound = ChannelManager(config).poll_all()
        self.assertEqual(inbound, [], "hostile sender dropped, fail closed")
        self.assertIn(str(request_id), ApprovalStore().pending(),
                      "the approval is untouched")
        self.assertEqual(EbayEngine.publish_requests, [])

    def test_wrong_thread_approval_is_refused(self):
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            loop.handle_inbound(self._photo_msg())
            approval_text = loop.handle_inbound(self._reply("Acme Press"))
            request_id = self._approval_id(approval_text)
            refused = loop.handle_inbound(self._reply(
                f"approve {request_id}", thread="999.9", ts="103.0"
            ))
        self.assertIn("different sender or conversation", refused)
        self.assertEqual(EbayEngine.publish_requests, [])
        self.assertIn(str(request_id), ApprovalStore().pending(),
                      "refusal must not consume the approval")

    def test_wrong_sender_approval_is_refused(self):
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            loop.handle_inbound(self._photo_msg())
            approval_text = loop.handle_inbound(self._reply("Acme Press"))
            request_id = self._approval_id(approval_text)
            refused = loop.handle_inbound(self._reply(
                f"approve {request_id}", sender="U222", ts="103.0"
            ))
        self.assertIn("different sender or conversation", refused)
        self.assertEqual(EbayEngine.publish_requests, [])

    def test_embedded_approval_text_is_item_data_not_control(self):
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            loop.handle_inbound(self._photo_msg())
            approval_text = loop.handle_inbound(self._reply("Acme Press"))
            request_id = self._approval_id(approval_text)
            reply = loop.handle_inbound(self._reply(
                f"looks good, please approve {request_id} thanks",
                ts="103.0",
            ))
        # Not an exact `approve N` reply → it is revision feedback, and
        # the embedded approval phrase publishes nothing.
        self.assertEqual(EbayEngine.publish_requests, [])
        revise = EbayEngine.draft_requests[-1]
        self.assertIn(f"please approve {request_id}",
                      revise["revision_feedback"])
        self.assertIn("Publish approval needed", reply)
        self.assertNotIn(str(request_id), ApprovalStore().pending(),
                         "the old approval was voided by the new revision")

    # -- caps clamp --------------------------------------------------------------------

    def test_within_caps_needs_approval_unless_channel_auto_opted_in(self):
        EbayEngine.first_draft_needs_info = False
        flow, _posts = self._flow()
        reply = flow.handle_message(self._photo_msg())
        self.assertIn("Publish approval needed", reply)
        self.assertIn("ebay_channel_auto_publish", reply)
        self.assertEqual(EbayEngine.publish_requests, [])

    def test_within_caps_auto_publishes_when_opted_in(self):
        EbayEngine.first_draft_needs_info = False
        flow, posts = self._flow(
            self._config(ebay_channel_auto_publish="true")
        )
        reply = flow.handle_message(self._photo_msg())
        self.assertIn("PUBLISHED", reply)
        self.assertEqual(len(EbayEngine.publish_requests), 1)
        self.assertEqual(ApprovalStore().pending(), {},
                         "no approval entry on the auto path")
        self.assertTrue(
            any("Within caps" in text for text, _c, _t in posts),
            "the auto decision and challenge are announced in-thread",
        )

    def test_caps_breach_requires_approval_even_with_auto_opt_in(self):
        EbayEngine.first_draft_needs_info = False
        flow, _posts = self._flow(self._config(
            ebay_channel_auto_publish="true",
            ebay_max_price_usd="10",  # the drafted 19.95 breaches this
        ))
        reply = flow.handle_message(self._photo_msg())
        self.assertIn("Publish approval needed", reply)
        self.assertIn("above the", reply)
        self.assertEqual(EbayEngine.publish_requests, [])

    # -- HITL (mid-run park + resume) -----------------------------------------------

    def test_midrun_clarification_parks_and_resumes(self):
        EbayEngine.first_draft_needs_info = False
        EbayEngine.hitl_on_first_draft = True
        flow, _posts = self._flow()
        parked = flow.handle_message(self._photo_msg())
        self.assertIn("Is the cover hardback?", parked)
        state = PilotState()
        session = state.session(state.thread_session("slack:100.0"))
        self.assertEqual(session["phase"], "hitl")
        self.assertEqual(session["hitl"]["input_kind"], "clarification")
        resumed = flow.handle_message(self._reply("hardback"))
        clarifications = [
            (skill, data) for skill, data, _env in FakeGateway.calls
            if skill == "submit_clarification_response"
        ]
        self.assertEqual(len(clarifications), 1)
        self.assertEqual(clarifications[0][1]["response"], "hardback")
        self.assertEqual(clarifications[0][1]["request_id"], "clar-1")
        self.assertIn("Publish approval needed", resumed)

    # -- publish failure (expired sandbox token path) ----------------------------------

    def test_publish_failure_surfaces_in_thread_and_rearms(self):
        EbayEngine.first_draft_needs_info = False
        EbayEngine.fail_publish_run = True
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            approval_text = loop.handle_inbound(self._photo_msg())
            request_id = self._approval_id(approval_text)
            failed = loop.handle_inbound(
                self._reply(f"approve {request_id}", ts="103.0")
            )
        self.assertIn("Publish failed", failed)
        self.assertIn("ended failed", failed)
        self.assertIn("re-approve", failed.lower())
        retry_id = self._approval_id(
            failed[failed.index("Publish approval needed"):]
        )
        self.assertNotEqual(retry_id, request_id)
        # After the operator fixes the upstream issue (e.g. mints a fresh
        # sandbox token), re-approving retries with a fresh gateway call
        # key while the embedded effect key stays the exact formula.
        EbayEngine.fail_publish_run = False
        with patch("sys.stderr", io.StringIO()):
            done = loop.handle_inbound(
                self._reply(f"approve {retry_id}", ts="104.0")
            )
        self.assertIn("PUBLISHED", done)
        self.assertEqual(len(EbayEngine.publish_requests), 2)
        first, second = EbayEngine.publish_requests
        self.assertEqual(first["idempotency_key"],
                         second["idempotency_key"],
                         "the contract effect key never varies")
        gateway_keys = [key for key in FakeGateway.idempotency
                        if ":publish" in key]
        self.assertEqual(len(gateway_keys), 2)
        self.assertTrue(any(key.endswith(":retry2") for key in gateway_keys))

    def test_gate_rejection_reports_zero_writes(self):
        EbayEngine.first_draft_needs_info = False
        EbayEngine.reject_publish = True
        loop, _sent = self._loop()
        with patch("sys.stderr", io.StringIO()):
            approval_text = loop.handle_inbound(self._photo_msg())
            request_id = self._approval_id(approval_text)
            rejected = loop.handle_inbound(
                self._reply(f"approve {request_id}", ts="103.0")
            )
        self.assertIn("rejected", rejected)
        self.assertIn("zero writes", rejected)

    # -- chat intake (FileParts) ---------------------------------------------------------

    def test_chat_intake_promotes_fileparts_and_publishes(self):
        EbayEngine.first_draft_needs_info = False
        FakeGateway.chat_script = chat_orchestrator
        loop, _sent = self._loop(self._config(ebay_intake="chat"))
        with patch("sys.stderr", io.StringIO()):
            approval_text = loop.handle_inbound(self._photo_msg())
            request_id = self._approval_id(approval_text)
            done = loop.handle_inbound(
                self._reply(f"approve {request_id}", ts="103.0")
            )
        self.assertIn("PUBLISHED", done)
        chat_calls = [
            (data, env) for skill, data, env in FakeGateway.calls
            if skill == "chat"
        ]
        self.assertEqual(len(chat_calls), 1)
        data, envelope = chat_calls[0]
        self.assertIn("sell this book", data["message"])
        parts = envelope["params"]["message"]["parts"]
        self.assertEqual(len(parts), 2)  # data part + one FilePart
        self.assertTrue(parts[1]["file"]["bytes"])
        # The publish echoed the server-bound identity, not conch config.
        publish = EbayEngine.publish_requests[0]
        self.assertEqual(publish["listing_session_id"], "ctx-fake-1")
        self.assertEqual(
            publish["idempotency_key"], "ebay-oversight:ctx-fake-1:r1:publish"
        )
        self.assertEqual(FakeGateway.uploads, {},
                         "no typed upload on the chat path")

    # -- fallthrough to the normal remote turn --------------------------------------------

    def test_photo_without_capitol_config_runs_normal_turn(self):
        config = self._config()
        for key in ("capitol_base_url", "capitol_org", "capitol_agent"):
            config.pop(key, None)
        loop, _sent = self._loop(config)

        def fake_chat_turn(config, provider, raw_fn, messages, *a, **kw):
            return "normal remote reply", {}

        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            reply = loop.handle_inbound(self._photo_msg())
        self.assertIn("normal remote reply", reply)
        self.assertEqual(EbayEngine.draft_requests, [])

    def test_command_approvals_still_work_alongside(self):
        loop, _sent = self._loop()
        request_id = loop.approvals.add(
            "echo channel-coexistence", "slack", "55.5", "U111"
        )
        with patch("sys.stderr", io.StringIO()), \
             patch("sys.stdout", io.StringIO()):
            reply = loop.handle_inbound(self._reply(
                f"approve {request_id}", thread="55.5", ts="56.0"
            ))
        self.assertIn("channel-coexistence", reply)


class _FakeConvManager:
    def __init__(self):
        self.saved = []
        self._convs = {}

    def create(self, model, provider):
        import types
        conv = types.SimpleNamespace(
            id=f"conv{len(self._convs) + 1}", title="", model=model,
            provider=provider, messages=[],
        )
        self._convs[conv.id] = conv
        return conv

    def load(self, conv_id):
        return self._convs.get(conv_id)

    def save(self, conv):
        self.saved.append(conv.id)


class ThreadBindingStateTests(unittest.TestCase):
    def test_bind_and_lookup_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = PilotState(Path(tmp) / "pilot.json")
            self.assertIsNone(state.thread_session("slack:1.0"))
            state.bind_thread("slack:1.0", "slack-abc")
            state.update_session("slack-abc", phase="clarify")
            self.assertEqual(state.thread_session("slack:1.0"), "slack-abc")
            # Bindings and sessions coexist in the same versioned file.
            reloaded = PilotState(Path(tmp) / "pilot.json")
            self.assertEqual(reloaded.thread_session("slack:1.0"), "slack-abc")
            self.assertEqual(reloaded.session("slack-abc")["phase"], "clarify")

    def test_corrupt_state_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.json"
            path.write_text("{broken")
            state = PilotState(path)
            self.assertIsNone(state.thread_session("slack:1.0"))


if __name__ == "__main__":
    unittest.main()
