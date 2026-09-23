"""Browser-capture native messaging host (capture plan, browser
satellite).

Proven here: the 4-byte-length JSON framing round-trips and fails
closed on truncation/oversize/non-objects; event validation rejects
unknown versions, kinds, origins, and detail fields (submit events
carry field NAMES only — non-string entries and secret-named fields
never pass); the authoritative secretguard scrub rejects
credential-shaped events whole; accepted events reach the kernel over
the control socket (real daemon), fall back to the direct kernel store
when no daemon answers, and spool bounded (oldest dropped) when neither
works — the browser is never blocked; the host refuses unexpected
caller extensions before reading a single event; everything is inert
until capture_enabled + capture_browser are both set; and the
native-host manifest installer writes per-browser manifests with the
pinned extension id.
"""

import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.kernel.browser_capture import (
    EXTENSION_ID,
    HOST_PROTOCOL_VERSION,
    MAX_MESSAGE_BYTES,
    NATIVE_HOST_NAME,
    BrowserEventRejected,
    KernelPoster,
    drain_spool,
    event_key,
    install_native_host,
    read_message,
    read_status,
    run_host,
    scrub_event,
    spool_append,
    spool_path,
    validate_event,
    write_message,
)
from conch.secretguard import CredentialRejected

ENABLED = {"capture_enabled": "true", "capture_browser": "true"}


def frame(obj) -> bytes:
    raw = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def click_message(**overrides):
    message = {
        "type": "event", "v": HOST_PROTOCOL_VERSION,
        "origin": "https://github.com", "ts": 1_800_000_000.5,
        "kind": "click",
        "detail": {"role": "button", "label": "Merge pull request"},
        "ext_version": "0.1.0",
    }
    message.update(overrides)
    return message


def hello_message():
    return {"type": "hello", "v": HOST_PROTOCOL_VERSION,
            "ext_version": "0.1.0"}


def replies(stdout: io.BytesIO):
    stdout.seek(0)
    out = []
    while True:
        message = read_message(stdout)
        if message is None:
            return out
        out.append(message)


class RecordingPoster:
    def __init__(self, fail=False):
        self.posted = []
        self.fail = fail
        self.closed = False

    def post(self, key, payload):
        if self.fail:
            raise OSError("injected delivery failure")
        self.posted.append((key, payload))
        return "socket"

    def close(self):
        self.closed = True


class IsolatedCase(unittest.TestCase):
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

    def run_host(self, messages, config=None, poster=None, caller=""):
        stdin = io.BytesIO(b"".join(frame(m) for m in messages))
        stdout = io.BytesIO()
        code = run_host(
            dict(ENABLED if config is None else config),
            stdin=stdin, stdout=stdout,
            poster=poster if poster is not None else RecordingPoster(),
            caller=caller,
        )
        return code, replies(stdout)


class TestFraming(unittest.TestCase):
    def test_round_trip(self):
        stream = io.BytesIO()
        write_message(stream, {"a": 1, "b": "x"})
        stream.seek(0)
        self.assertEqual(read_message(stream), {"a": 1, "b": "x"})
        self.assertIsNone(read_message(stream))  # clean EOF

    def test_truncation_fails_closed(self):
        with self.assertRaises(BrowserEventRejected):
            read_message(io.BytesIO(b"\x02"))  # short header
        with self.assertRaises(BrowserEventRejected):
            read_message(io.BytesIO(struct.pack("<I", 10) + b"{}"))

    def test_oversize_fails_closed(self):
        header = struct.pack("<I", MAX_MESSAGE_BYTES + 1)
        with self.assertRaises(BrowserEventRejected):
            read_message(io.BytesIO(header + b"x"))

    def test_non_object_fails_closed(self):
        with self.assertRaises(BrowserEventRejected):
            read_message(io.BytesIO(frame([1, 2, 3])))
        with self.assertRaises(BrowserEventRejected):
            raw = b"not json"
            read_message(
                io.BytesIO(struct.pack("<I", len(raw)) + raw)
            )


