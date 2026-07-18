"""Tests for channel gateways (plan 4.3): Slack, Twilio SMS, email — send,
poll, sender allowlisting (fail closed), and the notify router."""

import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from conch.channels import (
    ChannelManager,
    EmailChannel,
    InboundMessage,
    SlackChannel,
    TwilioSMSChannel,
    load_cursor_state,
    save_cursor_state,
)


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
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
