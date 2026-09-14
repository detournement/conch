"""Channel gateways for the remote agentic loop (plan 4.3).

Outbound: conch messages the user proactively (scheduled task results,
approval requests, notifications). Inbound: polled replies resume/steer
sessions. Four gateways, all stdlib-only:

- Matrix: self-hosted homeserver (access token by env reference),
  /sync long-poll, m.room.message send with thread relations
- Slack: bot token (SLACK_BOT_TOKEN), chat.postMessage / conversations.history
- SMS: Twilio REST API (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN), polling the
  Messages list (webhooks would be lower latency but need a public endpoint;
  polling works behind NAT)
- Email: SMTP send + IMAP UNSEEN polling (EMAIL_PASSWORD)

Plus one outbound-only push notifier (ntfy) for interrupts: approval
requests, digests, mission milestones. It is not a Channel — it has no
inbound side — and is routed by ``notify_push = ntfy`` so a conversation
transport (Matrix) and an interrupt transport (ntfy) can coexist.

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
from time import time_ns
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
# Matrix (sovereign phone channel)
# ---------------------------------------------------------------------------

class MatrixChannel(Channel):
    """Matrix client-server API over plain urllib (no SDK, no new deps).

    Config: ``matrix_homeserver`` (base URL — your own homeserver, or a
    local pantalaimon proxy for E2EE rooms), ``matrix_room`` (default
    room id), ``matrix_allowed_senders`` (FULL Matrix user IDs,
    ``@user:server`` — fail closed), optional ``matrix_user`` (the bot's
    own user id; discovered via /whoami when unset) and
    ``matrix_sync_timeout_ms`` (long-poll window, default 10000). The
    access token is held by reference only: the env var named by
    ``matrix_token_env`` (default MATRIX_ACCESS_TOKEN). It rides the
    Authorization header and is never logged, echoed, or put in a URL.

    Polling: ``/sync`` long-poll. The server's ``timeout`` parameter maps
    naturally onto the poll loop — one ``poll()`` call blocks up to the
    sync window and returns as soon as anything arrives. The ``since``
    cursor persists in channel cursor state (``matrix_since``) exactly
    like the other channels' cursors; the very first sync only
    establishes the cursor (no history replay), matching the SMS
    first-poll rule.

    Threads: ``thread_id`` is the room id, or ``<room id>|<thread root
    event id>`` when the message carries an ``m.thread`` relation —
    replies are sent with the same relation (plus the reply fallback) so
    an Element thread carries a whole conversation, mirroring Slack.

    Attachments: ``m.image`` events from allowlisted senders are fetched
    through the media API (authenticated v1 endpoint, falling back to
    the legacy v3 path for older servers), validated exactly like Slack
    (image magic bytes, size cap, per-message cap) and quarantined.

    E2EE, honestly: stdlib cannot do Olm/Megolm. v1 supports (a)
    unencrypted rooms on your OWN homeserver over TLS — sovereign
    transport, plaintext at rest on your own box — or (b) pointing
    ``matrix_homeserver`` at a self-hosted pantalaimon proxy, which
    handles encryption transparently. See README "Phone: sovereign
    setup" for the trade-offs.
    """

    name = "matrix"
    SYNC_FILTER = '{"room":{"timeline":{"limit":50}}}'
    DEFAULT_SYNC_TIMEOUT_MS = 10000

    def _token(self) -> str:
        env = (self.config.get("matrix_token_env") or "MATRIX_ACCESS_TOKEN").strip()
        return os.environ.get(env, "").strip()

    def _homeserver(self) -> str:
        return (self.config.get("matrix_homeserver") or "").strip().rstrip("/")

    def _room(self) -> str:
        return (self.config.get("matrix_room") or "").strip()

    def is_configured(self) -> bool:
        return bool(self._token() and self._homeserver() and self._room())

    def _sync_timeout_ms(self) -> int:
        try:
            value = int(self.config.get("matrix_sync_timeout_ms",
                                        self.DEFAULT_SYNC_TIMEOUT_MS))
        except (TypeError, ValueError):
            value = self.DEFAULT_SYNC_TIMEOUT_MS
        return max(0, min(value, 60000))

    def _request(self, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None, method: str = "",
                 timeout: float = 15.0) -> dict:
        url = self._homeserver() + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self._token()}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=headers,
            method=method or ("POST" if data is not None else "GET"),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # -- identity ------------------------------------------------------------

    def _own_user(self, state: Dict[str, Any]) -> str:
        """The bot's own user id (to skip echoes of our own sends).
        Config ``matrix_user`` wins; otherwise /whoami once, cached in
        cursor state."""
        configured = (self.config.get("matrix_user") or "").strip()
        if configured:
            return configured
        cached = str(state.get("matrix_own_user") or "").strip()
        if cached:
            return cached
        try:
            result = self._request("/_matrix/client/v3/account/whoami")
        except Exception:
            return ""
        user_id = str(result.get("user_id") or "").strip()
        if user_id:
            state["matrix_own_user"] = user_id
        return user_id

    # -- send ------------------------------------------------------------------

    @staticmethod
    def _split_thread(thread_id: str) -> tuple:
        """``room`` or ``room|thread-root`` → (room_id, root_event_id)."""
        raw = str(thread_id or "").strip()
        if "|" in raw:
            room, root = raw.split("|", 1)
            return room.strip(), root.strip()
        return raw, ""

    def send(self, text: str, thread_id: str = "") -> tuple:
        if not self.is_configured():
            return False, ("matrix not configured (matrix_homeserver +"
                           " matrix_room + MATRIX_ACCESS_TOKEN)")
        room, root = self._split_thread(thread_id)
        room = room or self._room()
        content: Dict[str, Any] = {"msgtype": "m.text", "body": text}
        if root:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": root,
                # Reply fallback so clients without thread support still
                # render the message in context (spec 11.6.2.1).
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": root},
            }
        txn_id = f"conch{time_ns()}"
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}"
                f"/send/m.room.message/{txn_id}")
        try:
            result = self._request(path, body=content, method="PUT")
        except Exception as exc:
            return False, f"matrix send failed: {exc}"
        return True, str(result.get("event_id") or "")

    # -- attachments -------------------------------------------------------------

    def _download_media(self, mxc_url: str) -> bytes:
        """Fetch mxc:// media with the bearer. Tries the authenticated
        v1.11 endpoint first, then the legacy v3 path for older servers.
        The token rides only the Authorization header."""
        raw = str(mxc_url or "")
        if not raw.startswith("mxc://"):
            raise ValueError("not an mxc URL")
        server, _, media_id = raw[len("mxc://"):].partition("/")
        if not server or not media_id or "/" in media_id:
            raise ValueError("malformed mxc URL")
        quoted = (urllib.parse.quote(server), urllib.parse.quote(media_id))
        paths = (
            "/_matrix/client/v1/media/download/%s/%s" % quoted,
            "/_matrix/media/v3/download/%s/%s" % quoted,
        )
        last_error: Exception = OSError("no media endpoint answered")
        for path in paths:
            req = urllib.request.Request(
                self._homeserver() + path,
                headers={"Authorization": f"Bearer {self._token()}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read(ATTACHMENT_MAX_BYTES + 1)
            except Exception as exc:
                last_error = exc
        raise last_error

    def _capture_image(self, content: dict, event_id: str,
                       index: int) -> Optional[Attachment]:
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        name = _safe_filename(content.get("body"), fallback=f"image-{index}")
        declared_mime = str(info.get("mimetype") or "").lower()
        if declared_mime not in IMAGE_MIME_ALLOWLIST:
            return None  # not an image: never fetched
        try:
            declared_size = int(info.get("size") or 0)
        except (TypeError, ValueError):
            declared_size = 0
        if declared_size > ATTACHMENT_MAX_BYTES:
            _warn(f"matrix: {name} over the "
                  f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB cap, skipped")
            return None
        mxc = str(content.get("url") or "")
        if not mxc:
            return None  # v1 handles unencrypted media only (no `file`)
        try:
            data = self._download_media(mxc)
        except Exception:
            _warn(f"matrix: download failed for attachment {name!r}, skipped")
            return None
        if len(data) > ATTACHMENT_MAX_BYTES:
            _warn(f"matrix: {name} over the size cap after download, skipped")
            return None
        sniffed = sniff_image_mime(data)
        if sniffed not in IMAGE_MIME_ALLOWLIST:
            _warn(f"matrix: {name} is not a recognized image, skipped")
            return None
        directory = quarantine_dir()
        directory.mkdir(parents=True, exist_ok=True)
        safe_event = _safe_filename(event_id, fallback="event")
        path = directory / f"matrix-{safe_event}-{index}-{name}"
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
            remote_id=event_id,
        )

    # -- polling -------------------------------------------------------------------

    def _inbound_from(self, room_id: str, event: dict,
                      own_user: str) -> Optional[InboundMessage]:
        if event.get("type") != "m.room.message":
            return None
        sender = str(event.get("sender") or "").strip()
        if not sender or (own_user and sender == own_user):
            return None  # never react to our own messages
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        msgtype = str(content.get("msgtype") or "")
        event_id = str(event.get("event_id") or "")
        relates = content.get("m.relates_to")
        thread_id = room_id
        if isinstance(relates, dict) and relates.get("rel_type") == "m.thread":
            root = str(relates.get("event_id") or "").strip()
            if root:
                thread_id = f"{room_id}|{root}"
        text = ""
        attachments: List[Attachment] = []
        if msgtype in ("m.text", "m.notice"):
            text = str(content.get("body") or "").strip()
        elif msgtype == "m.image":
            # Fetch bytes only for allowlisted senders — a stranger's
            # message is dropped later anyway; its media is never fetched.
            if self.sender_allowed(sender):
                attachment = self._capture_image(content, event_id, 0)
                if attachment is not None:
                    attachments.append(attachment)
        else:
            return None
        if not text and not attachments:
            return None
        return InboundMessage(
            channel="matrix",
            sender=sender,
            text=text,
            thread_id=thread_id,
            ts=str(event.get("origin_server_ts") or ""),
            attachments=attachments,
        )

    def poll(self, state: Dict[str, Any]) -> List[InboundMessage]:
        if not self.is_configured():
            return []
        since = str(state.get("matrix_since") or "")
        params: Dict[str, Any] = {"filter": self.SYNC_FILTER}
        timeout_ms = self._sync_timeout_ms() if since else 0
        params["timeout"] = timeout_ms
        if since:
            params["since"] = since
        try:
            result = self._request(
                "/_matrix/client/v3/sync", params=params,
                timeout=15.0 + timeout_ms / 1000.0,
            )
        except Exception:
            return []
        next_batch = str(result.get("next_batch") or "")
        if next_batch:
            state["matrix_since"] = next_batch
        if not since:
            return []  # first sync only establishes the cursor
        own_user = self._own_user(state)
        collected: List[InboundMessage] = []
        joined = (result.get("rooms") or {}).get("join") or {}
        for room_id, room in joined.items():
            if not isinstance(room, dict):
                continue
            events = ((room.get("timeline") or {}).get("events")) or []
            captured = 0
            for event in events:
                if not isinstance(event, dict):
                    continue
                if captured >= MAX_ATTACHMENTS_PER_MESSAGE and \
                        str((event.get("content") or {}).get("msgtype")) == "m.image":
                    _warn("matrix: attachment cap reached, extra images skipped")
                    continue
                inbound = self._inbound_from(str(room_id), event, own_user)
                if inbound is not None:
                    captured += len(inbound.attachments)
                    collected.append(inbound)
        return collected


# ---------------------------------------------------------------------------
# ntfy push notifier (outbound-only interrupts)
# ---------------------------------------------------------------------------

class NtfyNotifier:
    """Outbound push over a self-hosted ntfy topic (POST, stdlib only).

    Not a Channel: there is no inbound side, no allowlist, no cursor —
    it exists so interrupts (approval requests, digests, mission
    milestones) reach a phone as real push notifications while Matrix
    carries the conversation. Config: ``ntfy_url`` + ``ntfy_topic``;
    optional ``ntfy_token_env`` (default NTFY_TOKEN — the token itself
    is held by reference and rides only the Authorization header),
    ``ntfy_priority`` (default priority), ``ntfy_click`` (deep link; when
    unset and ``matrix_room`` is configured, a matrix.to link to that
    room is used so tapping the notification opens Element).

    v1 actions are click/view deep-links only. A one-tap approve button
    would need an authenticated HTTP endpoint that does not exist yet;
    the named upgrade path is a tailnet-only listener (see README) —
    never a public endpoint.
    """

    def __init__(self, config: dict):
        self.config = config or {}

    def _token(self) -> str:
        env = (self.config.get("ntfy_token_env") or "NTFY_TOKEN").strip()
        return os.environ.get(env, "").strip()

    def _base(self) -> str:
        return (self.config.get("ntfy_url") or "").strip().rstrip("/")

    def _topic(self) -> str:
        return (self.config.get("ntfy_topic") or "").strip()

    def is_configured(self) -> bool:
        return bool(self._base() and self._topic())

    def _click_url(self, click: str) -> str:
        explicit = click or (self.config.get("ntfy_click") or "").strip()
        if explicit:
            return explicit
        room = (self.config.get("matrix_room") or "").strip()
        if room:
            return "https://matrix.to/#/" + urllib.parse.quote(room)
        return ""

    @staticmethod
    def _header_safe(value: str) -> str:
        """ntfy headers are plain HTTP headers: strip newlines and
        anything outside latin-1 (the body carries the full text)."""
        cleaned = str(value or "").replace("\n", " ").replace("\r", " ")
        return cleaned.encode("latin-1", errors="ignore").decode("latin-1")[:200]

    def send(self, text: str, title: str = "", priority: str = "",
             tags: str = "", click: str = "") -> tuple:
        if not self.is_configured():
            return False, "ntfy not configured (ntfy_url + ntfy_topic)"
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        title = title or (self.config.get("ntfy_title") or "conch").strip()
        if title:
            headers["Title"] = self._header_safe(title)
        priority = priority or (self.config.get("ntfy_priority") or "").strip()
        if priority:
            headers["Priority"] = self._header_safe(priority)
        if tags:
            headers["Tags"] = self._header_safe(tags)
        click_url = self._click_url(click)
        if click_url:
            headers["Click"] = self._header_safe(click_url)
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self._base()}/{urllib.parse.quote(self._topic())}"
        req = urllib.request.Request(
            url, data=str(text or "").encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode() or "{}")
        except Exception as exc:
            return False, f"ntfy push failed: {exc}"
        return True, str(result.get("id") or "")


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
# Fake loopback channel (tests, demos, live acceptance drills)
# ---------------------------------------------------------------------------

class FakeChannel(Channel):
    """File-backed loopback transport: inbound messages are JSON lines in
    ``<fake_channel_dir>/inbound.jsonl`` (``{"sender", "text", "thread_id"}``),
    outbound replies append to ``<fake_channel_dir>/outbound.jsonl``.

    It exists so the remote loop — including the daemon-hosted intake — can
    be exercised end to end without a real workspace: same allowlist
    enforcement (``fake_allowed_senders``, fail closed), same cursor
    semantics (``fake_offset`` advances exactly once per consumed line),
    same reply path. Never configure it alongside production traffic; it is
    a test harness, not a transport.
    """

    name = "fake"

    def _dir(self) -> Path:
        return Path(str(self.config.get("fake_channel_dir") or "").strip())

    def is_configured(self) -> bool:
        return bool(str(self.config.get("fake_channel_dir") or "").strip())

    def send(self, text: str, thread_id: str = "") -> tuple:
        if not self.is_configured():
            return False, "fake channel not configured (fake_channel_dir)"
        import time

        directory = self._dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.time(), "thread_id": thread_id, "text": text,
            }
            with open(directory / "outbound.jsonl", "a") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:
            return False, f"fake channel send failed: {exc}"
        return True, str(entry["ts"])

    def poll(self, state: Dict[str, Any]) -> List[InboundMessage]:
        if not self.is_configured():
            return []
        try:
            lines = (self._dir() / "inbound.jsonl").read_text().splitlines()
        except OSError:
            return []
        try:
            offset = int(state.get("fake_offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        messages: List[InboundMessage] = []
        for line in lines[offset:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            sender = str(entry.get("sender") or "").strip()
            text = str(entry.get("text") or "").strip()
            if not sender or not text:
                continue
            messages.append(InboundMessage(
                channel="fake",
                sender=sender,
                text=text,
                thread_id=str(entry.get("thread_id") or sender),
                ts=str(entry.get("ts") or ""),
            ))
        state["fake_offset"] = len(lines)
        return messages


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

CHANNEL_TYPES = {
    "matrix": MatrixChannel,
    "slack": SlackChannel,
    "sms": TwilioSMSChannel,
    "email": EmailChannel,
    "fake": FakeChannel,
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
        self.push_notifier = NtfyNotifier(self.config)

    def configured(self) -> List[str]:
        return sorted(self.channels)

    def get(self, name: str) -> Optional[Channel]:
        return self.channels.get((name or "").strip().lower())

    # -- push interrupts (ntfy) ---------------------------------------------

    def push_enabled(self) -> bool:
        """Push is routed only when ``notify_push = ntfy`` is set AND the
        notifier is configured — conversation channels stay unaffected."""
        transport = str(self.config.get("notify_push") or "").strip().lower()
        return transport == "ntfy" and self.push_notifier.is_configured()

    def push_interrupt(self, text: str, title: str = "",
                       priority: str = "", tags: str = "") -> tuple:
        """Best-effort push for interrupts (approvals, digests,
        milestones). Never raises; returns (ok, detail)."""
        if not self.push_enabled():
            return False, "push not configured (notify_push=ntfy + ntfy_url/ntfy_topic)"
        try:
            return self.push_notifier.send(
                text, title=title, priority=priority, tags=tags
            )
        except Exception as exc:  # pushing must never break delivery
            return False, f"ntfy push failed: {exc}"

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