class TestValidation(unittest.TestCase):
    def test_good_click_normalizes(self):
        payload = validate_event(click_message())
        self.assertEqual(payload["origin"], "https://github.com")
        self.assertEqual(payload["kind"], "click")
        self.assertEqual(payload["detail"]["label"],
                         "Merge pull request")
        self.assertEqual(payload["ext_version"], "0.1.0")

    def test_version_mismatch_fails_closed(self):
        with self.assertRaises(BrowserEventRejected):
            validate_event(click_message(v=2))
        with self.assertRaises(BrowserEventRejected):
            validate_event(click_message(v=None))

    def test_unknown_kind_fails_closed(self):
        with self.assertRaises(BrowserEventRejected):
            validate_event(click_message(kind="keypress"))

    def test_non_http_origin_fails_closed(self):
        for origin in ("ftp://x.test", "chrome://settings", "", None,
                       "javascript:alert(1)"):
            with self.assertRaises(BrowserEventRejected):
                validate_event(click_message(origin=origin))

    def test_unknown_detail_field_fails_closed(self):
        message = click_message(
            detail={"role": "button", "label": "ok", "value": "hunter2"}
        )
        with self.assertRaises(BrowserEventRejected):
            validate_event(message)

    def test_submit_names_only(self):
        message = click_message(
            kind="submit",
            detail={"form": "signup",
                    "fields": ["email", "username", "password",
                               "csrf_token", "notes"]},
        )
        payload = validate_event(message)
        # secret-named fields dropped, the rest kept in order
        self.assertEqual(payload["detail"]["fields"],
                         ["email", "username", "notes"])

    def test_submit_value_smuggling_fails_closed(self):
        message = click_message(
            kind="submit",
            detail={"form": "f",
                    "fields": ["email", {"name": "x", "value": "y"}]},
        )
        with self.assertRaises(BrowserEventRejected):
            validate_event(message)

    def test_submit_field_flood_fails_closed(self):
        message = click_message(
            kind="submit",
            detail={"form": "f",
                    "fields": [f"field{i}" for i in range(41)]},
        )
        with self.assertRaises(BrowserEventRejected):
            validate_event(message)

    def test_nav_path_bare_only(self):
        good = validate_event(click_message(
            kind="nav", detail={"path": "/pulls"},
        ))
        self.assertEqual(good["detail"]["path"], "/pulls")
        with self.assertRaises(BrowserEventRejected):
            validate_event(click_message(
                kind="nav", detail={"path": "https://evil.test/x"},
            ))

    def test_timestamp_required(self):
        with self.assertRaises(BrowserEventRejected):
            validate_event(click_message(ts=0))
        with self.assertRaises(BrowserEventRejected):
            validate_event(click_message(ts="not-a-number"))

    def test_labels_clipped(self):
        payload = validate_event(click_message(
            detail={"role": "button", "label": "x" * 500},
        ))
        self.assertLessEqual(len(payload["detail"]["label"]), 120)


class TestScrubAndKeys(unittest.TestCase):
    def test_credential_shaped_event_rejected_whole(self):
        payload = validate_event(click_message(
            detail={"role": "input",
                    "label": "aws AKIAIOSFODNN7EXAMPLE"},
        ))
        with self.assertRaises(CredentialRejected):
            scrub_event(payload)

    def test_clean_event_passes(self):
        payload = validate_event(click_message())
        self.assertIs(scrub_event(payload), payload)

    def test_event_key_deterministic(self):
        a = validate_event(click_message())
        b = validate_event(click_message())
        self.assertEqual(event_key(a), event_key(b))
        c = validate_event(click_message(
            detail={"role": "button", "label": "Close"},
        ))
        self.assertNotEqual(event_key(a), event_key(c))
        self.assertTrue(event_key(a).startswith("browser:github.com:"))


