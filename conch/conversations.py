"""Conversation persistence with structured messages and full-text search."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = 2


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch" / "conversations"


def _index_path() -> Path:
    return _state_dir() / "index.json"


def _atomic_write_text(path: Path, text: str):
    """Crash-safe file replacement: write to a unique tmp file in the same
    directory, fsync it, then ``os.replace`` over the target.

    The old ``.tmp`` + ``Path.replace`` pattern had two failure modes that
    both produced the 2db46ee5 incident shape (valid JSON prefix followed
    by stale bytes): the tmp name was shared, so a second writer (shell +
    edge daemon) could tear a write in progress, and the data was never
    fsynced, so a power cut could publish the rename before the content
    reached disk. The unique name plus fsync-before-replace closes both;
    the target file is either the complete old content or the complete new
    content, never a mix. Files are written 0600 (transcripts can contain
    anything the model or a tool said), which also tightens pre-existing
    0644 files on their next save.
    """
    _ensure_private_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(PRIVATE_FILE_MODE)  # the umask may have widened it
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
REDACTION_MARKER = "[credential redacted]"


def _ensure_private_dir(directory: Path) -> None:
    """Transcripts are private to the user: the directory is created
    0700, and an existing one is tightened to 0700 (best effort)."""
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        if directory.stat().st_mode & 0o077:
            directory.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass


def _scrub_text(text: str) -> str:
    from .secretguard import redact_credentials

    scrubbed, _labels = redact_credentials(text, REDACTION_MARKER)
    return scrubbed


def scrub_messages_for_storage(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy of *messages* with credential-shaped spans in model and tool
    output replaced by ``REDACTION_MARKER``: assistant text, assistant
    tool-call arguments (a `curl -H 'Authorization: …'` the model
    composed), and tool results (the `cat .env` case). The in-memory
    conversation is left intact for the running session; only what is
    persisted or indexed is scrubbed."""
    scrubbed: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            scrubbed.append(message)
            continue
        role = message.get("role")
        if role in ("assistant", "tool"):
            copy = dict(message)
            copy["content"] = _scrub_content(copy.get("content"))
            tool_calls = copy.get("tool_calls")
            if isinstance(tool_calls, list):
                copy["tool_calls"] = [_scrub_tool_call(call) for call in tool_calls]
            scrubbed.append(copy)
        elif role == "user" and isinstance(message.get("content"), list):
            # Anthropic wire shape: tool results ride in a user turn as
            # tool_result blocks. Those are tool output; the user's own
            # text blocks are left alone.
            copy = dict(message)
            copy["content"] = [
                _scrub_content([block])[0]
                if isinstance(block, dict) and block.get("type") == "tool_result"
                else block
                for block in message["content"]
            ]
            scrubbed.append(copy)
        else:
            scrubbed.append(message)
    return scrubbed


def _scrub_content(content: Any) -> Any:
    if isinstance(content, str):
        return _scrub_text(content)
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, dict):
                block = dict(block)
                for key in ("text", "content"):
                    if isinstance(block.get(key), str):
                        block[key] = _scrub_text(block[key])
                    elif isinstance(block.get(key), list):
                        block[key] = _scrub_content(block[key])
                if "input" in block:
                    block["input"] = _scrub_json_value(block["input"])
                blocks.append(block)
            elif isinstance(block, str):
                blocks.append(_scrub_text(block))
            else:
                blocks.append(block)
        return blocks
    return content


def _scrub_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {key: _scrub_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_json_value(item) for item in value]
    return value


def _scrub_tool_call(call: Any) -> Any:
    if not isinstance(call, dict):
        return call
    call = dict(call)
    function = call.get("function")
    if isinstance(function, dict):
        function = dict(function)
        if isinstance(function.get("arguments"), str):
            function["arguments"] = _scrub_text(function["arguments"])
        elif isinstance(function.get("arguments"), (dict, list)):
            function["arguments"] = _scrub_json_value(function["arguments"])
        call["function"] = function
    return call


