"""Watched-folder intake for the eBay listing flow (edge daemon service).

Drop item photos (plus an optional ``.txt`` of notes) into
``ebay_watch_folder`` and the daemon starts the same governed listing
flow the Slack intake uses — drafting, clarification, revision review,
and the exact-approval publish gate. The pack engine's channel surface
(:class:`PackChannelFlow`) drives every step; this module only supplies
a local "folder" transport around it:

- outbound flow messages ride the kernel outbox (``notify_channel``
  when configured, the daemon log otherwise) and are mirrored into the
  drop record shown by ``/ebay drops`` and the status exporter;
- inbound continuation (clarify answers, approve/deny) arrives through
  the kernel control socket as ``ebay_folder`` inbox events posted by
  the ``/ebay answer|approve|deny`` shell verbs, so the daemon stays
  the single writer of flow state.

Safety invariants:

- a drop is **never consent to sell**: the folder surface forces the
  channel auto-publish opt-in off, so publishing always requires the
  exact-approval challenge, regardless of caps;
- images are admitted on magic bytes only, with the Slack intake's caps
  (12 MB each, 12 per drop); rejects land in ``rejected/`` with a
  reason file, originals are moved to ``processed/`` — never deleted;
- drops are deduplicated by content digest (restart-safe), and the
  dedupe record survives in the same atomic 0600 state file pattern the
  channel cursors use.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..channels import (
    ATTACHMENT_MAX_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    Attachment,
    InboundMessage,
    quarantine_dir,
)
from .errors import CapitolError
from .packs import load_pack
from .packs.engine import PackChannelFlow, clean_text
from .packs.state import PackState

__all__ = [
    "FOLDER_CHANNEL",
    "FOLDER_SENDER",
    "FolderIntakeService",
    "FolderListingFlow",
    "ebay_flow_config",
    "folder_state_path",
]

#: Synthetic channel name for the folder surface. Approvals created by
#: folder sessions are origin-bound to this channel, so an ``approve``
#: from a Slack thread (or any other origin) can never consume them.
FOLDER_CHANNEL = "folder"
#: Synthetic sender for folder drops — local, single-operator.
FOLDER_SENDER = "folder:local"
#: Inbox source consumed by the service (posted by the shell verbs).
INBOX_SOURCE = "ebay_folder"

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_NOTE_SUFFIXES = (".txt",)
_SKIP_SUFFIXES = (".part", ".crdownload", ".tmp", ".download")
_MAX_NOTES_CHARS = 4000
_MAX_HISTORY_LINES = 40


def ebay_flow_config(config: dict) -> dict:
    """The config the eBay flows run under.

    ``ebay_agent`` (when set) overrides ``capitol_agent`` so listing
    sessions target the eBay orchestrator while the rest of conch keeps
    its default agent. Returns a shallow copy; the caller's config is
    never mutated.
    """
    flow_config = dict(config or {})
    ebay_agent = str(flow_config.get("ebay_agent") or "").strip()
    if ebay_agent:
        flow_config["capitol_agent"] = ebay_agent
    return flow_config


def folder_state_path() -> Path:
    base = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    ) / "conch"
    return base / "ebay_drops.json"


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


class FolderListingFlow(PackChannelFlow):
    """The eBay pack's channel surface bound to the local folder
    transport. Auto-publish is structurally off: dropping a file is not
    consent to sell, so every publish takes the exact-approval path."""

    def __init__(self, config: dict, approvals,
                 notify: Callable[[str, str, str], Any], *,
                 state: Optional[PackState] = None):
        flow_config = ebay_flow_config(config)
        # A drop is never consent to sell: the channel-auto opt-in is
        # forced off for this surface no matter what the config says.
        flow_config["ebay_channel_auto_publish"] = "false"
        super().__init__(
            load_pack("ebay-listing"), flow_config, approvals, notify,
            state=state,
        )
        # The pack's channel_message intake names the real channel
        # (slack); this surface accepts the same spec on its own name.
        self.channel = FOLDER_CHANNEL

    def enabled(self) -> bool:  # folder gating happens in the service
        return True


class FolderIntakeService:
    """Edge-daemon service: watch ``ebay_watch_folder`` for drops and
    drive listing sessions through the pack engine.

    Constructed by the daemon-service seam with ``(store, config, log,
    clock)``; ``tick(stats)`` is called on the daemon cadence and
    self-throttles to ``ebay_watch_poll_seconds``.
    """

    def __init__(self, store, config: dict, log=print, clock=None):
        self.store = store
        self.config = config or {}
        self.log = log
        self.clock = clock or time.time
        self._last_poll = 0.0
        self._flow: Optional[FolderListingFlow] = None
        self._flow_error = ""
        self._state_path = folder_state_path()

    # -- gating ---------------------------------------------------------------

    def _watch_dir(self) -> Optional[Path]:
        raw = str(self.config.get("ebay_watch_folder") or "").strip()
        if not raw:
            return None
        return Path(raw).expanduser()

    def enabled(self) -> bool:
        if self._watch_dir() is None:
            return False
        return bool(
            str(self.config.get("capitol_base_url") or "").strip()
            and str(self.config.get("capitol_org") or "").strip()
            and str(self.config.get("capitol_agent")
                    or self.config.get("ebay_agent") or "").strip()
        )

    def _poll_seconds(self) -> float:
        try:
            value = float(self.config.get("ebay_watch_poll_seconds") or 5)
        except (TypeError, ValueError):
            value = 5.0
        return min(max(value, 1.0), 300.0)

    def _debounce_seconds(self) -> float:
        try:
            value = float(
                self.config.get("ebay_watch_debounce_seconds") or 8
            )
        except (TypeError, ValueError):
            value = 8.0
        return min(max(value, 1.0), 120.0)

    # -- flow plumbing ----------------------------------------------------------

    def _get_flow(self) -> Optional[FolderListingFlow]:
        if self._flow is None:
            try:
                from ..remote import ApprovalStore

                self._flow = FolderListingFlow(
                    self.config, ApprovalStore(), self._flow_notify
                )
                self._flow_error = ""
            except Exception as exc:  # config/pack problems must not
                message = f"{type(exc).__name__}: {exc}"  # brick the daemon
                if message != self._flow_error:
                    self._flow_error = message
                    self.log(f"ebay folder intake disabled: {message}")
                return None
        return self._flow

    def _flow_notify(self, text: str, channel: str, thread_id: str) -> None:
        """Outbound flow messages: kernel outbox (notify_channel or the
        daemon log) + the drop record for /ebay drops and the exporter."""
        drop_id = str(thread_id or "")
        body = f"[ebay drop {drop_id}] {text}"
        try:
            self.store.enqueue_outbox(
                "notify",
                {"text": body, "title": "conch eBay"},
                dedupe_key=(
                    f"ebaydrop:{drop_id}:"
                    + hashlib.sha256(body.encode()).hexdigest()[:16]
                ),
            )
        except Exception as exc:
            self.log(f"ebay folder notify enqueue failed: {exc}")
        self._append_history(drop_id, text)

    def _append_history(self, drop_id: str, text: str) -> None:
        state = _load_state(self._state_path)
        drops = state.setdefault("drops", {})
        record = drops.setdefault(drop_id, {})
        history = record.setdefault("history", [])
        history.append({
            "at": self.clock(),
            "text": clean_text(text, 600),
        })
        del history[:-_MAX_HISTORY_LINES]
        record["updated_at"] = self.clock()
        _save_state(self._state_path, state)

    # -- tick ------------------------------------------------------------------

    def tick(self, stats: Dict[str, int]) -> None:
        if not self.enabled():
            return
        now = self.clock()
        if now - self._last_poll < self._poll_seconds():
            return
        self._last_poll = now
        try:
            self._consume_inbox_events()
            self._scan_folder()
        except Exception as exc:
            # The watcher must never take the daemon down.
            self.log(f"ebay folder intake tick failed: {exc}")
        else:
            stats["ebay_folder_polls"] = stats.get(
                "ebay_folder_polls", 0
            ) + 1

    # -- inbound continuation (shell verbs via kernel inbox) --------------------

    def _consume_inbox_events(self) -> None:
        state = _load_state(self._state_path)
        last_seen = int(state.get("inbox_seen", 0))
        entries = self.store.list_inbox(source=INBOX_SOURCE, limit=100)
        advanced = last_seen
        for entry in entries:
            inbox_id = int(entry.get("inbox_id") or 0)
            if inbox_id <= last_seen:
                continue
            advanced = max(advanced, inbox_id)
            payload = entry.get("payload") or {}
            try:
                self._handle_verb(payload)
            except Exception as exc:
                self.log(f"ebay folder verb failed: {exc}")
        if advanced != last_seen:
            state["inbox_seen"] = advanced
            _save_state(self._state_path, state)

    def _handle_verb(self, payload: Dict[str, Any]) -> None:
        verb = str(payload.get("verb") or "")
        flow = self._get_flow()
        if flow is None:
            return
        if verb == "answer":
            drop_id = str(payload.get("drop") or "")
            text = clean_text(str(payload.get("text") or ""), 2000)
            if not (drop_id and text):
                return
            message = InboundMessage(
                channel=FOLDER_CHANNEL, sender=FOLDER_SENDER,
                text=text, thread_id=drop_id, ts=f"answer-{time.time()}",
            )
            reply = flow.handle_message(message)
            if reply:
                self._flow_notify(reply, FOLDER_CHANNEL, drop_id)
            return
        if verb in ("approve", "deny"):
            request_id = int(payload.get("request_id") or 0)
            pending = flow.approvals.pending().get(str(request_id))
            if not pending:
                self._flow_notify(
                    f"approval #{request_id} is not pending "
                    "(expired, consumed, or unknown).",
                    FOLDER_CHANNEL, str(payload.get("drop") or ""),
                )
                return
            if str(pending.get("channel") or "") != FOLDER_CHANNEL:
                # Never consume an approval that belongs to another
                # origin (a Slack thread's approval stays Slack's).
                self._flow_notify(
                    f"approval #{request_id} belongs to another "
                    "surface and was left untouched.",
                    FOLDER_CHANNEL, str(payload.get("drop") or ""),
                )
                return
            entry, error = flow.approvals.consume(
                request_id,
                channel=FOLDER_CHANNEL,
                thread_id=str(pending.get("thread_id") or ""),
                sender=str(pending.get("sender") or FOLDER_SENDER),
            )
            drop_id = str(pending.get("thread_id") or "")
            if entry is None:
                self._flow_notify(
                    f"approval #{request_id} could not be consumed "
                    f"({error}).",
                    FOLDER_CHANNEL, drop_id,
                )
                return
            message = InboundMessage(
                channel=FOLDER_CHANNEL, sender=FOLDER_SENDER,
                text=verb, thread_id=drop_id, ts=f"{verb}-{time.time()}",
            )
            reply = flow.handle_approval(request_id, entry, verb, message)
            if reply:
                self._flow_notify(reply, FOLDER_CHANNEL, drop_id)

    # -- folder scanning ---------------------------------------------------------

    def _scan_folder(self) -> None:
        watch = self._watch_dir()
        if watch is None:
            return
        try:
            watch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log(f"ebay watch folder unavailable: {exc}")
            return
        now = self.clock()
        debounce = self._debounce_seconds()
        candidates: List[Path] = []
        for item in sorted(watch.iterdir()):
            if not item.is_file() or item.name.startswith("."):
                continue
            suffix = item.suffix.lower()
            if suffix in _SKIP_SUFFIXES:
                continue
            if suffix not in _IMAGE_SUFFIXES + _NOTE_SUFFIXES:
                self._reject(watch, item, "unsupported file type")
                continue
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if now - mtime < debounce:
                return  # a drop is still landing; wait for quiet
            candidates.append(item)
        images = [p for p in candidates
                  if p.suffix.lower() in _IMAGE_SUFFIXES]
        notes_files = [p for p in candidates
                       if p.suffix.lower() in _NOTE_SUFFIXES]
        if not images:
            # Notes without photos cannot start a listing; leave them
            # for the next drop that brings images.
            return
        self._process_drop(watch, images, notes_files)

    def _reject(self, watch: Path, item: Path, reason: str) -> None:
        rejected = watch / "rejected"
        try:
            rejected.mkdir(exist_ok=True)
            target = rejected / item.name
            shutil.move(str(item), str(target))
            (rejected / f"{item.name}.reason.txt").write_text(
                reason + "\n"
            )
        except OSError as exc:
            self.log(f"ebay folder reject move failed for "
                     f"{item.name}: {exc}")
        self.log(f"ebay folder intake rejected {item.name}: {reason}")

    def _process_drop(self, watch: Path, images: List[Path],
                      notes_files: List[Path]) -> None:
        if len(images) > MAX_ATTACHMENTS_PER_MESSAGE:
            for extra in images[MAX_ATTACHMENTS_PER_MESSAGE:]:
                self._reject(
                    watch, extra,
                    f"drop exceeds {MAX_ATTACHMENTS_PER_MESSAGE} photos",
                )
            images = images[:MAX_ATTACHMENTS_PER_MESSAGE]

        validated: List[tuple] = []  # (path, bytes, mime, digest)
        for image in images:
            try:
                data = image.read_bytes()
            except OSError as exc:
                self._reject(watch, image, f"unreadable: {exc}")
                continue
            if len(data) > ATTACHMENT_MAX_BYTES:
                self._reject(
                    watch, image,
                    f"{len(data)} bytes exceeds the "
                    f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB cap",
                )
                continue
            from ..channels import sniff_image_mime

            mime = sniff_image_mime(data)
            if not mime:
                self._reject(
                    watch, image,
                    "content is not a JPEG/PNG/GIF/WebP image",
                )
                continue
            digest = hashlib.sha256(data).hexdigest()
            validated.append((image, data, mime, digest))
        if not validated:
            return

        drop_digest = hashlib.sha256(
            "\n".join(sorted(d for _, _, _, d in validated)).encode()
        ).hexdigest()
        drop_id = f"drop-{drop_digest[:12]}"

        state = _load_state(self._state_path)
        drops = state.setdefault("drops", {})
        if drop_id in drops:
            # Restart or duplicate re-drop: never start a second
            # session for identical content. Move the files aside with
            # a note so the folder does not wedge.
            self._archive(watch, [p for p, _, _, _ in validated]
                          + notes_files, drop_id, note="duplicate drop")
            self.log(f"ebay folder intake: duplicate {drop_id} skipped")
            return

        notes = ""
        for notes_file in notes_files:
            try:
                notes += notes_file.read_text(errors="replace") + "\n"
            except OSError:
                continue
        notes = clean_text(notes, _MAX_NOTES_CHARS)

        qdir = quarantine_dir() / f"ebay-{drop_id}"
        qdir.mkdir(parents=True, exist_ok=True)
        attachments: List[Attachment] = []
        for path, data, mime, digest in validated:
            target = qdir / path.name
            target.write_bytes(data)
            attachments.append(Attachment(
                filename=path.name, mime_type=mime,
                size_bytes=len(data), path=str(target),
                remote_id=digest[:16],
            ))

        drops[drop_id] = {
            "digest": drop_digest,
            "created_at": self.clock(),
            "updated_at": self.clock(),
            "photos": [a.filename for a in attachments],
            "notes": bool(notes),
            "history": [],
        }
        _save_state(self._state_path, state)

        self._archive(watch, [p for p, _, _, _ in validated]
                      + notes_files, drop_id)

        flow = self._get_flow()
        if flow is None:
            self._flow_notify(
                "drop received but the listing flow is not available "
                f"({self._flow_error or 'Capitol not configured'}); "
                "it will not be retried automatically — re-drop after "
                "fixing the configuration.",
                FOLDER_CHANNEL, drop_id,
            )
            return
        message = InboundMessage(
            channel=FOLDER_CHANNEL, sender=FOLDER_SENDER,
            text=notes, thread_id=drop_id, ts=drop_id,
            attachments=attachments,
        )
        self._flow_notify(
            f"new drop: {len(attachments)} photo(s)"
            + (" + notes" if notes else "") + " — drafting …",
            FOLDER_CHANNEL, drop_id,
        )
        try:
            reply = flow.handle_message(message)
        except CapitolError as exc:
            reply = f"listing flow failed: {clean_text(exc, 400)}"
        if reply:
            self._flow_notify(reply, FOLDER_CHANNEL, drop_id)

    def _archive(self, watch: Path, files: List[Path], drop_id: str,
                 *, note: str = "") -> None:
        processed = watch / "processed" / drop_id
        try:
            processed.mkdir(parents=True, exist_ok=True)
            for path in files:
                if path.exists():
                    shutil.move(str(path), str(processed / path.name))
            if note:
                (processed / "NOTE.txt").write_text(note + "\n")
        except OSError as exc:
            self.log(f"ebay folder archive failed for {drop_id}: {exc}")


def drops_summary() -> List[Dict[str, Any]]:
    """Read-only view of folder drops for /ebay drops and the status
    exporter — safe to call from the shell while the daemon runs."""
    state = _load_state(folder_state_path())
    pack_state = PackState(filename=load_pack("ebay-listing").state_file)
    sessions = pack_state.load().get("sessions", {})
    by_thread: Dict[str, Dict[str, Any]] = {}
    for session_id, session in sessions.items():
        thread = str(session.get("thread_id") or "")
        if thread:
            by_thread[thread] = dict(session, session_id=session_id)
    out: List[Dict[str, Any]] = []
    for drop_id, record in sorted(
        (state.get("drops") or {}).items(),
        key=lambda kv: kv[1].get("created_at") or 0,
    ):
        session = by_thread.get(drop_id) or {}
        out.append({
            "drop_id": drop_id,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "photos": record.get("photos") or [],
            "notes": bool(record.get("notes")),
            "phase": str(session.get("phase") or "received"),
            "session_id": str(session.get("session_id") or ""),
            "approval_id": session.get("approval_id"),
            "history": record.get("history") or [],
        })
    return out
