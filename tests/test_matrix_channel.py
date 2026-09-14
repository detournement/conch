"""Tests for the Matrix channel (sovereign phone layer) and the ntfy push
notifier: /sync cursor resume, long-poll timeout mapping, fail-closed
sender allowlists (including lookalike user IDs), thread mapping,
attachment quarantine via the media API, send/reply with thread
relations, token-by-reference hygiene, and ntfy POST fields."""

import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from conch.channels import (
    ATTACHMENT_MAX_BYTES,
    ChannelManager,
    MatrixChannel,
    NtfyNotifier,
    quarantine_dir,
)

JPEG = b"\xff\xd8\xff\xe0conch-test-jpeg-bytes"

MATRIX_CONFIG = {
    "matrix_homeserver": "http://hs.test",
    "matrix_room": "!room:conch.local",
    "matrix_allowed_senders": "@you:conch.local",
    "matrix_user": "@conch:conch.local",
}

NTFY_CONFIG = {
    "ntfy_url": "http://ntfy.test",
    "ntfy_topic": "conch-alerts",
}


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, limit=None):
        if isinstance(self._payload, bytes):
            return self._payload if limit is None else self._payload[:limit]
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _MatrixRouter:
    """Fake homeserver: routes urlopen calls by path, records everything."""

    def __init__(self):
        self.requests = []
        self.sync_responses = []       # consumed in order
        self.media = {}                # url substring -> bytes | Exception
        self.send_response = {"event_id": "$sent1"}
        self.whoami = {"user_id": "@conch:conch.local"}

    def __call__(self, req, timeout=None):
        record = {
            "method": req.get_method(),
            "url": req.full_url,
            "headers": dict(req.headers),
            "body": req.data,
            "timeout": timeout,
        }
        self.requests.append(record)
        url = req.full_url
        if "/_matrix/client/v3/sync" in url:
            payload = (self.sync_responses.pop(0) if self.sync_responses
                       else {"next_batch": "s-empty"})
            if isinstance(payload, Exception):
                raise payload
            return _FakeHTTPResponse(payload)
        if "/account/whoami" in url:
            return _FakeHTTPResponse(self.whoami)
        if "/media/download/" in url:
            for key, blob in self.media.items():
                if key in url:
                    if isinstance(blob, Exception):
                        raise blob
                    return _FakeHTTPResponse(blob)
            raise OSError("unscripted media download")
        if "/send/m.room.message/" in url:
            if isinstance(self.send_response, Exception):
                raise self.send_response
            return _FakeHTTPResponse(self.send_response)
        raise OSError(f"unscripted request: {url}")

    def sync_urls(self):
        return [r["url"] for r in self.requests
                if "/_matrix/client/v3/sync" in r["url"]]

    def media_urls(self):
        return [r["url"] for r in self.requests
                if "/media/download/" in r["url"]]

    def send_requests(self):
        return [r for r in self.requests
                if "/send/m.room.message/" in r["url"]]


def _sync_payload(events, room="!room:conch.local", next_batch="s2"):
    return {
        "next_batch": next_batch,
        "rooms": {"join": {room: {"timeline": {"events": events}}}},
    }


def _text_event(body, sender="@you:conch.local", event_id="$e1",
                ts=1700000000000, relates=None):
    content = {"msgtype": "m.text", "body": body}
    if relates:
        content["m.relates_to"] = relates
    return {
        "type": "m.room.message", "sender": sender, "event_id": event_id,
        "origin_server_ts": ts, "content": content,
    }


def _image_event(sender="@you:conch.local", event_id="$img1",
                 mimetype="image/jpeg", size=1234,
                 url="mxc://conch.local/media1", name="photo.jpg"):
    return {
        "type": "m.room.message", "sender": sender, "event_id": event_id,
        "origin_server_ts": 1700000000001,
        "content": {
            "msgtype": "m.image", "body": name, "url": url,
            "info": {"mimetype": mimetype, "size": size},
        },
    }


class MatrixTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "MATRIX_ACCESS_TOKEN": "syt-secret-token",
            "NTFY_TOKEN": "tk_ntfy_secret",
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def poll(self, router, state, config=None):
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("sys.stderr", io.StringIO()):
            return MatrixChannel(config or MATRIX_CONFIG).poll(state)


