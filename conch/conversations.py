"""Multi-conversation persistence for Conch chat.

Each conversation stores its full message history to disk. Conversations
are listed with titles auto-generated from the first user message.

Storage: ~/.local/state/conch/conversations/
  - index.json          — ordered list of conversation metadata
  - {id}.json           — full message history for each conversation
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

CONV_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "conch" / "conversations"


def _ensure_dir():
    CONV_DIR.mkdir(parents=True, exist_ok=True)


def _index_path() -> Path:
    return CONV_DIR / "index.json"


def _conv_path(conv_id: str) -> Path:
    return CONV_DIR / f"{conv_id}.json"


def _load_index() -> List[dict]:
    path = _index_path()
    if path.is_file():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_index(index: List[dict]):
    _ensure_dir()
    tmp = _index_path().with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    tmp.replace(_index_path())


def _generate_title(messages: List[dict]) -> str:
    """Generate a short title from the first user message."""
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            text = m["content"].strip()
            if len(text) > 60:
                text = text[:57] + "..."
            return text
    return "New conversation"


class Conversation:
    def __init__(self, conv_id: str = None, title: str = "New conversation",
                 created_at: str = None, updated_at: str = None,
                 model: str = "", provider: str = ""):
        self.id = conv_id or str(uuid.uuid4())[:8]
        self.title = title
        self.created_at = created_at or time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = updated_at or self.created_at
        self.model = model
        self.provider = provider
        self.messages: List[dict] = []

    def to_meta(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "provider": self.provider,
            "message_count": len([m for m in self.messages
                                   if m.get("role") in ("user", "assistant")
                                   and isinstance(m.get("content"), str)]),
        }

    def save(self):
        """Persist full message history to disk."""
        _ensure_dir()
        # Only save serializable messages
        saveable = []
        for m in self.messages:
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                content = "\n".join(parts) if parts else str(content)
            saveable.append({"role": m.get("role", ""), "content": content})
        tmp = _conv_path(self.id).with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(saveable, f, indent=2)
        tmp.replace(_conv_path(self.id))

    def load(self) -> bool:
        """Load message history from disk."""
        path = _conv_path(self.id)
        if not path.is_file():
            return False
        try:
            with open(path) as f:
                self.messages = json.load(f)
            return True
        except (json.JSONDecodeError, OSError):
            return False

    def update_title(self):
        new_title = _generate_title(self.messages)
        if new_title != "New conversation":
            self.title = new_title


class ConversationManager:
    """Manages multiple conversations with persistence."""

    def __init__(self):
        self._index = _load_index()

    def list_all(self) -> List[dict]:
        return list(self._index)

    def create(self, model: str = "", provider: str = "") -> Conversation:
        conv = Conversation(model=model, provider=provider)
        self._index.insert(0, conv.to_meta())
        _save_index(self._index)
        return conv

    def load(self, conv_id: str) -> Optional[Conversation]:
        meta = None
        for m in self._index:
            if m["id"] == conv_id:
                meta = m
                break
        if not meta:
            return None
        conv = Conversation(
            conv_id=meta["id"],
            title=meta.get("title", ""),
            created_at=meta.get("created_at", ""),
            updated_at=meta.get("updated_at", ""),
            model=meta.get("model", ""),
            provider=meta.get("provider", ""),
        )
        conv.load()
        return conv

    def save(self, conv: Conversation):
        """Save conversation and update index."""
        conv.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        conv.update_title()
        conv.save()
        # Update index entry
        for i, m in enumerate(self._index):
            if m["id"] == conv.id:
                self._index[i] = conv.to_meta()
                break
        else:
            self._index.insert(0, conv.to_meta())
        # Move to top (most recent)
        self._index.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        _save_index(self._index)

    def delete(self, conv_id: str) -> bool:
        before = len(self._index)
        self._index = [m for m in self._index if m["id"] != conv_id]
        if len(self._index) < before:
            _save_index(self._index)
            path = _conv_path(conv_id)
            if path.is_file():
                path.unlink()
            return True
        return False

    def get_most_recent(self) -> Optional[Conversation]:
        if not self._index:
            return None
        return self.load(self._index[0]["id"])