def _slugify_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    cleaned = cleaned[:60] if cleaned else "New conversation"
    return cleaned


def _extract_title(messages: List[Dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return _slugify_title(msg["content"])
    return "New conversation"


@dataclass
class Conversation:
    id: str
    title: str
    model: str
    provider: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    schema_version: int = SCHEMA_VERSION

    @property
    def path(self) -> Path:
        return _state_dir() / f"{self.id}.json"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "provider": self.provider,
            "messages": self.messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def storage_copy(self) -> "Conversation":
        """The conversation as it may be written to disk or indexed:
        same metadata, messages scrubbed of credential-shaped model and
        tool output."""
        return Conversation(
            id=self.id,
            title=self.title,
            model=self.model,
            provider=self.provider,
            messages=scrub_messages_for_storage(self.messages),
            created_at=self.created_at,
            updated_at=self.updated_at,
            schema_version=self.schema_version,
        )

    def save(self):
        self.updated_at = datetime.now().isoformat()
        self.title = self.title or _extract_title(self.messages)
        _atomic_write_text(
            self.path, json.dumps(self.storage_copy().to_dict(), indent=2)
        )

    @classmethod
    def load(cls, path: Path) -> "Conversation":
        data = json.loads(path.read_text())
        if isinstance(data, list):
            messages = data
            return cls(
                id=path.stem,
                title=_extract_title(messages),
                model="",
                provider="",
                messages=messages,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
                schema_version=1,
            )
        return cls(
            id=data["id"],
            title=data.get("title") or "New conversation",
            model=data.get("model", ""),
            provider=data.get("provider", ""),
            messages=data.get("messages", []),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            schema_version=data.get("schema_version", 1),
        )


def _salvage_valid_prefix(text: str) -> Optional[Dict[str, Any]]:
    """Recover a conversation dict from the valid JSON prefix of a corrupt
    file (the power-outage shape: complete old JSON followed by stale
    trailing bytes). Returns the parsed dict only when the prefix decodes
    cleanly AND looks like a conversation (a dict carrying the schema's
    ``id`` and a ``messages`` list); anything else is not salvageable."""
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("id"), str) or not obj["id"]:
        return None
    if not isinstance(obj.get("messages"), list):
        return None
    return obj


def _quarantine_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    candidate = path.with_name(f"{path.name}.corrupt-{stamp}")
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:4]}"
        )
    return candidate


def _fts_match_expression(query: str) -> str:
    """Build a safe FTS5 MATCH expression: each keyword becomes a quoted
    prefix phrase, OR-ed together (mirrors the old substring-ish scan)."""
    keywords = [kw for kw in query.lower().split() if kw]
    parts = []
    for kw in keywords:
        escaped = kw.replace('"', '""')
        parts.append(f'"{escaped}"*')
    return " OR ".join(parts)


