"""Persistent memory store."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from .secretguard import CredentialRejected, credential_findings

logger = logging.getLogger("conch.memory")


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"


def _memory_path() -> Path:
    return _state_dir() / "memory.json"


def _tokenize(text: str) -> set[str]:
    return {part.lower() for part in text.replace("\n", " ").split() if part.strip()}


def credential_withheld(content: str, where: str) -> bool:
    """Scan-on-read guard for one retrieved entry: True when it must be
    withheld. Legacy entries predating the write gate (or slipping
    patterns added later) are dropped from retrieval and logged by TYPE —
    the content never reaches model context, logs, or the report."""
    found = credential_findings(content)
    if not found:
        return False
    logger.warning(
        "memory retrieval (%s): dropped an entry matching credential "
        "pattern(s) %s — review with /memories and remove it with /forget",
        where, ", ".join(found),
    )
    return True


def drop_credentialed(contents: List[str], where: str) -> List[str]:
    """Filter retrieved memory contents through the scan-on-read guard."""
    return [
        content for content in contents
        if not credential_withheld(str(content), where)
    ]


# ---------------------------------------------------------------------------
# Always-loaded facts tier (plan 2.8): a bounded, user-editable markdown file
# whose contents ride in the system prompt every session — no retrieval
# involved. Query-relevant recall stays in MemoryStore.build_context.
# ---------------------------------------------------------------------------

FACTS_MAX_CHARS = 2000


def facts_path() -> Path:
    config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch"
    return config_dir / "facts.md"


def load_facts(max_chars: int = FACTS_MAX_CHARS) -> str:
    """Contents of the facts file, bounded so it can't swamp small models."""
    try:
        text = facts_path().read_text().strip()
    except (FileNotFoundError, OSError):
        return ""
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [facts truncated]"
    return f"Standing facts (from {facts_path()}):\n{text}"


def append_fact(text: str) -> bool:
    """Append a line to the facts file (used by /fact)."""
    text = (text or "").strip()
    if not text:
        return False
    path = facts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    try:
        existing = path.read_text()
    except (FileNotFoundError, OSError):
        pass
    line = f"- {text}" if not text.startswith("-") else text
    with open(path, "a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(line + "\n")
    return True


@dataclass
class MemoryEntry:
    id: int
    content: str
    created_at: str
    source: str

    def as_dict(self) -> Dict[str, Union[str, int]]:
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "source": self.source,
        }


class MemoryStore:
    def __init__(self):
        self._path = _memory_path()
        self._entries = self._load()

    def _load(self) -> List[Dict[str, Union[str, int]]]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2))
        # Owner-only, like the kernel db: memories are personal context.
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def get_all(self) -> List[Dict[str, Union[str, int]]]:
        return list(self._entries)

    def add(
        self, content: str, source: str = "user"
    ) -> Dict[str, Union[str, int]]:
        """Append one entry. Every write path — the save_memory tool,
        /remember, session summaries, mission consolidation — lands here,
        so this is where the credential gate holds: an entry matching
        credential detection is rejected whole (:class:`CredentialRejected`
        carries type labels only), never sanitized — the same discipline as
        the mission-lesson consolidation gate."""
        found = credential_findings(content)
        if found:
            raise CredentialRejected(found)
        new_id = max((int(item["id"]) for item in self._entries), default=0) + 1
        entry = MemoryEntry(
            id=new_id,
            content=content.strip(),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            source=source,
        ).as_dict()
        self._entries.append(entry)
        self._save()
        return entry

    def forget(self, entry_id: int) -> bool:
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if int(entry["id"]) != entry_id]
        if len(self._entries) == before:
            return False
        self._save()
        return True

    def build_context(self, query: str, limit: int = 5) -> str:
        """Query-relevant memories, ranked with FTS5/bm25 (plan 2.8) and
        falling back to keyword-overlap scoring when FTS5 is unavailable."""
        if not query.strip() or not self._entries:
            return ""
        contents = self._fts_rank(query, limit)
        if contents is None:
            contents = self._keyword_rank(query, limit)
        contents = drop_credentialed(contents or [], where="build_context")
        if not contents:
            return ""
        lines = ["Relevant remembered context:"]
        lines.extend(f"- {content}" for content in contents)
        return "\n".join(lines)

    def rank_entries(
        self,
        query: str,
        limit: int = 5,
        source_prefix: str = "",
        exclude_source: str = "",
    ) -> List[Dict[str, Union[str, int]]]:
        """Full entries (not just contents) ranked against a query, with
        optional source filtering — mission rehydration retrieves shared
        cross-mission lessons this way (``source_prefix="mission:"``,
        excluding the requesting mission's own)."""
        if not query.strip():
            return []
        pool = [
            entry for entry in self._entries
            if (
                not source_prefix
                or str(entry.get("source", "")).startswith(source_prefix)
            )
            and (
                not exclude_source
                or str(entry.get("source", "")) != exclude_source
            )
        ]
        if not pool:
            return []
        indices = self._fts_rank_indices(query, limit, pool)
        if indices is None:
            q_tokens = _tokenize(query)
            scored = []
            for index, entry in enumerate(pool):
                score = len(q_tokens & _tokenize(str(entry["content"])))
                if score:
                    scored.append((score, index))
            scored.sort(key=lambda item: item[0], reverse=True)
            indices = [index for _, index in scored[:limit]]
        entries = [dict(pool[index]) for index in indices]
        return [
            entry for entry in entries
            if not credential_withheld(str(entry.get("content", "")), "rank_entries")
        ]

    def _fts_rank_indices(
        self, query: str, limit: int, pool: List[Dict[str, Union[str, int]]]
    ) -> Optional[List[int]]:
        """bm25-ranked pool indices via in-memory FTS5, or None without
        FTS5 support."""
        keywords = [kw for kw in query.lower().split() if kw]
        if not keywords:
            return []
        match = " OR ".join('"%s"*' % kw.replace('"', '""') for kw in keywords)
        conn = None
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE mem USING fts5(content)")
            conn.executemany(
                "INSERT INTO mem(rowid, content) VALUES (?, ?)",
                [
                    (index, str(entry["content"]))
                    for index, entry in enumerate(pool)
                ],
            )
            rows = conn.execute(
                "SELECT rowid FROM mem WHERE mem MATCH ? "
                "ORDER BY bm25(mem) LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.close()
        return [int(row[0]) for row in rows]

    def _fts_rank(
        self, query: str, limit: int
    ) -> Optional[List[str]]:
        """bm25-ranked entry contents via an in-memory FTS5 table, or None
        when FTS5 isn't compiled into this sqlite."""
        keywords = [kw for kw in query.lower().split() if kw]
        if not keywords:
            return []
        match = " OR ".join('"%s"*' % kw.replace('"', '""') for kw in keywords)
        conn = None
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE mem USING fts5(content)")
            conn.executemany(
                "INSERT INTO mem VALUES (?)",
                [(str(entry["content"]),) for entry in self._entries],
            )
            rows = conn.execute(
                "SELECT content FROM mem WHERE mem MATCH ? "
                "ORDER BY bm25(mem) LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.close()
        return [row[0] for row in rows]

    def _keyword_rank(self, query: str, limit: int) -> List[str]:
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scored: List[tuple[int, str]] = []
        for entry in self._entries:
            score = len(q_tokens & _tokenize(str(entry["content"])))
            if score:
                scored.append((score, str(entry["content"])))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [content for _, content in scored[:limit]]

