"""Channel gateways for the remote agentic loop (plan 4.3).

Outbound: conch messages the user proactively (scheduled task results,
approval requests, notifications). Inbound: polled replies resume/steer
sessions. Three gateways, all stdlib-only:

- Slack: bot token (SLACK_BOT_TOKEN), chat.postMessage / conversations.history
- SMS: Twilio REST API (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN), polling the
  Messages list (webhooks would be lower latency but need a public endpoint;
  polling works behind NAT)
- Email: SMTP send + IMAP UNSEEN polling (EMAIL_PASSWORD)

Safety posture (plan 4.3): inbound is fail-closed — a channel with no
configured sender allowlist accepts NO inbound messages. Remote sessions are
capped at safe_auto permissions and anything beyond the allowlist goes
through the approval-over-channel flow (see remote.py / tooling.py).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"


def _cursor_path() -> Path:
    return _state_dir() / "channels.json"


def quarantine_dir() -> Path:
    """Where inbound channel attachments land before anything trusts them."""
    return _state_dir() / "quarantine"


def load_cursor_state() -> Dict[str, Any]:
    try:
        return json.loads(_cursor_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_cursor_state(state: Dict[str, Any]):
    _cursor_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _cursor_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_cursor_path())


def _split_list(value: str) -> List[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Attachments (Slack first — eBay pilot Milestone 1b)
# ---------------------------------------------------------------------------

#: Inbound attachment cap, aligned with the oversight app's image proxy.
ATTACHMENT_MAX_BYTES = 12 * 1024 * 1024
#: Per-message attachment cap, aligned with the eBay media contract limit.
MAX_ATTACHMENTS_PER_MESSAGE = 12
#: This flow accepts images only; anything else is ignored, never fetched.
IMAGE_MIME_ALLOWLIST = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(data: bytes) -> str:
    """MIME type from magic bytes — the declared type is remote-controlled
    data, so quarantined files are admitted on content, never on labels."""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _safe_filename(name: str, fallback: str = "attachment") -> str:
    base = Path(str(name or "")).name
    cleaned = "".join(
        c if (c.isalnum() or c in "._-") else "_" for c in base
    ).strip("._") or fallback
    return cleaned[:80]


@dataclass
class Attachment:
    """One validated inbound file, already written to the quarantine dir."""

    filename: str
    mime_type: str   # sniffed from content, not the sender's label
    size_bytes: int
    path: str        # local quarantine path
    remote_id: str = ""


@dataclass
class InboundMessage:
    channel: str    # "slack" | "sms" | "email"
    sender: str     # slack user id / phone number / email address
    text: str
    thread_id: str  # slack thread ts / phone number / email address
    ts: str = ""    # transport message id/timestamp (slack message ts)
    attachments: List[Attachment] = field(default_factory=list)


class Channel:
    """Gateway interface. Subclasses implement send/poll for one transport."""

    name = "base"

    def __init__(self, config: dict):
        self.config = config or {}

    def is_configured(self) -> bool:
        return False

    def allowed_senders(self) -> List[str]:
        return _split_list(self.config.get(f"{self.name}_allowed_senders", ""))

    def sender_allowed(self, sender: str) -> bool:
        """Fail closed: no allowlist means no inbound is accepted."""
        allowed = self.allowed_senders()
        return bool(allowed) and str(sender).strip() in allowed

    def send(self, text: str, thread_id: str = "") -> tuple:
        raise NotImplementedError

    def poll(self, state: Dict[str, Any]) -> List[InboundMessage]:
        """New inbound messages since the cursor in *state* (mutated)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

