"""Conversation persistence with structured messages."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = 2


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch" / "conversations"


def _index_path() -> Path:
    return _state_dir() / "index.json"


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

    def save(self):
        self.updated_at = datetime.now().isoformat()
        self.title = self.title or _extract_title(self.messages)
        _state_dir().mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        tmp.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> "Conversation":
        data = json.loads(path.read_text())
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


class ConversationManager:
    def __init__(self):
        self._index = self._load_index()

    def _load_index(self) -> Dict[str, Any]:
        try:
            return json.loads(_index_path().read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"schema_version": SCHEMA_VERSION, "conversations": []}

    def _save_index(self):
        _state_dir().mkdir(parents=True, exist_ok=True)
        tmp = _index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index, indent=2))
        tmp.replace(_index_path())

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

    def load(self, conv_id: str) -> Optional[Conversation]:
        path = _state_dir() / f"{conv_id}.json"
        if not path.exists():
            return None
        return Conversation.load(path)

    def delete(self, conv_id: str) -> bool:
        path = _state_dir() / f"{conv_id}.json"
        if not path.exists():
            return False
        path.unlink()
        self._index["conversations"] = [
            item for item in self._index["conversations"] if item["id"] != conv_id
        ]
        self._save_index()
        return True

    def list_all(self) -> List[Dict[str, Any]]:
        return list(self._index.get("conversations", []))

    def get_most_recent(self) -> Optional[Conversation]:
        conversations = self.list_all()
        if not conversations:
            return None
        return self.load(conversations[0]["id"])

