"""The compilation session: a bounded AgentSession that designs, never
provisions.

Stages per the plan:

- **Clarify** — bounded, park-on-ambiguity: consequential unknowns land
  in the card's ``open_questions`` (surfaced at review) instead of being
  guessed into infrastructure.
- **Discover** — the deterministic scaffolding enumerates org workflows,
  agents, collections, loaded packs, the node catalog, and Conch-side
  capabilities before the model sees anything; the session additionally
  gets ``capitol_control`` restricted to read/discovery ops (start,
  respond, uploads, and every admin op are refused — the session designs,
  it never provisions and never runs).
- **Design** — reuse-first is a hard rule enforced in code
  (:func:`conch.capitol.compiler.card.normalize_card` rejects creating
  what discovery already found); the Rev 2.1 split (agentic judgment,
  deterministic gates only at money/irreversibility) is the prompt's
  contract and the card's structure.
- **Emit** — the model submits the card through the
  ``compiler_workspace`` tool; validation is fail-closed code and a
  rejected card comes back as the tool result so the model can repair it
  within the session's budgets.

The session loads the shipped ``capitol`` and ``pack-author`` skills —
this is their designed purpose — and runs under mission-session-style
budgets (wall clock enforced by a required-policy check, tool rounds and
token budget by ``SessionBudgets``).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from ..errors import CapitolAuthError, CapitolError
from ..tool import CapitolSessionClient
from .card import (
    CARD_SCHEMA,
    DEFAULT_ASSET_PREFIX,
    CardError,
    empty_discovery,
    normalize_card,
)
from .graph import STAGE_KINDS

#: Mission-session-style defaults (kernel SESSION_DEFAULTS shape).
COMPILE_WALL_SECONDS = 600
COMPILE_MAX_TOOL_ROUNDS = 15
COMPILE_TOKEN_BUDGET = 200000

#: Bounded discovery digest injected into the session prompt.
_DISCOVERY_SECTION_CAP = 4000

#: capitol_control ops a compilation session may call. Everything else —
#: start, respond, upload/download, admin, packs — is refused by name.
READ_OPS = frozenset({
    "discover", "workflows", "describe", "suggest", "versions", "stats",
    "runs", "status", "outputs", "evals",
})

COMPILER_SYSTEM_PROMPT = """You are the Conch ProcessCompiler running one bounded, headless compilation session. Your job is to DESIGN a process, not to build or run anything: you turn the user's goal into one reviewable Architecture Card. A human reviews and approves the card before any asset is created; nothing you do here has external effects.

Method:
1. Read the discovery digest below — it lists what already exists (org workflows, agents, collections, flow packs, workflow node catalog, Conch-side capabilities). Use capitol_control (read ops only) when you need detail, e.g. op='describe' on a workflow you might reuse.
2. REUSE-FIRST IS A HARD RULE: never design a new asset when an existing one fits; list reused assets in assets.reuse with the real ids discovery gave you. Card validation rejects duplicates of existing assets.
3. Apply the judgment/determinism split: judgment lives in agentic stages (agent nodes with clear, self-contained system prompts, measured by eval criteria); deterministic gates only where money or irreversibility lives (caps, approval classes, HITL points).
4. Park on ambiguity: if a consequential parameter is genuinely unknowable from the goal (which account, which channel, spend limits), put a precise question in open_questions and choose the safest conservative default — never guess a consequential value silently.
5. Emit the card with compiler_workspace op='emit_card'. If validation rejects it, fix exactly what the error names and emit again. The session fails without a valid emitted card.

