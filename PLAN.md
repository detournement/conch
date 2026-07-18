# Conch improvement plan

Phased plan from the July 2026 architectural review, focused on making conch a
first-class agent against a local Ollama server, then building up agent
capability and extensibility.

**Governing constraint (tools-only models):** conch supports *only*
tool-calling-capable models, on every provider. Non-tool models are out of
scope. Enforcement mechanism on Ollama: `POST /api/show` → `capabilities`
array must contain `"tools"`. Consequences baked into this plan:

- Textual/regex tool-call recovery (`extract_textual_tool_use_blocks`) is
  **removed**, not improved.
- Ask-mode free-text command scraping in `llm.py` (`extract_command`,
  `_SHELL_PREFIXES`, fenced-block/backtick regexes) is **replaced** with
  structured output: Ollama's `format` JSON-schema parameter locally, a forced
  tool call / structured response format on cloud providers. No classifier
  model.

Effort scale: **S** = hours, **M** = 1–3 days, **L** = a week or more.

**Status (July 2026):** Phases 0–3 are complete (✅ markers below); Phase 4
is not started. One deliberate deviation from the text of 0.5 is noted
inline; 2.7 applies the weak model to summaries and compaction (conversation
titles never used an LLM — they come from the first user message). Hotfix
f3ad212 (older/smaller local Ollama servers: /api/show name compat,
unknown-capability models kept, conservative num_ctx when the model max is
unknown, ask-mode legacy format fallback, graceful startup resolution) is
folded into the 0.1/0.4/0.6 behavior described here.

---

## Phase 0 — Correctness on local Ollama (do first)

These fix defects that make local sessions appear randomly broken today, plus
the tools-only enforcement. All are small and independent.

### 0.1 Set `num_ctx` and `keep_alive` on every Ollama request — **S** ✅
- **Problem:** `raw_ollama` / `stream_ollama` never pass `options.num_ctx`.
  Ollama defaults to 4k context under 24 GiB VRAM and truncates silently from
  the top (evicting system prompt + tool schemas), while
  `CONTEXT_LIMITS["ollama"] = 28000` in `runtime.py` assumes 28k. Result:
  tool calling "inexplicably" stops after a few exchanges.
- **Fix:** config key `ollama_num_ctx` (default 32768) passed as
  `options.num_ctx`; set `keep_alive` to keep the model warm; read the model's
  max context from `/api/show` (`model_info`) and clamp; derive the runtime
  context limit from the same number.
- **Modules:** `providers.py`, `runtime.py`, `config.py`.

### 0.2 Accumulate streamed tool calls from every chunk — **S** ✅
- **Problem:** `stream_ollama` reads `message.tool_calls` only from the final
  `done` chunk. Current Ollama emits tool calls on intermediate chunks, so in
  interactive chat (streaming always on) tool calls are silently dropped.
- **Fix:** accumulate `message.tool_calls` across all chunks before `done`.
  Coordinate with the in-flight qwen tool-calling fix on this branch.
- **Modules:** `providers.py` (`stream_ollama`).

### 0.3 Unify error signaling; never persist error strings — **M** ✅
- **Problem:** Ollama errors use the `[Ollama error:` prefix, but all
  retry/fallback logic in `chat_turn` keys on `[API error:`. A dead server
  yields no retry, no warning; the error string is appended to history as an
  assistant reply, and `_summarize_and_save` at exit can save
  `[Session summary] [Ollama error: ...]` as a permanent memory.
- **Fix:** structured error signaling (single prefix or an `_error` field);
  extend transient detection to connection-refused/timeout/404; never append
  error text to history or memory; print a clear "Ollama server unreachable
  at <url>" message and keep the session alive for retry.
- **Modules:** `providers.py`, `runtime.py`, `app.py`.

### 0.4 Capability gating via `/api/show` (tools-only directive) — **S–M** ✅
- **Fix:** on model selection, verify `capabilities` contains `"tools"`;
  reject non-tool models with a clear message. Enforce at startup config
  validation, `/model`, `/provider`, `conch_config` `set_model`/`set_provider`,
  and in the fallback chain. Cache the result per model name. Apply the same
  tool-capable-only rule to model lists for all providers.
- **Modules:** `providers.py`, `commands.py`, `tooling.py`, `config.py`.