class SearchIndex:
    """SQLite FTS5 index over conversation messages (plan 2.8).

    Replaces the linear load-every-file scan in ConversationManager.search.
    Kept in sync on save/delete, with a lazy sync pass at search time for
    conversations written by other sessions. Titles are indexed as special
    rows (message_index -1) so title-only matches still surface.
    """

    def __init__(self):
        self._conn: Optional[sqlite3.Connection] = None
        self._ok: Optional[bool] = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            _ensure_private_dir(_state_dir())
            db_path = _state_dir() / "search.db"
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                os.chmod(db_path, PRIVATE_FILE_MODE)  # indexed transcript text
            except OSError:
                pass
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                "conv_id UNINDEXED, message_index UNINDEXED, role UNINDEXED, text)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS indexed_convs "
                "(conv_id TEXT PRIMARY KEY, updated_at TEXT)"
            )
            self._conn = conn
        return self._conn

    def available(self) -> bool:
        if self._ok is None:
            try:
                self._connect()
                self._ok = True
            except sqlite3.Error:
                self._ok = False
        return self._ok

    def indexed_state(self) -> Dict[str, str]:
        conn = self._connect()
        return dict(conn.execute("SELECT conv_id, updated_at FROM indexed_convs"))

    def index_conversation(self, conv: "Conversation"):
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM messages_fts WHERE conv_id = ?", (conv.id,))
            if conv.title and conv.title != "New conversation":
                conn.execute(
                    "INSERT INTO messages_fts VALUES (?, ?, ?, ?)",
                    (conv.id, -1, "title", conv.title),
                )
            for i, msg in enumerate(conv.messages):
                if msg.get("role") == "system":
                    continue
                text = _extract_searchable_text(msg)
                if text.strip():
                    conn.execute(
                        "INSERT INTO messages_fts VALUES (?, ?, ?, ?)",
                        (conv.id, i, msg.get("role", ""), text),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO indexed_convs VALUES (?, ?)",
                (conv.id, conv.updated_at),
            )

    def remove_conversation(self, conv_id: str):
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM messages_fts WHERE conv_id = ?", (conv_id,))
            conn.execute("DELETE FROM indexed_convs WHERE conv_id = ?", (conv_id,))

    def search_rows(self, query: str, limit: int = 400) -> List[tuple]:
        """Matching (conv_id, message_index, role, text) rows, best first."""
        match = _fts_match_expression(query)
        if not match:
            return []
        conn = self._connect()
        return conn.execute(
            "SELECT conv_id, message_index, role, text FROM messages_fts "
            "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
            (match, limit),
        ).fetchall()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class ConversationManager:
    def __init__(self):
        self._index = self._load_index()
        self._search_index = SearchIndex()

    def close(self):
        self._search_index.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _load_index(self) -> Dict[str, Any]:
        try:
            data = json.loads(_index_path().read_text())
            if isinstance(data, list):
                return {"schema_version": 1, "conversations": data}
            if isinstance(data, dict):
                data.setdefault("schema_version", SCHEMA_VERSION)
                data.setdefault("conversations", [])
                return data
            return {"schema_version": SCHEMA_VERSION, "conversations": []}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"schema_version": SCHEMA_VERSION, "conversations": []}

    def _save_index(self):
        _atomic_write_text(_index_path(), json.dumps(self._index, indent=2))

    def _upsert_index_entry(self, conv: Conversation):
        entry = {
            "id": conv.id,
            "title": conv.title,
            "model": conv.model,
            "provider": conv.provider,
            "updated_at": conv.updated_at,
            "message_count": len(conv.messages),
        }
        conversations = [item for item in self._index["conversations"] if item["id"] != conv.id]
        conversations.append(entry)
        conversations.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        self._index["conversations"] = conversations
        self._save_index()

    def create(self, model: str, provider: str) -> Conversation:
        conv = Conversation(
            id=uuid.uuid4().hex[:8],
            title="New conversation",
            model=model,
            provider=provider,
        )
        self.save(conv)
        return conv

    def save(self, conversation: Conversation):
        if conversation.messages and conversation.title == "New conversation":
            conversation.title = _extract_title(conversation.messages)
        conversation.save()
        self._upsert_index_entry(conversation)
        if self._search_index.available():
            try:
                # Index what was written, never the unscrubbed original.
                self._search_index.index_conversation(conversation.storage_copy())
            except sqlite3.Error:
                pass

    def load(self, conv_id: str) -> Optional[Conversation]:
        path = _state_dir() / f"{conv_id}.json"
        if not path.exists():
            return None
        try:
            return Conversation.load(path)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
            return self._recover_corrupt(conv_id, path)

    def _recover_corrupt(self, conv_id: str, path: Path) -> Optional[Conversation]:
        """A corrupt conversation file must never crash the shell.

        First try valid-prefix salvage (the power-outage shape: complete
        old JSON followed by stale trailing bytes). The original bytes are
        always kept at ``<name>.json.corrupt-<timestamp>``; on salvage the
        recovered content is written back atomically, otherwise the file
        is quarantined under that name, dropped from the index, and the
        caller falls through to the next conversation (or a fresh one).
        One warning line names the preserved file either way.
        """
        text = path.read_bytes().decode("utf-8", errors="replace")
        salvaged = _salvage_valid_prefix(text)
        backup = _quarantine_path(path)
        try:
            os.replace(path, backup)
        except OSError:
            return None  # someone else already moved it; nothing to load
        if salvaged is not None:
            try:
                _atomic_write_text(path, json.dumps(salvaged, indent=2))
                conv = Conversation.load(path)
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                salvaged = None  # fall through to quarantine
            else:
                print(
                    f"conch: conversation {conv_id} was corrupt; recovered "
                    f"{len(conv.messages)} messages from the valid prefix "
                    f"(original kept at {backup})",
                    file=sys.stderr,
                )
                return conv
        self._drop_index_entry(conv_id)
        print(
            f"conch: conversation {conv_id} is corrupt and could not be "
            f"recovered; quarantined to {backup}",
            file=sys.stderr,
        )
        return None

    def _drop_index_entry(self, conv_id: str):
        entries = [
            item for item in self._index["conversations"]
            if item.get("id") != conv_id
        ]
        if len(entries) != len(self._index["conversations"]):
            self._index["conversations"] = entries
            self._save_index()
        if self._search_index.available():
            try:
                self._search_index.remove_conversation(conv_id)
            except sqlite3.Error:
                pass

    def delete(self, conv_id: str) -> bool:
        path = _state_dir() / f"{conv_id}.json"
        if not path.exists():
            return False
        path.unlink()
        self._index["conversations"] = [
            item for item in self._index["conversations"] if item["id"] != conv_id
        ]
        self._save_index()
        if self._search_index.available():
            try:
                self._search_index.remove_conversation(conv_id)
            except sqlite3.Error:
                pass
        return True

    def list_all(self) -> List[Dict[str, Any]]:
        return list(self._index.get("conversations", []))

    def get_most_recent(self) -> Optional[Conversation]:
        # A corrupt most-recent file loads as None (salvage failed →
        # quarantined); fall through to the next conversation so startup
        # always proceeds.
        for entry in self.list_all():
            conv = self.load(entry["id"])
            if conv is not None:
                return conv
        return None

    def search(
        self, query: str, *, max_results: int = 20, context_chars: int = 120
    ) -> List[Dict[str, Any]]:
        """Search all conversations for a query string. Returns matches with
        context snippets, sorted by relevance.

        Uses the SQLite FTS5 index (plan 2.8) when available, falling back
        to the linear per-file scan otherwise.
        """
        if not query.strip():
            return []
        if self._search_index.available():
            try:
                self._sync_search_index()
                return self._search_fts(query, max_results=max_results,
                                        context_chars=context_chars)
            except sqlite3.Error:
                pass
        return self._search_linear(query, max_results=max_results,
                                   context_chars=context_chars)

    def _sync_search_index(self):
        """Index conversations that are new/stale (written by other sessions)
        and drop entries for deleted ones."""
        indexed = self._search_index.indexed_state()
        live_ids = set()
        for entry in self.list_all():
            conv_id = entry["id"]
            live_ids.add(conv_id)
            if indexed.get(conv_id) == entry.get("updated_at"):
                continue
            conv = self.load(conv_id)
            if conv:
                self._search_index.index_conversation(conv)
        for stale_id in set(indexed) - live_ids:
            self._search_index.remove_conversation(stale_id)

    def _search_fts(
        self, query: str, *, max_results: int, context_chars: int
    ) -> List[Dict[str, Any]]:
        keywords = query.lower().split()
        by_conv: Dict[str, Dict[str, Any]] = {}
        meta = {entry["id"]: entry for entry in self.list_all()}
        for conv_id, message_index, role, text in self._search_index.search_rows(query):
            entry = meta.get(conv_id)
            if entry is None:
                continue
            bucket = by_conv.setdefault(conv_id, {"score": 0, "matches": []})
            text_lower = text.lower()
            hit_count = sum(text_lower.count(kw) for kw in keywords) or 1
            if message_index == -1:  # title row
                bucket["score"] += 5
                continue
            bucket["score"] += hit_count
            if len(bucket["matches"]) < 8:
                bucket["matches"].append({
                    "role": role,
                    "message_index": message_index,
                    "snippet": _extract_snippet(text, keywords, context_chars),
                    "hits": hit_count,
                })
        results = []
        for conv_id, bucket in by_conv.items():
            entry = meta[conv_id]
            results.append({
                "id": conv_id,
                "title": entry.get("title", ""),
                "score": bucket["score"],
                "updated_at": entry.get("updated_at", ""),
                "message_count": entry.get("message_count", 0),
                "matches": bucket["matches"],
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max_results]

    def _search_linear(
        self, query: str, *, max_results: int = 20, context_chars: int = 120
    ) -> List[Dict[str, Any]]:
        """Fallback linear scan (used when SQLite/FTS5 is unavailable)."""
        keywords = query.lower().split()
        results: List[Tuple[int, Dict[str, Any]]] = []

        for entry in self.list_all():
            conv = self.load(entry["id"])
            if not conv:
                continue
            matches: List[Dict[str, Any]] = []
            score = 0

            if any(kw in conv.title.lower() for kw in keywords):
                score += 5

            for i, msg in enumerate(conv.messages):
                role = msg.get("role", "")
                if role == "system":
                    continue
                text = _extract_searchable_text(msg)
                if not text.strip():
                    continue
                text_lower = text.lower()
                hit_count = sum(text_lower.count(kw) for kw in keywords)
                if not hit_count:
                    continue
                score += hit_count
                snippet = _extract_snippet(text, keywords, context_chars)
                matches.append({
                    "role": role,
                    "message_index": i,
                    "snippet": snippet,
                    "hits": hit_count,
                })

            if score > 0:
                results.append((score, {
                    "id": conv.id,
                    "title": conv.title,
                    "score": score,
                    "updated_at": conv.updated_at,
                    "message_count": len(conv.messages),
                    "matches": matches[:8],
                }))

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:max_results]]


