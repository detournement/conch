# conch.flow_pack.v1 — the grammar as implemented

Authoritative sources, in precedence order:
`conch/capitol/packs/manifest.py` (sections, keys, validation),
`conch/capitol/packs/templates.py` (the two template mini-languages),
`conch/capitol/packs/engine.py` (execution semantics),
`conch/capitol/packs/registry.py` (discovery/loading). The control
design document sketched a richer schema **before the engine existed**;
this file documents what loads today — divergences are listed at the
end. Everything not listed here **fails closed** at load: unknown
top-level sections, unknown keys inside a section, unsupported intake
kinds/cap ops/id schemes, and malformed templates all raise `PackError`
naming the offending path.

## Loading and identity

- A pack is `<dir>/pack.json`; `pack.name` must equal the directory
  name (mismatch fails closed). `~/.config/conch/packs/<name>/` wins
  over the shipped `conch/capitol/packs/data/<name>/` by name.
- `pack_digest = "sha256:" + sha256(canonical JSON)` (sorted keys,
  compact separators) — the pack pin. Any edit changes it.
- Loading parses **every** reachable template expression and formula, so
  a template typo surfaces at load, never mid-flow.

## Top-level sections

Required: `schema` (exactly `"conch.flow_pack.v1"`), `pack`, `capitol`.
Optional: `contracts`, `intakes`, `bindings`, `requests`, `flow`,
`approvals`, `state`, `notifications`, `messages`, `presentation`,
`acceptance`, `scope`. Nothing else.

### `pack`  (required: name, version)
`name`, `version`, `description`, `replaces` (list of module paths this
pack retires).

### `capitol`  (required: workflows)
`org`, `agent` (informational `${config.…}` refs), and `workflows`: a
non-empty map of alias → spec. Each spec (keys: `pin`, `id`,
`discover`, `inputs_key`, `request_schema`) needs at least one of
`pin` / `id` / `discover`; `discover` has exactly
`{"name_contains_any": [substrings]}`. Resolution
(`engine.resolve_workflows`): config pins win; otherwise each listed
workflow claims at most one alias by case-insensitive name match, in
the pack's declared alias order. `inputs_key: "auto"` uses
`engine.workflow_inputs_key`: from `describe_workflow.fields[]` pick
the field with `field_id == "value"` (else the single field), key is
`"{node_instance_id}.{field_id}"`, default `"value"`.

### `contracts`
`prefix` (the schema-prefix the contract walker scans run outputs for),
plus four optional shapes (each requires `schema` when present):

- `session`: `name`, `schema`, `status_field` (default `"status"`),
  `identity` (the pin fields for approvals/staleness).
- `effect`: `schema`, `facts` (dotted paths surfaced on success),
  `link_fact`, `success` (`{"fact": …, "equals_ci": …}` predicate).
- `rejection`: `schema`. - `guidance`: `schema`.

### `intakes`  (a list; kind-specific keys, anything else fails closed)
- `channel_message`: `channel`, `enabled_key`, `match`
  (`attachments_mime`, `min_attachments`), `attachments` (`max_count`,
  `max_bytes`, `suffixes`, `noun`), `session`, `start` (a binding
  name), `context_default`.
  `session` keys: `scope`, `id_scheme`, `dedupe`. The only supported
  `id_scheme` is `"channel-sha16"` —
  `{channel}-sha256(thread_key + ":" + ts)[:16]`, an engine scheme
  (an id *template* would be code).
- `shell_command`: `command` (e.g. `"/ebay"`), `usage`, `start`.
- `watched_folder`: `path`, `enabled`, `note` — validated but **not
  runnable** (engine gap G1: no edge-daemon folder watcher yet).