### 0.5 Remove textual tool-call recovery — **S** ✅
- **Fix:** delete `extract_textual_tool_use_blocks` and its call site in
  `chat_turn` (the `<tool_called .../>` XML and `ast.literal_eval` paths).
  Native `tool_calls` only. Also removes the hazard of executing tool-call
  syntax merely *quoted* in prose.
- **Modules:** `runtime.py`.
- **As implemented (deviation):** the `<tool_called>` XML and
  `ast.literal_eval` paths are deleted as specified, but the strict
  JSON-only recovery (bare `{"name": ..., "arguments": ...}` content and
  `<tool_call>` JSON tags) is kept: live testing showed qwen2.5-coder via
  Ollama emitting real tool calls this way that Ollama fails to parse into
  structured `tool_calls`. The guard requires exact tool-call shape, so
  quoted prose and ordinary JSON replies are never executed.

### 0.6 Replace ask-mode regex extraction with structured output — **M** ✅
- **Fix:** delete `_SHELL_PREFIXES` / `extract_command` and the per-provider
  extraction heuristics. Ask mode requests
  `format: {"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}`
  on Ollama, and a forced single tool call (or JSON response format) on
  OpenAI/Anthropic/Cerebras. Also fix `call_ollama`: send proper system+user
  roles (it currently flattens both into one user message) and align its
  default model with `config.py`.
- **Modules:** `llm.py`, `prompts.py`.

### 0.7 Local fallback chain from live model list — **S–M** ✅
- **Problem:** `get_fallback_chain` walks hardcoded `KNOWN_MODELS["ollama"]`
  (models likely not pulled), 404ing/timing out serially, then tries cloud
  providers that don't exist offline.
- **Fix:** build the Ollama fallback list from live `/api/tags`, filtered by
  the 0.4 capability check. Drop `KNOWN_MODELS["ollama"]` as source of truth.
  Dovetails with the live-model-list work already on this branch.
- **Modules:** `providers.py`.

---

## Phase 1 — Context economy and early extensibility

Small models live or die on token budget. Items 1.7–1.9 are the cheap,
high-value extensibility features pulled forward because they need no Phase 0
plumbing and immediately improve daily use.

### 1.1 Stable prompt prefix for KV-cache reuse — **S** ✅
- **Problem:** `messages[0]` is rewritten every turn (timestamp + memory
  context), invalidating Ollama's KV prefix cache → full prompt re-ingestion
  each turn; tens of seconds on long histories with local hardware.
- **Fix:** keep the system message byte-stable within a session; move
  timestamp and memory-recall context into the current user message.
- **Modules:** `app.py`.

### 1.2 Slim the local system prompt (~250 tokens) — **S** ✅
- **Problem:** the ollama chat prompt is ~990 tokens, mostly slash-command
  docs (explicitly "handled by Conch, NOT by you"), Composio/APILayer
  catalogs, and a credential dump from `~/.config/conch/*`.
- **Fix:** compact ollama prompt variant; remove `_load_config_credentials`
  injection and the "save API keys to memory" instruction entirely.
- **Modules:** `prompts.py`, `app.py`.

### 1.3 Real token accounting + context gauge — **M** ✅
- **Fix:** calibrate `estimate_tokens` against the `prompt_eval_count` Ollama
  returns on every response; show a context-usage gauge in the per-turn usage
  line; warn at ~80% of `num_ctx`.
- **Modules:** `runtime.py`, `app.py`.

### 1.4 Model-generated compaction (auto-compact) — **M** ✅
- **Problem:** `compress_context` char-slices messages (first 200 + last 100
  chars) and drops middles — garbage input for small models.
- **Fix:** at ~70% of `num_ctx`, summarize older history with the LLM into a
  single summary message; keep system + last N turns verbatim. Keep cheap
  char-capping only as a first layer for oversized single messages
  (Claude Code auto-compact / Hermes compressor pattern).
- **Modules:** `runtime.py`.

### 1.5 Token-aware tool-result truncation — **S** ✅
- **Fix:** replace fixed char caps (15,000 in `local_shell`, 8,000 in
  `chat_turn`) with a budget scaled to context (e.g. ≤10% of `num_ctx` per
  result), keeping head + tail.
- **Modules:** `runtime.py`, `tooling.py`.

### 1.6 Small relevant tool set for local + config-defined profiles — **M** ✅
- **Fix:** cap effective tools at ~10–12 for local models, selected by
  relevance to the current user turn instead of list order
  (`PROVIDER_TOOL_LIMITS` truncation today is arbitrary). Auto-activate the
  `minimal` profile when provider is ollama unless overridden. Make tool
  profiles **user-definable in config** (named group sets in
  `~/.config/conch/config` or a profiles file), not just builtin presets —
  the `custom_profiles` prefs hook in `tooling.py` already exists.
