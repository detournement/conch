"""Status exporter: gating, sanitization, change-driven push, tolerance."""

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from conch.capitol.status_export import StatusExporterService, build_snapshot


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "HOME": str(self.root / "home"),
            "CONCH_STATUS_WRITE_TOKEN": "test-write-token",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "home").mkdir()
        self.now = 5_000_000.0
        self.posts = []
        self.post_status = 200

    def clock(self):
        return self.now

    def service(self, config=None):
        svc = StatusExporterService(
            object(), config if config is not None else {
                "status_page_url": "https://status.example/",
            },
            log=self._log, clock=self.clock,
        )
        self.logged = []
        svc._post = self._recording_post
        return svc

    def _log(self, message):
        getattr(self, "logged", []).append(str(message))

    def _recording_post(self, url, headers, body):
        self.posts.append((url, headers, body))
        return self.post_status

    def seed_session(self, title="Vintage compass", price="45.00"):
        from conch.capitol.ebay import PilotState

        state = PilotState()
        state.update_session(
            "folder-abc123", phase="awaiting_approval", channel="folder",
            thread_id="drop-abc", revision={
                "title": title, "price": price, "category": "550",
                "revision": 1,
            },
        )

    def tick(self, svc):
        svc._last_push = 0.0
        svc.tick({})


class TestGating(ExportCase):
    def test_off_without_url(self):
        svc = self.service({})
        self.assertFalse(svc.enabled())
        self.tick(svc)
        self.assertEqual(self.posts, [])

    def test_off_without_token(self):
        with patch.dict(os.environ, {"CONCH_STATUS_WRITE_TOKEN": ""}):
            svc = self.service()
            self.assertFalse(svc.enabled())


class TestSnapshot(ExportCase):
    def test_allowlisted_fields_only(self):
        self.seed_session()
        snapshot = build_snapshot({}, now=self.now)
        self.assertEqual(snapshot["schema"], "conch.status.v1")
        listing = snapshot["listings"][0]
        self.assertEqual(
            sorted(listing),
            ["listing_id", "listing_url", "phase", "revision",
             "session_id", "surface", "updated_at"],
        )
        self.assertEqual(listing["revision"]["title"], "Vintage compass")
        self.assertEqual(listing["phase"], "awaiting_approval")

    def test_push_and_change_detection(self):
        self.seed_session()
        svc = self.service()
        self.tick(svc)
        self.assertEqual(len(self.posts), 1)
        url, headers, body = self.posts[0]
        self.assertEqual(url, "https://status.example/api/ingest")
        self.assertEqual(
            headers["Authorization"], "Bearer test-write-token"
        )
        payload = json.loads(body)
        self.assertEqual(payload["schema"], "conch.status.v1")
        # unchanged snapshot: no second post
        self.tick(svc)
        self.assertEqual(len(self.posts), 1)
        # time alone is not a change (generated_at excluded from digest)
        self.now += 60
        self.tick(svc)
        self.assertEqual(len(self.posts), 1)
        # a real change pushes again
        self.now += 60
        self.seed_session(title="Vintage compass — brass")
        self.tick(svc)
        self.assertEqual(len(self.posts), 2)

    def test_credential_shaped_snapshot_is_refused_whole(self):
        self.seed_session(
            title="ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        )
        svc = self.service()
        self.tick(svc)
        self.assertEqual(self.posts, [])
        self.assertTrue(
            any("REFUSED" in line for line in self.logged)
        )


class TestTolerance(ExportCase):
    def test_network_failure_logged_never_raised(self):
        self.seed_session()
        svc = self.service()

        def boom(url, headers, body):
            raise urllib.error.URLError("connection refused")

        svc._post = boom
        self.tick(svc)  # must not raise
        self.assertTrue(
            any("push failed" in line for line in self.logged)
        )
        # recovery: next tick with a working poster sends
        svc._post = self._recording_post
        self.now += 60
        self.tick(svc)
        self.assertEqual(len(self.posts), 1)

    def test_rejected_status_logged(self):
        self.seed_session()
        self.post_status = 401
        svc = self.service()
        self.tick(svc)
        self.assertTrue(
            any("rejected: HTTP 401" in line for line in self.logged)
        )
        # digest not recorded on failure: retried after interval
        self.post_status = 200
        self.now += 60
        self.tick(svc)
        self.assertEqual(len(self.posts), 2)


if __name__ == "__main__":
    unittest.main()
