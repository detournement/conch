# ebay-listing — flow pack

Photo → sandbox eBay listing over governed Capitol workflows: draft and
revise with verbatim clarification relay, the caps-clamped publish gate,
exact-approval publish (confirmation exactly `proceed to post`, challenge
exactly `POST r{rev} {hash[-12:]}`, idempotency key exactly
`{app}:{session}:r{rev}:publish`), and effect confirmation over the
originating thread. Capitol's `ebay_approval_node` re-verifies every
exact-approval field deterministically, so what the user approved is
byte-identical to what the effect validates — two independent staleness
checks in series.

This pack replaces the bespoke flow logic of `conch/capitol/ebay.py` and
`conch/capitol/channel_flow.py` (both are now thin shims over the
generic engine in `conch/capitol/packs/engine.py`). Equivalence with the
pre-extraction drivers is proven by the golden fixtures in
`tests/fixtures/capitol_golden/` (`tests/test_capitol_golden.py`), which
freeze the wire-call sequences, approval-store records, durable state,
and reply text; `/capitol pack verify ebay-listing` replays that suite.

## Configuration

Deployment identity stays in the conch config, referenced by
`${config.…}` placeholders — never in this manifest:
`capitol_base_url` / `capitol_org` / `capitol_agent` (adapter),
`ebay_actor_id`, `ebay_fulfillment_policy_id`, `ebay_payment_policy_id`,
`ebay_return_policy_id`, `ebay_merchant_location_key` (required),
`ebay_app_id`, `ebay_account_ref`, `ebay_draft_workflow`,
`ebay_publish_workflow` (pins), `ebay_intake` (chat|typed),
`ebay_channel_intake`, `ebay_auto_publish`, `ebay_channel_auto_publish`,
`ebay_allowed_category_ids`, `ebay_max_price_usd` (caps). Credentials
are resolved per call from `$CAPITOL_A2A_BEARER` or the A2Actrl
registry; secrets never enter manifests or state.

## Divergences from the design's example manifest

The control design (`conch-capitol-control-design.md` §3.7) sketched
this manifest before the engine existed; the shipped pack adjusts it to
what refactor stage R1 actually implements. Every divergence:

1. **`intakes[].session.id_scheme: "channel-sha16"`** replaces the
   illustrative `id_template` string. Session-id construction is an
   engine scheme (`{channel}-sha256(thread_key:ts)[:16]`), not free-form
   template code — packs are data, and an id template would be code.
2. **`fill_missing(config: field=config_key, …)`** uses explicit
   `field=config_key` pairs; the design's bare config-key list left the
   target field names implicit.
3. **`bindings.draft_initial.default_mode`** ("chat") makes the intake
   mode default explicit (previously hardcoded in the driver).
4. **`intakes[0].context_default`** carries the image-only default item
   context (previously duplicated across the driver's intake paths; the
   request templates keep the same default as belt-and-braces).
5. **`contracts.effect.link_fact` and `contracts.effect.success`** name
   the done-reply URL fact and the effect success predicate
   (`state == "PUBLISHED"`, case-insensitive) — previously hardcoded.
6. **`state.file: "ebay_pilot.json"` and `state.effect_field:
   "listing"`** pin the historical state file (no migration needed;
   kernel-native pack state is design gap G2, refactor stage R4) and the
   session key the effect facts land under.
7. **`approvals[0].policy_fields`** lists the request fields copied into
   the `capitol.ebay.publish` required-policy payload (the engine always
   adds `workflow_id`, `idempotency_key`, `caps_auto`, and
   `channel_approval` on channel consumes) — previously hardcoded.
8. **`messages` + `presentation` sections** carry all user-visible
   wording and the drafted-revision/effect rendering as data. The design
   names wording as pack-domain (§1.2: "notification wording") but its
   example omitted the fields; the engine renders every reply from these
   tables, byte-identical to the pre-extraction drivers (golden-fixture
   proven). `attachments.noun` and the cap checks' `label`/`unit` feed
   the same wording.
9. **`approvals[0].describe`** is the approval-store description formula
   (previously an f-string in the driver).
10. **The `watched_folder` intake is omitted** — engine gap G1 (the
    edge-daemon folder watcher does not exist yet); the design example
    carried it as a disabled placeholder.
11. **`presentation_id` uses `engine.unique_id('conch-presentation')` on
    both surfaces**; the pre-extraction channel flow used a
    `conch-channel-` prefix. Presentation ids are generated ids —
    volatile by the equivalence rules and ignored by Capitol's checks.
12. **`flow.phases` / `flow.recovery_order` are validated, not
    programmable**: they must equal the engine's fixed phase machine
    (`drafting/clarify/hitl/awaiting_approval/publishing/done/failed`,
    recovery `hitl → contract_status → failed`). The manifest declares
    them as documentation; anything else fails closed.
13. **`acceptance`** points at the golden-scenario suite instead of the
    design's fixture-assets sketch: for this pack the drill *is* the R0
    scenario suite against the fake gateway (run via
    `/capitol pack verify ebay-listing`; requires a source checkout with
    `tests/` present).

Engine-global behavior the manifest intentionally does not express (per
design §3.4): untrusted-text posture (control bytes stripped, bounded,
never a selector), the literal `continue`/`stop` HITL token protocol,
origin-bound approval consume mechanics, and the required-policy gate —
those are the engine's, not the pack's, and packs cannot widen them.
