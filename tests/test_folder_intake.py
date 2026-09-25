"""Generic folder-watch machinery + the pack/mission handler bindings."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.capitol.errors import CapitolError
from conch.capitol.folder_intake import (
    FOLDER_CHANNEL,
    PackFolderFlow,
    PackFolderHandler,
    drops_summary,
    ebay_flow_config,
)
from conch.kernel.folderwatch import (
    FolderWatchService,
    parse_watches,
    watch_state_path,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"IDAT" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class FakeStore:
    def __init__(self):
        self.outbox = []
        self.inbox_rows = []

    def enqueue_outbox(self, kind, payload, dedupe_key, mission_id=""):
        if any(entry[2] == dedupe_key for entry in self.outbox):
            return False
        self.outbox.append((kind, payload, dedupe_key))
        return True

    def list_inbox(self, source="", limit=500, since=0.0):
        return [row for row in self.inbox_rows
                if not source or row["source"] == source][:limit]


class FakeEngine:
    def __init__(self):
        self.delivered = []

    def deliver_event(self, source, key, payload, mission_id="",
                      wake=True):
        self.delivered.append((source, key, payload, mission_id, wake))
        return {"inbox_id": len(self.delivered), "duplicate": False,
                "woken": wake}


class FakeHandler:
    def __init__(self, accepts=None):
        self.drops = []
        self.verbs = []
        self._accepts = accepts or {}
        self.reply = "handled"
        self.raise_error = None

    def accepts(self):
        return dict(self._accepts)

    def handle_drop(self, watch, drop_id, attachments, notes, notify):
        if self.raise_error:
            raise self.raise_error
        self.drops.append((watch, drop_id, attachments, notes))
        return self.reply

    def handle_verb(self, payload, notify):
        self.verbs.append(payload)
        return f"verb {payload.get('verb')} ok"


class WatchCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "HOME": str(self.root / "home"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "home").mkdir()
        self.watch = self.root / "drop"
        self.watch.mkdir()
        self.now = 1_000_000.0
        self.config = {
            "folder_watch_test": str(self.watch),
            "folder_watch_test_handler": "fake:x",
            "folder_watch_test_poll": "1",
            "folder_watch_test_debounce": "5",
        }

    def clock(self):
        return self.now

    def service(self, config=None, engine=None):
        svc = FolderWatchService(
            engine or FakeEngine(), FakeStore(),
            config or self.config, log=lambda *a, **k: None,
            clock=self.clock,
        )
        self.handler = FakeHandler()
        svc._handlers["test"] = self.handler
        return svc

    def drop(self, name, data, age=60.0):
        path = self.watch / name
        path.write_bytes(data)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def tick(self, svc):
        svc._last_poll = {}
        svc.tick({})


class TestConfigParsing(unittest.TestCase):
    def test_parse_watches(self):
        watches = parse_watches({
            "folder_watch_ebay": "~/EbayDrop",
            "folder_watch_ebay_handler": "pack:ebay-listing",
            "folder_watch_ebay_debounce": "12",
            "folder_watch_inbox": "~/Inbox",
            "folder_watch_inbox_handler": "mission:msn-1",
            "unrelated_key": "x",
        })
        self.assertEqual(sorted(watches), ["ebay", "inbox"])
        self.assertEqual(watches["ebay"]["handler"], "pack:ebay-listing")
        self.assertEqual(watches["ebay"]["debounce"], 12.0)
        self.assertEqual(watches["inbox"]["handler"], "mission:msn-1")

    def test_no_watches_no_work(self):
        self.assertEqual(parse_watches({"other": "1"}), {})


class TestGenericMachinery(WatchCase):
    def test_drop_grouped_with_notes(self):
        self.drop("front.png", PNG)
        self.drop("back.jpg", JPEG)
        self.drop("notes.txt", b"pristine, original box")
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(self.handler.drops), 1)
        watch, drop_id, attachments, notes = self.handler.drops[0]
        self.assertEqual(watch, "test")
        self.assertEqual(len(attachments), 2)
        self.assertIn("pristine", notes)
        names = {p.name for p in (self.watch / "processed").rglob("*")
                 if p.is_file()}
        self.assertIn("front.png", names)
        self.assertIn("notes.txt", names)
        self.assertEqual(
            [p for p in self.watch.iterdir() if p.is_file()], []
        )

    def test_debounce_waits_for_quiet(self):
        self.drop("front.png", PNG, age=1.0)
        svc = self.service()
        self.tick(svc)
        self.assertEqual(self.handler.drops, [])
        self.now += 10
        self.tick(svc)
        self.assertEqual(len(self.handler.drops), 1)

    def test_validation_rejects_with_reason_files(self):
        self.drop("fake.jpg", b"definitely not an image bytes")
        self.drop("huge.png", PNG + b"\x00" * (12 * 1024 * 1024))
        self.drop("weird.pdf", b"%PDF-1.4")
        self.drop("real.png", PNG)
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(self.handler.drops), 1)
        self.assertEqual(len(self.handler.drops[0][2]), 1)
        rejected = {p.name for p in (self.watch / "rejected").iterdir()}
        self.assertIn("fake.jpg", rejected)
        self.assertIn("huge.png", rejected)
        self.assertIn("weird.pdf", rejected)
        self.assertIn("fake.jpg.reason.txt", rejected)

    def test_dedupe_survives_restart(self):
        self.drop("a.png", PNG)
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(self.handler.drops), 1)
        self.drop("a-again.png", PNG)  # identical content
        svc2 = self.service()  # fresh instance = restart
        self.tick(svc2)
        self.assertEqual(self.handler.drops, [])
        self.assertTrue(watch_state_path().exists())

    def test_handler_error_never_raises(self):
        self.drop("a.png", PNG)
        svc = self.service()
        self.handler.raise_error = RuntimeError("boom")
        self.tick(svc)  # must not raise
        state_drops = drops_summary()
        self.assertEqual(len(state_drops), 1)
        texts = " ".join(i["text"] for i in state_drops[0]["history"])
        self.assertIn("boom", texts)

    def test_unknown_handler_kind_disables_watch(self):
        config = dict(self.config,
                      folder_watch_test_handler="nosuch:thing")
        svc = FolderWatchService(
            FakeEngine(), FakeStore(), config,
            log=lambda *a, **k: None, clock=self.clock,
        )
        self.drop("a.png", PNG)
        svc._last_poll = {}
        svc.tick({})  # no handler → no processing, no crash
        self.assertTrue((self.watch / "a.png").exists())

    def test_verbs_route_to_handler_once(self):
        svc = self.service()
        svc.store.inbox_rows = [{
            "inbox_id": 3, "source": "folder_watch",
            "payload": {"verb": "answer", "watch": "test",
                        "drop": "drop-x", "text": "brown, size 11"},
        }]
        self.tick(svc)
        self.assertEqual(len(self.handler.verbs), 1)
        self.tick(svc)  # consumed exactly once
        self.assertEqual(len(self.handler.verbs), 1)


class TestMissionBinding(WatchCase):
    def test_drop_becomes_mission_event(self):
        config = {
            "folder_watch_docs": str(self.watch),
            "folder_watch_docs_handler": "mission:msn-42",
            "folder_watch_docs_poll": "1",
            "folder_watch_docs_debounce": "5",
        }
        engine = FakeEngine()
        svc = FolderWatchService(
            engine, FakeStore(), config,
            log=lambda *a, **k: None, clock=self.clock,
        )
        self.drop("scan.png", PNG)
        svc._last_poll = {}
        svc.tick({})
        self.assertEqual(len(engine.delivered), 1)
        source, key, payload, mission_id, wake = engine.delivered[0]
        self.assertEqual(source, "folder_watch")
        self.assertEqual(mission_id, "msn-42")
        self.assertTrue(wake)
        self.assertEqual(payload["kind"], "folder_drop")
        self.assertEqual(len(payload["files"]), 1)
        self.assertNotIn("data", payload["files"][0])  # paths, not bytes


class TestPackBinding(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(root / "state"),
            "HOME": str(root / "home"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (root / "home").mkdir()
        self.config = {
            "capitol_base_url": "http://localhost:9",
            "capitol_org": "o", "capitol_agent": "a",
        }

    def handler(self, config=None):
        return PackFolderHandler(
            "ebay-listing", FakeStore(), config or self.config,
            log=lambda *a, **k: None,
        )

    def test_accepts_from_pack_spec(self):
        accepts = self.handler().accepts()
        self.assertIn(".jpg", accepts["extensions"])
        self.assertEqual(accepts["max_count"], 12)
        self.assertTrue(accepts["magic"])
        self.assertTrue(accepts["notes_sidecar"])

    def test_auto_publish_structurally_off(self):
        from conch.remote import ApprovalStore
        from conch.capitol.packs import load_pack

        flow = PackFolderFlow(
            load_pack("ebay-listing"),
            dict(self.config, ebay_channel_auto_publish="true"),
            ApprovalStore(), lambda text, channel, thread: None,
        )
        self.assertEqual(
            flow.config.get("ebay_channel_auto_publish"), "false"
        )
        self.assertEqual(flow.channel, FOLDER_CHANNEL)
        self.assertTrue(flow.enabled())
        # the watched_folder intake's context default applies
        self.assertIn("folder drop", flow._context_default())

    def test_flow_applies_ebay_agent(self):
        from conch.remote import ApprovalStore
        from conch.capitol.packs import load_pack

        flow = PackFolderFlow(
            load_pack("ebay-listing"),
            dict(self.config, ebay_agent="ebay-operator"),
            ApprovalStore(), lambda text, channel, thread: None,
        )
        self.assertEqual(flow.config["capitol_agent"], "ebay-operator")

    def test_drop_routes_to_flow(self):
        handler = self.handler()

        class FakeFlow:
            def __init__(self):
                self.messages = []

            def enabled(self):
                return True

            def handle_message(self, message):
                self.messages.append(message)
                return "drafting r1"

        handler._flow = FakeFlow()
        reply = handler.handle_drop(
            "ebay", "drop-1", [], "notes", lambda text: None
        )
        self.assertEqual(reply, "drafting r1")
        message = handler._flow.messages[0]
        self.assertEqual(message.channel, FOLDER_CHANNEL)
        self.assertEqual(message.thread_id, "drop-1")

    def test_capitol_error_becomes_reply(self):
        handler = self.handler()

        class FailingFlow:
            def enabled(self):
                return True

            def handle_message(self, message):
                raise CapitolError("gateway down")

        handler._flow = FailingFlow()
        reply = handler.handle_drop(
            "ebay", "drop-1", [], "", lambda text: None
        )
        self.assertIn("gateway down", reply)

    def test_approve_refuses_foreign_origin(self):
        handler = self.handler()

        class Approvals:
            def __init__(self):
                self._pending = {"4": {"channel": "slack",
                                       "thread_id": "T1"}}

            def pending(self):
                return dict(self._pending)

            def consume(self, *a, **k):
                raise AssertionError("must not consume foreign origin")

        class FlowStub:
            approvals = Approvals()

        handler._flow = FlowStub()
        reply = handler.handle_verb(
            {"verb": "approve", "request_id": 4}, lambda text: None
        )
        self.assertIn("another surface", reply)

    def test_approve_consumes_folder_origin(self):
        handler = self.handler()
        consumed = []

        class Approvals:
            def __init__(self):
                self._pending = {"5": {"channel": FOLDER_CHANNEL,
                                       "thread_id": "drop-abc",
                                       "sender": "folder:local"}}

            def pending(self):
                return dict(self._pending)

            def consume(self, request_id, *, channel, thread_id,
                        sender, max_age=3600):
                consumed.append((request_id, channel))
                return self._pending.pop(str(request_id)), ""

        class FlowStub:
            approvals = Approvals()

            def handle_approval(self, request_id, entry, verb, message):
                return f"{verb} handled for {message.thread_id}"

        handler._flow = FlowStub()
        reply = handler.handle_verb(
            {"verb": "approve", "request_id": 5}, lambda text: None
        )
        self.assertEqual(consumed, [(5, FOLDER_CHANNEL)])
        self.assertIn("approve handled for drop-abc", reply)


class TestFlowConfig(unittest.TestCase):
    def test_agent_override(self):
        config = {"capitol_agent": "default", "ebay_agent": "ebay"}
        out = ebay_flow_config(config)
        self.assertEqual(out["capitol_agent"], "ebay")
        self.assertEqual(config["capitol_agent"], "default")

    def test_no_override_without_key(self):
        out = ebay_flow_config({"capitol_agent": "default"})
        self.assertEqual(out["capitol_agent"], "default")


if __name__ == "__main__":
    unittest.main()
