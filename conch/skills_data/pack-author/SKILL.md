---
name: pack-author
description: Author and verify Conch flow packs (conch.flow_pack.v1) — the declarative JSON manifests that express a Capitol use case (intakes, request templates, approval gates with caps, phase flow, wording) executed by the generic pack engine. Use when the user asks to create, edit, extend, review, or debug a flow pack or pack.json, mentions request templates, caps, approval kinds, intakes, acceptance drills, or wants a new Capitol use case "like the eBay pack".
tools: local_shell, capitol_control
---

# Authoring flow packs

A flow pack is a directory with a JSON manifest — never Python — that
the generic engine (`conch/capitol/packs/engine.py`) executes over
`CapitolRuntime`. You edit files; the user loads and verifies them. The
full grammar **as the engine actually implements it** is in
`reference.md` beside this file (read it before writing any manifest —
where the design doc and the engine diverge, the engine wins). Worked
recipes are in `cookbook.md`.

```
~/.config/conch/packs/<name>/     user packs (win by name)
  pack.json                       the manifest — dir name MUST equal pack.name
  README.md                       human docs (not machine-read)
  assets/                         optional fixtures/prompt assets
conch/capitol/packs/data/<name>/  packs shipped with conch (ebay-listing)
```

## The two invariants (verbatim, from the control design §3.4)

1. **"Packs are data, not code"** and **"Packs cannot grant tools or
   authority."** No executable content loads from a pack: request
   templates are a bounded, deterministic expression language — no
   eval, no callables. Mounting tools, mission authority, budgets, and
   credentials stay governed by mission specs, tool profiles, config,
   and the required-policy registry; a pack referencing a workflow or
   server does not authorize it. Required policy fails closed always.
2. **"Caps clamp outcomes; they never script reasoning."** Deterministic
   machinery only where money or irreversibility lives; every judgment
   stage is agentic, measured by evals. A manifest cannot express
   clarification checklists, content validators, or schema-linting of
   drafted copy — `flow.on_status` relays questions verbatim, cap
   checks read outcome fields only, and the deterministic surface is
   exactly requests/keys/challenges/ledgers/cursors. When a configured
   cap cannot read its outcome field, the clamp routes to exact
   approval — clamps clamp; they never guess.

Also non-negotiable: secrets never enter manifests or state (config
*key names* only, `${config.…}`); user-visible wording lives in
`messages`/`presentation` tables, not code; unknown fields fail closed.

## Pack anatomy (what each section owns)

- `pack` — name/version/description; `replaces` names retired modules.
- `capitol` — org/agent scoping + workflow bindings per alias: a config
  `pin`, a literal `id`, or a `discover.name_contains_any` rule, plus
  `inputs_key` (`"auto"` = discover from `describe_workflow`).
- `contracts` — the typed-output dialect: schema `prefix`, the session
  contract (status field, identity pin fields), effect/rejection/
  guidance schemas. Contracts are read from run outputs, never prose.
- `intakes` — how sessions start: `channel_message` (match rules,
  attachment caps, thread-session binding), `shell_command`,
  `watched_folder` (validated, engine gap G1 — not yet runnable).
- `bindings` — intake→workflow triggers: which request template, chat
  vs typed modes, gateway retry-key policy.
- `requests` — deterministic request templates: literal fields,
  `${…}` expressions, `from_contract` + `require` (verbatim echo,
  fail-closed), formula strings for keys/challenges.
- `flow` — the fixed phase machine (declared, not programmable),
  clarify relay rule, HITL parking, `revision_invalidates_approval`.
- `approvals` — approval classes: store `kind`, the request each
  consume constructs, pinned identity, `policy_event`, caps, exact
  phrase/challenge, TTL, re-issue policy.
- `state` — durable session/collection/thread shape (file-backed).
- `messages`/`presentation` — every user-visible string, per surface.
- `acceptance` — the pack's drill, run via `/capitol pack verify`.
- `scope` — org/agent config-key names.

## The authoring loop

1. **Skeleton from the shipped pack.** Copy
   `conch/capitol/packs/data/ebay-listing/pack.json` into
   `~/.config/conch/packs/<name>/pack.json`, rename (`pack.name` must
   equal the directory name), strip sections you don't need yet.
   Top-level minimum: `schema`, `pack`, `capitol`.
2. **Workflows pinned.** Fill `capitol.workflows` — prefer config pins
   (`"pin": "${config.<key>:}"`) with a `discover` fallback; confirm
   against the live agent (`capitol_control op='workflows'` /
   `op='describe'` for the inputs key and request schema).
3. **Request templates.** Write `requests.*` with the expression
   grammar from reference.md; `require` every contract field the
   template echoes; put idempotency keys and challenges in formulas so
   the bytes are deterministic.
4. **Fail-closed load.** `/capitol packs` (user runs it) or
   `python3 -c "from conch.capitol.packs import load_pack; load_pack('<name>')"`
   — a typo\'d field, unknown key, bad template, or unsupported cap op
   raises at load, naming the path. Fix until it loads clean; never
   ship a pack that only "mostly" validates.
5. **The verify drill.** Declare `acceptance` and have the user run
   `/capitol pack verify <name>` — manifest validation plus the pack's
   drill. The shipped kind is `golden_scenarios` (a module exporting
   `verify_all(log=…)`); the drill is the pack's safety net, so a pack
   without one is a draft, not a deliverable.

## Hard rules

- Never invent grammar: if reference.md doesn't list a section, key,
  op, or template form, the engine rejects it (or worse, you're
  designing against the doc instead of the code — cite
  `conch/capitol/packs/manifest.py` when in doubt).
- Deployment identity (org, agent, policy ids, actor ids) enters via
  `${config.…}` — never literal UUIDs in the manifest.
- Approval `policy_event` names are contracts — preserve them verbatim
  when porting an existing flow (e.g. `capitol.ebay.publish`).
- Pack edits are user-explicit: propose file contents, let the user
  place/verify them (`capitol_control` refuses pack mutations by
  design).