class TestMatrixConfigured(MatrixTestCase):
    def test_configured_requires_server_room_and_token(self):
        self.assertTrue(MatrixChannel(MATRIX_CONFIG).is_configured())
        self.assertFalse(MatrixChannel({}).is_configured())
        incomplete = dict(MATRIX_CONFIG)
        incomplete.pop("matrix_room")
        self.assertFalse(MatrixChannel(incomplete).is_configured())

    def test_token_env_by_reference(self):
        config = dict(MATRIX_CONFIG, matrix_token_env="MY_MATRIX_TOKEN")
        self.assertFalse(MatrixChannel(config).is_configured(),
                         "named env unset means unconfigured")
        with patch.dict(os.environ, {"MY_MATRIX_TOKEN": "syt-other"}):
            channel = MatrixChannel(config)
            self.assertTrue(channel.is_configured())
            self.assertEqual(channel._token(), "syt-other")

    def test_missing_token_means_unconfigured(self):
        with patch.dict(os.environ, {"MATRIX_ACCESS_TOKEN": ""}):
            self.assertFalse(MatrixChannel(MATRIX_CONFIG).is_configured())


class TestMatrixSync(MatrixTestCase):
    def test_first_sync_only_establishes_cursor(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload(
            [_text_event("history that must not replay")], next_batch="s1",
        )]
        state = {}
        messages = self.poll(router, state)
        self.assertEqual(messages, [], "first sync must not replay history")
        self.assertEqual(state["matrix_since"], "s1")
        query = urllib.parse.urlparse(router.sync_urls()[0]).query
        params = dict(urllib.parse.parse_qsl(query))
        self.assertEqual(params["timeout"], "0",
                         "cursor-establishing sync must not long-poll")
        self.assertNotIn("since", params)

    def test_resume_uses_since_cursor_and_long_poll_timeout(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([_text_event("hello conch")])]
        state = {"matrix_since": "s1"}
        messages = self.poll(router, state)
        self.assertEqual([m.text for m in messages], ["hello conch"])
        self.assertEqual(state["matrix_since"], "s2", "cursor advances")
        request = router.requests[0]
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(request["url"]).query
        ))
        self.assertEqual(params["since"], "s1")
        self.assertEqual(params["timeout"], "10000",
                         "the sync timeout parameter IS the long poll")
        self.assertGreaterEqual(request["timeout"], 25.0,
                                "socket timeout exceeds the sync window")

    def test_sync_timeout_configurable_and_clamped(self):
        config = dict(MATRIX_CONFIG, matrix_sync_timeout_ms="30000")
        self.assertEqual(MatrixChannel(config)._sync_timeout_ms(), 30000)
        config = dict(MATRIX_CONFIG, matrix_sync_timeout_ms="999999")
        self.assertEqual(MatrixChannel(config)._sync_timeout_ms(), 60000)
        config = dict(MATRIX_CONFIG, matrix_sync_timeout_ms="junk")
        self.assertEqual(MatrixChannel(config)._sync_timeout_ms(), 10000)

    def test_own_messages_skipped(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _text_event("our own echo", sender="@conch:conch.local"),
            _text_event("the user's message"),
        ])]
        messages = self.poll(router, {"matrix_since": "s1"})
        self.assertEqual([m.text for m in messages], ["the user's message"])

    def test_own_user_discovered_via_whoami_when_unset(self):
        config = dict(MATRIX_CONFIG)
        config.pop("matrix_user")
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _text_event("echo", sender="@conch:conch.local"),
        ])]
        state = {"matrix_since": "s1"}
        messages = self.poll(router, state, config=config)
        self.assertEqual(messages, [], "whoami identity filters the echo")
        self.assertEqual(state["matrix_own_user"], "@conch:conch.local")
        # cached: second poll makes no further whoami call
        router.sync_responses = [_sync_payload([])]
        self.poll(router, state, config=config)
        whoami_calls = [r for r in router.requests
                        if "whoami" in r["url"]]
        self.assertEqual(len(whoami_calls), 1)

    def test_thread_relation_maps_to_room_plus_root(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _text_event("in a thread", relates={
                "rel_type": "m.thread", "event_id": "$root9",
            }),
            _text_event("top level", event_id="$e2"),
        ])]
        messages = self.poll(router, {"matrix_since": "s1"})
        self.assertEqual(messages[0].thread_id, "!room:conch.local|$root9")
        self.assertEqual(messages[1].thread_id, "!room:conch.local")

    def test_network_failure_returns_empty_and_keeps_cursor(self):
        router = _MatrixRouter()
        router.sync_responses = [OSError("connection refused")]
        state = {"matrix_since": "s1"}
        self.assertEqual(self.poll(router, state), [])
        self.assertEqual(state["matrix_since"], "s1")

    def test_token_rides_header_never_url(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([_text_event("hi")])]
        self.poll(router, {"matrix_since": "s1"})
        for request in router.requests:
            self.assertNotIn("syt-secret-token", request["url"],
                             "the token must never appear in a URL")
            self.assertEqual(request["headers"].get("Authorization"),
                             "Bearer syt-secret-token")


