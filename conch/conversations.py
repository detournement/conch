"""Conversation persistence with structured messages and full-text search."""

from __future__ import annotations

import json
import os
import re
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


class ConversationManager:
    def __init__(self):
        self._index = self._load_index()

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

    def search(
        self, query: str, *, max_results: int = 20, context_chars: int = 120
    ) -> List[Dict[str, Any]]:
        """Search all conversations for a query string. Returns matches with
        context snippets, sorted by relevance (match count)."""
        if not query.strip():
            return []

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