def _extract_searchable_text(msg: Dict[str, Any]) -> str:
    """Extract all searchable text from a message regardless of format.

    Handles plain string content, Anthropic-style list content (text/tool_use/
    tool_result blocks), and OpenAI-style tool_calls arrays.
    """
    parts: List[str] = []
    content = msg.get("content", "")

    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                parts.append(block.get("name", ""))
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    parts.append(json.dumps(tool_input, ensure_ascii=False))
                elif isinstance(tool_input, str):
                    parts.append(tool_input)
            elif btype == "tool_result":
                rc = block.get("content", "")
                if isinstance(rc, str):
                    parts.append(rc)
                elif isinstance(rc, list):
                    for rb in rc:
                        if isinstance(rb, dict) and rb.get("type") == "text":
                            parts.append(rb.get("text", ""))

    for tc in msg.get("tool_calls", []):
        fn = tc.get("function", {})
        parts.append(fn.get("name", ""))
        args = fn.get("arguments", "")
        if isinstance(args, str):
            parts.append(args)
        elif isinstance(args, dict):
            parts.append(json.dumps(args, ensure_ascii=False))

    return "\n".join(p for p in parts if p)


def _extract_snippet(text: str, keywords: List[str], context_chars: int = 120) -> str:
    """Find the best snippet around the first keyword match."""
    text_lower = text.lower()
    best_pos = len(text)
    for kw in keywords:
        pos = text_lower.find(kw)
        if pos != -1 and pos < best_pos:
            best_pos = pos

    if best_pos == len(text):
        return text[:context_chars].strip()

    start = max(0, best_pos - context_chars // 3)
    end = min(len(text), best_pos + context_chars)

    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."

    snippet = snippet.replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet)
    return snippet