class TestMatrixAllowlist(MatrixTestCase):
    def test_fail_closed_without_allowlist(self):
        config = dict(MATRIX_CONFIG)
        config.pop("matrix_allowed_senders")
        channel = MatrixChannel(config)
        self.assertFalse(channel.sender_allowed("@you:conch.local"),
                         "no allowlist must mean no inbound")

    def test_lookalike_user_ids_rejected(self):
        channel = MatrixChannel(MATRIX_CONFIG)
        self.assertTrue(channel.sender_allowed("@you:conch.local"))
        for lookalike in (
            "@you:conch.local.evil",     # homeserver suffix attack
            "@you:evil.conch.local",     # subdomain
            "@you2:conch.local",         # localpart suffix
            "@YOU:conch.local",          # case variant
            "you:conch.local",           # missing sigil
            "@you:conch.locaI",          # capital I for l
            "@you",                      # bare localpart
        ):
            self.assertFalse(channel.sender_allowed(lookalike), lookalike)

    def test_manager_drops_non_allowlisted_matrix_sender(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _text_event("ignore me", sender="@mallory:conch.local"),
            _text_event("legit", event_id="$e2"),
        ])]
        from conch.channels import save_cursor_state
        save_cursor_state({"matrix_since": "s1"})
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("sys.stderr", io.StringIO()):
            inbound = ChannelManager(dict(MATRIX_CONFIG)).poll_all()
        self.assertEqual([m.text for m in inbound], ["legit"])


class TestMatrixAttachments(MatrixTestCase):
    def test_image_quarantined_via_media_api(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([_image_event()])]
        router.media = {"conch.local/media1": JPEG}
        messages = self.poll(router, {"matrix_since": "s1"})
        self.assertEqual(len(messages), 1)
        attachment = messages[0].attachments[0]
        self.assertEqual(attachment.mime_type, "image/jpeg")
        self.assertEqual(attachment.size_bytes, len(JPEG))
        path = Path(attachment.path)
        self.assertTrue(str(path).startswith(str(quarantine_dir())))
        self.assertEqual(path.read_bytes(), JPEG)
        # authenticated v1 endpoint first, bearer in header only
        media = router.media_urls()[0]
        self.assertIn("/_matrix/client/v1/media/download/", media)
        self.assertNotIn("syt-secret-token", media)

    def test_media_falls_back_to_legacy_v3(self):
        channel = MatrixChannel(MATRIX_CONFIG)
        calls = []

        def side_effect(req, timeout=None):
            calls.append(req.full_url)
            if "/_matrix/client/v1/media/" in req.full_url:
                raise OSError("404 on the modern endpoint")
            return _FakeHTTPResponse(JPEG)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            data = channel._download_media("mxc://conch.local/media1")
        self.assertEqual(data, JPEG)
        self.assertIn("/_matrix/media/v3/download/", calls[1])

    def test_mislabeled_content_rejected_by_magic_bytes(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([_image_event()])]
        router.media = {"conch.local/media1": b"#!/bin/sh\nrm -rf /"}
        messages = self.poll(router, {"matrix_since": "s1"})
        self.assertEqual(messages, [], "a non-image never becomes a message")

    def test_oversized_declared_never_fetched(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _image_event(size=ATTACHMENT_MAX_BYTES + 1),
        ])]
        self.assertEqual(self.poll(router, {"matrix_since": "s1"}), [])
        self.assertEqual(router.media_urls(), [],
                         "oversized files must never be downloaded")

    def test_non_image_mimetype_never_fetched(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _image_event(mimetype="application/pdf"),
        ])]
        self.assertEqual(self.poll(router, {"matrix_since": "s1"}), [])
        self.assertEqual(router.media_urls(), [])

    def test_non_allowlisted_sender_media_never_downloaded(self):
        router = _MatrixRouter()
        router.sync_responses = [_sync_payload([
            _image_event(sender="@mallory:conch.local"),
        ])]
        router.media = {"conch.local/media1": JPEG}
        self.assertEqual(self.poll(router, {"matrix_since": "s1"}), [])
        self.assertEqual(router.media_urls(), [],
                         "no bytes fetched for non-allowlisted senders")

    def test_malformed_mxc_rejected(self):
        channel = MatrixChannel(MATRIX_CONFIG)
        for bad in ("https://evil.test/x", "mxc://", "mxc://only-server",
                    "mxc://srv/a/b"):
            with self.assertRaises(ValueError):
                channel._download_media(bad)