class TestSpool(IsolatedCase):
    def test_bounded_oldest_dropped(self):
        for index in range(7):
            dropped = spool_append(f"k{index}", {"n": index}, limit=5)
        self.assertEqual(dropped, 1)  # the last append dropped one
        lines = spool_path().read_text().splitlines()
        self.assertEqual(len(lines), 5)
        first = json.loads(lines[0])
        self.assertEqual(first["key"], "k2")  # k0/k1 gone

    def test_drain_stops_at_first_failure(self):
        for index in range(3):
            spool_append(f"k{index}", {"n": index})
        calls = []

        def post(key, payload):
            if key == "k1":
                raise OSError("still down")
            calls.append(key)

        self.assertEqual(drain_spool(post), 1)
        self.assertEqual(calls, ["k0"])
        # k1 and k2 remain for the next drain
        remaining = [json.loads(line)["key"]
                     for line in spool_path().read_text().splitlines()]
        self.assertEqual(remaining, ["k1", "k2"])
        self.assertEqual(drain_spool(lambda k, p: None), 2)
        self.assertEqual(spool_path().read_text(), "")


class TestHostLoop(IsolatedCase):
    def test_hello_handshake_and_status_file(self):
        poster = RecordingPoster()
        code, out = self.run_host([hello_message()], poster=poster)
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["type"], "hello")
        self.assertTrue(out[0]["ok"])
        self.assertTrue(out[0]["capture"])
        self.assertFalse(out[0]["daemon"])
        status = read_status()
        self.assertTrue(status["capture"])
        self.assertEqual(status["ext_version"], "0.1.0")
        self.assertIn("last_handshake", status)

    def test_disabled_rejects_everything(self):
        poster = RecordingPoster()
        code, out = self.run_host(
            [hello_message(), click_message()],
            config={}, poster=poster,
        )
        self.assertEqual(code, 0)
        self.assertFalse(out[0]["capture"])
        self.assertFalse(out[1]["ok"])
        self.assertIn("disabled", out[1]["reason"])
        self.assertEqual(poster.posted, [])
        # capture_enabled alone is not enough — capture_browser gates too
        _code, out = self.run_host(
            [click_message()],
            config={"capture_enabled": "true"}, poster=poster,
        )
        self.assertFalse(out[0]["ok"])
        self.assertEqual(poster.posted, [])

    def test_accepted_event_posts_and_acks(self):
        poster = RecordingPoster()
        code, out = self.run_host(
            [click_message(), click_message(kind="copy",
                                            detail={"role": "pre",
                                                    "label": "log"})],
            poster=poster,
        )
        self.assertEqual(code, 0)
        self.assertEqual([m["ok"] for m in out], [True, True])
        self.assertEqual(len(poster.posted), 2)
        key, payload = poster.posted[0]
        self.assertTrue(key.startswith("browser:github.com:"))
        self.assertEqual(payload["kind"], "click")

    def test_invalid_and_credential_events_ack_with_reason(self):
        poster = RecordingPoster()
        _code, out = self.run_host([
            click_message(kind="keypress"),
            click_message(detail={"role": "input",
                                  "label": "AKIAIOSFODNN7EXAMPLE"}),
            {"type": "mystery"},
        ], poster=poster)
        self.assertEqual([m["ok"] for m in out],
                         [False, False, False])
        self.assertIn("kind", out[0]["reason"])
        self.assertIn("credential", out[1]["reason"])
        # the reason names the pattern label, never the bytes
        self.assertNotIn("AKIA", out[1]["reason"])
        self.assertIn("unknown message type", out[2]["reason"])
        self.assertEqual(poster.posted, [])

    def test_delivery_failure_spools_never_blocks(self):
        poster = RecordingPoster(fail=True)
        _code, out = self.run_host(
            [click_message(), click_message(ts=1_800_000_001.0)],
            poster=poster,
        )
        self.assertEqual([m["via"] for m in out], ["spool", "spool"])
        self.assertEqual(
            len(spool_path().read_text().splitlines()), 2
        )
        # recovery: the next hello drains the spool through the poster
        working = RecordingPoster()
        _code, out = self.run_host([hello_message()], poster=working)
        self.assertEqual(out[0]["drained"], 2)
        self.assertEqual(len(working.posted), 2)
        self.assertEqual(spool_path().read_text(), "")

    def test_unexpected_caller_refused_before_reading(self):
        poster = RecordingPoster()
        code, out = self.run_host(
            [hello_message(), click_message()],
            poster=poster,
            caller="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, [])  # no negotiation with strangers
        self.assertEqual(poster.posted, [])

    def test_pinned_caller_accepted(self):
        poster = RecordingPoster()
        code, out = self.run_host(
            [click_message()],
            poster=poster,
            caller=f"chrome-extension://{EXTENSION_ID}/",
        )
        self.assertEqual(code, 0)
        self.assertTrue(out[0]["ok"])


class TestKernelDelivery(IsolatedCase):
    def test_direct_store_fallback_when_no_daemon(self):
        poster = KernelPoster()
        self.addCleanup(poster.close)
        payload = validate_event(click_message())
        via = poster.post(event_key(payload), payload)
        self.assertEqual(via, "direct")
        from conch.kernel.store import MissionStore

        store = MissionStore()
        self.addCleanup(store.close)
        rows = store.list_inbox(source="browser")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["kind"], "click")
        self.assertEqual(rows[0]["payload"]["origin"],
                         "https://github.com")

    def test_socket_delivery_through_real_daemon(self):
        from conch.kernel.daemon import EdgeDaemon

        socket_path = self.root / "run" / "edge.sock"
        daemon = EdgeDaemon(
            {"provider": "openai", "mission_reviews": "false",
             "mission_consolidation": "false"},
            kernel_dir=self.root / "kernel",
            state_dir=self.root,
            socket_path=socket_path,
            session_factory=lambda *args: ("ok", {}),
        )
        self.addCleanup(daemon.shutdown)
        daemon.start()
        poster = KernelPoster(socket_path=socket_path)
        self.addCleanup(poster.close)
        code, out = self.run_host(
            [click_message()], poster=poster,
        )
        self.assertEqual(code, 0)
        self.assertEqual(out[0]["via"], "socket")
        rows = daemon.store.list_inbox(source="browser")
        self.assertEqual(len(rows), 1)
        self.assertTrue(
            rows[0]["idempotency_key"].startswith("browser:github.com:")
        )
        # idempotency: the same event re-sent lands once
        _code, _out = self.run_host([click_message()], poster=poster)
        self.assertEqual(
            len(daemon.store.list_inbox(source="browser")), 1
        )


