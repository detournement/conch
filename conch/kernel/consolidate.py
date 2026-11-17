"""Cross-mission memory consolidation (roadmap: mission judgment and
shared memory).

After a mission session checkpoints (and on mission completion), a
weak-model pass distills durable, non-secret learnings from that session's
journal delta into the shared memory tiers (:mod:`conch.memory`), tagged
with mission id + topic. Any mission's rehydration then retrieves the
top-K relevant shared lessons into a clearly labeled context block.

Non-negotiables enforced here, deterministically, on the model's OUTPUT:

- Lessons NEVER carry credential references, org/agent UUIDs, or channel
  identities — :func:`lesson_rejection` extends the secret-canary
  discipline with hard regex gates (long tokens with digits, secret
  keywords, UUIDs, Slack-style ids, emails, phone-like numbers, long digit
  runs). A lesson that trips any gate is dropped whole, never sanitized.
- Lessons are deduplicated against existing memories (normalized-exact or
  high token overlap) and size-capped per lesson, per pass, and across the
  store (oldest mission lessons evicted first; user memories untouched).
- Consolidation is skippable (``mission_consolidation=false``) and never
  blocks the session path: the model call + write run on a worker thread
  with a timeout; timeout or failure is a logged skip. The checkpoint has
  already committed by the time consolidation starts.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from .model import ITEM_EVENT_KINDS, MissionKind
from .store import MissionStore

#: Journal event kinds worth distilling (bounded; reviews excluded — a
#: verdict about the work is not new knowledge from the work).
#:
#: Personal-space fence (personal-items plan): item_* events are NEVER
#: consolidation input. Item events live on per-item chains, so a
#: mission's delta cannot contain them by construction; the disjointness
#: assertion and the belt-and-braces filter in
#: :func:`session_delta_text` keep that true as taxonomies grow.
DELTA_EVENT_KINDS = (
    "checkpoint_recorded",
    "mission_note",
    "plan_recorded",
    "task_created",
    "task_transitioned",
    "attempt_finished",
    "action_recorded",
    "action_resolved",
    "approval_requested",
    "mission_transitioned",
)

assert not set(DELTA_EVENT_KINDS) & ITEM_EVENT_KINDS, (
    "personal-item events must never be consolidation input"
)

CONSOLIDATION_DEFAULTS = {
    "enabled": True,
    "max_lessons": 3,     # per pass
    "max_chars": 240,     # per lesson
    "timeout": 20.0,      # seconds; the session path never waits longer
    "input_chars": 6000,  # journal delta fed to the weak model
    "store_cap": 200,     # mission-sourced entries kept in shared memory
    "k": 3,               # lessons retrieved into rehydration
    "block_chars": 700,   # hard cap on the rehydrated lessons block
}

CONSOLIDATE_SYSTEM_PROMPT = (
    "You distill durable operational lessons from one mission work "
    "session's journal, for reuse by OTHER, unrelated missions.\n\n"
    "Extract at most {max_lessons} short, self-contained, general lessons "
    "a future mission would genuinely benefit from: environment quirks, "
    "tool/API behaviors, effective procedures, dead ends to avoid. Skip "
    "session narration, one-off facts, and anything mission-specific "
    "that cannot transfer.\n\n"
    "NEVER include credentials, tokens, API keys, UUIDs, account/org/"
    "channel identifiers, email addresses, phone numbers, or people's "
    "names. Lessons violating this are dropped by a hard filter.\n\n"
    "If nothing durable was learned, return an empty list. Respond with "
    "ONLY this JSON shape:\n"
    '{{"lessons": [{{"topic": "2-4 word tag", '
    '"lesson": "one self-contained sentence"}}]}}'
)

LESSONS_LABEL = (
    "Lessons from prior missions (shared memory; verify before relying"
    " on them):"
)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)
#: Long unbroken token containing a digit — catches API keys, canaries,
#: kernel ids, bearer fragments. Ordinary prose breaks on punctuation.
_LONG_TOKEN_RE = re.compile(
    r"(?=[A-Za-z0-9_\-+/=]{0,39}\d)[A-Za-z0-9_\-+/=]{20,}"
)
_SECRET_WORDS_RE = re.compile(
    r"(?i)\b(api[ _-]?key|apikey|secret|bearer|passw(?:or)?d|credential|"
    r"access[ _-]token|refresh[ _-]token|auth[ _-]token|private[ _-]key|"
    r"client[ _-]secret)\b"
    r"|\bsk-[A-Za-z0-9]"
    r"|\bxox[a-z]-"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|BEGIN [A-Z ]*PRIVATE KEY"
)
#: Slack-style channel/user/workspace ids (must contain a digit so plain
#: uppercase words never trip it).
_CHANNEL_ID_RE = re.compile(r"\b[UWC](?=[A-Z0-9]{8,}\b)[A-Z0-9]*\d[A-Z0-9]*\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+\d[\d().\s-]{5,}\d")
_DIGIT_RUN_RE = re.compile(r"\b\d{10,}\b")


def lesson_rejection(text: str) -> str:
    """Deterministic reason this lesson may not enter shared memory,
    or "" when it is clean. Whole-lesson rejection, never redaction."""
    text = str(text or "")
    if len(text.strip()) < 8:
        return "too short to be a lesson"
    if _UUID_RE.search(text):
        return "contains a UUID (org/agent/channel identifiers are banned)"
    if _SECRET_WORDS_RE.search(text):
        return "mentions credential material"
    if _LONG_TOKEN_RE.search(text):
        return "contains a long token-like string"
    if _CHANNEL_ID_RE.search(text):
        return "contains a channel/user identifier"
    if _EMAIL_RE.search(text):
        return "contains an email address"
    if _PHONE_RE.search(text) or _DIGIT_RUN_RE.search(text):
        return "contains a phone-like or long numeric identifier"
    return ""


def _norm_tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


def is_duplicate(candidate: str, existing_contents: List[str]) -> bool:
    """Same-lesson check: normalized-exact or high token overlap."""
    cand_tokens = _norm_tokens(candidate)
    if not cand_tokens:
        return True
    cand_norm = " ".join(sorted(cand_tokens))
    for content in existing_contents:
        tokens = _norm_tokens(content)
        if not tokens:
            continue
        if " ".join(sorted(tokens)) == cand_norm:
            return True
        union = cand_tokens | tokens
        if union and len(cand_tokens & tokens) / len(union) >= 0.75:
            return True
    return False


def resolve_settings(config: Optional[dict]) -> Dict[str, Any]:
    from ..config import get_bool, get_int

    config = config or {}
    settings = dict(CONSOLIDATION_DEFAULTS)
    settings["enabled"] = get_bool(config, "mission_consolidation", True)
    settings["max_lessons"] = max(1, get_int(
        config, "mission_consolidation_max_lessons",
        settings["max_lessons"],
    ))
    settings["max_chars"] = max(40, get_int(
        config, "mission_consolidation_max_chars", settings["max_chars"]
    ))
    try:
        settings["timeout"] = float(
            config.get("mission_consolidation_timeout",
                       settings["timeout"])
        )
    except (TypeError, ValueError):
        pass
    settings["store_cap"] = max(1, get_int(
        config, "mission_consolidation_store_cap", settings["store_cap"]
    ))
    settings["k"] = get_int(config, "mission_lessons_k", settings["k"])
    settings["block_chars"] = max(120, get_int(
        config, "mission_lessons_max_chars", settings["block_chars"]
    ))
    return settings


# ---------------------------------------------------------------------------
# Journal delta → consolidation input (bounded)
# ---------------------------------------------------------------------------

def session_delta_text(store: MissionStore, mission: Dict[str, Any],
                       input_chars: int) -> str:
    """This session's journal delta: events after the previous session's
    checkpoint (the just-committed checkpoint is the last one)."""
    mission_id = mission["mission_id"]
    checkpoints = [
        event["seq"] for event in store.events_since(
            mission_id, 0, ("session_checkpointed",), limit=10000
        )
    ]
    boundary = checkpoints[-2] if len(checkpoints) >= 2 else 0
    lines: List[str] = [
        f"Mission goal: {mission['spec'].get('goal', '')}",
        "Journal delta from the just-finished work session:",
    ]
    for event in store.events_since(
        mission_id, boundary, DELTA_EVENT_KINDS, limit=60
    ):
        if event["kind"] in ITEM_EVENT_KINDS:
            continue  # personal-space fence: items never feed lessons
        data = event["data"]
        brief = {
            key: str(data[key])[:400]
            for key in ("summary", "text", "title", "to", "reason",
                        "error", "detail", "failure_class", "action_class",
                        "status")
            if data.get(key)
        }
        if event["kind"] == "plan_recorded":
            brief["steps"] = "; ".join(
                str(step)[:80]
                for step in (data.get("content") or {}).get("steps", [])[:10]
            )
        lines.append(f"- {event['kind']}: {brief}")
    text = "\n".join(lines)
    if len(text) > input_chars:
        text = text[:input_chars] + "\n... [clipped]"
    return text


# ---------------------------------------------------------------------------
# The default runner: one weak-model call, no tools
# ---------------------------------------------------------------------------

def default_runner(config: dict, max_lessons: int) -> Callable:
    """``fn(text) -> (reply, error)`` on the weak model (fallback: main)."""

    def run(text: str):
        from ..providers import RAW_FNS
        from ..runtime import is_error_response, weak_model_config

        cfg = weak_model_config(config) or dict(config or {})
        provider = str(cfg.get("provider") or "").lower()
        raw_fn = RAW_FNS.get(provider)
        if raw_fn is None:
            return "", f"no provider available ({provider!r})"
        messages = [
            {"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT.format(
                max_lessons=max_lessons
            )},
            {"role": "user", "content": text},
        ]
        try:
            response = raw_fn(cfg, messages, None)
        except Exception as exc:
            return "", f"{type(exc).__name__}: {exc}"
        if is_error_response(response):
            return "", str(response.get("_error") or "provider error")
        return str(response.get("content") or ""), ""

    return run


def parse_lessons(reply: str, max_lessons: int,
                  max_chars: int) -> List[Dict[str, str]]:
    reply = str(reply or "")
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        raw = json.loads(reply[start:end + 1])
    except ValueError:
        return []
    lessons = []
    for row in (raw.get("lessons") or [])[:max_lessons]:
        if not isinstance(row, dict):
            continue
        topic = re.sub(r"\s+", " ", str(row.get("topic") or "")).strip()
        lesson = re.sub(r"\s+", " ", str(row.get("lesson") or "")).strip()
        if not lesson:
            continue
        lessons.append({
            "topic": topic[:40] or "general",
            "lesson": lesson[:max_chars],
        })
    return lessons


# ---------------------------------------------------------------------------
# The post-checkpoint side task
# ---------------------------------------------------------------------------

class _Consolidation(threading.Thread):
    """Worker: model call → scrub → dedupe → capped write. Checks the
    abandoned flag before writing so a timed-out pass never lands late."""

    def __init__(self, runner, text, mission_id, settings):
        super().__init__(name="conch-consolidate", daemon=True)
        self.runner = runner
        self.text = text
        self.mission_id = mission_id
        self.settings = settings
        self.abandoned = False
        self.result: Dict[str, Any] = {"status": "pending"}

    def run(self):
        try:
            self.result = self._consolidate()
        except Exception as exc:
            self.result = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _consolidate(self) -> Dict[str, Any]:
        reply, error = self.runner(self.text)
        if error:
            return {"status": "error", "error": error}
        lessons = parse_lessons(
            reply, self.settings["max_lessons"], self.settings["max_chars"]
        )
        if not lessons:
            return {"status": "ok", "saved": 0, "note": "no lessons"}
        if self.abandoned:
            return {"status": "abandoned"}
        from ..memory import MemoryStore

        # Fresh load right before the write step (the store is a small
        # atomically-replaced JSON file).
        memory = MemoryStore()
        existing = [
            str(entry.get("content", "")) for entry in memory.get_all()
        ]
        saved, rejected, duplicates = [], [], 0
        for row in lessons:
            content = f"[{row['topic']}] {row['lesson']}"
            reason = lesson_rejection(content)
            if reason:
                rejected.append(reason)
                continue
            if is_duplicate(content, existing):
                duplicates += 1
                continue
            if self.abandoned:
                return {"status": "abandoned", "saved": len(saved)}
            memory.add(content, source=f"mission:{self.mission_id}")
            existing.append(content)
            saved.append(content)
        self._evict_over_cap(memory)
        return {
            "status": "ok", "saved": len(saved),
            "rejected": rejected, "duplicates": duplicates,
        }

    def _evict_over_cap(self, memory) -> None:
        cap = int(self.settings["store_cap"])
        mission_entries = [
            entry for entry in memory.get_all()
            if str(entry.get("source", "")).startswith("mission:")
        ]
        overflow = len(mission_entries) - cap
        for entry in sorted(
            mission_entries, key=lambda item: int(item["id"])
        )[:max(overflow, 0)]:
            memory.forget(int(entry["id"]))


def after_checkpoint(store: MissionStore, mission: Dict[str, Any],
                     session_id: str, config: Optional[dict],
                     log: Optional[Callable[[str], None]] = None,
                     runner: Optional[Callable] = None) -> Dict[str, Any]:
    """Post-checkpoint consolidation side task.

    Called by the engine AFTER the session checkpoint committed — a
    failure or timeout here is a logged skip and can never affect the
    session outcome. Returns a status dict for logging/tests.
    """
    log = log or (lambda line: None)
    settings = resolve_settings(config)
    if not settings["enabled"]:
        return {"status": "disabled"}
    if mission["spec"].get("kind") == MissionKind.SCHEDULED_PROMPT:
        return {"status": "skipped", "note": "scheduled prompts are not"
                " consolidated"}
    text = session_delta_text(store, mission, settings["input_chars"])
    worker = _Consolidation(
        runner or default_runner(config or {}, settings["max_lessons"]),
        text, mission["mission_id"], settings,
    )
    worker.start()
    worker.join(timeout=settings["timeout"])
    if worker.is_alive():
        worker.abandoned = True
        log(
            f"consolidation for {mission['mission_id']} session"
            f" {session_id}: timed out after {settings['timeout']}s —"
            " skipped"
        )
        return {"status": "timeout"}
    result = worker.result
    if result.get("status") == "ok":
        log(
            f"consolidation for {mission['mission_id']} session"
            f" {session_id}: saved {result.get('saved', 0)} lesson(s)"
            + (f", {result['duplicates']} duplicate(s) skipped"
               if result.get("duplicates") else "")
            + (f", {len(result['rejected'])} rejected"
               if result.get("rejected") else "")
        )
    else:
        log(
            f"consolidation for {mission['mission_id']} session"
            f" {session_id}: skipped"
            f" ({result.get('error', result.get('status'))})"
        )
    return result


# ---------------------------------------------------------------------------
# Rehydration: the shared-lessons context block
# ---------------------------------------------------------------------------

def lessons_block(mission: Dict[str, Any], plan_steps: List[str],
                  task_titles: List[str],
                  config: Optional[dict]) -> str:
    """Top-K relevant shared lessons for this mission's rehydration.

    FTS match on goal/plan/task keywords; K small; hard char cap; the
    mission's own lessons excluded (its journal already knows them).
    Returns "" when disabled, empty, or on any failure — rehydration
    must never break on the memory tier.
    """
    settings = resolve_settings(config)
    if not settings["enabled"] or settings["k"] <= 0:
        return ""
    try:
        from ..memory import MemoryStore

        query = " ".join(
            [str(mission["spec"].get("goal", ""))]
            + [str(step) for step in plan_steps[:10]]
            + [str(title) for title in task_titles[:10]]
        )[:600]
        entries = MemoryStore().rank_entries(
            query, limit=settings["k"], source_prefix="mission:",
            exclude_source=f"mission:{mission['mission_id']}",
        )
    except Exception:
        return ""
    if not entries:
        return ""
    lines = [LESSONS_LABEL]
    budget = settings["block_chars"] - len(LESSONS_LABEL)
    for entry in entries:
        line = f"  - {entry.get('content', '')} ({entry.get('source', '')})"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