class SlackChannel(Channel):
    """Slack via a bot token. Config: slack_channel (channel id),
    slack_allowed_senders (Slack user ids). Token: SLACK_BOT_TOKEN (or the
    env var named by slack_token_env).

    Attachments: message ``files[]`` from allowlisted senders are fetched
    via ``url_private_download`` with the bot bearer (requires the
    ``files:read`` bot scope), validated (image magic bytes, 12 MB cap,
    ≤12 per message), and quarantined under the XDG state dir. The bearer
    is sent only as a request header and never logged.

    Thread replies: ``conversations.history`` does not return replies
    inside threads, so every thread the bot posts into is remembered in
    cursor state and polled via ``conversations.replies`` until it ages
    out — this is what lets a Slack thread carry a whole conversation.
    """

    name = "slack"
    API = "https://slack.com/api"
    MAX_WATCHED_THREADS = 20

    def _token(self) -> str:
        env = (self.config.get("slack_token_env") or "SLACK_BOT_TOKEN").strip()
        return os.environ.get(env, "").strip()

    def _channel_id(self) -> str:
        return (self.config.get("slack_channel") or "").strip()

    def is_configured(self) -> bool:
        return bool(self._token() and self._channel_id())

    def _api(self, method: str, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
        url = f"{self.API}/{method}"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
        elif params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def send(self, text: str, thread_id: str = "") -> tuple:
        if not self.is_configured():
            return False, "slack not configured (SLACK_BOT_TOKEN + slack_channel)"
        body = {"channel": self._channel_id(), "text": text}
        if thread_id:
            body["thread_ts"] = thread_id
        try:
            result = self._api("chat.postMessage", body=body)
        except Exception as exc:
            return False, f"slack send failed: {exc}"
        if not result.get("ok"):
            return False, f"slack send failed: {result.get('error', 'unknown')}"
        ts = result.get("ts", "")
        if thread_id:
            # We just posted into a thread: watch it so the user's replies
            # (which conversations.history never returns) are polled.
            self._watch_thread(thread_id, ts)
        return True, ts

    # -- thread watching ----------------------------------------------------

    def _watch_thread(self, thread_ts: str, last_ts: str):
        try:
            state = load_cursor_state()
            threads = dict(state.get("slack_threads") or {})
            threads[thread_ts] = _max_ts(threads.get(thread_ts, ""), last_ts or thread_ts)
            state["slack_threads"] = _prune_threads(threads, self.MAX_WATCHED_THREADS)
            save_cursor_state(state)
        except OSError:
            pass  # watching is best-effort; sending must never fail on it

    # -- attachments ----------------------------------------------------------

    def _download_file(self, url: str) -> bytes:
        """Fetch a private Slack file with the bot bearer. The token rides
        only the Authorization header; it is never logged or echoed."""
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token()}"}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.read(ATTACHMENT_MAX_BYTES + 1)

    def _capture_file(self, file_obj: dict, ts: str, index: int) -> Optional[Attachment]:
        name = _safe_filename(file_obj.get("name"), fallback=f"file-{index}")
        declared_mime = str(file_obj.get("mimetype") or "").lower()
        if declared_mime not in IMAGE_MIME_ALLOWLIST:
            return None  # not an image: not for this flow, never fetched
        try:
            declared_size = int(file_obj.get("size") or 0)
        except (TypeError, ValueError):
            declared_size = 0
        if declared_size > ATTACHMENT_MAX_BYTES:
            _warn(f"slack: {name} over the "
                  f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB cap, skipped")
            return None
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not url:
            return None
        try:
            data = self._download_file(url)
        except Exception:
            _warn(f"slack: download failed for attachment {name!r}, skipped")
            return None
        if len(data) > ATTACHMENT_MAX_BYTES:
            _warn(f"slack: {name} over the size cap after download, skipped")
            return None
        sniffed = sniff_image_mime(data)
        if sniffed not in IMAGE_MIME_ALLOWLIST:
            _warn(f"slack: {name} is not a recognized image, skipped")
            return None
        directory = quarantine_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"slack-{(ts or 'msg').replace('.', '-')}-{index}-{name}"
        path.write_bytes(data)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return Attachment(
            filename=name,
            mime_type=sniffed,
            size_bytes=len(data),
            path=str(path),
            remote_id=str(file_obj.get("id") or ""),
        )

    def _capture_files(self, files: list, ts: str) -> List[Attachment]:
        captured: List[Attachment] = []
        for index, file_obj in enumerate(files):
            if len(captured) >= MAX_ATTACHMENTS_PER_MESSAGE:
                _warn("slack: attachment cap reached, extra files skipped")
                break
            if not isinstance(file_obj, dict):
                continue
            attachment = self._capture_file(file_obj, ts, index)
            if attachment is not None:
                captured.append(attachment)
        return captured

    # -- polling ---------------------------------------------------------------

    def _inbound_from(self, msg: dict) -> Optional[InboundMessage]:
        """One history/replies message → InboundMessage, or None to skip.

        Files are fetched only for allowlisted senders — a non-allowlisted
        sender's message is dropped later anyway, so its bytes are never
        downloaded (fail closed, and no quarantine writes for strangers).
        """
        if msg.get("bot_id") or not msg.get("user"):
            return None  # never react to our own (or other bots') messages
        ts = msg.get("ts", "")
        sender = msg["user"]
        text = (msg.get("text") or "").strip()
        attachments: List[Attachment] = []
        files = msg.get("files") or []
        if files and self.sender_allowed(sender):
            attachments = self._capture_files(files, ts)
        if not text and not attachments:
            return None
        return InboundMessage(
            channel="slack",
            sender=sender,
            text=text,
            thread_id=msg.get("thread_ts") or ts,
            ts=ts,
            attachments=attachments,
        )

    def poll(self, state: Dict[str, Any]) -> List[InboundMessage]:
        if not self.is_configured():
            return []
        collected: List[InboundMessage] = []

        params = {"channel": self._channel_id(), "limit": 50}
        oldest = state.get("slack_last_ts", "")
        if oldest:
            params["oldest"] = oldest
        try:
            result = self._api("conversations.history", params=params)
        except Exception:
            result = {}
        if result.get("ok"):
            max_ts = oldest
            for msg in result.get("messages", []):
                ts = msg.get("ts", "")
                max_ts = _max_ts(max_ts, ts)
                if oldest and ts and float(ts) <= float(oldest):
                    continue
                inbound = self._inbound_from(msg)
                if inbound is not None:
                    collected.append(inbound)
            if max_ts:
                state["slack_last_ts"] = max_ts

        collected.extend(self._poll_watched_threads(state))
        collected.sort(key=lambda m: float(m.ts or 0))  # oldest first
        return collected

    def _poll_watched_threads(self, state: Dict[str, Any]) -> List[InboundMessage]:
        threads = dict(state.get("slack_threads") or {})
        if not threads:
            return []
        collected: List[InboundMessage] = []
        for thread_ts, cursor in list(threads.items()):
            try:
                result = self._api("conversations.replies", params={
                    "channel": self._channel_id(),
                    "ts": thread_ts,
                    "oldest": cursor or thread_ts,
                    "limit": 50,
                })
            except Exception:
                continue
            if not result.get("ok"):
                continue
            newest = cursor
            for msg in result.get("messages", []):
                ts = msg.get("ts", "")
                newest = _max_ts(newest, ts)
                if ts == thread_ts:
                    continue  # the thread parent came through history
                if cursor and ts and float(ts) <= float(cursor):
                    continue
                if msg.get("subtype") == "thread_broadcast":
                    continue  # broadcast replies arrive via history
                inbound = self._inbound_from(msg)
                if inbound is not None:
                    inbound.thread_id = thread_ts
                    collected.append(inbound)
            threads[thread_ts] = newest
        state["slack_threads"] = _prune_threads(threads, self.MAX_WATCHED_THREADS)
        return collected


def _max_ts(current: str, candidate: str) -> str:
    """The larger of two Slack ts strings, tolerating junk."""
    if not candidate:
        return current
    if not current:
        return candidate
    try:
        return candidate if float(candidate) > float(current) else current
    except (TypeError, ValueError):
        return current


def _prune_threads(threads: Dict[str, str], keep: int) -> Dict[str, str]:
    """Keep the *keep* most recently active watched threads."""
    if len(threads) <= keep:
        return threads

    def activity(item):
        try:
            return float(item[1] or item[0])
        except (TypeError, ValueError):
            return 0.0

    newest = sorted(threads.items(), key=activity, reverse=True)[:keep]
    return dict(newest)


def _warn(text: str):
    import sys
    print(f"  \033[33m⚠ {text}\033[0m", file=sys.stderr)


# ---------------------------------------------------------------------------
# SMS (Twilio)
# ---------------------------------------------------------------------------

class TwilioSMSChannel(Channel):
    """SMS via Twilio's REST API (polling). Config: twilio_from (your Twilio
    number), sms_to (default recipient), sms_allowed_senders (phone numbers).
    Credentials: TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN."""

    name = "sms"
    API = "https://api.twilio.com/2010-04-01"

    def _credentials(self) -> tuple:
        return (
            os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
            os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        )

    def is_configured(self) -> bool:
        sid, token = self._credentials()
        return bool(sid and token and (self.config.get("twilio_from") or "").strip())

    def _request(self, path: str, form: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        sid, token = self._credentials()
        url = f"{self.API}/Accounts/{sid}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def send(self, text: str, thread_id: str = "") -> tuple:
        if not self.is_configured():
            return False, "sms not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN + twilio_from)"
        to = thread_id or (self.config.get("sms_to") or "").strip()
        if not to:
            return False, "sms send failed: no recipient (set sms_to)"
        try:
            result = self._request("/Messages.json", form={
                "From": self.config["twilio_from"].strip(),
                "To": to,
                "Body": text[:1500],  # stay within concatenated-SMS sanity
            })
        except Exception as exc:
            return False, f"sms send failed: {exc}"
        return True, result.get("sid", "")

    def poll(self, state: Dict[str, Any]) -> List[InboundMessage]:
        if not self.is_configured():
            return []
        try:
            result = self._request("/Messages.json", params={
                "To": self.config["twilio_from"].strip(),
                "PageSize": 20,
            })
        except Exception:
            return []
        seen = set(state.get("sms_seen_sids", []))
        first_poll = "sms_seen_sids" not in state
        messages: List[InboundMessage] = []
        all_sids: List[str] = []
        for msg in result.get("messages", []):
            sid = msg.get("sid", "")
            if not sid:
                continue
            all_sids.append(sid)
            if str(msg.get("direction", "")).startswith("outbound"):
                continue
            if sid in seen or first_poll:
                continue  # first poll only establishes the cursor
            text = (msg.get("body") or "").strip()
            sender = (msg.get("from") or "").strip()
            if not text or not sender:
                continue
            messages.append(InboundMessage(
                channel="sms", sender=sender, text=text, thread_id=sender,
            ))
        state["sms_seen_sids"] = all_sids[:100] + [s for s in seen if s not in all_sids][:100]
        messages.reverse()
        return messages


# ---------------------------------------------------------------------------
# Email (SMTP out, IMAP in)
# ---------------------------------------------------------------------------

class EmailChannel(Channel):
    """Email via SMTP (send) and IMAP UNSEEN polling (receive). Config:
    email_address, email_to (default recipient), email_smtp_host,
    email_imap_host, optional *_port, email_allowed_senders. Password:
    EMAIL_PASSWORD (or the env var named by email_password_env)."""

    name = "email"

    def _password(self) -> str:
        env = (self.config.get("email_password_env") or "EMAIL_PASSWORD").strip()
        return os.environ.get(env, "").strip()

    def _address(self) -> str:
        return (self.config.get("email_address") or "").strip()

    def is_configured(self) -> bool:
        return bool(
            self._address()
            and self._password()
            and (self.config.get("email_smtp_host") or "").strip()
        )

    def send(self, text: str, thread_id: str = "") -> tuple:
        if not self.is_configured():
            return False, "email not configured (email_address + email_smtp_host + EMAIL_PASSWORD)"
        import smtplib
        from email.mime.text import MIMEText

        to = thread_id or (self.config.get("email_to") or "").strip() or self._address()
        msg = MIMEText(text)
        msg["Subject"] = "conch"
        msg["From"] = self._address()
        msg["To"] = to
        host = self.config["email_smtp_host"].strip()
        port = int(self.config.get("email_smtp_port", 465) or 465)
        try:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                smtp.login(self._address(), self._password())
                smtp.sendmail(self._address(), [to], msg.as_string())
        except Exception as exc:
            return False, f"email send failed: {exc}"
        return True, to

    def poll(self, state: Dict[str, Any]) -> List[InboundMessage]:
        if not self.is_configured() or not (self.config.get("email_imap_host") or "").strip():
            return []
        import email as email_mod
        import email.utils
        import imaplib

        host = self.config["email_imap_host"].strip()
        port = int(self.config.get("email_imap_port", 993) or 993)
        messages: List[InboundMessage] = []
        try:
            imap = imaplib.IMAP4_SSL(host, port)
            try:
                imap.login(self._address(), self._password())
                imap.select("INBOX")
                status, data = imap.search(None, "UNSEEN")
                if status != "OK":
                    return []
                for num in (data[0].split() if data and data[0] else []):
                    status, parts = imap.fetch(num, "(RFC822)")
                    if status != "OK" or not parts or not parts[0]:
                        continue
                    parsed = email_mod.message_from_bytes(parts[0][1])
                    sender = email.utils.parseaddr(parsed.get("From", ""))[1]
                    body = _email_body(parsed)
                    if not body.strip() or not sender:
                        continue
                    messages.append(InboundMessage(
                        channel="email", sender=sender,
                        text=body.strip(), thread_id=sender,
                    ))
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception:
            return []
        return messages


def _email_body(parsed) -> str:
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = parsed.get_payload(decode=True)
    return payload.decode("utf-8", errors="replace") if payload else ""


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

CHANNEL_TYPES = {
    "slack": SlackChannel,
    "sms": TwilioSMSChannel,
    "email": EmailChannel,
}


class ChannelManager:
    """Holds configured channels; routes notifications and polls inbound."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.channels: Dict[str, Channel] = {}
        for name, cls in CHANNEL_TYPES.items():
            channel = cls(self.config)
            if channel.is_configured():
                self.channels[name] = channel

    def configured(self) -> List[str]:
        return sorted(self.channels)

    def get(self, name: str) -> Optional[Channel]:
        return self.channels.get((name or "").strip().lower())

    def notify(self, text: str, channel: str = "", thread_id: str = "") -> tuple:
        """Send *text* over *channel* (default: config notify_channel)."""
        name = (channel or self.config.get("notify_channel") or "").strip().lower()
        target = self.get(name)
        if target is None:
            return False, f"channel '{name or '(unset)'}' not configured"
        return target.send(text, thread_id=thread_id)

    def poll_all(self) -> List[InboundMessage]:
        """Poll every configured channel; return allowlisted inbound messages
        (oldest first). Non-allowlisted senders are dropped and noted."""
        state = load_cursor_state()
        inbound: List[InboundMessage] = []
        for name, channel in self.channels.items():
            for message in channel.poll(state):
                if not channel.sender_allowed(message.sender):
                    import sys
                    print(
                        f"  \033[33m⚠ {name}: dropped inbound from "
                        f"non-allowlisted sender {message.sender!r}\033[0m",
                        file=sys.stderr,
                    )
                    continue
                inbound.append(message)
        save_cursor_state(state)
        return inbound
