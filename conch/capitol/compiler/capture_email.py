"""Email-based capture source (capture component).

An opt-in reader over a *designated* IMAP folder — the capture mailbox —
normalizing its messages into the same capture-context shape the
Capture→Card synthesis consumes. It reuses the email channel's config
surface (``email_address``, ``email_imap_host``, ``EMAIL_PASSWORD`` by
env reference, ``email_allowed_senders``) plus one capture-specific key:
``capture_email_folder`` names the folder explicitly — there is no
default, and an unset folder means the source is absent (fail closed).

Discipline:

- **Allowlist**: only messages from ``email_allowed_senders`` are
  captured (exact-address match, the channel rule); an empty allowlist
  captures nothing. Non-allowlisted messages are skipped and counted —
  never read into the context.
- **Since-cursor**: a UID cursor per folder (state file under the XDG
  state dir) makes re-runs ingest only new mail; the cursor commits only
  after the capture actually produced a stored compilation, so a failed
  synthesis can be retried without losing the window.
- **Read-only**: the folder is opened read-only; capture never marks,
  moves, or deletes mail.
- **Bounded**: at most ``capture_email_max_messages`` per run, each
  body clipped; the assembled block is credential-guarded whole (a
  capture mailbox is curated input — a credential in it is an anomaly
  that rejects the capture by name, the journal rule).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import CapitolError
from .capture import CAPTURE_BLOCK_CAP, guard_capture_text

DEFAULT_MAX_MESSAGES = 25
_BODY_CAP = 1200


def _state_path() -> Path:
    root = os.environ.get("XDG_STATE_HOME", "")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "conch" / "capture" / "email_cursor.json"


def _load_cursors() -> Dict[str, int]:
    path = _state_path()
    try:
        data = json.loads(path.read_text())
        return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _save_cursor(folder: str, uid: int) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cursors = _load_cursors()
    cursors[folder] = int(uid)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursors, sort_keys=True))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def reset_cursor(folder: str) -> None:
    _save_cursor(folder, 0)


def _allowed_senders(config: dict) -> List[str]:
    raw = str(config.get("email_allowed_senders") or "")
    return [part.strip().lower() for part in raw.split(",")
            if part.strip()]


def email_capture_unconfigured_reason(config: dict) -> str:
    """Empty string when the source is usable; otherwise the exact
    missing piece (fail closed, named)."""
    if not str(config.get("capture_email_folder") or "").strip():
        return ("capture_email_folder is not set — name the designated "
                "capture folder explicitly (there is no default)")
    if not str(config.get("email_imap_host") or "").strip():
        return "email_imap_host is not configured"
    if not str(config.get("email_address") or "").strip():
        return "email_address is not configured"
    env = str(config.get("email_password_env")
              or "EMAIL_PASSWORD").strip()
    if not os.environ.get(env, "").strip():
        return f"{env} is not set in the environment"
    if not _allowed_senders(config):
        return ("email_allowed_senders is empty — the capture source is "
                "allowlist-gated exactly like the channel (empty = "
                "capture nothing)")
    return ""


def _body_text(parsed) -> str:
    # Local twin of the channel's text/plain extraction — deliberately
    # not imported from conch.channels (private helper there; capture
    # must survive the coming package split without edge internals).
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = parsed.get_payload(decode=True)
    return payload.decode("utf-8", errors="replace") if payload else ""


def _default_imap_factory(config: dict):
    import imaplib

    host = str(config["email_imap_host"]).strip()
    port = int(config.get("email_imap_port", 993) or 993)
    imap = imaplib.IMAP4_SSL(host, port)
    env = str(config.get("email_password_env")
              or "EMAIL_PASSWORD").strip()
    imap.login(
        str(config.get("email_address") or "").strip(),
        os.environ.get(env, "").strip(),
    )
    return imap


def _clip(text: str, cap: int) -> str:
    text = str(text or "").strip()
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def capture_from_email(
    config: dict, *,
    imap_factory: Optional[Callable[[dict], Any]] = None,
) -> Tuple[Dict[str, Any], Callable[[], None]]:
    """Read new mail from the capture folder into a capture context.

    Returns ``(context, commit)`` — call ``commit()`` only after the
    capture produced a stored compilation; it advances the UID cursor
    over everything scanned this run (allowlisted or skipped), so a
    retried failure re-reads the same window and a success never
    re-ingests.
    """
    reason = email_capture_unconfigured_reason(config)
    if reason:
        raise CapitolError(f"email capture unavailable: {reason}")
    import email as email_mod
    import email.utils as email_utils

    folder = str(config["capture_email_folder"]).strip()
    allowed = set(_allowed_senders(config))
    try:
        max_messages = int(
            config.get("capture_email_max_messages", DEFAULT_MAX_MESSAGES)
            or DEFAULT_MAX_MESSAGES
        )
    except (TypeError, ValueError):
        max_messages = DEFAULT_MAX_MESSAGES
    max_messages = max(1, min(max_messages, 200))
    since = _load_cursors().get(folder, 0)

    factory = imap_factory or _default_imap_factory
    imap = factory(config)
    entries: List[Dict[str, Any]] = []
    skipped = 0
    highest = since
    try:
        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise CapitolError(
                f"email capture: cannot open folder {folder!r} "
                "(check capture_email_folder)"
            )
        status, data = imap.uid("search", None, f"UID {since + 1}:*")
        if status != "OK":
            raise CapitolError("email capture: UID search failed")
        uids = [int(u) for u in (data[0].split() if data and data[0]
                                 else []) if int(u) > since]
        for uid in sorted(uids)[-max_messages:]:
            highest = max(highest, uid)
            status, parts = imap.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not parts or not parts[0]:
                continue
            parsed = email_mod.message_from_bytes(parts[0][1])
            sender = email_utils.parseaddr(
                parsed.get("From", "")
            )[1].lower()
            if sender not in allowed:
                skipped += 1
                continue
            try:
                stamp = email_utils.parsedate_to_datetime(
                    parsed.get("Date", "")
                ).timestamp()
            except (TypeError, ValueError):
                stamp = 0.0
            entries.append({
                "uid": uid,
                "sender": sender,
                "date": stamp,
                "subject": _clip(parsed.get("Subject", ""), 140),
                "body": _clip(_body_text(parsed), _BODY_CAP),
            })
        # cursor covers the whole scanned window, skipped mail included
        if uids:
            highest = max(highest, max(uids))
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    if not entries:
        raise CapitolError(
            f"email capture: no new allowlisted mail in {folder!r} "
            f"since UID {since}"
            + (f" ({skipped} message(s) skipped: sender not "
               "allowlisted)" if skipped else "")
            + " — /compile from-email --rescan re-reads the folder"
        )
    entries.sort(key=lambda e: (e["date"], e["uid"]))
    lines = [
        f"Email capture — folder {folder!r}, {len(entries)} message(s)"
        + (f", {skipped} skipped (sender not allowlisted)"
           if skipped else "") + ":",
    ]
    for entry in entries:
        when = (
            time.strftime("%Y-%m-%d %H:%M",
                          time.gmtime(entry["date"]))
            if entry["date"] else "undated"
        )
        lines.append(f"  [{when}] {entry['sender']} — "
                     f"{entry['subject']}")
        for row in entry["body"].splitlines():
            if row.strip():
                lines.append(f"    {row.rstrip()}")
    block = "\n".join(lines)
    if len(block) > CAPTURE_BLOCK_CAP:
        block = block[: CAPTURE_BLOCK_CAP - 15] + "\n... [clipped]"
    block = guard_capture_text(block)

    subjects = [entry["subject"] for entry in entries
                if entry["subject"]]
    context = {
        "kind": "email",
        "source_id": folder,
        "label": f"email folder {folder!r} "
                 f"({len(entries)} message(s))",
        "default_goal": (
            f"Automate the process described in the captured email "
            f"thread \"{subjects[0]}\"" if subjects else ""
        ),
        "block": block,
        "provenance": {
            "kind": "email",
            "source": folder,
            "uid_range": [entries[0]["uid"], highest],
            "messages": len(entries),
            "skipped": skipped,
        },
    }

    def commit() -> None:
        _save_cursor(folder, highest)

    return context, commit