### `bindings`  (map name → trigger; required: workflow)
Keys: `workflow` (must name a `capitol.workflows` alias), `kind`
(`typed_request` | `chat_intake`), `request` (a `requests` template
name), `mode_key` (config key choosing a mode), `default_mode`,
`modes` (map mode → `{kind, message, attachments, reconcile, upload,
request}`), `gateway_key` (`{"base": "request.idempotency_key",
"retry_suffix": ":retry{attempt}"}` — the *gateway* call key gets the
suffix on re-approval after a failed effect; the request's embedded
key never varies, so Capitol's effect ledger still replays duplicates).

### `requests`  (map name → template; required: schema, fields)
- `schema`: stamped into the built request.
- `fields`: the template body — literals, nested objects/lists,
  `${…}` expressions, `{…}` formulas (grammar below).
- `from_contract`: build against the named session contract.
- `require`: contract fields that must be present (fail-closed verbatim
  echo — the `build_publish_request` discipline).
- `idempotency_key`: a formula rendered over the built request (e.g.
  `"{app_id}:{listing_session_id}:r{revision}:publish"`).

### `flow`
Keys: `session_contract`, `phases`, `on_status`, `hitl`,
`recovery_order`, `revision_invalidates_approval` (default true).
`phases` and `recovery_order` are **validated constants, not
programmable**: they must equal the engine machine
`["drafting","clarify","hitl","awaiting_approval","publishing","done","failed"]`
and `["hitl","contract_status","failed"]` — anything else fails closed
(the v1 engine implements one machine; the manifest declares it as
documentation). `on_status` maps a contract status to a rule whose
`phase` is `clarify` (`questions_field`, `max_rounds`, `reply_action` —
must name a binding) or `gate` (`approval` — an approval kind). `hitl`
carries `park_on`, `clarification`/`intervention` relay specs (the
`continue`/`stop` token protocol is engine-global, not configurable).

### `approvals`  (a list; required: kind, constructs, pin, policy_event)
- `kind`: the ApprovalStore kind string (must be unique per surface —
  the remote loop dispatches consumes on it).
- `constructs`: the binding whose request an approve builds — consume
  never runs a command.
- `pin`: the identity paths frozen into the approval
  (`session.id`, `contract.revision`, …); staleness is re-checked at
  consume and Capitol re-verifies server-side.
- `policy_event`: the required-policy event name (preserve verbatim
  when porting, e.g. `capitol.ebay.publish`). `policy_fields`: request
  fields copied into the policy payload — the engine always adds
  `workflow_id`, `idempotency_key`, `caps_auto`, and
  `channel_approval` on channel consumes.
- `caps`: `toggle_key` (config bool, default true), `checks` (list of
  `{field, op, config_key, skip_when_unset, label, unit}`),
  `unreadable_outcome` (only meaningful value: `exact_approval`).
  **Supported ops: `in_csv`, `lte_float` — nothing else** (design's
  `gte_float`/`eq` are not implemented). Unset config key: skipped
  when `skip_when_unset`, else a within-caps failure reason. An
  unreadable outcome field routes to exact approval (clamps never
  guess).
- `auto_within_caps`: per-surface auto policy —
  `{"shell": {"confirm": true}, "channel": {"opt_in_key": …,
  "default": false}}`.
- `exact`: `{phrase, challenge}` — `phrase` required; `challenge` is a
  formula (e.g. `"POST r{revision} {draft_hash[-12:]}"`).
- `on_effect_failure`: `{"reissue": true, "reason": …}` re-arms a fresh
  approval after an upstream failure. `ttl_key`: config key for expiry.
- `describe`: formula for the approval-store description line.

### `state`
Keys: `store` (`pack_state` — versioned atomic JSON under the XDG state
dir), `file` (default `<name>.json`), `effect_field`, `session_fields`,
`collections` (map name → field list; `runs` rows carry
`last_sequence` cursors), `threads` (`{key, value, doubles_as}` — the
`{channel}:{thread_id}` → session map that doubles as intake dedupe).

### `messages` / `presentation`
`messages`: surfaces `channel` and `shell` only; every value is a
formula validated at load. All user-visible wording lives here.
`presentation`: `channel`/`shell` (line specs for the drafted
contract) and `effect` (per-surface line specs for the effect facts).

### `acceptance`  (required: kind)
Keys: `kind`, `module`, `fixtures`, `checks`, `note`. The only kind
`/capitol pack verify` can run is `golden_scenarios`: `module` names an
importable module exporting `verify_all(log=callable) -> [{"scenario",
"ok", "diff_hint"?}]`; any not-ok row fails the drill. An unimportable
module reports SKIPPED (drills ship with the source checkout's
`tests/`); an unknown kind fails closed. No `kind` = manifest
validation only.

### `scope`
`org_config_key`, `agent_config_key`.

### `notifications`
Accepted at top level; contents are informational (not deep-validated).

## Template language 1 — `${…}` expressions (full-string values)

Roots: `config` (values str()-ed and stripped), `session`, `intake`,
`contract` (`${contract}` alone = the whole object, by reference),
`engine` (only `engine.unique_id('prefix')` → `"{prefix}-{10 hex}"`).

```
${root.path}                              value reference
${root.path:default}                      default on falsy (defaults nest:
                                          ${a.b:${c.d:}})
${config.key!required}                    fail closed when unset; missing
                                          keys are collected so one error
                                          names all of them
${contract.field + 1}                     integer increment (int-only)
${contract.items[].{a, b} +order}         list projection: pick fields per
                                          item, skip items missing the
                                          first field, +order adds the
                                          original index; must END the
                                          expression
${expr !nonempty}                         fail closed on empty result
${expr | fill_missing(config: field=config_key, …) !complete}
                                          dict filter: back-fill each
                                          listed field from config when
                                          absent; result carries exactly
                                          the listed fields in order;
                                          !complete fails closed naming
                                          what is still missing
```

## Template language 2 — `{…}` formulas (plain strings)

Resolved against a flat/nested value dict (the built request, a
contract, message variables):

```
{path.to.field}      str(value)   (missing → "None")
{#path}              len() of a list/tuple/dict value (else 0)
{path[-12:]}         last 12 chars of str(value)
{path:80}            clean_text(value, 80) — control bytes stripped
{path:80=fallback}   bounded, with fallback when empty
{path=fallback}      fallback when missing/empty (no bound)
```

Formulas power idempotency keys, challenges, approval descriptions, and
every `messages` entry. Any brace not matching the token grammar fails
validation at load.

## Line specs (presentation sections)

A list whose items are: a formula string; `{"if": path, "line":
formula}` / `{"if": path, "append": formula}` (append glues to the
previous line); or `{"each": path, "line": formula}` (one line per
item, exposed as `item`). Anything else fails closed.

## Engine-global behavior packs cannot express or change

Untrusted-text posture (workflow/model/channel text is bounded,
control-stripped business data — never a tool or workflow selector);
the literal `continue`/`stop` intervention protocol; origin-bound
approval consume mechanics and TTLs; the required-policy gate (fails
closed always); credential resolution (env/registry by reference,
never stored).

## Divergences from the design document (design ≠ engine; engine wins)

1. Cap ops are `in_csv` and `lte_float` only (design §3.2 also named
   `gte_float`/`eq`).
2. `intakes[].session.id_scheme: "channel-sha16"` replaces the design's
   free-form `id_template` (an id template would be code).
3. `fill_missing` takes explicit `field=config_key` pairs (design
   showed a bare config-key list).
4. There are **no** `provision`, `mission`, `identity`, `dedupe`, or
   `caps_note` sections in v1 — the funding-style pack (design §3.8) is
   not yet expressible; provisioning stays on `CapitolAdmin` via
   `/capitol admin` (user-explicit) and mission supervision on the
   mission spec's `capitol` envelope.
5. Intake kinds are `channel_message` / `shell_command` /
   `watched_folder` only; the design's `capitol_schedule`,
   `kernel_timer`, `mission_trigger`, `backfill`, `synthetic` kinds are
   not in the v1 engine (backfills run via `capitol_control op='start'`
   with a window input instead).
6. `flow.phases`/`flow.recovery_order` are validated against the fixed
   engine machine, not programmable.
7. `messages` and `presentation` are real, validated sections (the
   design's example manifest omitted the wording tables).
8. `contracts.effect` gained `link_fact` and `success` (previously
   hardcoded driver behavior).
9. The runnable acceptance kind is `golden_scenarios` (design sketched
   fixture-asset checks).

See also `conch/capitol/packs/data/ebay-listing/README.md`, which
walks the same divergences for the shipped pack.
