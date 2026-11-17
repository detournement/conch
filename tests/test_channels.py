"""Tests for channel gateways (plan 4.3): Slack, Twilio SMS, email — send,
poll, sender allowlisting (fail closed), the notify router, and Slack
attachment capture / watched-thread reply polling (Milestone 1b)."""

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
    EmailChannel,
    InboundMessage,
    SlackChannel,
    TwilioSMSChannel,
    load_cursor_state,
    quarantine_dir,
    save_cursor_state,
    sniff_image_mime,
)

JPEG = b"\xff\xd8\xff\xe0conch-test-jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\nconch-test-png-bytes"


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


class StateDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": self._tmp.name,
            "SLACK_BOT_TOKEN": "xoxb-test",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "EMAIL_PASSWORD": "pw",
        })
        patcher.start()
        self.addCleanup(patcher.stop)


SLACK_CONFIG = {
    "slack_channel": "C123",
    "slack_allowed_senders": "U111, U222",
}
SMS_CONFIG = {
    "twilio_from": "+15550001111",
    "sms_to": "+15559998888",
    "sms_allowed_senders": "+15559998888",
}
EMAIL_CONFIG = {
    "email_address": "conch@example.com",
    "email_to": "me@example.com",
    "email_smtp_host": "smtp.example.com",
    "email_imap_host": "imap.example.com",
    "email_allowed_senders": "me@example.com",
}


class TestSenderAllowlist(StateDirTestCase):
    def test_fail_closed_without_allowlist(self):
        channel = SlackChannel({"slack_channel": "C123"})
        self.assertFalse(channel.sender_allowed("U111"),
                         "no allowlist must mean no inbound")

    def test_allowlisted_sender_accepted(self):
        channel = SlackChannel(SLACK_CONFIG)
        self.assertTrue(channel.sender_allowed("U111"))
        self.assertTrue(channel.sender_allowed("U222"))
        self.assertFalse(channel.sender_allowed("U999"))


class TestSlackChannel(StateDirTestCase):
    def test_configured(self):
        self.assertTrue(SlackChannel(SLACK_CONFIG).is_configured())
        self.assertFalse(SlackChannel({}).is_configured())

    def test_send_posts_message(self):
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["url"] = req.full_url
            recorded["body"] = json.loads(req.data.decode())
            recorded["auth"] = req.headers.get("Authorization")
            return _FakeHTTPResponse({"ok": True, "ts": "1234.5"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, ts = SlackChannel(SLACK_CONFIG).send("hello", thread_id="99.1")
        self.assertTrue(ok)
        self.assertEqual(ts, "1234.5")
        self.assertIn("chat.postMessage", recorded["url"])
        self.assertEqual(recorded["body"]["channel"], "C123")
        self.assertEqual(recorded["body"]["thread_ts"], "99.1")
        self.assertEqual(recorded["auth"], "Bearer xoxb-test")

    def test_send_error_reported(self):
        with patch("urllib.request.urlopen",
                   return_value=_FakeHTTPResponse({"ok": False, "error": "channel_not_found"})):
            ok, detail = SlackChannel(SLACK_CONFIG).send("x")
        self.assertFalse(ok)
        self.assertIn("channel_not_found", detail)

    def test_poll_returns_new_user_messages(self):
        payload = {"ok": True, "messages": [
            {"ts": "300.0", "user": "U111", "text": "newest"},
            {"ts": "200.0", "user": "U111", "text": "older"},
            {"ts": "150.0", "bot_id": "B1", "text": "bot noise"},
        ]}
        state = {"slack_last_ts": "100.0"}
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(payload)):
            messages = SlackChannel(SLACK_CONFIG).poll(state)
        self.assertEqual([m.text for m in messages], ["older", "newest"])
        self.assertEqual(state["slack_last_ts"], "300.0", "cursor advances")

    def test_poll_skips_already_seen(self):
        payload = {"ok": True, "messages": [
            {"ts": "100.0", "user": "U111", "text": "old"},
        ]}
        state = {"slack_last_ts": "100.0"}
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(payload)):
            messages = SlackChannel(SLACK_CONFIG).poll(state)
        self.assertEqual(messages, [])


def _file_obj(name="photo.jpg", mimetype="image/jpeg", size=1234, file_id="F1",
              url="https://files.slack.com/dl/photo.jpg"):
    return {"id": file_id, "name": name, "mimetype": mimetype, "size": size,
            "url_private_download": url}