class TestMatrixSend(MatrixTestCase):
    def _send(self, router, text, thread_id=""):
        with patch("urllib.request.urlopen", side_effect=router):
            return MatrixChannel(MATRIX_CONFIG).send(text, thread_id=thread_id)

    def test_send_puts_message_to_default_room(self):
        router = _MatrixRouter()
        ok, event_id = self._send(router, "hello phone")
        self.assertTrue(ok)
        self.assertEqual(event_id, "$sent1")
        request = router.send_requests()[0]
        self.assertEqual(request["method"], "PUT")
        self.assertIn(urllib.parse.quote("!room:conch.local"),
                      request["url"])
        body = json.loads(request["body"].decode())
        self.assertEqual(body["msgtype"], "m.text")
        self.assertEqual(body["body"], "hello phone")
        self.assertNotIn("m.relates_to", body)
        self.assertNotIn("syt-secret-token", request["url"])
        self.assertEqual(request["headers"].get("Authorization"),
                         "Bearer syt-secret-token")

    def test_reply_in_thread_carries_thread_relation(self):
        router = _MatrixRouter()
        ok, _ = self._send(router, "threaded reply",
                           thread_id="!room:conch.local|$root9")
        self.assertTrue(ok)
        body = json.loads(router.send_requests()[0]["body"].decode())
        relation = body["m.relates_to"]
        self.assertEqual(relation["rel_type"], "m.thread")
        self.assertEqual(relation["event_id"], "$root9")
        self.assertTrue(relation["is_falling_back"])
        self.assertEqual(relation["m.in_reply_to"]["event_id"], "$root9")

    def test_transaction_ids_unique(self):
        router = _MatrixRouter()
        self._send(router, "one")
        self._send(router, "two")
        txns = [r["url"].rsplit("/", 1)[1] for r in router.send_requests()]
        self.assertNotEqual(txns[0], txns[1])

    def test_send_failure_reported(self):
        router = _MatrixRouter()
        router.send_response = OSError("boom")
        ok, detail = self._send(router, "x")
        self.assertFalse(ok)
        self.assertIn("matrix send failed", detail)

    def test_unconfigured_send_fails_cleanly(self):
        ok, detail = MatrixChannel({}).send("x")
        self.assertFalse(ok)
        self.assertIn("not configured", detail)