- **Modules:** `tooling.py`, `runtime.py`, `config.py`.

### 1.7 User-defined slash commands — **S** *(extensibility)* ✅
- **Fix:** markdown files in `~/.config/conch/commands/` become `/name`
  commands; the file body is a prompt template with `$ARGUMENTS`
  interpolation. Loader + dispatch in `handle_slash_command`; add names to the
  readline completer.
- **Modules:** `commands.py`, `app.py` (completer).

### 1.8 Project-level context and config — **S–M** *(extensibility)* ✅
- **Fix:** read `CONCH.md` / `AGENTS.md` from cwd walking up to the git root
  and inject into the system prompt (the CLAUDE.md pattern: persistent
  instructions live outside compactable history). Support per-project
  `.conchrc` overriding global config (provider, model, num_ctx, profile).
  Budget the injected context (cap at ~1–2k tokens) so it cannot swamp small
  models.
- **Modules:** `config.py`, `prompts.py`, `app.py`.

### 1.9 Per-model system-prompt template overrides — **S** *(extensibility)* ✅
- **Fix:** allow config to map provider/model (glob) → prompt template file,
  overriding the builtin `CHAT_PROMPTS`/`ASK_PROMPTS`. Pairs naturally with
  1.2 (users tuning prompts for their specific local model).
- **Modules:** `prompts.py`, `config.py`.

---

## Phase 2 — Agent capability and integration surface

### 2.1 Permission model upgrade — **M** ✅
- **Fix:** replace exact-command always-allow with command-prefix allowlists
  (`git status`, `ls`, … auto-approved) plus a destructive-command check that
  still prompts even in agent mode. Graded modes like Codex CLI:
  prompt-all / safe-auto / yolo.
- **Modules:** `tooling.py` (`LocalShellClient`).

### 2.2 Lifecycle hooks — **M** *(extensibility)* ✅
- **Fix:** user shell scripts configured for `pre_tool_use` (receives tool
  name + JSON args; non-zero exit blocks the call, stdout can rewrite the
  command), `post_tool_use` (receives result), and `on_turn_end`. This is
  also the natural extension point for custom policy beyond 2.1 (the
  Claude Code hooks pattern: deterministic gates around the loop, not prompt
  pleading).
- **Modules:** `runtime.py` (dispatch points), `tooling.py`, `config.py`
  (hook registration).

### 2.3 Executable tools directory — **M** *(extensibility)* ✅
- **Fix:** any executable in `~/.config/conch/tools/` becomes a tool:
  `--schema` flag emits its JSON schema (name/description/parameters);
  invocation passes arguments as JSON on stdin; stdout is the tool result
  (subject to 1.5 truncation). Registered alongside MCP/builtin tools in
  `inject_builtin_tools` / the tool map, grouped as `user` for profile
  filtering. Much lighter than writing an MCP server for one-off tools.
- **Modules:** `tooling.py`.

### 2.4 Custom OpenAI-compatible providers — **S–M** *(extensibility)* ✅
- **Fix:** `provider=custom` (or named custom entries) in config with
  `base_url` + optional `api_key_env` + model name, reusing the existing
  OpenAI adapter and `_stream_openai_compat`. Supports vLLM, LM Studio,
  llama.cpp server, or a second Ollama box via its OpenAI endpoint.
  Capability gating (Phase 0.4) applies: custom endpoints are assumed
  tool-capable, verified by a startup probe call. Independent of other Phase 2
  items — can be pulled forward if a non-Ollama local backend is needed.
- **Modules:** `providers.py`, `config.py`, `commands.py` (`/provider`).

### 2.5 Plan/todo scratchpad tool — **M** ✅
- **Fix:** a todo/plan tool whose state is re-injected each round *outside*
  compactable history — keeps small models on track across long tool
  sequences (Claude Code TodoWrite / Hermes kanban pattern).
- **Modules:** `tooling.py` (new tool), `runtime.py` (injection).

### 2.6 Budget accounting and graceful exhaustion — **S** ✅
- **Fix:** extend `max_tool_rounds` with a token budget; on exhaustion, ask
  the model to summarize progress instead of returning
  `[max tool call rounds reached]` (Hermes IterationBudget pattern).
