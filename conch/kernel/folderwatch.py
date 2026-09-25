"""Generic watched-folder intake — a first-class conch pattern.

Named watches in config turn local folders into governed intake
surfaces: drop files in, and a bound handler decides what the drop
*means*. The kernel owns the mechanics — polling, debounce/grouping,
magic-byte validation, quarantine, ``processed/``/``rejected/``
archival, digest-based dedupe that survives restarts — while handlers
arrive through the plugin registry, so the kernel never imports a
product.

Config (one watch per name)::

    folder_watch_<name> = <path>
    folder_watch_<name>_handler = pack:<pack-name> | mission:<mission-id>
    folder_watch_<name>_poll = 5          # seconds, optional
    folder_watch_<name>_debounce = 8      # seconds, optional

Bindings:

- ``pack:<name>`` — routes a grouped drop into a flow pack's
  ``watched_folder`` intake (registered by the works plugin; the
  ebay-listing pack is the first real binding). Folder drops are never
  consent for an effect: pack folder surfaces force auto-effects off.
- ``mission:<id>`` — journals the drop as a kernel inbox event on the
  existing ``event.post`` path and wakes the named mission (timer- or
  input-parked; never paused or approval-gated missions).
- ``capture`` — designed follow-up (drops as /compile capture
  evidence); the binding seam already carries it.

Continuation verbs (clarify answers, approval decisions for pack
sessions) arrive through the kernel inbox (``folder_watch`` source,
posted by shell verbs over the control socket) so the daemon stays the
single writer of flow state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "FolderWatchService",
    "INBOX_SOURCE",
    "parse_watches",
    "watch_state_path",
]

INBOX_SOURCE = "folder_watch"

_SKIP_SUFFIXES = (".part", ".crdownload", ".tmp", ".download")
_NOTE_SUFFIXES = (".txt",)
_MAX_NOTES_CHARS = 4000
_MAX_HISTORY_LINES = 40
_DEFAULT_ACCEPTS = {
    "extensions": (".jpg", ".jpeg", ".png", ".gif", ".webp"),
    "max_bytes": 12 * 1024 * 1024,
    "max_count": 12,
    "notes_sidecar": True,
}


def watch_state_path() -> Path:
    base = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    ) / "conch"
    return base / "folder_watch.json"


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


def parse_watches(config: dict) -> Dict[str, Dict[str, Any]]:
    """``folder_watch_<name>`` keys → {name: {path, handler, poll,
    debounce}}. A watch without a handler binding is reported invalid
    rather than silently defaulted — a folder must never feed a surface
    the operator didn't name."""
    watches: Dict[str, Dict[str, Any]] = {}
    suffixes = ("_handler", "_poll", "_debounce")
    for key, value in (config or {}).items():
        key = str(key)
        if not key.startswith("folder_watch_"):
            continue
        if any(key.endswith(suffix) for suffix in suffixes):
            continue
        name = key[len("folder_watch_"):]
        if not name:
            continue
        watches[name] = {
            "path": str(value or "").strip(),
            "handler": str(
                config.get(f"folder_watch_{name}_handler") or ""
            ).strip(),
            "poll": _seconds(config.get(f"folder_watch_{name}_poll"),
                             5.0, 1.0, 300.0),
            "debounce": _seconds(
                config.get(f"folder_watch_{name}_debounce"),
                8.0, 1.0, 120.0,
            ),
        }
    return watches


def _seconds(raw: Any, default: float, floor: float, cap: float) -> float:
    try:
        value = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        value = default
    return min(max(value, floor), cap)


class _Attachment:
    """Quarantined drop file (duck-compatible with channels.Attachment)."""

    def __init__(self, filename: str, mime_type: str, size_bytes: int,
                 path: str, remote_id: str = ""):
        self.filename = filename
        self.mime_type = mime_type
        self.size_bytes = size_bytes
        self.path = path
        self.remote_id = remote_id


