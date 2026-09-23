"""Deterministic recurrence mining over captured work (capture plan,
feature 2).

"You've done this 14 times — compile it?" is computed, never
model-ranked (the personal-items urgency rule). Sequences of normalized
steps — shell commands and tool calls from conversations, external
actions from mission journals — are reduced to n-gram signatures;
recurring signatures rank by count with a deterministic tiebreak, and
every suggestion carries its own explanation (the step shape, the count,
the sessions, the window). Same inputs → same signatures, same ranking:
a suggestion is reproducible evidence, not a vibe.

Kernel-side module: stdlib only, takes plain data (message dicts, event
dicts) — the command surface in the compiler feeds it and owns all I/O.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

MIN_NGRAM = 3
MAX_NGRAM = 6
DEFAULT_MIN_COUNT = 3
DEFAULT_WINDOW_DAYS = 30

_NOISE_TOKEN = re.compile(
    r"^(?:/|\.|~|https?://|\d+$|[0-9a-f]{7,}$|--?[\w-]+=)"
)


def normalize_command(command: str) -> str:
    """A shell command's stable shape: up to two significant tokens,
    options dropped, paths/urls/ids/numbers collapsed to ``·``."""
    keep: List[str] = []
    for token in str(command or "").split():
        if token.startswith("-"):
            continue
        if _NOISE_TOKEN.match(token):
            token = "·"
        keep.append(token.lower())
        if len(keep) == 2:
            break
    return " ".join(keep)


def paired_steps_from_messages(messages: List[dict]
                               ) -> List[tuple]:
    """Ordered ``(normalized, raw)`` step pairs from one conversation —
    one extraction, so signature indices always align with the raw
    commands a suggestion later shows as evidence."""
    import json

    pairs: List[tuple] = []
    for message in messages or []:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            name = str(function.get("name") or "")
            if not name:
                continue
            if name == "local_shell":
                raw = function.get("arguments")
                try:
                    arguments = (
                        json.loads(raw) if isinstance(raw, str)
                        else (raw or {})
                    )
                except ValueError:
                    arguments = {}
                command = str(arguments.get("command", "")).strip()
                normalized = normalize_command(command)
                if normalized:
                    pairs.append((normalized, command[:200]))
            else:
                pairs.append((f"tool:{name}", f"tool {name}"))
    return pairs


def steps_from_messages(messages: List[dict]) -> List[str]:
    """Ordered normalized steps from one conversation's messages."""
    return [pair[0] for pair in paired_steps_from_messages(messages)]


def steps_from_mission_events(events: List[dict]) -> List[str]:
    """Ordered normalized steps from a mission's material events
    (external actions carry the procedure shape)."""
    steps: List[str] = []
    for event in events or []:
        if event.get("kind") not in ("action_recorded",):
            continue
        data = event.get("data") or {}
        detail = data.get("detail") or {}
        op = str(detail.get("op") or detail.get("kind") or "").strip()
        action_class = str(data.get("action_class") or "").strip()
        if op:
            steps.append(
                f"action:{action_class}:{normalize_command(op)}"
            )
    return steps


def _signature(gram: tuple) -> str:
    return hashlib.sha256(
        "\x1f".join(gram).encode("utf-8")
    ).hexdigest()[:12]


def mine_sequences(
    sources: List[Dict[str, Any]],
    *,
    min_count: int = DEFAULT_MIN_COUNT,
    min_ngram: int = MIN_NGRAM,
    max_ngram: int = MAX_NGRAM,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Recurring step shapes across sources.

    Each source is ``{"id": str, "kind": "session"|"mission",
    "steps": [normalized step, ...]}``. Returns ranked suggestions:
    ``{signature, steps, count, sources, kinds, why}`` — maximal only
    (an n-gram wholly contained in a reported longer gram with at least
    its count is folded into it), ranked count desc → length desc →
    signature (stable).
    """
    min_count = max(2, int(min_count))
    grams: Dict[tuple, Dict[str, Any]] = {}
    for source in sources:
        steps = [s for s in source.get("steps") or [] if s]
        source_id = str(source.get("id") or "")
        kind = str(source.get("kind") or "session")
        for n in range(min_ngram, max_ngram + 1):
            if len(steps) < n:
                continue
            seen_here = set()
            for index in range(len(steps) - n + 1):
                gram = tuple(steps[index:index + n])
                entry = grams.setdefault(gram, {
                    "count": 0, "sources": set(), "kinds": set(),
                })
                entry["count"] += 1
                # count every occurrence, but remember distinct sources
                if gram not in seen_here:
                    seen_here.add(gram)
                entry["sources"].add(source_id)
                entry["kinds"].add(kind)
    candidates = [
        (gram, entry) for gram, entry in grams.items()
        if entry["count"] >= min_count
    ]
    # maximal-gram fold: drop a gram contiguously contained in a longer
    # candidate whose count is >= its own
    def contains(longer: tuple, shorter: tuple) -> bool:
        if len(shorter) >= len(longer):
            return False
        for start in range(len(longer) - len(shorter) + 1):
            if longer[start:start + len(shorter)] == shorter:
                return True
        return False

    kept = []
    for gram, entry in candidates:
        folded = any(
            contains(other, gram) and o_entry["count"] >= entry["count"]
            for other, o_entry in candidates if other != gram
        )
        if not folded:
            kept.append((gram, entry))
    kept.sort(key=lambda item: (
        -item[1]["count"], -len(item[0]), _signature(item[0]),
    ))
    suggestions = []
    for gram, entry in kept[:max(1, int(limit))]:
        sources_list = sorted(entry["sources"])
        shape = f"{gram[0]} → … → {gram[-1]}" if len(gram) > 2 \
            else " → ".join(gram)
        suggestions.append({
            "signature": _signature(gram),
            "steps": list(gram),
            "count": entry["count"],
            "sources": sources_list,
            "kinds": sorted(entry["kinds"]),
            "why": (
                f"same {len(gram)}-step {shape} shape, "
                f"{entry['count']} occurrence(s) across "
                f"{len(sources_list)} {'/'.join(sorted(entry['kinds']))}"
                "(s)"
            ),
        })
    return suggestions


def occurrences_in_steps(steps: List[str],
                         gram: List[str]) -> List[int]:
    """Start indices where ``gram`` occurs contiguously in ``steps``."""
    gram_tuple = tuple(gram)
    n = len(gram_tuple)
    return [
        index for index in range(len(steps) - n + 1)
        if tuple(steps[index:index + n]) == gram_tuple
    ]


def parse_window_days(config: dict,
                      default: int = DEFAULT_WINDOW_DAYS) -> int:
    try:
        days = int(config.get("compile_suggest_window_days", default)
                   or default)
    except (TypeError, ValueError):
        days = default
    return max(1, min(days, 365))


def parse_min_count(config: dict,
                    default: int = DEFAULT_MIN_COUNT) -> int:
    try:
        count = int(config.get("compile_suggest_min_count", default)
                    or default)
    except (TypeError, ValueError):
        count = default
    return max(2, min(count, 100))
