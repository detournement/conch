"""Persistent semantic memory for Conch chat. Stdlib only.

Two layers:
  1. Session memory — the conversation history within a single chat (handled
     by chat.py's messages list, not this module).
  2. Persistent memory — user-saved facts, preferences, and context that
     survive across chat sessions. Stored as JSON, retrieved via TF-IDF
     scoring so the most relevant memories surface for each query.

Storage: ~/.local/state/conch/memory.json  (XDG_STATE_HOME respected)
"""
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MEMORY_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "conch"
MEMORY_FILE = MEMORY_DIR / "memory.json"

# Stop words filtered out during tokenisation so scoring focuses on
# content-bearing terms.
STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "this", "that",
    "these", "those", "i", "you", "he", "she", "it", "we", "they", "me",
    "him", "her", "us", "them", "my", "your", "his", "its", "our", "their",
    "what", "which", "who", "whom", "when", "where", "why", "how", "not",
    "no", "so", "if", "as", "just", "about", "into", "over", "after",
    "before", "between", "under", "above", "up", "down", "out", "off",
    "then", "than", "too", "very", "also", "there", "here", "all", "any",
    "each", "every", "both", "few", "more", "most", "other", "some",
    "such", "only", "own", "same", "like", "get", "got", "make", "made",
    "use", "using", "used", "want", "need", "know", "think", "say", "said",
    "tell", "told", "ask", "asked", "go", "went", "gone", "come", "came",
    "see", "saw", "look", "let", "keep", "still", "try", "call", "give",
    "take", "well", "way", "because", "thing", "things", "much", "many",
    "really", "always", "never", "often", "sometimes",
})


def _tokenize(text: str) -> List[str]:
    """Split text into meaningful lowercase tokens."""
    words = re.findall(r"[a-zA-Z0-9_./:@-]+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


class MemoryStore:
    """Persistent memory store with TF-IDF semantic retrieval."""

    def __init__(self):
        self._memories: List[dict] = []
        self._idf: Dict[str, float] = {}
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self):
        if MEMORY_FILE.is_file():
            try:
                with open(MEMORY_FILE) as f:
                    self._memories = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._memories = []
        self._rebuild_idf()

    def _save(self):
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MEMORY_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._memories, f, indent=2)
        tmp.replace(MEMORY_FILE)

    # -- TF-IDF index ---------------------------------------------------------

    def _rebuild_idf(self):
        n = len(self._memories) + 1
        df: Counter = Counter()
        for mem in self._memories:
            for t in set(mem.get("tokens", [])):
                df[t] += 1
        self._idf = {t: math.log(n / (1 + count)) for t, count in df.items()}

    # -- public API -----------------------------------------------------------

    def add(self, content: str) -> dict:
        """Save a new persistent memory. Returns the stored entry."""
        tokens = _tokenize(content)
        next_id = max((m["id"] for m in self._memories), default=0) + 1
        entry = {
            "id": next_id,
            "content": content.strip(),
            "tokens": tokens,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._memories.append(entry)
        self._rebuild_idf()
        self._save()
        return entry

    def search(self, query: str, limit: int = 10) -> List[Tuple[dict, float]]:
        """Return the most relevant memories for *query*, scored by TF-IDF."""
        if not self._memories:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return [(m, 0.0) for m in self._memories[:limit]]

        query_counter = Counter(query_tokens)
        scored: List[Tuple[dict, float]] = []

        for mem in self._memories:
            mem_counter = Counter(mem.get("tokens", []))
            if not mem_counter:
                continue
            total_mem_tokens = sum(mem_counter.values())
            score = 0.0
            for token, q_freq in query_counter.items():
                if token in mem_counter:
                    tf = mem_counter[token] / total_mem_tokens
                    idf = self._idf.get(token, 1.0)
                    score += tf * idf * q_freq

            # Gentle recency boost — 1% decay per day
            try:
                age_days = (time.time() - time.mktime(
                    time.strptime(mem["created_at"], "%Y-%m-%d %H:%M:%S")
                )) / 86400
            except (ValueError, KeyError):
                age_days = 0
            score *= 1.0 / (1.0 + age_days * 0.01)

            scored.append((mem, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def get_all(self) -> List[dict]:
        return list(self._memories)

    def forget(self, memory_id: int) -> bool:
        """Delete a memory by id. Returns True if found and removed."""
        before = len(self._memories)
        self._memories = [m for m in self._memories if m["id"] != memory_id]
        if len(self._memories) < before:
            self._rebuild_idf()
            self._save()
            return True
        return False

    def clear(self) -> int:
        """Delete all memories. Returns count removed."""
        count = len(self._memories)
        self._memories.clear()
        self._idf.clear()
        self._save()
        return count

    def build_context(self, query: str, limit: int = 10) -> str:
        """Build a context block of relevant memories for the system prompt.

        If there are few memories (<= limit), all are included so user-curated
        facts are never silently dropped.  Otherwise TF-IDF picks the best.
        """
        if not self._memories:
            return ""

        if len(self._memories) <= limit:
            selected = self._memories
        else:
            results = self.search(query, limit=limit)
            # Include everything with a positive score, plus the most recent
            # as a fallback so brand-new memories always appear.
            by_score = [m for m, s in results if s > 0]
            recent = sorted(self._memories, key=lambda m: m.get("created_at", ""))
            recent_ids = {m["id"] for m in recent[-3:]}
            seen = {m["id"] for m in by_score}
            for m in recent[-3:]:
                if m["id"] not in seen:
                    by_score.append(m)
            selected = by_score[:limit]

        lines = ["[Saved memories]"]
        for mem in selected:
            lines.append(f"- {mem['content']}  ({mem['created_at']})")
        return "\n".join(lines)