class _MissionHandler:
    """Builtin ``mission:<id>`` binding: the drop becomes a kernel inbox
    event (paths + digests, never file bytes) and wakes the mission."""

    def __init__(self, mission_id: str, engine, log):
        self.mission_id = mission_id
        self.engine = engine
        self.log = log

    def accepts(self) -> Dict[str, Any]:
        return dict(_DEFAULT_ACCEPTS)

    def handle_drop(self, watch: str, drop_id: str,
                    attachments: List[Any], notes: str, notify) -> str:
        payload = {
            "kind": "folder_drop",
            "watch": watch,
            "drop_id": drop_id,
            "files": [
                {"filename": a.filename, "mime": a.mime_type,
                 "bytes": a.size_bytes, "path": a.path,
                 "digest": a.remote_id}
                for a in attachments
            ],
            "notes": notes,
        }
        result = self.engine.deliver_event(
            INBOX_SOURCE, f"{watch}:{drop_id}", payload,
            mission_id=self.mission_id, wake=True,
        )
        woken = " (mission woken)" if result.get("woken") else ""
        return (
            f"delivered to mission {self.mission_id}"
            f"{woken}: {len(attachments)} file(s)"
        )


class FolderWatchService:
    """Kernel-owned daemon service driving every configured watch."""

    def __init__(self, engine, store, config: dict, log=print,
                 clock=None):
        self.engine = engine
        self.store = store
        self.config = config or {}
        self.log = log
        self.clock = clock or time.time
        self._state_path = watch_state_path()
        self._handlers: Dict[str, Any] = {}
        self._handler_errors: Dict[str, str] = {}
        self._last_poll: Dict[str, float] = {}

    # -- handler resolution -------------------------------------------------

    def _handler(self, name: str, binding: str) -> Optional[Any]:
        if name in self._handlers:
            return self._handlers[name]
        kind, _, target = binding.partition(":")
        handler = None
        error = ""
        if not kind:
            error = "no handler binding configured"
        elif kind == "mission":
            if target and self.engine is not None:
                handler = _MissionHandler(target, self.engine, self.log)
            else:
                error = "mission binding needs mission:<mission-id>"
        else:
            from ..plugins import folder_handler_factory, \
                load_builtin_plugins

            load_builtin_plugins()
            factory = folder_handler_factory(kind)
            if factory is None:
                error = (
                    f"no '{kind}' folder handler is registered "
                    "(is the owning component installed?)"
                )
            else:
                try:
                    handler = factory(
                        target, self.store, self.config, self.log
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
        if handler is None:
            if self._handler_errors.get(name) != error:
                self._handler_errors[name] = error
                self.log(f"folder watch '{name}' disabled: {error}")
            return None
        self._handlers[name] = handler
        self._handler_errors.pop(name, None)
        return handler

    # -- notifications ------------------------------------------------------

    def _notify(self, watch: str, drop_id: str, text: str) -> None:
        body = f"[{watch} {drop_id}] {text}"
        try:
            self.store.enqueue_outbox(
                "notify", {"text": body, "title": f"conch {watch}"},
                dedupe_key=(
                    f"fw:{watch}:{drop_id}:"
                    + hashlib.sha256(body.encode()).hexdigest()[:16]
                ),
            )
        except Exception as exc:
            self.log(f"folder watch notify failed: {exc}")
        self._append_history(watch, drop_id, text)

    def _append_history(self, watch: str, drop_id: str,
                        text: str) -> None:
        state = _load_state(self._state_path)
        drops = state.setdefault("drops", {})
        record = drops.setdefault(drop_id, {"watch": watch})
        history = record.setdefault("history", [])
        history.append({"at": self.clock(), "text": str(text)[:600]})
        del history[:-_MAX_HISTORY_LINES]
        record["updated_at"] = self.clock()
        _save_state(self._state_path, state)

    # -- tick ---------------------------------------------------------------

    def tick(self, stats: Dict[str, int]) -> None:
        watches = parse_watches(self.config)
        if not watches:
            return
        now = self.clock()
        try:
            self._consume_verbs(watches)
        except Exception as exc:
            self.log(f"folder watch verb pass failed: {exc}")
        for name, spec in watches.items():
            if now - self._last_poll.get(name, 0.0) < spec["poll"]:
                continue
            self._last_poll[name] = now
            try:
                self._scan(name, spec)
            except Exception as exc:
                # One broken watch never takes the others (or the
                # daemon) down.
                self.log(f"folder watch '{name}' tick failed: {exc}")
            else:
                stats["folder_watch_polls"] = stats.get(
                    "folder_watch_polls", 0
                ) + 1

    # -- continuation verbs (shell → control socket → inbox) -----------------

    def _consume_verbs(self, watches: Dict[str, Dict[str, Any]]) -> None:
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
                self._route_verb(payload, state, watches)
            except Exception as exc:
                self.log(f"folder watch verb failed: {exc}")
        if advanced != last_seen:
            state = _load_state(self._state_path)
            state["inbox_seen"] = advanced
            _save_state(self._state_path, state)

    def _route_verb(self, payload: Dict[str, Any],
                    state: Dict[str, Any],
                    watches: Dict[str, Dict[str, Any]]) -> None:
        drop_id = str(payload.get("drop") or "")
        watch = str(payload.get("watch") or "")
        if not watch and drop_id:
            record = (state.get("drops") or {}).get(drop_id) or {}
            watch = str(record.get("watch") or "")
        if not watch and len(watches) == 1:
            watch = next(iter(watches))
        spec = watches.get(watch)
        if spec is None:
            self.log(
                f"folder watch verb for unknown watch "
                f"({payload.get('verb')}, drop={drop_id!r})"
            )
            return
        handler = self._handler(watch, spec["handler"])
        if handler is None or not hasattr(handler, "handle_verb"):
            self.log(
                f"folder watch '{watch}' has no continuation verbs"
            )
            return
        reply = handler.handle_verb(
            payload,
            lambda text, d=drop_id: self._notify(watch, d or "-", text),
        )
        if reply:
            self._notify(watch, drop_id or "-", reply)

    # -- scanning -----------------------------------------------------------

    def _scan(self, name: str, spec: Dict[str, Any]) -> None:
        raw_path = spec["path"]
        if not raw_path:
            return
        handler = self._handler(name, spec["handler"])
        if handler is None:
            return
        accepts = dict(_DEFAULT_ACCEPTS)
        try:
            accepts.update(handler.accepts() or {})
        except Exception:
            pass
        watch_dir = Path(raw_path).expanduser()
        try:
            watch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log(f"folder watch '{name}' unavailable: {exc}")
            return
        now = self.clock()
        extensions = tuple(
            str(s).lower() for s in accepts.get("extensions") or ()
        )
        notes_ok = bool(accepts.get("notes_sidecar", True))
        candidates: List[Path] = []
        for item in sorted(watch_dir.iterdir()):
            if not item.is_file() or item.name.startswith("."):
                continue
            suffix = item.suffix.lower()
            if suffix in _SKIP_SUFFIXES:
                continue
            if suffix not in extensions and not (
                notes_ok and suffix in _NOTE_SUFFIXES
            ):
                self._reject(name, watch_dir, item,
                             "unsupported file type")
                continue
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if now - mtime < spec["debounce"]:
                return  # drop still landing; wait for quiet
            candidates.append(item)
        files = [p for p in candidates if p.suffix.lower() in extensions]
        notes_files = [p for p in candidates
                       if notes_ok and p.suffix.lower() in _NOTE_SUFFIXES
                       and p.suffix.lower() not in extensions]
        if not files:
            return  # sidecars alone never start a drop
        self._process_drop(name, spec, handler, accepts, watch_dir,
                           files, notes_files)

    def _reject(self, name: str, watch_dir: Path, item: Path,
                reason: str) -> None:
        rejected = watch_dir / "rejected"
        try:
            rejected.mkdir(exist_ok=True)
            shutil.move(str(item), str(rejected / item.name))
            (rejected / f"{item.name}.reason.txt").write_text(
                reason + "\n"
            )
        except OSError as exc:
            self.log(f"folder watch '{name}' reject move failed for "
                     f"{item.name}: {exc}")
        self.log(f"folder watch '{name}' rejected {item.name}: {reason}")

    def _process_drop(self, name: str, spec: Dict[str, Any], handler,
                      accepts: Dict[str, Any], watch_dir: Path,
                      files: List[Path], notes_files: List[Path]) -> None:
        max_count = int(accepts.get("max_count") or 12)
        if len(files) > max_count:
            for extra in files[max_count:]:
                self._reject(name, watch_dir, extra,
                             f"drop exceeds {max_count} files")
            files = files[:max_count]
        max_bytes = int(accepts.get("max_bytes") or 12 * 1024 * 1024)
        validate_magic = bool(accepts.get("magic", True))

        validated: List[tuple] = []  # (path, data, mime, digest)
        for path in files:
            try:
                data = path.read_bytes()
            except OSError as exc:
                self._reject(name, watch_dir, path, f"unreadable: {exc}")
                continue
            if len(data) > max_bytes:
                self._reject(
                    name, watch_dir, path,
                    f"{len(data)} bytes exceeds the "
                    f"{max_bytes // (1024 * 1024)} MB cap",
                )
                continue
            mime = ""
            if validate_magic:
                from ..channels import sniff_image_mime

                mime = sniff_image_mime(data)
                if not mime:
                    self._reject(
                        name, watch_dir, path,
                        "content failed magic-byte validation",
                    )
                    continue
            validated.append(
                (path, data, mime or "application/octet-stream",
                 hashlib.sha256(data).hexdigest())
            )
        if not validated:
            return

        drop_digest = hashlib.sha256(
            "\n".join(sorted(d for _, _, _, d in validated)).encode()
        ).hexdigest()
        drop_id = f"drop-{drop_digest[:12]}"

        state = _load_state(self._state_path)
        drops = state.setdefault("drops", {})
        if drop_id in drops:
            self._archive(name, watch_dir,
                          [p for p, _, _, _ in validated] + notes_files,
                          drop_id, note="duplicate drop")
            self.log(f"folder watch '{name}': duplicate {drop_id} "
                     "skipped")
            return

        notes = ""
        for notes_file in notes_files:
            try:
                notes += notes_file.read_text(errors="replace") + "\n"
            except OSError:
                continue
        notes = notes.strip()[:_MAX_NOTES_CHARS]

        from ..channels import quarantine_dir

        qdir = quarantine_dir() / f"fw-{name}-{drop_id}"
        qdir.mkdir(parents=True, exist_ok=True)
        attachments: List[_Attachment] = []
        for path, data, mime, digest in validated:
            target = qdir / path.name
            target.write_bytes(data)
            attachments.append(_Attachment(
                filename=path.name, mime_type=mime,
                size_bytes=len(data), path=str(target),
                remote_id=digest[:16],
            ))

        drops[drop_id] = {
            "watch": name,
            "digest": drop_digest,
            "created_at": self.clock(),
            "updated_at": self.clock(),
            "files": [a.filename for a in attachments],
            "notes": bool(notes),
            "history": [],
        }
        _save_state(self._state_path, state)
        self._archive(name, watch_dir,
                      [p for p, _, _, _ in validated] + notes_files,
                      drop_id)

        self._notify(
            name, drop_id,
            f"new drop: {len(attachments)} file(s)"
            + (" + notes" if notes else ""),
        )
        try:
            reply = handler.handle_drop(
                name, drop_id, attachments, notes,
                lambda text, d=drop_id: self._notify(name, d, text),
            )
        except Exception as exc:
            reply = f"handler failed: {type(exc).__name__}: {exc}"
        if reply:
            self._notify(name, drop_id, str(reply))

    def _archive(self, name: str, watch_dir: Path, files: List[Path],
                 drop_id: str, *, note: str = "") -> None:
        processed = watch_dir / "processed" / drop_id
        try:
            processed.mkdir(parents=True, exist_ok=True)
            for path in files:
                if path.exists():
                    shutil.move(str(path), str(processed / path.name))
            if note:
                (processed / "NOTE.txt").write_text(note + "\n")
        except OSError as exc:
            self.log(f"folder watch '{name}' archive failed for "
                     f"{drop_id}: {exc}")


def drops_overview() -> List[Dict[str, Any]]:
    """Read-only drop records (all watches) — shell/status surfaces."""
    state = _load_state(watch_state_path())
    out = []
    for drop_id, record in sorted(
        (state.get("drops") or {}).items(),
        key=lambda kv: kv[1].get("created_at") or 0,
    ):
        out.append(dict(record, drop_id=drop_id))
    return out