- **Modules:** `runtime.py`.

### 2.7 Weak-model side tasks — **M** ✅
- **Fix:** run conversation titles, session summaries, and 1.4 compaction on
  a small fast local model (configurable, e.g. a 3B) instead of the main chat
  model (aider weak-model pattern).
- **Modules:** `app.py`, `runtime.py`, `config.py`.

### 2.8 Memory upgrade — **M–L** ✅
- **Fix:** move toward Hermes's tiers: a bounded always-loaded facts file
  plus SQLite FTS5 search over conversation history, replacing keyword-overlap
  scoring in `memory.build_context` and the linear scan in
  `ConversationManager.search`.
- **Modules:** `memory.py`, `conversations.py`.

---

## Phase 3 — Bigger bets

### 3.1 `delegate_task` subagent — **L** ✅
- **Fix:** builtin tool that runs a fresh `chat_turn` with clean context, a
  narrowed toolset, and its own round budget, returning only a summary to the
  parent. The highest-leverage context-protection pattern from both
  Claude Code and Hermes; sequential execution is fine on a single local GPU.
- **Local-Ollama note:** run subagents **serially**, not concurrently — a
  second concurrent model load on one Ollama server competes for VRAM and can
  evict the parent's model (losing its KV cache). Default the subagent to the
  parent's model; allow a configured smaller/faster model for simple subtasks
  (Ollama can hold two models if VRAM allows; otherwise accept the swap cost).
- **Extended by:** Phase 4.2 (skill-scoped subagents) adds per-subagent
  skills, prompts, and model preferences on top of this mechanism.
- **Modules:** `runtime.py`, `tooling.py`.

### 3.2 Repo-map-style orientation context — **L** ✅
- **Fix:** when cwd is a git repo, inject a relevance-ranked structural
  overview (file tree + top-level symbols) within a ~1k-token budget (aider
  repo-map pattern; a cheap tree + symbols version first, tree-sitter later).
- **Modules:** new module, `app.py`.

### 3.3 Backend health/preflight — **M** ✅
- **Fix:** ping `/api/tags` at startup and before turns following a failure;
  graceful "server offline — retry?" UX instead of error text in the
  transcript. Builds on Phase 0.3.
- **Modules:** `providers.py`, `app.py`.

---

## Phase 4 — Skills and remote operation

Later-phase capabilities that compose the earlier building blocks. Ordering
within the phase matters: 4.1 (skills) before 4.2 (skill-scoped subagents);
4.3 (remote loop) hard-depends on the Phase 2 permission model.

### 4.1 Skill system + in-chat skill builder — **M–L**
- **What:** skills as reusable definitions in `~/.config/conch/skills/` —
  one markdown file per skill with frontmatter (name, description, allowed
  tools, optional model preference) and a body of instructions/procedure.
  Skills are the richer sibling of Phase 1.7's slash-command templates: a
  slash command is a one-shot prompt template; a skill also scopes *tools*
  and *model*, and can be invoked by the model itself (a `use_skill` /
  `skill_manage` tool that injects the skill body into context —
  cheap, same-window, per the Claude Code SkillTool vs AgentTool distinction).
- **Skill builder:** an in-chat authoring flow — "turn what we just did into
  a skill" — where conch summarizes the successful procedure from the current
  conversation (steps, commands, pitfalls, verification) and writes the skill
  file via the same `skill_manage` tool, for user review before saving. This
  is the human-in-the-loop half of Hermes's self-evolving skills; the
  autonomous Curator/refinement loop is deliberately out of scope.
- **Dependencies:** none hard; benefits from 1.7 (shared loader conventions
  for `~/.config/conch/` definition files) and 2.7 (weak model can draft the
  skill summary cheaply).
- **Modules:** `tooling.py` (skill loader, `skill_manage` tool),
  `commands.py` (`/skills`, `/skill <name>`), `prompts.py` (injection),
  `config.py`.

### 4.2 Skill-scoped subagents — **M** (on top of 3.1)
- **What:** extends the Phase 3.1 `delegate_task` mechanism so a delegation
  names a skill: the subagent gets that skill's instructions as its scoped
  system prompt, only the skill's allowed tools, and the skill's model
  preference (e.g. a cheap/fast local model for mechanical subtasks, per the
  aider weak-model pattern). This is the Hermes `delegate_task`-with-toolset
  pattern and Claude Code's custom agents (`.claude/agents/*.md`) mapped onto
  conch's skill files — one definition format serves both in-context skill
  use (4.1) and isolated subagent runs.