class _SlackRouter:
    """Route fake urlopen calls: Slack Web API methods get scripted JSON,
    file downloads get scripted bytes (or an exception)."""

    def __init__(self, api_payloads, downloads=None):
        self.api_payloads = dict(api_payloads)   # method name -> payload(s)
        self.downloads = dict(downloads or {})   # url -> bytes | Exception
        self.download_headers = []
        self.api_requests = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if "slack.com/api/" in url:
            method = url.split("slack.com/api/")[1].split("?")[0]
            self.api_requests.append((method, url))
            payload = self.api_payloads.get(method, {"ok": False, "error": "unscripted"})
            if isinstance(payload, list):
                payload = payload.pop(0) if payload else {"ok": False}
            return _FakeHTTPResponse(payload)
        self.download_headers.append(dict(req.headers))
        blob = self.downloads.get(url)
        if isinstance(blob, Exception):
            raise blob
        if blob is None:
            raise OSError("unscripted download")
        return _FakeHTTPResponse(blob)


class TestSlackAttachments(StateDirTestCase):
    def _poll(self, router, state=None, config=None):
        state = state if state is not None else {"slack_last_ts": "100.0"}
        with patch("urllib.request.urlopen", side_effect=router), \
             patch("sys.stderr", io.StringIO()):
            return SlackChannel(config or SLACK_CONFIG).poll(state), state

    def test_photo_message_is_captured_and_quarantined(self):
        router = _SlackRouter(
            {"conversations.history": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "sell this",
                 "files": [_file_obj()]},
            ]}},
            {"https://files.slack.com/dl/photo.jpg": JPEG},
        )
        messages, _state = self._poll(router)
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.text, "sell this")
        self.assertEqual(message.ts, "200.0")
        self.assertEqual(len(message.attachments), 1)
        attachment = message.attachments[0]
        self.assertEqual(attachment.mime_type, "image/jpeg")
        self.assertEqual(attachment.size_bytes, len(JPEG))
        self.assertEqual(attachment.remote_id, "F1")
        path = Path(attachment.path)
        self.assertTrue(str(path).startswith(str(quarantine_dir())))
        self.assertEqual(path.read_bytes(), JPEG)
        # The bot bearer authorized the download but was never logged.
        self.assertEqual(
            router.download_headers[0].get("Authorization"), "Bearer xoxb-test"
        )

    def test_photo_only_message_still_delivered(self):
        router = _SlackRouter(
            {"conversations.history": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "",
                 "files": [_file_obj()]},
            ]}},
            {"https://files.slack.com/dl/photo.jpg": JPEG},
        )
        messages, _state = self._poll(router)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "")
        self.assertEqual(len(messages[0].attachments), 1)

    def test_non_image_and_oversized_files_never_fetched(self):
        router = _SlackRouter(
            {"conversations.history": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "docs",
                 "files": [
                     _file_obj(name="notes.pdf", mimetype="application/pdf",
                               url="https://files.slack.com/dl/notes.pdf"),
                     _file_obj(name="huge.jpg", size=ATTACHMENT_MAX_BYTES + 1,
                               url="https://files.slack.com/dl/huge.jpg"),
                 ]},
            ]}},
        )
        messages, _state = self._poll(router)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].attachments, [])
        self.assertEqual(router.download_headers, [],
                         "rejected files must never be downloaded")

    def test_mislabeled_content_rejected_by_magic_bytes(self):
        router = _SlackRouter(
            {"conversations.history": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "x",
                 "files": [_file_obj()]},
            ]}},
            {"https://files.slack.com/dl/photo.jpg": b"#!/bin/sh\nrm -rf /"},
        )
        messages, _state = self._poll(router)
        self.assertEqual(messages[0].attachments, [],
                         "content that is not an image must be dropped")

    def test_download_failure_still_delivers_message(self):
        router = _SlackRouter(
            {"conversations.history": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "sell this",
                 "files": [
                     _file_obj(url="https://files.slack.com/dl/broken.jpg"),
                     _file_obj(name="ok.png", mimetype="image/png", file_id="F2",
                               url="https://files.slack.com/dl/ok.png"),
                 ]},
            ]}},
            {"https://files.slack.com/dl/broken.jpg": OSError("boom"),
             "https://files.slack.com/dl/ok.png": PNG},
        )
        messages, _state = self._poll(router)
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(messages[0].attachments), 1)
        self.assertEqual(messages[0].attachments[0].mime_type, "image/png")

    def test_non_allowlisted_sender_files_never_downloaded(self):
        router = _SlackRouter(
            {"conversations.history": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U999", "text": "malicious",
                 "files": [_file_obj()]},
            ]}},
            {"https://files.slack.com/dl/photo.jpg": JPEG},
        )
        messages, _state = self._poll(router)
        self.assertEqual(router.download_headers, [],
                         "no bytes fetched for non-allowlisted senders")
        # The message itself is still surfaced (and dropped by poll_all).
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].attachments, [])

    def test_sniff_image_mime(self):
        self.assertEqual(sniff_image_mime(JPEG), "image/jpeg")
        self.assertEqual(sniff_image_mime(PNG), "image/png")
        self.assertEqual(sniff_image_mime(b"GIF89a..."), "image/gif")
        self.assertEqual(
            sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp"
        )
        self.assertEqual(sniff_image_mime(b"plain text"), "")