class TestManifestInstall(IsolatedCase):
    def test_writes_for_installed_browsers_only(self):
        home = Path(os.environ["HOME"])
        import sys as _sys

        if _sys.platform == "darwin":
            chrome_dir = (home / "Library" / "Application Support"
                          / "Google" / "Chrome")
        else:
            chrome_dir = (Path(os.environ["XDG_CONFIG_HOME"])
                          / "google-chrome")
        chrome_dir.mkdir(parents=True)
        with patch(
            "conch.kernel.browser_capture.host_command_path",
            return_value="/opt/conch/bin/conch-capture-host",
        ):
            outcome = install_native_host()
        self.assertEqual(list(outcome["written"]), ["Chrome"])
        self.assertIn("Brave", outcome["skipped"])
        manifest = json.loads(
            Path(outcome["written"]["Chrome"]).read_text()
        )
        self.assertEqual(manifest["name"], NATIVE_HOST_NAME)
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(
            manifest["allowed_origins"],
            [f"chrome-extension://{EXTENSION_ID}/"],
        )
        self.assertEqual(manifest["path"],
                         "/opt/conch/bin/conch-capture-host")

    def test_extension_id_override(self):
        home = Path(os.environ["HOME"])
        import sys as _sys

        if _sys.platform == "darwin":
            chrome_dir = (home / "Library" / "Application Support"
                          / "Google" / "Chrome")
        else:
            chrome_dir = (Path(os.environ["XDG_CONFIG_HOME"])
                          / "google-chrome")
        chrome_dir.mkdir(parents=True)
        with patch(
            "conch.kernel.browser_capture.host_command_path",
            return_value="/opt/conch/bin/conch-capture-host",
        ):
            outcome = install_native_host("b" * 32)
        manifest = json.loads(
            Path(outcome["written"]["Chrome"]).read_text()
        )
        self.assertEqual(
            manifest["allowed_origins"],
            [f"chrome-extension://{'b' * 32}/"],
        )


if __name__ == "__main__":
    unittest.main()