class TestNtfyNotifier(MatrixTestCase):
    def _send(self, config=None, **kwargs):
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["url"] = req.full_url
            recorded["headers"] = dict(req.headers)
            recorded["body"] = req.data
            recorded["method"] = req.get_method()
            return _FakeHTTPResponse({"id": "m1"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = NtfyNotifier(config or dict(NTFY_CONFIG)).send(
                "the message", **kwargs
            )
        return result, recorded

    def test_configured(self):
        self.assertTrue(NtfyNotifier(NTFY_CONFIG).is_configured())
        self.assertFalse(NtfyNotifier({}).is_configured())
        self.assertFalse(NtfyNotifier({"ntfy_url": "http://x"}).is_configured())

    def test_post_carries_title_priority_tags_click(self):
        (ok, message_id), recorded = self._send(
            title="Approval needed", priority="high", tags="lock",
            click="https://matrix.to/#/!room",
        )
        self.assertTrue(ok)
        self.assertEqual(message_id, "m1")
        self.assertEqual(recorded["method"], "POST")
        self.assertEqual(recorded["url"], "http://ntfy.test/conch-alerts")
        self.assertEqual(recorded["body"], b"the message")
        self.assertEqual(recorded["headers"]["Title"], "Approval needed")
        self.assertEqual(recorded["headers"]["Priority"], "high")
        self.assertEqual(recorded["headers"]["Tags"], "lock")
        self.assertEqual(recorded["headers"]["Click"],
                         "https://matrix.to/#/!room")

    def test_auth_token_by_reference_in_header_only(self):
        config = dict(NTFY_CONFIG, ntfy_token_env="NTFY_TOKEN")
        (ok, _), recorded = self._send(config=config)
        self.assertTrue(ok)
        self.assertEqual(recorded["headers"]["Authorization"],
                         "Bearer tk_ntfy_secret")
        self.assertNotIn("tk_ntfy_secret", recorded["url"])

    def test_no_token_no_auth_header(self):
        with patch.dict(os.environ, {"NTFY_TOKEN": ""}):
            (ok, _), recorded = self._send()
        self.assertTrue(ok)
        self.assertNotIn("Authorization", recorded["headers"])

    def test_click_defaults_to_matrix_to_room_link(self):
        config = dict(NTFY_CONFIG, matrix_room="!room:conch.local")
        (_ok, _), recorded = self._send(config=config)
        self.assertEqual(
            recorded["headers"]["Click"],
            "https://matrix.to/#/" + urllib.parse.quote("!room:conch.local"),
        )

    def test_explicit_click_config_wins(self):
        config = dict(NTFY_CONFIG, matrix_room="!room:conch.local",
                      ntfy_click="element://custom")
        (_ok, _), recorded = self._send(config=config)
        self.assertEqual(recorded["headers"]["Click"], "element://custom")

    def test_title_header_sanitized(self):
        (_ok, _), recorded = self._send(title="line1\nline2\r☠ skull")
        self.assertNotIn("\n", recorded["headers"]["Title"])
        self.assertNotIn("☠", recorded["headers"]["Title"])

    def test_unconfigured_send_fails_cleanly(self):
        ok, detail = NtfyNotifier({}).send("x")
        self.assertFalse(ok)
        self.assertIn("not configured", detail)

    def test_network_failure_reported_not_raised(self):
        with patch("urllib.request.urlopen",
                   side_effect=OSError("refused")):
            ok, detail = NtfyNotifier(dict(NTFY_CONFIG)).send("x")
        self.assertFalse(ok)
        self.assertIn("ntfy push failed", detail)


class TestPushRouting(MatrixTestCase):
    def test_push_requires_notify_push_route_and_config(self):
        self.assertFalse(ChannelManager(dict(NTFY_CONFIG)).push_enabled(),
                         "ntfy configured but not routed: no push")
        self.assertFalse(ChannelManager(
            {"notify_push": "ntfy"}
        ).push_enabled(), "routed but unconfigured: no push")
        self.assertTrue(ChannelManager(
            dict(NTFY_CONFIG, notify_push="ntfy")
        ).push_enabled())

    def test_push_interrupt_posts_when_routed(self):
        manager = ChannelManager(dict(NTFY_CONFIG, notify_push="ntfy"))
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["url"] = req.full_url
            recorded["headers"] = dict(req.headers)
            return _FakeHTTPResponse({"id": "m2"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, _ = manager.push_interrupt(
                "Approval needed [#3]", title="Conch: approval needed",
                priority="high",
            )
        self.assertTrue(ok)
        self.assertEqual(recorded["url"], "http://ntfy.test/conch-alerts")
        self.assertEqual(recorded["headers"]["Priority"], "high")

    def test_push_interrupt_never_raises(self):
        manager = ChannelManager(dict(NTFY_CONFIG, notify_push="ntfy"))
        with patch("urllib.request.urlopen",
                   side_effect=RuntimeError("catastrophe")):
            ok, detail = manager.push_interrupt("x")
        self.assertFalse(ok)
        self.assertIn("ntfy push failed", detail)

    def test_unrouted_push_is_noop(self):
        manager = ChannelManager(dict(NTFY_CONFIG))
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("must not call the network")):
            ok, detail = manager.push_interrupt("x")
        self.assertFalse(ok)
        self.assertIn("push not configured", detail)

    def test_matrix_is_a_manager_channel(self):
        manager = ChannelManager(dict(MATRIX_CONFIG))
        self.assertEqual(manager.configured(), ["matrix"])
        self.assertIsNotNone(manager.get("matrix"))


if __name__ == "__main__":
    unittest.main()