class TestSlackThreadReplies(StateDirTestCase):
    def test_send_into_thread_registers_watch(self):
        router = _SlackRouter(
            {"chat.postMessage": {"ok": True, "ts": "201.0"}}
        )
        with patch("urllib.request.urlopen", side_effect=router):
            ok, _ts = SlackChannel(SLACK_CONFIG).send("question?", thread_id="200.0")
        self.assertTrue(ok)
        state = load_cursor_state()
        self.assertEqual(state["slack_threads"], {"200.0": "201.0"})

    def test_watched_thread_replies_polled(self):
        router = _SlackRouter({
            "conversations.history": {"ok": True, "messages": []},
            "conversations.replies": {"ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "parent"},
                {"ts": "201.0", "user": "UBOT", "bot_id": "B1", "text": "us"},
                {"ts": "202.0", "user": "U111", "text": "the reply"},
            ]},
        })
        state = {"slack_last_ts": "205.0", "slack_threads": {"200.0": "201.0"}}
        with patch("urllib.request.urlopen", side_effect=router):
            messages = SlackChannel(SLACK_CONFIG).poll(state)
        self.assertEqual([m.text for m in messages], ["the reply"])
        self.assertEqual(messages[0].thread_id, "200.0")
        self.assertEqual(state["slack_threads"]["200.0"], "202.0",
                         "per-thread cursor advances")
        # Second poll with the advanced cursor yields nothing new.
        router.api_payloads["conversations.replies"] = {
            "ok": True, "messages": [
                {"ts": "200.0", "user": "U111", "text": "parent"},
                {"ts": "202.0", "user": "U111", "text": "the reply"},
            ]}
        with patch("urllib.request.urlopen", side_effect=router):
            again = SlackChannel(SLACK_CONFIG).poll(state)
        self.assertEqual(again, [])

    def test_thread_broadcasts_deduped_against_history(self):
        router = _SlackRouter({
            "conversations.history": {"ok": True, "messages": [
                {"ts": "203.0", "user": "U111", "text": "broadcast",
                 "thread_ts": "200.0", "subtype": "thread_broadcast"},
            ]},
            "conversations.replies": {"ok": True, "messages": [
                {"ts": "203.0", "user": "U111", "text": "broadcast",
                 "thread_ts": "200.0", "subtype": "thread_broadcast"},
            ]},
        })
        state = {"slack_last_ts": "100.0", "slack_threads": {"200.0": "201.0"}}
        with patch("urllib.request.urlopen", side_effect=router):
            messages = SlackChannel(SLACK_CONFIG).poll(state)
        self.assertEqual(len(messages), 1, "broadcast reply arrives once")
        self.assertEqual(messages[0].thread_id, "200.0")

    def test_watched_threads_pruned(self):
        config = dict(SLACK_CONFIG)
        threads = {f"{i}.0": f"{i}.5" for i in range(1, 30)}
        state = {"slack_last_ts": "999.0", "slack_threads": dict(threads)}
        router = _SlackRouter({
            "conversations.history": {"ok": True, "messages": []},
            "conversations.replies": {"ok": True, "messages": []},
        })
        with patch("urllib.request.urlopen", side_effect=router):
            SlackChannel(config).poll(state)
        kept = state["slack_threads"]
        self.assertEqual(len(kept), SlackChannel.MAX_WATCHED_THREADS)
        self.assertIn("29.0", kept, "most recently active threads survive")
        self.assertNotIn("1.0", kept)

    def test_inbound_message_defaults_stay_compatible(self):
        message = InboundMessage(
            channel="slack", sender="U1", text="hi", thread_id="1.0"
        )
        self.assertEqual(message.attachments, [])
        self.assertEqual(message.ts, "")


