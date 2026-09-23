"""Browser-capture native messaging host (capture plan, browser satellite).

The MV3 extension in ``conch/satellites/browser_capture/`` observes DOM
interaction on user-allowlisted origins and hands events to this host
over Chrome native messaging — stdio with 4-byte-length JSON framing,
the browser-sanctioned transport (no listening ports, consistent with
the no-inbound-network posture). The host is the trust boundary: it
validates fail-closed (protocol version, caller extension origin, event
schema, web origin), runs the authoritative Python secretguard scrub
(reject-whole, never sanitize-and-continue), and forwards accepted
events to the edge kernel via the existing ``event.post`` control op —
journaled as ``inbox_received`` with ``source="browser"`` so the
deterministic mining and ``/compile from-browser`` read them exactly
like every other capture source.

Delivery never blocks the browser: control socket first, direct kernel
store when no daemon answers (the DirectKernelClient precedent), and a
bounded oldest-dropped spool as the last resort, drained on the next
successful contact.

Everything is gated: the host accepts nothing unless ``capture_enabled``
and ``capture_browser`` are both set (it answers the handshake with
``capture: false`` so the extension can show an honest OFF badge).
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple

from ..secretguard import CredentialRejected, credential_findings
from .store import default_state_dir

#: Native messaging host identity (the browser manifest key).
NATIVE_HOST_NAME = "com.conch.capture"

#: Extension ↔ host message protocol version (fail closed on mismatch).
HOST_PROTOCOL_VERSION = 1

#: The satellite extension's pinned id (its manifest carries the public
#: key this id derives from, so load-unpacked installs get it too).
EXTENSION_ID = "hgmjnpkpdnaeekckabogikdcpdfeeghh"

#: Event kinds the extension may report. Anything else fails closed.
BROWSER_EVENT_KINDS = ("nav", "click", "submit", "copy")

#: One framed message never exceeds this many bytes.
MAX_MESSAGE_BYTES = 64 * 1024

#: Bounds on individual event fields (defense in depth — the extension
#: clips too, but the host is the authority).
MAX_LABEL_CHARS = 120
MAX_PATH_CHARS = 200
MAX_FIELD_NAME_CHARS = 80
MAX_FORM_FIELDS = 40

#: Local spool bound when neither the daemon nor the kernel store can
#: take an event: size-capped, oldest dropped, never blocking.
SPOOL_MAX_EVENTS = 500

#: Field names that smell like secrets are dropped from submit events
#: even though only NAMES ever travel (the extension excludes them at
#: the source; this is the second net).
_SECRET_NAME_HINTS = (
    "pass", "secret", "token", "pwd", "otp", "auth", "cvv", "card",
    "ssn", "pin", "credential", "apikey", "api_key", "private",
)


class BrowserEventRejected(ValueError):
    """An extension message failed fail-closed validation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Native messaging framing: 4-byte length (native byte order — little
# endian on every platform Chrome ships on) + one JSON object.
# ---------------------------------------------------------------------------

def read_message(stream: BinaryIO) -> Optional[Dict[str, Any]]:
    """One framed message from the browser, ``None`` on clean EOF.

    Truncation, oversize frames, and non-object payloads all raise
    :class:`BrowserEventRejected` — a broken frame means the transport
    itself is unsound, so the caller exits rather than resynchronizing.
    """
    header = stream.read(4)
    if not header:
        return None
    if len(header) < 4:
        raise BrowserEventRejected("truncated frame header")
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        raise BrowserEventRejected(
            f"frame exceeds {MAX_MESSAGE_BYTES} bytes"
        )
    raw = stream.read(length)
    if len(raw) < length:
        raise BrowserEventRejected("truncated frame body")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BrowserEventRejected(f"malformed frame: {exc}")
    if not isinstance(message, dict):
        raise BrowserEventRejected("frame is not a JSON object")
    return message


def write_message(stream: BinaryIO, message: Dict[str, Any]) -> None:
    raw = json.dumps(message, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<I", len(raw)))
    stream.write(raw)
    stream.flush()


# ---------------------------------------------------------------------------
# Fail-closed event validation
# ---------------------------------------------------------------------------

def _clean_origin(value: Any) -> Tuple[str, str]:
    """``(origin, host)`` for an http(s) origin, or reject."""
    from urllib.parse import urlsplit

    text = str(value or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BrowserEventRejected(f"origin {text[:80]!r} is not http(s)")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}", parts.hostname


def _clip(value: Any, cap: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:cap]


def _secret_named(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_NAME_HINTS)


def validate_event(message: Dict[str, Any]) -> Dict[str, Any]:
    """A normalized event payload from one extension message, or reject.

    The detail schema is a per-kind whitelist: unknown detail keys fail
    closed (a field the host does not understand might carry bytes it
    must not journal). Submit events carry field NAMES only — any list
    entry that is not a plain string is rejected whole.
    """
    if message.get("v") != HOST_PROTOCOL_VERSION:
        raise BrowserEventRejected(
            f"unsupported protocol version {message.get('v')!r}"
        )
    kind = str(message.get("kind") or "")
    if kind not in BROWSER_EVENT_KINDS:
        raise BrowserEventRejected(f"unknown event kind {kind[:40]!r}")
    origin, _host = _clean_origin(message.get("origin"))
    try:
        ts = float(message.get("ts") or 0.0)
    except (TypeError, ValueError):
        raise BrowserEventRejected("non-numeric timestamp")
    if ts <= 0:
        raise BrowserEventRejected("missing timestamp")
    raw_detail = message.get("detail")
    if raw_detail is None:
        raw_detail = {}
    if not isinstance(raw_detail, dict):
        raise BrowserEventRejected("detail is not an object")
    allowed = {
        "nav": {"path"},
        "click": {"role", "label"},
        "submit": {"form", "fields"},
        "copy": {"role", "label"},
    }[kind]
    unknown = set(raw_detail) - allowed
    if unknown:
        raise BrowserEventRejected(
            f"unexpected detail field(s): {sorted(unknown)[:4]}"
        )
    detail: Dict[str, Any] = {}
    if kind == "nav":
        path = _clip(raw_detail.get("path"), MAX_PATH_CHARS)
        if path and not path.startswith("/"):
            raise BrowserEventRejected("nav path must be a bare path")
        detail["path"] = path or "/"
    elif kind in ("click", "copy"):
        detail["role"] = _clip(raw_detail.get("role"), 40)
        detail["label"] = _clip(raw_detail.get("label"), MAX_LABEL_CHARS)
    elif kind == "submit":
        detail["form"] = _clip(raw_detail.get("form"), MAX_LABEL_CHARS)
        raw_fields = raw_detail.get("fields")
        if raw_fields is None:
            raw_fields = []
        if not isinstance(raw_fields, list):
            raise BrowserEventRejected("submit fields must be a list")
        if len(raw_fields) > MAX_FORM_FIELDS:
            raise BrowserEventRejected(
                f"submit carries more than {MAX_FORM_FIELDS} fields"
            )
        fields: List[str] = []
        for entry in raw_fields:
            if not isinstance(entry, str):
                # A non-string entry could smuggle a value object.
                raise BrowserEventRejected(
                    "submit fields must be name strings only"
                )
            name = _clip(entry, MAX_FIELD_NAME_CHARS)
            if not name or _secret_named(name):
                continue
            fields.append(name)
        detail["fields"] = fields
    return {
        "origin": origin,
        "kind": kind,
        "ts": ts,
        "detail": detail,
        "ext_version": _clip(message.get("ext_version"), 20),
    }


def scrub_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The authoritative secretguard net: reject the event whole when
    any field carries credential-shaped bytes (labels only surface —
    the secret bytes themselves never leave this function)."""
    findings = credential_findings(
        json.dumps(payload, sort_keys=True)
    )
    if findings:
        raise CredentialRejected(sorted(set(findings)))
    return payload


def event_key(payload: Dict[str, Any]) -> str:
    """Deterministic idempotency key: a re-sent event lands once."""
    import hashlib

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    _origin, host = _clean_origin(payload["origin"])
    return f"browser:{host}:{int(float(payload['ts']) * 1000)}:{digest}"


# ---------------------------------------------------------------------------
# Delivery: control socket → direct kernel store → bounded spool
# ---------------------------------------------------------------------------

def spool_path() -> Path:
    return default_state_dir() / "browser_capture_spool.jsonl"


def status_path() -> Path:
    return default_state_dir() / "browser_capture_status.json"


def _read_spool() -> List[Dict[str, Any]]:
    try:
        raw = spool_path().read_text()
    except OSError:
        return []
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _write_spool(records: List[Dict[str, Any]]) -> None:
    path = spool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, separators=(",", ":")) + "\n"
        for record in records
    )
    path.write_text(payload)
    os.chmod(path, 0o600)


def spool_append(key: str, payload: Dict[str, Any],
                 limit: int = SPOOL_MAX_EVENTS) -> int:
    """Append one event to the bounded spool; returns how many oldest
    records were dropped to stay within *limit*."""
    records = _read_spool()
    records.append({"key": key, "payload": payload})
    dropped = max(0, len(records) - max(1, int(limit)))
    if dropped:
        records = records[dropped:]
    _write_spool(records)
    return dropped


def drain_spool(post: Callable[[str, Dict[str, Any]], str]) -> int:
    """Deliver spooled events through *post*; stops at the first failure
    (remaining records stay spooled). Returns how many were sent."""
    records = _read_spool()
    if not records:
        return 0
    sent = 0
    for record in records:
        try:
            post(str(record.get("key") or ""), record.get("payload") or {})
        except Exception:
            break
        sent += 1
    _write_spool(records[sent:])
    return sent


class KernelPoster:
    """Deliver one browser event into the kernel inbox.

    Control socket when the daemon answers (``event.post``), else the
    kernel store directly (sqlite serializes across processes — the
    DirectKernelClient precedent). Raises when neither path works; the
    host loop spools then.
    """

    def __init__(self, socket_path: Optional[Path] = None):
        self._socket_path = socket_path
        self._store = None

    def post(self, key: str, payload: Dict[str, Any]) -> str:
        from . import control

        if not key:
            raise BrowserEventRejected("empty event key")
        try:
            control.request("event.post", {
                "source": "browser", "key": key, "payload": payload,
                "mission_id": "", "wake": False,
            }, socket_path=self._socket_path)
            return "socket"
        except control.ControlError:
            pass
        if self._store is None:
            from .store import MissionStore

            self._store = MissionStore()
        self._store.receive_inbox("browser", key, payload)
        return "direct"

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


# ---------------------------------------------------------------------------
# Host status (what /install capture shows)
# ---------------------------------------------------------------------------

def write_status(**fields: Any) -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_status()
    current.update(fields)
    current["updated_at"] = time.time()
    path.write_text(json.dumps(current, indent=2, sort_keys=True))
    os.chmod(path, 0o600)


def read_status() -> Dict[str, Any]:
    try:
        data = json.loads(status_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def handshake_line(status: Optional[Dict[str, Any]] = None) -> str:
    """One human line about the last extension→host→kernel handshake."""
    status = read_status() if status is None else status
    stamp = status.get("last_handshake")
    if not stamp:
        return "no handshake yet (load the extension and open a tab)"
    age = max(0, int(time.time() - float(stamp)))
    if age < 120:
        rendered = f"{age}s ago"
    elif age < 7200:
        rendered = f"{age // 60}m ago"
    else:
        rendered = f"{age // 3600}h ago"
    via = "daemon" if status.get("daemon") else "direct kernel"
    return (f"handshake {rendered} (extension "
            f"{status.get('ext_version') or '?'} → host → {via})")


# ---------------------------------------------------------------------------
# The host loop
# ---------------------------------------------------------------------------

def run_host(config: dict, *,
             stdin: Optional[BinaryIO] = None,
             stdout: Optional[BinaryIO] = None,
             poster: Optional[KernelPoster] = None,
             caller: str = "") -> int:
    """Serve one native messaging connection on stdio.

    *caller* is the extension origin Chrome passes in argv; anything but
    the pinned extension (or the configured override) is refused before
    a single event is read. Config gates are answered honestly on the
    hello handshake and enforced on every event.
    """
    from ..config import get_bool
    from . import control

    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    allowed_id = str(
        config.get("capture_browser_extension_id") or EXTENSION_ID
    ).strip()
    if caller and caller.rstrip("/") != f"chrome-extension://{allowed_id}":
        # Never negotiate with an unexpected caller: no reply, exit.
        write_status(last_refused_caller=caller[:120])
        return 2
    enabled = (get_bool(config, "capture_enabled", False)
               and get_bool(config, "capture_browser", False))
    own_poster = poster is None
    poster = poster or KernelPoster()
    counters = {"accepted": 0, "rejected": 0, "spooled": 0}

    def post_or_spool(key: str, payload: Dict[str, Any]) -> str:
        try:
            via = poster.post(key, payload)
        except Exception:
            spool_append(key, payload)
            counters["spooled"] += 1
            return "spool"
        counters["accepted"] += 1
        return via

    try:
        while True:
            try:
                message = read_message(stdin)
            except BrowserEventRejected:
                return 1  # broken framing: the transport is unsound
            if message is None:
                return 0
            mtype = str(message.get("type") or "event")
            if mtype == "hello":
                daemon = control.daemon_alive()
                write_status(
                    last_handshake=time.time(),
                    ext_version=_clip(message.get("ext_version"), 20),
                    daemon=daemon,
                    capture=enabled,
                )
                if enabled:
                    drained = drain_spool(post_or_spool)
                else:
                    drained = 0
                write_message(stdout, {
                    "type": "hello", "ok": True,
                    "v": HOST_PROTOCOL_VERSION,
                    "capture": enabled, "daemon": daemon,
                    "drained": drained,
                })
                continue
            if mtype != "event":
                counters["rejected"] += 1
                write_message(stdout, {
                    "type": "ack", "ok": False,
                    "reason": f"unknown message type {mtype[:20]!r}",
                })
                continue
            if not enabled:
                counters["rejected"] += 1
                write_message(stdout, {
                    "type": "ack", "ok": False,
                    "reason": "capture disabled — /install capture browser",
                })
                continue
            try:
                payload = scrub_event(validate_event(message))
            except BrowserEventRejected as exc:
                counters["rejected"] += 1
                write_message(stdout, {
                    "type": "ack", "ok": False, "reason": exc.reason,
                })
                continue
            except CredentialRejected as exc:
                counters["rejected"] += 1
                write_message(stdout, {
                    "type": "ack", "ok": False,
                    "reason": "credential-shaped content ("
                              + ", ".join(exc.types) + ")",
                })
                continue
            via = post_or_spool(event_key(payload), payload)
            write_message(stdout, {"type": "ack", "ok": True, "via": via})
    finally:
        write_status(**counters)
        if own_poster:
            poster.close()


# ---------------------------------------------------------------------------
# Setup: the native-host manifest per browser
# ---------------------------------------------------------------------------

def native_host_dirs() -> Dict[str, Path]:
    """NativeMessagingHosts directory per Chromium-family browser on
    this platform (Firefox is a documented follow-up, not built)."""
    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        return {
            "Chrome": support / "Google" / "Chrome"
            / "NativeMessagingHosts",
            "Chromium": support / "Chromium" / "NativeMessagingHosts",
            "Brave": support / "BraveSoftware" / "Brave-Browser"
            / "NativeMessagingHosts",
            "Edge": support / "Microsoft Edge" / "NativeMessagingHosts",
        }
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", home / ".config")
    )
    return {
        "Chrome": config_home / "google-chrome" / "NativeMessagingHosts",
        "Chromium": config_home / "chromium" / "NativeMessagingHosts",
        "Brave": config_home / "BraveSoftware" / "Brave-Browser"
        / "NativeMessagingHosts",
        "Edge": config_home / "microsoft-edge" / "NativeMessagingHosts",
    }


def _sh_quote(value: str) -> str:
    """Single-quote *value* for /bin/sh (spaces and quotes survive)."""
    return "'" + value.replace("'", "'\\''") + "'"


def launcher_path() -> Path:
    return default_data_dir() / "browser-capture" / "conch-capture-host"


def write_host_launcher(target: Optional[Path] = None) -> Path:
    """Write the launcher the native-host manifest points at.

    Chrome execs the manifest path directly, but a Python console
    script's shebang can break when the install prefix contains spaces
    (uv tools, pipx venvs, "Application Support"-style homes), and the
    script itself is rewritten or moved on reinstalls. This launcher is
    a two-line ``/bin/sh`` shim at a stable per-user path: it execs the
    exact interpreter conch is running under (quoted), with conch's own
    package root on ``PYTHONPATH``, and calls the host entrypoint
    directly — no PATH lookup, no console-script shim, no shebang with
    spaces. Re-running the browser install step regenerates it, so
    switching install methods (uv → pipx → venv) just needs a re-run.
    """
    package_root = Path(__file__).resolve().parent.parent.parent
    launcher = Path(target) if target else launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/bin/sh\n"
        "# Generated by `/install capture browser` — the Chrome native\n"
        "# messaging manifest points here. Regenerate by re-running it.\n"
        f"PYTHONPATH={_sh_quote(str(package_root))}"
        "${PYTHONPATH:+:$PYTHONPATH}\n"
        "export PYTHONPATH\n"
        f"exec {_sh_quote(sys.executable)} -c "
        "'from conch.entrypoints import capture_host_main; "
        "raise SystemExit(capture_host_main())' \"$@\"\n"
    )
    os.chmod(launcher, 0o755)
    return launcher


def host_manifest(extension_id: str = "",
                  command_path: str = "") -> Dict[str, Any]:
    ext = (extension_id or EXTENSION_ID).strip()
    return {
        "name": NATIVE_HOST_NAME,
        "description": "Conch browser capture native messaging host",
        "path": command_path or str(launcher_path()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext}/"],
    }


def install_native_host(extension_id: str = "") -> Dict[str, Any]:
    """Write the native-host manifest for every installed browser.

    A browser counts as installed when its profile directory exists;
    the NativeMessagingHosts subdirectory is created as needed. Returns
    ``{"written": {browser: path}, "skipped": [browser, ...]}``.
    """
    launcher = write_host_launcher()
    manifest = host_manifest(extension_id, command_path=str(launcher))
    written: Dict[str, str] = {}
    skipped: List[str] = []
    for browser, directory in sorted(native_host_dirs().items()):
        if not directory.parent.is_dir():
            skipped.append(browser)
            continue
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{NATIVE_HOST_NAME}.json"
        target.write_text(json.dumps(manifest, indent=2) + "\n")
        os.chmod(target, 0o600)
        written[browser] = str(target)
    return {"written": written, "skipped": skipped,
            "command": manifest["path"]}


#: Extension files a valid satellite install must carry.
EXTENSION_FILES = (
    "manifest.json", "background.js", "content.js",
    "options.html", "options.js",
)


def extension_dir() -> Optional[Path]:
    """The packaged satellite extension directory
    (``conch/satellites/browser_capture/extension`` — shipped as
    package data, so it exists in checkouts and wheel installs alike)."""
    candidate = (
        Path(__file__).resolve().parent.parent
        / "satellites" / "browser_capture" / "extension"
    )
    return candidate if (candidate / "manifest.json").exists() else None


def default_data_dir() -> Path:
    return Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
    ) / "conch"


def extension_install_dir() -> Path:
    """Where the loadable extension copy lives. Chrome's load-unpacked
    reference must survive pip upgrades and venv reinstalls, so the
    packaged files are copied out of site-packages to this stable
    per-user directory."""
    return default_data_dir() / "browser-capture" / "extension"


def install_extension_files(target: Optional[Path] = None) -> Path:
    """Copy the packaged extension to the stable install dir (fresh
    every run — re-running after a conch upgrade refreshes the files;
    Chrome picks changes up on its next extension reload)."""
    source = extension_dir()
    if source is None:
        raise BrowserEventRejected(
            "the packaged browser extension is missing — reinstall "
            "conch-shell (the satellite ships as package data)"
        )
    missing = [
        name for name in EXTENSION_FILES
        if not (source / name).is_file()
    ]
    if missing:
        raise BrowserEventRejected(
            f"packaged extension is incomplete (missing: {missing}) — "
            "reinstall conch-shell"
        )
    destination = Path(target) if target else extension_install_dir()
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    readme = source.parent / "README.md"
    if readme.is_file():
        shutil.copy2(readme, destination.parent / "README.md")
    return destination