- **Local-Ollama constraint:** inherits 3.1's serialization rule — subagent
  turns run serially on one Ollama server; when a skill requests a different
  model, accept the load/swap cost or keep a designated small model resident
  alongside the main one when VRAM allows. Subagent iteration/token budgets
  come from 2.6.
- **Dependencies:** 3.1 (delegation mechanism), 4.1 (skill definitions),
  2.6 (budgets); 0.4's capability gate applies to any skill model preference.
- **Modules:** `runtime.py`, `tooling.py`, `config.py`.

### 4.3 Remote agentic loop over Slack, SMS, email — **L**
- **What:** conch messages the user proactively over a channel (scheduled
  task results, long-running task completion, approval requests), and inbound
  replies on that channel resume/steer the session — a full remote loop:
  channel message → mapped to a conversation → `chat_turn` runs → reply sent
  back over the channel.
- **Building blocks already present:** `scheduler.py` runs background
  prompts through `_scheduled_executor` (`app.py`) but currently discards the
  output — the first increment is simply delivering that output over a
  channel. `composio.py` provides OAuth'd Gmail/Slack tool access for
  *outbound* sends today. Inbound requires either polling (a scheduler task
  that reads the Slack channel / Gmail inbox via Composio tools and feeds new
  messages into the loop — simplest, works behind NAT on a home network) or a
  webhook/Socket-Mode listener (lower latency, more moving parts). Start with
  polling. **SMS options:** Twilio is the default choice (webhook inbound;
  polling via the Messages API is possible but slow); alternatives: Vonage,
  AWS SNS (outbound-only), or email-to-SMS gateways as a zero-dependency
  fallback. Session mapping: channel thread / sender → conversation id in
  `ConversationManager`, so a Slack thread *is* a conch conversation.
- **Safety (prerequisite, not optional):** remote channel + shell execution
  is the dangerous combination. Interactive y/n approval doesn't exist
  remotely, and `LocalShellPolicy(interactive=False)` currently just refuses —
  the right target state. Hard requirements before any remote inbound
  processing: Phase 2.1 permission modes (remote sessions run at most
  safe-auto — allowlisted read-only commands only; destructive commands are
  never auto-approved remotely), Phase 2.2 hooks as a deterministic gate, and
  an explicit approval-over-channel flow (conch posts the proposed command,
  user replies "approve <id>") for anything beyond the allowlist. Also:
  authenticate inbound senders (allowlist of Slack user IDs / phone numbers /
  email addresses) since an SMS/email inbox is an unauthenticated prompt
  surface.
- **Dependencies:** 2.1 + 2.2 (hard, safety), scheduler executor rework
  (`app.py` `_scheduled_executor` must return/route output), 0.3 (a dead
  Ollama server must produce a clean channel message, not silence).
- **Modules:** new `channels.py` (gateway abstraction: poll/receive/send per
  channel), `scheduler.py`, `composio.py`, `app.py`, `conversations.py`
  (channel↔conversation mapping), `tooling.py` (approval flow).

---

## Sequencing rationale

- **Phase 0** items 0.1–0.3 fix the three defects that make local Ollama
  sessions appear randomly broken (silent truncation, dropped streamed tool
  calls, invisible errors); 0.4–0.7 implement the tools-only directive and
  delete ~200 lines of regex scaffolding.
- **Phase 1** reclaims token budget (the binding resource for small local
  models) and front-loads the three cheap extensibility wins (custom slash
  commands, project context, prompt overrides) that improve daily use with no
  dependencies on the rest of the plan.
- **Phase 2** hardens the agent (permissions, hooks, budgets) and opens the
  integration surface (executable tools, custom providers).
- **Phase 3** holds the large structural bets until plain multi-turn tool
  sessions against the local server are reliably boring.
- **Phase 4** composes earlier pieces into higher-order capabilities: skills
  build on the Phase 1 definition-file conventions, skill-scoped subagents
  require both the Phase 3.1 delegation mechanism and the Phase 4.1 skill
  format, and the remote loop is gated on the Phase 2 permission model —
  shipping remote inbound control before graded permissions exist would be
  an unattended-shell hazard.

Coordination note: 0.2 and 0.7 overlap with the qwen tool-calling and live
model-list fixes in progress on the `curses` branch — check that diff before
implementing.
