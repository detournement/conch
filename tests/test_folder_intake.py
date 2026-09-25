"""Watched-folder eBay intake: grouping, validation, dedupe, safety."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.capitol.errors import CapitolError
from conch.capitol.folder_intake import (
    FOLDER_CHANNEL,
    FolderIntakeService,
    FolderListingFlow,
    drops_summary,
    ebay_flow_config,
    folder_state_path,
)

# A real 1x1 PNG (magic bytes + minimal chunks) — content-sniffable.
PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"IDAT" + b"\x00" * 8
)
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


class FakeFlow:
    def __init__(self):
        self.messages = []
        self.approvals_handled = []
        self.reply = "drafted r1"
        self.raise_error = None

        class _Approvals:
            def __init__(self):
                self._pending = {}

            def pending(self):
                return dict(self._pending)

            def consume(self, request_id, *, channel, thread_id, sender,
                        max_age=3600):
                entry = self._pending.get(str(request_id))
                if entry is None:
                    return None, "missing"
                if entry.get("channel") != channel:
                    return None, "origin_mismatch"
                del self._pending[str(request_id)]
                return entry, ""

        self.approvals = _Approvals()

    def handle_message(self, message):
        if self.raise_error:
            raise self.raise_error
        self.messages.append(message)
        return self.reply

    def handle_approval(self, request_id, entry, verb, message):
        self.approvals_handled.append((request_id, verb))
        return f"{verb} handled"


class FolderCase(unittest.TestCase):
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
            "capitol_base_url": "http://localhost:9",
            "capitol_org": "org", "capitol_agent": "agent",
            "ebay_watch_folder": str(self.watch),
            "ebay_watch_poll_seconds": "1",
            "ebay_watch_debounce_seconds": "5",
        }

    def clock(self):
        return self.now

    def service(self, config=None):
        svc = FolderIntakeService(
            FakeStore(), config or self.config,
            log=lambda *a, **k: None, clock=self.clock,
        )
        svc._flow = FakeFlow()
        return svc

    def drop(self, name, data, age=60.0):
        path = self.watch / name
        path.write_bytes(data)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def tick(self, svc):
        svc._last_poll = 0.0
        svc.tick({})


class TestGating(FolderCase):
    def test_disabled_without_watch_folder(self):
        config = dict(self.config)
        config.pop("ebay_watch_folder")
        svc = self.service(config)
        self.assertFalse(svc.enabled())
        self.drop("a.png", PNG)
        self.tick(svc)
        self.assertEqual(svc._flow.messages, [])

    def test_disabled_without_capitol(self):
        config = dict(self.config)
        config.pop("capitol_base_url")
        self.assertFalse(self.service(config).enabled())

    def test_ebay_agent_counts_as_agent(self):
        config = dict(self.config)
        config.pop("capitol_agent")
        config["ebay_agent"] = "ebay-agent"
        self.assertTrue(self.service(config).enabled())


class TestDrops(FolderCase):
    def test_drop_starts_one_session_with_notes(self):
        self.drop("front.png", PNG)
        self.drop("back.jpg", JPEG)
        self.drop("notes.txt", b"pristine, original box")
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(svc._flow.messages), 1)
        message = svc._flow.messages[0]
        self.assertEqual(message.channel, FOLDER_CHANNEL)
        self.assertEqual(len(message.attachments), 2)
        self.assertIn("pristine", message.text)
        # originals archived, not deleted
        processed = list((self.watch / "processed").rglob("*"))
        names = {p.name for p in processed if p.is_file()}
        self.assertIn("front.png", names)
        self.assertIn("notes.txt", names)
        self.assertEqual(
            [p for p in self.watch.iterdir() if p.is_file()], []
        )

    def test_debounce_waits_for_quiet(self):
        self.drop("front.png", PNG, age=1.0)  # newer than debounce
        svc = self.service()
        self.tick(svc)
        self.assertEqual(svc._flow.messages, [])
        self.now += 10
        self.tick(svc)
        self.assertEqual(len(svc._flow.messages), 1)

    def test_notes_alone_do_not_start(self):
        self.drop("notes.txt", b"just words")
        svc = self.service()
        self.tick(svc)
        self.assertEqual(svc._flow.messages, [])

    def test_validation_rejects_bad_files(self):
        self.drop("fake.jpg", b"not an image at all" * 3)
        self.drop("huge.png", PNG + b"\x00" * (12 * 1024 * 1024))
        self.drop("weird.pdf", b"%PDF-1.4")
        self.drop("real.png", PNG)
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(svc._flow.messages), 1)
        self.assertEqual(len(svc._flow.messages[0].attachments), 1)
        rejected = {p.name for p in (self.watch / "rejected").iterdir()}
        self.assertIn("fake.jpg", rejected)
        self.assertIn("huge.png", rejected)
        self.assertIn("weird.pdf", rejected)
        self.assertIn("fake.jpg.reason.txt", rejected)

    def test_duplicate_drop_never_starts_second_session(self):
        self.drop("a.png", PNG)
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(svc._flow.messages), 1)
        # identical content re-dropped after a "restart" (new instance,
        # same state file)
        self.drop("a-again.png", PNG)
        svc2 = self.service()
        self.tick(svc2)
        self.assertEqual(svc2._flow.messages, [])
        self.assertTrue(folder_state_path().exists())

    def test_flow_error_is_notified_not_raised(self):
        self.drop("a.png", PNG)
        svc = self.service()
        svc._flow.raise_error = CapitolError("gateway down")
        self.tick(svc)  # must not raise
        drops = drops_summary()
        self.assertEqual(len(drops), 1)
        texts = " ".join(i["text"] for i in drops[0]["history"])
        self.assertIn("gateway down", texts)


class TestContinuation(FolderCase):
    def test_answer_routes_to_flow(self):
        svc = self.service()
        svc.store.inbox_rows = [{
            "inbox_id": 7, "source": "ebay_folder",
            "payload": {"verb": "answer", "drop": "drop-abc",
                        "text": "size 11, brown"},
        }]
        self.tick(svc)
        self.assertEqual(len(svc._flow.messages), 1)
        self.assertEqual(svc._flow.messages[0].thread_id, "drop-abc")
        self.assertIn("size 11", svc._flow.messages[0].text)
        # consumed exactly once
        self.tick(svc)
        self.assertEqual(len(svc._flow.messages), 1)

    def test_approve_consumes_folder_origin_only(self):
        svc = self.service()
        svc._flow.approvals._pending["4"] = {
            "channel": "slack", "thread_id": "T1", "kind": "ebay_publish",
        }
        svc.store.inbox_rows = [{
            "inbox_id": 1, "source": "ebay_folder",
            "payload": {"verb": "approve", "request_id": 4},
        }]
        self.tick(svc)
        self.assertEqual(svc._flow.approvals_handled, [])
        self.assertIn("4", svc._flow.approvals._pending)

        svc2 = self.service()
        svc2._flow.approvals._pending["5"] = {
            "channel": FOLDER_CHANNEL, "thread_id": "drop-abc",
            "kind": "ebay_publish",
        }
        svc2.store.inbox_rows = [{
            "inbox_id": 2, "source": "ebay_folder",
            "payload": {"verb": "approve", "request_id": 5},
        }]
        self.tick(svc2)
        self.assertEqual(svc2._flow.approvals_handled, [(5, "approve")])


class TestFlowConfig(unittest.TestCase):
    def test_agent_override(self):
        config = {"capitol_agent": "default", "ebay_agent": "ebay"}
        out = ebay_flow_config(config)
        self.assertEqual(out["capitol_agent"], "ebay")
        self.assertEqual(config["capitol_agent"], "default")  # unmutated

    def test_no_override_without_key(self):
        out = ebay_flow_config({"capitol_agent": "default"})
        self.assertEqual(out["capitol_agent"], "default")


class TestFolderFlowSafety(unittest.TestCase):
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

    def test_auto_publish_is_structurally_off(self):
        from conch.remote import ApprovalStore

        flow = FolderListingFlow(
            {"capitol_base_url": "http://localhost:9",
             "capitol_org": "o", "capitol_agent": "a",
             "ebay_channel_auto_publish": "true"},  # config says yes …
            ApprovalStore(), lambda text, channel, thread: None,
        )
        # … the folder surface still refuses: a drop is not consent.
        self.assertEqual(
            flow.config.get("ebay_channel_auto_publish"), "false"
        )
        self.assertEqual(flow.channel, FOLDER_CHANNEL)
        self.assertTrue(flow.enabled())

    def test_flow_applies_ebay_agent(self):
        from conch.remote import ApprovalStore

        flow = FolderListingFlow(
            {"capitol_base_url": "http://localhost:9",
             "capitol_org": "o", "capitol_agent": "default",
             "ebay_agent": "ebay-operator"},
            ApprovalStore(), lambda text, channel, thread: None,
        )
        self.assertEqual(flow.config["capitol_agent"], "ebay-operator")


if __name__ == "__main__":
    unittest.main()
