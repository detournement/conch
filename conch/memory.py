"""Persistent memory store."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"


def _memory_path() -> Path:
    return _state_dir() / "memory.json"


def _tokenize(text: str) -> set[str]:
    return {part.lower() for part in text.replace("\n", " ").split() if part.strip()}


@dataclass
class MemoryEntry:
    id: int
    content: str
    created_at: str
    source: str

    def as_dict(self) -> Dict[str, str | int]:
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

    def _load(self) -> List[Dict[str, str | int]]:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2))
        tmp.replace(self._path)

    def get_all(self) -> List[Dict[str, str | int]]:
        return list(self._entries)

    def add(self, content: str, source: str = "user") -> Dict[str, str | int]:
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
        q_tokens = _tokenize(query)
        if not q_tokens:
            return ""
        scored: List[tuple[int, Dict[str, str | int]]] = []
        for entry in self._entries:
            score = len(q_tokens & _tokenize(str(entry["content"])))
            if score:
                scored.append((score, entry))
        if not scored:
            return ""
        scored.sort(key=lambda item: item[0], reverse=True)
        lines = ["Relevant remembered context:"]
        for _, entry in scored[:limit]:
            lines.append(f"- {entry['content']}")
        return "\n".join(lines)