Design constraints (validation enforces them):
- Created asset identities are lowercase-hyphen names starting with the given prefix; the schedule ships DISABLED unless the goal demands otherwise (shadow rollout: the user arms it).
- Agent-node system prompts must be complete operating instructions (fixed in the workflow; only the input value arrives as data). Reference a collection id inside a prompt as $collection:<id-or-created-identity> and it will be substituted at materialization. When the workflow must be drillable without live accounts, design the input so a synthetic payload (e.g. a JSON array of synthetic records) is processed instead of live data, and say so in the prompt.
- Drill fixtures are synthetic ONLY: any email-like string must use example.com/.test/.invalid domains. The drill runs the real workflow with your fixture input and asserts the expected gates.
- The supervising mission spec is dry-run (enforced) and should bind the created workflows with a modest cadence and budgets.
Never ask questions in plain text — the user is not watching this session. The card IS your deliverable."""


def card_requirements(prefix: str) -> str:
    """The card contract shown to the model (deterministic text)."""
    return f"""ARCHITECTURE CARD SHAPE (schema {CARD_SCHEMA}) — emit exactly this JSON object via compiler_workspace op='emit_card':
{{
  "schema": "{CARD_SCHEMA}",
  "goal": "<the goal, verbatimish>",
  "success_criteria": ["<observable outcomes>", ...],
  "narrative": "<how the process works end to end, a short paragraph>",
  "assets": {{
    "reuse": [{{"kind": "workflow|agent|collection|node|pack|conch", "id": "<discovered id>", "name": "<name>", "reason": "<why it fits>"}}, ...],
    "create": {{
      "workflows": [{{"identity": "{prefix}-<name>", "name": "{prefix}-<name>", "description": "...",
        "stages": [
          {{"kind": "text_input", "role": "<role>", "name": "...", "default": "<scheduled default input>", "info": "..."}},
          {{"kind": "agent", "role": "<role>", "name": "...", "system_prompt": "<complete instructions>", "tools": ["<catalog node ids>"], "model": "<optional>", "temperature": 0.2, "timeout": 600}},
          {{"kind": "docx", "role": "<role>", "name": "...", "from": "<earlier role>"}},
          {{"kind": "notify", "role": "<role>", "name": "...", "title": "...", "from": "<earlier role>"}}
        ]}}],
      "agent": {{"identity": "{prefix}-<name>", "name": "{prefix}-<name>", "description": "...", "workflows": ["$create:<workflow identity>", ...]}} or null,
      "schedules": [{{"identity": "{prefix}-<name>", "name": "{prefix}-<name>", "workflow": "$create:<workflow identity>", "cron": "<5 fields>", "timezone": "UTC", "enabled": false}}],
      "collections": [{{"identity": "{prefix}-<name>", "name": "...", "description": "..."}}]
    }}
  }},
  "pack": {{"name": "{prefix}-<name>", "description": "..."}},
  "caps": ["<hard limits>"], "approval_classes": ["<what needs approval>"],
  "hitl": ["<where a human is in the loop>"], "eval_criteria": ["<how judgment stages are measured>"],
  "drill": {{"fixtures": [{{"workflow": "$create:<workflow identity>", "input": <synthetic input value for the input stage>, "expect": {{"status": "success", "output_contains": ["<marker the run output must contain>"]}}}}], "notes": "..."}},
  "rollout": {{"rung": "shadow", "notes": "..."}},
  "rollback": ["<ordered rollback steps>"],
  "estimates": {{"cost": "...", "latency": "..."}},
  "open_questions": ["<questions for the reviewer>"],
  "mission": {{"goal": "supervise <process>", "budgets": {{"sessions": 24}}, "cadence_seconds": 3600,
    "capitol": {{"workflows": ["$create:<workflow identity>"], "allow_start": false, "allow_respond": true, "bind_scheduled": true}}}}
}}
Stage kinds: {", ".join(sorted(STAGE_KINDS))} (first stage must be text_input or json_input; later stages chain via "from"; agent tools must be node-catalog ids).
Workflow/collection references: "$create:<identity>" for assets this card creates; otherwise a real discovered id."""


# ---------------------------------------------------------------------------
# Discovery (deterministic scaffolding, best-effort against the live org)
# ---------------------------------------------------------------------------

def conch_capabilities() -> List[str]:
    """Conch-side capabilities from the builtin tool registry."""
    from ... import tooling

    capabilities: List[str] = []
    for attr in sorted(dir(tooling)):
        if not attr.endswith("_TOOL"):
            continue
        tool = getattr(tooling, attr)
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if name:
            description = str(function.get("description") or "")
            capabilities.append(f"{name}: {description[:120]}")
    return capabilities


def build_discovery(config: dict) -> Dict[str, Any]:
    """Enumerate what exists: org workflows/agents (runtime card),
    collections + node catalog (admin-token reads when a token resolves),
    loaded flow packs, and Conch builtin capabilities. Sections that
    cannot be reached degrade to a note — discovery never blocks a
    compile, it just narrows it."""
    discovery = empty_discovery()
    notes: List[str] = []
    try:
        from ..client import CapitolRuntime

        runtime = CapitolRuntime.from_config(config)
        for workflow in runtime.list_workflows():
            discovery["workflows"].append({
                "id": str(workflow.get("workflow_id")
                          or workflow.get("id") or ""),
                "name": str(workflow.get("name") or ""),
            })
        for agent in runtime.list_org_agents():
            discovery["agents"].append({
                "id": str(agent.get("agent_id") or ""),
                "name": str(agent.get("name") or ""),
            })
    except (CapitolError, CapitolAuthError) as exc:
        notes.append(f"agent-card discovery unavailable: {exc}"[:200])
    org = str(config.get("capitol_org") or "").strip()
    workflow_url = str(config.get("capitol_base_url") or "").strip()
    platform_url = str(config.get("capitol_platform_url") or "").strip()
    token = ""
    if org and platform_url:
        try:
            from ..credentials import resolve_admin_token

            token, _source = resolve_admin_token(config, org, platform_url)
        except (CapitolError, CapitolAuthError) as exc:
            notes.append(f"platform reads unavailable: {exc}"[:200])
    if token and platform_url:
        try:
            from ..together_funding import _http_json

            payload = _http_json(
                f"{platform_url.rstrip('/')}/collections/{org}",
                headers={"Authorization": f"Bearer {token}"},
            )
            rows = (
                payload if isinstance(payload, list)
                else (payload or {}).get("collections") or []
            )
            for row in rows:
                discovery["collections"].append({
                    "id": str(row.get("id") or ""),
                    "name": str(row.get("name") or ""),
                })
        except CapitolError as exc:
            notes.append(f"collection listing unavailable: {exc}"[:200])
    if token and workflow_url:
        try:
            from ..together_funding import fetch_node_catalog

            catalog = fetch_node_catalog(workflow_url, org, token)
            discovery["node_catalog"] = sorted(catalog)
        except CapitolError as exc:
            notes.append(f"node catalog unavailable: {exc}"[:200])
    try:
        from ..packs import list_packs

        for pack in list_packs():
            discovery["packs"].append({"name": pack.name})
    except Exception as exc:
        notes.append(f"pack listing unavailable: {exc}"[:200])
    discovery["conch"] = conch_capabilities()
    discovery["notes"] = notes
    return discovery


def _clip(text: str, cap: int) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    return text[:cap] + "\n... [clipped]"


def discovery_digest(discovery: Dict[str, Any]) -> str:
    """The bounded discovery block injected into the session prompt."""
    parts: List[str] = ["DISCOVERY DIGEST (what already exists):"]
    workflows = discovery.get("workflows") or []
    parts.append(_clip(
        f"Org workflows ({len(workflows)}):\n" + "\n".join(
            f"  {row['id']}  {row['name']}" for row in workflows
        ) if workflows else "Org workflows: (none discovered)",
        _DISCOVERY_SECTION_CAP,
    ))
    agents = discovery.get("agents") or []
    parts.append(_clip(
        f"Org agents ({len(agents)}):\n" + "\n".join(
            f"  {row['id']}  {row['name']}" for row in agents
        ) if agents else "Org agents: (none discovered)",
        1500,
    ))
    collections = discovery.get("collections") or []
    parts.append(_clip(
        f"Collections ({len(collections)}):\n" + "\n".join(
            f"  {row['id']}  {row['name']}" for row in collections
        ) if collections else "Collections: (none discovered)",
        1500,
    ))
    packs = discovery.get("packs") or []
    parts.append(
        "Flow packs: " + (", ".join(
            row["name"] for row in packs
        ) if packs else "(none)")
    )
    catalog = discovery.get("node_catalog") or []
    parts.append(_clip(
        f"Workflow node catalog ({len(catalog)} node kinds): "
        + ", ".join(catalog) if catalog
        else "Workflow node catalog: (unavailable)",
        _DISCOVERY_SECTION_CAP,
    ))
    parts.append(_clip(
        "Conch-side capabilities:\n" + "\n".join(
            f"  {line}" for line in discovery.get("conch") or []
        ),
        2000,
    ))
    for note in discovery.get("notes") or []:
        parts.append(f"note: {note}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Session tool clients
# ---------------------------------------------------------------------------

class ReadOnlyCapitolClient:
    """``capitol_control`` restricted to read/discovery ops.

    The compilation session designs; it never starts runs, answers HITL,
    moves artifacts, or provisions. Refusals name the boundary so the
    model stops trying instead of working around it.
    """

    name = "capitol_control"

    def __init__(self, config: dict):
        self._inner = CapitolSessionClient(config)

    def call_tool(self, name: str, arguments: dict) -> Dict[str, Any]:
        arguments = arguments or {}
        op = str(arguments.get("op") or "").strip().lower()
        if op not in READ_OPS:
            return {"content": [{"type": "text", "text": (
                f"capitol_control refuses {op!r} in a compilation "
                "session: the compiler designs, it never provisions or "
                "runs. Read/discovery ops only: "
                + ", ".join(sorted(READ_OPS))
            )}]}
        return self._inner.call_tool(name, arguments)


COMPILER_WORKSPACE_TOOL = {
    "type": "function",
    "function": {
        "name": "compiler_workspace",
        "description": (
            "The compilation workspace: op='requirements' returns the "
            "exact Architecture Card JSON contract; op='emit_card' "
            "submits the card (validated fail-closed — a rejection names "
            "exactly what to fix; emit again after fixing). The session "
            "succeeds only when a card has been accepted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["requirements", "emit_card"],
                    "description": "The workspace operation.",
                },
                "card": {
                    "type": "object",
                    "description": (
                        "emit_card: the full architecture card object."
                    ),
                },
            },
            "required": ["op"],
        },
    },
}


class CompilerWorkspaceClient:
    """Holds the staged card; validation is code, the model fills
    judgment. A validated card replaces any earlier staged one."""

    name = "compiler_workspace"

    def __init__(self, discovery: Dict[str, Any], prefix: str):
        self._discovery = discovery
        self._prefix = prefix
        self.card: Optional[Dict[str, Any]] = None
        self.attempts = 0

    @staticmethod
    def _text(message: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": message}]}

    def call_tool(self, name: str, arguments: dict) -> Dict[str, Any]:
        arguments = arguments or {}
        op = str(arguments.get("op") or "").strip().lower()
        if op == "requirements":
            return self._text(card_requirements(self._prefix))
        if op == "emit_card":
            raw = arguments.get("card")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError as exc:
                    return self._text(
                        f"emit_card rejected: card is not valid JSON "
                        f"({exc})"
                    )
            self.attempts += 1
            try:
                self.card = normalize_card(
                    raw, self._discovery, prefix=self._prefix
                )
            except (CardError, CapitolError) as exc:
                return self._text(
                    f"emit_card rejected (fix exactly this and emit "
                    f"again): {exc}"
                )
            except Exception as exc:  # CredentialRejected and friends
                return self._text(
                    f"emit_card rejected: {type(exc).__name__}: {exc}"
                )
            questions = len(self.card.get("open_questions") or [])
            return self._text(
                "Card accepted"
                + (f" with {questions} open question(s) parked for the"
                   " reviewer" if questions else "")
                + ". You are done — end the session with a one-line"
                " summary."
            )
        return self._text(
            f"unknown compiler_workspace op {op!r} — requirements or "
            "emit_card"
        )


# ---------------------------------------------------------------------------
# The session itself
# ---------------------------------------------------------------------------

def _default_session_factory(config: dict) -> Callable:
    """Real AgentSession runner (fresh bounded session per compile)."""

    def run(messages: List[dict], workspace: CompilerWorkspaceClient,
            capitol_client: Optional[ReadOnlyCapitolClient],
            caps: Dict[str, int]):
        import time as _time

        from ...bootstrap import build_agent_session
        from ...policy import (
            PolicyDecision,
            register_required_policy,
            unregister_required_policy,
        )
        from ...session import SessionBudgets
        from ...tooling import default_permissions
        from ..tool import CAPITOL_SESSION_TOOL

        session = build_agent_session(
            config, interactive=False, permissions=default_permissions(),
        )
        deadline = _time.monotonic() + caps["wall_seconds"]
        check_name = f"compile-wall:{id(workspace)}"

        def wall_check(event: str, payload: dict):
            if _time.monotonic() > deadline:
                return PolicyDecision.deny(
                    "compilation session wall budget exhausted",
                    check=check_name,
                )
            return PolicyDecision.allow()

        register_required_policy(check_name, wall_check)
        try:
            session.budgets = SessionBudgets(
                max_tool_rounds=caps["max_tool_rounds"],
                turn_token_budget=caps["token_budget"],
            )
            # The compilation session sees exactly two tools: the
            # read-only Capitol surface and the workspace. Everything
            # else (shell, items, delegation, admin) is out of scope for
            # a design session.
            session.builtin_clients = {
                "compiler_workspace": workspace,
            }
            tools = [COMPILER_WORKSPACE_TOOL]
            if capitol_client is not None:
                session.builtin_clients["capitol_control"] = capitol_client
                tools.append(CAPITOL_SESSION_TOOL)
            return session.run_turn(
                messages, tools=tools, tool_map={},
                max_tool_rounds=caps["max_tool_rounds"],
            )
        finally:
            unregister_required_policy(check_name)
            session.close()
    return run


def _skill_blocks() -> str:
    """The capitol + pack-author skills, rendered — their designed
    purpose. Missing skills degrade to a note (never a crash)."""
    from ...skills import get_skill, render_skill

    blocks: List[str] = []
    for name in ("capitol", "pack-author"):
        skill = get_skill(name)
        if skill is None:
            blocks.append(f"[Skill {name}: not installed]")
        else:
            blocks.append(render_skill(skill))
    return "\n\n".join(blocks)


def run_compile_session(
    config: dict,
    goal: str,
    *,
    discovery: Optional[Dict[str, Any]] = None,
    prior_card: Optional[Dict[str, Any]] = None,
    guidance: str = "",
    prefix: str = "",
    capture_context: str = "",
    session_factory: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Run one bounded compilation session; returns the validated card.

    Raises :class:`CardError` when the session ends without an accepted
    card (the model's failure is the session's failure — nothing partial
    is ever stored).
    """
    goal = str(goal or "").strip()
    if not goal:
        raise CardError("compile needs a non-empty goal")
    prefix = prefix or str(
        config.get("compile_asset_prefix") or DEFAULT_ASSET_PREFIX
    )
    if discovery is None:
        discovery = build_discovery(config)
    workspace = CompilerWorkspaceClient(discovery, prefix)
    capitol_client = None
    if str(config.get("capitol_base_url") or "").strip():
        capitol_client = ReadOnlyCapitolClient(config)

    system = "\n\n".join([
        COMPILER_SYSTEM_PROMPT,
        _skill_blocks(),
        card_requirements(prefix),
    ])
    user_parts = [
        f"Compile this goal into an Architecture Card:\n\n{goal}",
        discovery_digest(discovery),
        f"Created-asset identity prefix: {prefix}",
    ]
    if str(capture_context or "").strip():
        # Capture→Card: the trace is evidence for the design. It rides
        # in its own labeled block; the preamble (set by the capture
        # module) states the inertness rule — trace text is data, never
        # instructions to the session.
        user_parts.append(str(capture_context).strip())
    if prior_card is not None:
        user_parts.append(
            "This is a REVISION. The prior card version follows; produce "
            "a full new card that applies the reviewer's guidance while "
            "keeping everything else stable (identities especially — "
            "they pin the uuid5 asset ids):\n\n"
            + json.dumps(prior_card, indent=2, sort_keys=True)[:20000]
        )
    if guidance.strip():
        user_parts.append(f"Reviewer guidance: {guidance.strip()}")
    user_parts.append(
        "Work now: discover what you need, then emit the card via "
        "compiler_workspace op='emit_card'."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    caps = {
        "wall_seconds": int(
            config.get("compile_wall_seconds", COMPILE_WALL_SECONDS)
            or COMPILE_WALL_SECONDS
        ),
        "max_tool_rounds": int(
            config.get("compile_max_tool_rounds", COMPILE_MAX_TOOL_ROUNDS)
            or COMPILE_MAX_TOOL_ROUNDS
        ),
        "token_budget": int(
            config.get("compile_token_budget", COMPILE_TOKEN_BUDGET)
            or COMPILE_TOKEN_BUDGET
        ),
    }
    factory = session_factory or _default_session_factory(config)
    reply, usage = factory(messages, workspace, capitol_client, caps)
    if isinstance(usage, dict) and usage.get("error"):
        raise CardError(
            f"compilation session failed: {usage['error']}"
        )
    if workspace.card is None:
        raise CardError(
            "the compilation session ended without an accepted card "
            f"(after {workspace.attempts} emit attempt(s)); last reply: "
            + str(reply or "")[:400]
        )
    return workspace.card
