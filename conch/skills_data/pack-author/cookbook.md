# pack-author cookbook

Three worked recipes: a minimal pack from nothing, a guided tour of the
shipped `ebay-listing` pack, and authoring an acceptance drill. Always
propose file contents and let the user place and verify them —
`capitol_control` refuses pack mutations by design.

## Recipe 1 — a minimal pack, end to end

Goal: a pack that binds one existing Capitol workflow behind a shell
intake with a typed request. Example: a "research-note" workflow the
user runs as `/note <topic>`.

1. **Confirm the workflow on the live agent** (ids are discovered,
   never invented):

```json
{"op": "workflows"}
{"op": "describe", "workflow_id": "<the-id>"}
```

Note the id (pin it to config, e.g. `note_workflow=<id>` in the conch
config) and the request-input key shape.

2. **Skeleton.** `mkdir -p ~/.config/conch/packs/research-note`, then
`pack.json` (directory name and `pack.name` must match):

```json
{
  "schema": "conch.flow_pack.v1",
  "pack": {
    "name": "research-note",
    "version": "0.1.0",
    "description": "One-shot research note: /note <topic> starts the pinned workflow with a typed request."
  },
  "capitol": {
    "org": "${config.capitol_org}",
    "agent": "${config.capitol_agent}",
    "workflows": {
      "note": {
        "pin": "${config.note_workflow:}",
        "discover": {"name_contains_any": ["research note"]},
        "inputs_key": "auto"
      }
    }
  },
  "intakes": [
    {"kind": "shell_command", "command": "/note",
     "usage": "/note <topic>", "start": "note_start"}
  ],
  "bindings": {
    "note_start": {"workflow": "note", "kind": "typed_request",
                    "request": "note"}
  },
  "requests": {
    "note": {
      "schema": "example.research_note_request.v1",
      "fields": {
        "topic": "${intake.text!nonempty}",
        "requested_by": "${config.note_actor:conch}",
        "session_id": "${session.id}"
      },
      "idempotency_key": "research-note:{session_id}:{topic:40}"
    }
  },
  "scope": {"org_config_key": "capitol_org",
             "agent_config_key": "capitol_agent"}
}
```

Every value that varies by deployment is a `${config.…}` reference;
the idempotency key is a formula over the built request, so identical
requests replay.

3. **Fail-closed load.** Have the user run `/capitol packs` — the pack
   lists with its digest, or names the exact invalid path. Equivalent
   check without the shell:

```bash
python3 -c "from conch.capitol.packs import load_pack; p = load_pack('research-note'); print(p.name, p.version, p.digest)"
```

Typical load failures: an unknown key (`manifest has unknown fields`),
a template typo (`template path must start with …`), a binding naming a
missing workflow alias or request.

4. **Verify.** With no `acceptance` section, `/capitol pack verify
   research-note` validates the manifest only and says so. Add a drill
   (recipe 3) before calling the pack done.

5. **Exercise it.** The user runs `/note quantum radar` — the engine
   resolves the workflow (pin, else discovery), builds the request from
   the template, starts it keyed, and supervises to terminal. Observe
   runs with `capitol_control op='runs'` / `op='watch'`.

## Recipe 2 — reading the ebay-listing pack (the reference anatomy)

Open `conch/capitol/packs/data/ebay-listing/pack.json` next to its
README (which lists every divergence from the design sketch). The tour,
section by section:

- **Two workflows, pinned-or-discovered** (`draft`, `publish`) with
  `inputs_key: "auto"` — config pins win, name-match fallback.
- **Contracts**: schema prefix `ebay.`; the session contract
  (`ebay.listing_revision.v1`) pins identity
  `[listing_session_id, revision, draft_hash]`; the effect contract
  declares its `success` predicate (`state == "PUBLISHED"`,
  case-insensitive) and `link_fact` as data.
- **Intakes**: a Slack `channel_message` intake (image attachments,
  caps, `channel-sha16` session ids, per-message-ts dedupe) and the
  `/ebay` `shell_command` — both start the same `draft_initial`
  binding.
- **Bindings**: `draft_initial` is dual-mode (`chat_intake` with
  context-id reconciliation vs `typed_request` with private-artifact
  upload), chosen by config `ebay_intake`; `publish` carries the
  gateway retry-key policy (`:retry{attempt}` suffix on re-approval —
  the embedded request key never varies).
- **Requests**: `draft` (literals + config policy ids, `!required`
  where the deployment must decide), `revise` (`from_contract` +
  `require` verbatim echo, `${contract.revision + 1}`, a projection
  with `!nonempty`, `fill_missing … !complete` for policies), `publish`
  (the exact-approval bytes: `confirmation`, the `challenge` formula,
  the idempotency-key formula).
- **Flow**: `needs_info` → clarify (questions relayed verbatim, 5
  rounds, reply re-enters `draft_revise`); `draft_review` → gate on the
  `ebay_publish` approval; a new revision voids a pending approval.
- **Approval**: caps (`in_csv` category, `lte_float` price;
  `unreadable_outcome: exact_approval`), per-surface auto policy, the
  exact phrase/challenge, re-issue on effect failure, the preserved
  policy event `capitol.ebay.publish`, `policy_fields`.
- **State / messages / presentation**: the durable session shape
  (`ebay_pilot.json`), and every user-visible string as validated
  formulas/line specs.
- **Acceptance**: `golden_scenarios` against
  `tests.capitol_golden_scenarios` — the R0 equivalence suite replayed
  by `/capitol pack verify ebay-listing`.

When you need a construct, find where this pack uses it first; if it
doesn't and reference.md doesn't list it, the engine does not have it.

## Recipe 3 — authoring the acceptance drill

The drill is the pack's safety net: synthetic input through the real
engine against the fake gateway, fail closed on any divergence.

1. Declare it in the manifest:

```json
"acceptance": {
  "kind": "golden_scenarios",
  "module": "tests.research_note_scenarios",
  "note": "replays the recorded shell-intake scenario against the fake gateway"
}
```

2. The module contract (see `tests/capitol_golden_scenarios.py` for the
   full-size example): importable, exporting

```python
def verify_all(log=print):
    # drive the pack's flows against a scripted fake gateway,
    # compare wire calls / state / replies to recorded fixtures
    return [
        {"scenario": "shell-intake", "ok": True},
        # not-ok rows carry a diff hint:
        # {"scenario": "…", "ok": False, "diff_hint": "wire[2].inputs"}
    ]
```

Any not-ok row fails `/capitol pack verify <name>`, printing each
`diff_hint`. A module that is not importable (installed package
without `tests/`) reports SKIPPED — the drill runs from a source
checkout.

3. What a good drill asserts, in order of value: the exact wire
   envelopes (`call_workflow` inputs and idempotency keys — byte
   equality modulo declared-volatile fields like generated ids),
   approval-store records (kind, pins, description), durable state
   after each step, the reply wording, and a **replay-dedupe pass**
   (feed the identical intake twice; assert zero new effects — the
   funding pack's acceptance gate made this the house rule).

4. Keep fixtures deterministic: fixed clocks, seeded ids (patch
   `engine.unique_id`), a marker that varies dedupe keys per drill run
   when the drill hits a live stack.