class TestTwilioChannel(StateDirTestCase):
    def test_configured(self):
        self.assertTrue(TwilioSMSChannel(SMS_CONFIG).is_configured())
        self.assertFalse(TwilioSMSChannel({}).is_configured())

    def test_send_uses_messages_api(self):
        recorded = {}

        def side_effect(req, timeout=None):
            recorded["url"] = req.full_url
            recorded["form"] = dict(urllib.parse.parse_qsl(req.data.decode()))
            return _FakeHTTPResponse({"sid": "SM123"})

        with patch("urllib.request.urlopen", side_effect=side_effect):
            ok, sid = TwilioSMSChannel(SMS_CONFIG).send("ping")
        self.assertTrue(ok)
        self.assertEqual(sid, "SM123")
        self.assertIn("/Accounts/ACtest/Messages.json", recorded["url"])
        self.assertEqual(recorded["form"]["From"], "+15550001111")
        self.assertEqual(recorded["form"]["To"], "+15559998888")
        self.assertEqual(recorded["form"]["Body"], "ping")

    def test_first_poll_only_sets_cursor(self):
        payload = {"messages": [
            {"sid": "SM1", "direction": "inbound", "from": "+15559998888", "body": "hi"},
        ]}
        state = {}
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(payload)):
            messages = TwilioSMSChannel(SMS_CONFIG).poll(state)
        self.assertEqual(messages, [], "first poll must not replay history")
        self.assertIn("SM1", state["sms_seen_sids"])

    def test_new_inbound_after_cursor(self):
        payload = {"messages": [
            {"sid": "SM2", "direction": "inbound", "from": "+15559998888", "body": "do the thing"},
            {"sid": "SM1", "direction": "inbound", "from": "+15559998888", "body": "old"},
            {"sid": "SMout", "direction": "outbound-api", "from": "+15550001111", "body": "our reply"},
        ]}
        state = {"sms_seen_sids": ["SM1", "SMout"]}
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(payload)):
            messages = TwilioSMSChannel(SMS_CONFIG).poll(state)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "do the thing")
        self.assertEqual(messages[0].thread_id, "+15559998888")


class TestEmailChannel(StateDirTestCase):
    def test_configured(self):
        self.assertTrue(EmailChannel(EMAIL_CONFIG).is_configured())
        self.assertFalse(EmailChannel({}).is_configured())

    def test_send_via_smtp(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                sent["host"] = host
                sent["port"] = port

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                sent["login"] = (user, password)

            def sendmail(self, from_addr, to_addrs, body):
                sent["from"] = from_addr
                sent["to"] = to_addrs
                sent["body"] = body

        with patch("smtplib.SMTP_SSL", FakeSMTP):
            ok, detail = EmailChannel(EMAIL_CONFIG).send("status update")
        self.assertTrue(ok)
        self.assertEqual(sent["host"], "smtp.example.com")
        self.assertEqual(sent["to"], ["me@example.com"])
        self.assertIn("status update", sent["body"])


class TestChannelManager(StateDirTestCase):
    def test_only_configured_channels_active(self):
        manager = ChannelManager(dict(SLACK_CONFIG))
        self.assertEqual(manager.configured(), ["slack"])

    def test_notify_routes_to_default_channel(self):
        config = dict(SLACK_CONFIG, notify_channel="slack")
        with patch("urllib.request.urlopen",
                   return_value=_FakeHTTPResponse({"ok": True, "ts": "1.0"})):
            ok, _ = ChannelManager(config).notify("hello")
        self.assertTrue(ok)

    def test_notify_unconfigured_channel_fails_cleanly(self):
        ok, detail = ChannelManager({}).notify("hello")
        self.assertFalse(ok)
        self.assertIn("not configured", detail)

    def test_poll_all_drops_non_allowlisted(self):
        config = dict(SLACK_CONFIG, slack_allowed_senders="U111")
        payload = {"ok": True, "messages": [
            {"ts": "2.0", "user": "U999", "text": "attacker"},
            {"ts": "1.5", "user": "U111", "text": "legit"},
        ]}
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(payload)), \
             patch("sys.stderr", io.StringIO()):
            inbound = ChannelManager(config).poll_all()
        self.assertEqual([m.text for m in inbound], ["legit"])

    def test_cursor_state_persists(self):
        save_cursor_state({"slack_last_ts": "42.0"})
        self.assertEqual(load_cursor_state()["slack_last_ts"], "42.0")


if __name__ == "__main__":
    unittest.main()
