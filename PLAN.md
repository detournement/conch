# Conch improvement plan

Phased plan from the July 2026 architectural review, focused on making conch a
first-class agent against a local Ollama server, then building up agent
capability and extensibility.

**Governing constraint (tools-only models):** conch supports *only*
tool-calling-capable models, on every provider. Non-tool models are out of
scope. Enforcement mechanism on Ollama: `POST /api/show` → `capabilities`
array must contain `"tools"`. Consequences baked into this plan:

- Textual/regex tool-call execution and recovery is **removed**, not improved.
  Legacy shape detection remains diagnostic-only and cannot call a tool.
- Ask-mode free-text command scraping in `llm.py` (`extract_command`,
  `_SHELL_PREFIXES`, fenced-block/backtick regexes) is **replaced** with a
  native `shell_command` call on every provider. No classifier model.

Effort scale: **S** = hours, **M** = 1–3 days, **L** = a week or more.

**Status (September 2026, `edge` hardening):** Phases 0–4 are implemented;
Phase 5 is not started. The edge audit tightened several earlier
implementations: unknown local capabilities now fail closed, textual calls
cannot execute, request-specific tool authorization and schema validation are
enforced, context compaction preserves complete protocol groups, local-only
sessions cannot fall through to cloud providers, and supported container
topologies are now non-root and data-preserving. Secure interactive execution
is also implemented: credential-aware commands receive a direct, uncaptured
local terminal handoff, and validated OpenSSH ControlMaster sessions provide
connect/exec/shell/status/disconnect semantics without storing credentials.

**Swarm Phase 0 (September 2026): landed.** Foundation work from the Conch
Swarm roadmap (separate document), implemented without changing interactive
CLI behavior:

- `conch/session.py`: `AgentSession` owns provider/model config, tool
  clients, permission/agent mode, cwd, and budgets per session, wrapping the
  existing `chat_turn`. The `_agent_mode`/`_permission_mode` module globals
  became a per-session `PermissionState` (module helpers keep operating on
  the process default). chat_loop, scheduled runs, remote turns, and
  delegated subagents all construct or receive sessions; tests prove two
  simultaneous sessions cannot leak policy/cwd/tool state.
- `conch/bootstrap.py`: reusable startup wiring (config/provider/model
  resolution, client construction, MCP/tool state, session factory,
  scheduler/remote startup) callable headlessly — importing it pulls in
  neither readline nor `conch.app`; `chat_loop` is now the interactive
  composition of it.
- Dormant entrypoints `conch-controller`, `conch-edge`, `conch-worker`, and
  `conch-hostctl` (pyproject scripts + argparse): each prints a "not yet
  enabled" notice and exits 69 until its phase lands. `conch` is untouched.
- `conch/swarm/protocol.py`: versioned task envelope / event / receipt /
  lease dataclasses with canonical JSON, canonical IDs, failure-class and
  action-class taxonomies, and data classifications; unknown fields and
  unknown/newer versions fail closed.
- `conch/policy.py`: required-policy registry that denies on exception,
  timeout, or invalid decision — wired ahead of the (still fail-open) user
  `pre_tool_use` hook in all three dispatch paths; empty registry preserves
  today's behavior.
- Gates in tests: session isolation, secret-canary sweep (prompts,
  transcripts, terminal output, tool results, state files), and protocol
  round-trip/version-rejection.

**eBay pilot / Capitol adapter (September 2026): landed.** Milestone 1 of the
Capitol contract: a stdlib-only A2A adapter (`conch/capitol/` — discovery,
handshake, idempotent workflow calls, SSE resume, HITL relay, artifacts) and
the `/ebay` photo→listing driver. Listing judgment (content, price, when to
clarify) belongs to the workflow's model; determinism lives only at the money
boundary (exact-approval challenge over the immutable revision hash, publish
idempotency key, thin caps clamp, required-policy consult) and in the tiny
XDG run-linkage state file. Live sandbox conformance reached the publish
effect: draft, clarify, and the exact-approval gate all passed; replay and
stale-approval zero-write proofs captured. Publishing itself is parked on an
expired eBay sandbox user token (no refresh token in the org credential
bundle) — an operator re-auth, not a code gap.

**Swarm Phase 1 — durable mission/controller kernel (September 2026):
landed.** Mission coordination moved from JSON files into one transactional
SQLite kernel (`conch/kernel/`, WAL, single-writer thread) under
`~/.local/state/conch/kernel/`; JSON/JSONL remain export/audit formats.
Every mutation commits an optimistic version check, an immutable
hash-chained event, the projection update, budget operations, and outbox
inserts in ONE transaction; projections rebuild exactly from the journal
(replay == live is a tested invariant) and unknown event versions fail
closed. Entities: missions, mission_events, plans, tasks, task_attempts,
leases, checkpoints, integer-unit budgets (reserve/commit/release; child
scopes strict subsets), an external-action ledger with idempotency keys and
query-before-retry unknown outcomes, origin-bound approvals (canonical-args
hash + expiry + one-use nonce, extending remote.py's patterns), timers
(logical key, generation fencing, claim leases, skip/coalesce/bounded
catch_up misfires), a transactional inbox/outbox (dedupe keys,
at-least-once delivery, exactly-once ledger effects), artifacts, and
resource bindings for later Capitol/fleet linkage.

Missions run as bounded checkpointed work sessions over `AgentSession` —
fresh rehydrated context every time (spec + latest plan/checkpoint + open
tasks + event tail + remaining budgets, size-capped regardless of journal
length), hard wall/token/round caps, and a `mission_control` tool for
plans/notes/tasks, next-wake scheduling, input requests, and completion.
STOP (global kill file + per-mission flag) is honored at session start and
re-checked before every tool round through the fail-closed required-policy
layer. The `conch-edge` daemon (gated on `edge_daemon=true`) owns the
kernel exclusively — OS flock plus controller-epoch fencing so a superseded
zombie can never write — fires timers/sessions/outbox deliveries, migrates
legacy tasks.json idempotently (original kept as `.bak`), stops gracefully
on SIGTERM, and serves shell attach (`/missions`, `/mission`, `/approvals`,
`/approve`, `/deny`, plus the unchanged `/schedule` UX) over a
0700/0600-protected local socket speaking a tiny versioned JSON protocol;
without the daemon the same commands hit the kernel directly. The
no-daemon invariant is a tested gate: with `edge_daemon` unset, the classic
in-process scheduler runs and `conch.kernel` is never imported. Gates in
tests (137 new): fake-clock weeks of timers and misfire policies,
kill/restart at every claim/fire/session boundary with no duplicate
effects, hard budget enforcement, replayable projections, a scripted
backup/restore drill, bounded rehydration, wrong-origin/expired/replayed
approval rejection, outbox exactly-once-effect semantics under redelivery,
kernel secret-canary sweeps, and real-process SIGTERM/kill -9 lifecycle
drills. Milestone A ran live on this machine — see the evidence note at
the end of this file.

**Milestone 1b — Slack-first channel intake (September 2026): landed.** The
primary UX from contract Rev 2: a Slack photo message is the intake, and its
thread carries the whole session. `SlackChannel` now captures `files[]`
(bot-bearer download, magic-byte validation, XDG quarantine, `files:read`
scope) and polls `conversations.replies` for threads the bot posted into;
`conch/capitol/channel_flow.py` runs the listing session as a durable state
machine over the thread (clarify / mid-run HITL park+resume / awaiting
approval — all restart-safe, deduped on the message ts); publishing rides the
existing origin-bound `ApprovalStore` via the new `ebay_publish` approval
kind whose consume constructs the exact `ebay.publish_request.v1` from the
pinned revision (conch staleness check + Capitol's approval node = two gates
in series). Channel auto-publish within caps is a separate explicit opt-in
(`ebay_channel_auto_publish`, default off). Verified with fixtures only (no
Slack workspace configured); the publish effect stays parked on the expired
sandbox token. Email threading (Message-ID correlation) and watched-folder
intake remain later items.

**Swarm Phase 2 — trusted SSH fleet and distributed subagents
(September 2026): landed.** The controller can deploy bounded, replaceable
workers to trusted SSH hosts and dispatch versioned task envelopes to them;
mission truth, credentials, budgets, and the external-action ledger stay on
the controller. New package `conch/fleet/`, all opt-in — the interactive
shell never imports it.

- **Signed artifacts (`conch/fleet/artifacts.py` + `tools/`):** reproducible
  single-file worker builds via stdlib `zipapp` (byte-identical across
  builds; bundles `conch` + `pygments`, entry = `conch-worker`), a
  canonical-JSON sha256 manifest, and OpenSSH `sshsig` signatures
  (`ssh-keygen -Y sign/verify`, namespace-bound, allowed-signers file — no
  new Python deps). Verification is mandatory and fail-closed: missing/invalid
  signature, unknown manifest version, or digest/size mismatch refuses
  activation. The Dockerfile gained a non-root `worker` target for the
  optional strict-isolation profile with identical delivery semantics.
- **`conch-hostctl` (`conch/fleet/hostctl.py`, stdlib-only single file):**
  streamed onto bare hosts and installed by verified sha256; provides
  `probe` (arch/OS, python3, systemd version + sandboxing, docker, disk,
  cgroups, GPU via nvidia-smi, reachable local model endpoints), a
  content-addressed artifact store (atomic, idempotent), fail-closed signed
  `deploy`, `activate`/`rollback` with previous-revision retention and
  idempotent operation receipts under a remote deployment lock (repeat op =
  recorded receipt; re-deploy of staged content = no-op), worker supervision
  under a hardened user-level systemd unit (Linux default), a plain
  supervised process (macOS dev), or docker, and the bounded `rpc` relay to
  the worker supervisor socket.
- **FleetRegistry (`conch/fleet/registry.py` + kernel tables):**
  event-sourced worker + dispatch truth (replayed, hash-chained,
  replay==live), separating admin-assigned trust/data labels from observed
  probe capabilities and runtime profiles; identity, incarnation,
  artifact/config digests, protocol range, heartbeat sequence, and the
  PENDING/ACTIVE/DRAINING/OFFLINE/UNREACHABLE/UPDATING/QUARANTINED/REVOKED
  state machine. Enrollment (`conch/fleet/enroll.py`) is built on
  `ssh_control.py`: strict host-key first-enroll, then a fresh BatchMode
  connection check that marks a host autonomy-capable only when restart-safe
  key auth works.
- **Worker supervisor (`conch/fleet/worker.py`) + task executor
  (`conch/fleet/taskexec.py`):** durable offer/start receipt handshake
  (persist `(task, attempt, fence, epoch)` before ack; duplicates return the
  recorded receipt), fencing (stale fences rejected, newer fence supersedes
  with a process-group kill), bounded queue with retry-after rejection,
  per-attempt event spool with an ack watermark, cancellation as a state
  transition + process-group kill, and wall-clock enforcement. Tasks run the
  real `chat_turn` via an `AgentSession` restricted to the envelope's exact
  tool intersection (request-scoped authorization preserved); events spool
  locally until controller ack.
- **Task plane (`conch/fleet/plane.py`):** filter→score scheduling
  (protocol/state/trust/data ceiling/model residency/capacity + shared
  resource-group caps so one Ollama box is never oversubscribed), offer/start
  with controller epoch + per-dispatch fencing (a receipt commits only when
  `(attempt, fence)` matches; the kernel epoch guard already fences a
  superseded controller out of all writes), retries by protocol failure class
  with backoff/jitter and an attempt cap (`unknown_external_outcome` →
  NEEDS_RECONCILE, never blind-retried), heartbeats with monotonic deadlines
  (silent worker → UNREACHABLE + requeue), content-addressed artifact
  transfer with digest verification both directions, and effectively-once
  external side effects recorded through the kernel ledger keyed by the
  envelope idempotency key.
- **Brokered delegation:** a worker's `delegate_task` raises to park the task
  (a `delegation_requested` event); the controller validates depth, fan-out,
  authority-subset (tools/action-classes/data class), and trust placement
  into a child dispatch; the parent parks WAITING_CHILD (releasing its worker
  slot) and drives its own resume from the child's terminal outcome, folding
  the child result back as a complete tool-result group. Interactive local
  `delegate_task` is unchanged.
- **Gates (all in tests; +125 new, 1,308 total on Python 3.9 and 3.14, ruff
  clean at every commit):** the worker + hostctl run as real local
  subprocesses over a byte-identical fake SSH transport (a local socket in
  place of the SSH hop). Covered: signed/unsigned/tampered artifacts fail
  closed (bytes flipped, re-verified); duplicate offers/starts never
  duplicate receipts; obsolete fences rejected end to end; repeated deploy =
  no-op receipts; process-profile rollback drill with a live supervised
  worker (and a process-group-kill escape check); docker-profile rollback
  drill verified live on Docker Desktop (CI-skipped behind
  `CONCH_FLEET_DOCKER_DRILL=1`); systemd units validated textually
  (`systemd-analyze` unavailable on macOS — see follow-ups); resource-group
  oversubscription guard; effectively-once side effects through the kernel
  ledger under redelivery; fault injection killing the worker mid-run and
  after a result is written (exactly one terminal outcome per attempt); and a
  fleet-path secret-canary sweep (keys/bearers never in envelopes, events,
  receipts, argv, or logs).

**Real Linux/SSH host verification: first field run completed 2026-09-14**
(Ubuntu 26.04 LTS, systemd 259, cgroups v2, 2× NVIDIA GPU). The full
lifecycle ran end to end over real OpenSSH — probe → trust-install →
artifact-put ×3 → fail-closed signed deploy → activate → worker-start →
live worker.status RPC over SSH stdio — and the security posture held
(signature verification, digest/size pinning, informative fail-closed
protocol errors). The worker runs live under the **process** profile. The
run produced six findings, all fixed same-day: the systemd unit carried
system-scope-only capability directives (user scope always EPERMs —
directives removed), the documented build command redirected the human
summary into allowed_signers (summary → stderr; trust-install now
validates the anchor), worker-start reported ok for a unit dying into a
restart loop (now confirms active (running) with status+journal on
failure), Linger=no silently killed workers at logout (probe reports it;
worker-start refuses without --force; enrollment docs gained the
enable-linger step), the rpc CLI didn't match its docs (positional worker
+ --op convenience), and the model-catalog docs now note the
per-account-entitlement limit of audit annotations. Still needing the
live host:

- systemd-profile start verification: the capability-directive fix landed
  after the field run — the re-run should see the unit reach
  active (running) and exercise stop/restart plus the systemd rollback
  swap. (The field host's systemd re-run is pending on the operator's
  side; the worker currently runs under the process profile.)
- A rollback exercise on the live host: exercised live 2026-09-15 — burt-1
  was brought to the current build (new signed revision deployed, activated,
  worker restarted over key-based BatchMode SSH), then with two revisions
  present `rollback` swapped to the prior revision and roll-forward
  re-activated the new one, receipts idempotent (duplicate replays) both
  ways and the worker restarting on each swap under the process profile;
  remaining host-side item is the systemd-profile re-run (enable-linger is
  now done — the host reports Linger=yes). The redeploy also surfaced a
  keyless-worker gap: a process-profile worker started over a fresh
  BatchMode SSH session missed ANTHROPIC_API_KEY, which lives in
  `systemctl --user show-environment` on that host, so its first dispatch
  failed ("worker produced no reply") until the environment was imported
  by hand — fixed: process-profile worker-start now layers the
  user-manager environment under its explicit variables (explicit config
  wins, the manager env fills gaps, imported names only in the receipt),
  matching what a systemd unit inherits natively.
- GPU residency scheduling against an actual `nvidia-smi` host and a
  shared Ollama endpoint under real concurrent load (unchanged).
- A controller-driven live dispatch (`/fleet run <worker> "..."` through
  a running `conch-controller`) against the field host — the harness
  covers the full path over the fake transport; the live smoke needs
  key-based BatchMode SSH from the controller machine.

**The fleet awakening (September 2026): landed — conch-controller is
live.** The Phase 2 substrate now has a driver: `conch-controller` is a
supervised daemon (gated on `fleet_controller=true`, with
`install|uninstall|status` on the same launchd/systemd machinery as
conch-edge) owning its own fleet kernel scope at
`~/.local/state/conch/fleet/` — a separate database, `daemon.lock`, and
fencing epoch from the edge daemon's mission kernel, so both coexist on
one machine. Each tick drives the TaskPlane end to end: scheduling queued
dispatches onto eligible workers over WorkerTransport, event/receipt
polling with fence checks, failure-class retries with backoff,
cancellation propagation, heartbeat sweeps on cadence, worker skill
inventory recording, and artifact pull (files a task leaves in its
workspace `out/` publish as content-addressed artifacts, auto-pulled to
the controller's local store). On top of it:

- **`/fleet` command family** — `workers` (registry + live probe),
  `run <worker|auto> "<prompt>"` with `--skill/--tools/--actions/--model/
  --budget/--data/--wall`, `task`/`tasks`/`cancel`, `artifacts <id> pull`,
  `drain|enable`, `grant`, `enroll`, `status` — over the controller socket
  when it runs, direct-driving the fleet kernel one-shot when it doesn't
  (the mission-command two-transport pattern).
- **Owner grants + the authority clamp** (`conch/fleet/authority.py`):
  worker ceilings default READ-only/narrow; `/fleet grant` raises them via
  ledgered `worker_updated` events (replay == live); every envelope clamps
  to `min(requested, worker ceiling, caller authority)` with explicit
  excess refused by name; hard exclusions (`conch_config`,
  `manage_tools`, `skill_manage`, `interactive_terminal`, `ssh_remote`,
  `fleet_delegate`) never lift; the scheduler's eligibility filter is
  ceiling-aware.
- **Skill-addressed dispatch**: envelopes carry skill names; workers
  report installed skills through `worker.status` (capability, never
  authority); the skill's tool scope becomes the envelope's requested
  tools and the worker loads its own copy of the skill's prompt,
  re-intersecting tools at execution; dispatch to a worker missing the
  skill is refused with a clear error (v1 does not ship skill files).
- **`fleet_delegate`** — the model-callable brokered delegation for local
  sessions only (interactive + missions; excluded from remote/channel
  sessions, workers, and implicit local-subagent inheritance), returning
  worker summaries + artifact references as tool output.
- **Mission fleet block**: mission specs accept
  `fleet: {workers, skills, tools, actions, data, token_budget}`; mission
  sessions get an envelope-scoped fleet_delegate clamped to it.
- **Tests**: controller-socket E2E over real worker subprocesses (fake
  SSH transport), artifact round-trip, drain semantics, kill/restart
  mid-dispatch with epoch fencing (no double execution), skill-addressed
  dispatch + refusals, the full clamp/grant matrix, grant-ledger
  replay == live, and a fleet_delegate round-trip inside a real
  chat_turn.

**Swarm Phase 3 — full Capitol integration (September 2026): landed.**
Conch drives Capitol as a governed, subordinate process-execution fabric —
discovering, invoking, supervising, and (under a separately authorized,
default-off profile) provisioning Capitol assets — while mission truth,
policy, budgets, approvals, and the external-action ledger stay on the
controller. All in `conch/capitol/`, dependency-light (stdlib HTTP + a
hand-rolled SSE reader; no CLI subprocess on the production control path);
the eBay pilot paths (`channel_flow.py`, the `/ebay` driver) ride the same
adapter unchanged. The `A2Actrl` reference client remains normative — the
adapter is verified differentially against the `a2actrl` CLI, and any
accepted divergences are documented.

- **`CapitolRuntime` (`conch/capitol/client.py`):** the full adapter surface
  — AgentCard discovery with fail-closed capability gating (`ensure_skill`
  refuses an unknown/absent skill or an unsupported wire schema *before* any
  call), org agent directory, workflow list/describe/suggest/versions/stats,
  `call_workflow` with caller idempotency keys, HITL replies, artifact
  upload (presigned PUT) / download, eval roll-ups, and `watch_run` — SSE
  streaming that resumes with `since_sequence=last+1` across drops and
  degrades to a resumable `get_workflow_events` poll when streaming is not
  advertised. Bearer bytes are scrubbed from every error; no automatic
  re-auth (a 401 parks as "credential needed").
- **Resource-binding lifecycle + mission envelope (`conch/kernel/`):**
  `resource_bindings` gained `task_id/status/cursor/detail/updated_at` (an
  additive migration; replay reproduces migrated tables byte-for-byte),
  `binding_updated` joined the event taxonomy with monotonic-cursor and
  terminal-immutability enforcement, and mission specs accept a validated
  `capitol` authority envelope (workflow allowlist, `allow_start`/
  `allow_respond`, `max_runs`) that fails closed on unknown fields.
- **Daemon supervision + bounded mission tool (`conch/capitol/supervisor.py`,
  `conch/kernel/daemon.py`):** `CapitolSupervisor` advances persisted event
  cursors for bound runs, wakes parked missions on terminal/failure, maps
  HITL checkpoints into origin-bound expiring kernel approvals whose decision
  flows back as the exact HITL reply exactly once (shared ledger key), maps
  mission abort onto `stop_workflow`/`CancelTask`, and degrades with
  exponential backoff when Capitol is unreachable — cursors survive outages
  and supervision resumes cleanly. The mission-facing `capitol_control` tool
  (`start_capitol_run`/`check_run`/`respond_hitl`) derives every grant from
  the spec envelope plus required policy, never the prompt; starts, replies,
  and cancels are ledgered external actions whose idempotency keys ride the
  wire.
- **`CapitolAdmin` (`conch/capitol/admin.py`) — the bounded builder
  profile:** config-gated (default off; `capitol_admin=true`) *and*
  required-policy-checked (`capitol.admin.{op}`, fail-closed) provisioning
  over the platform/workflow management APIs: create orchestrator agents,
  publish/pin workflow versions, manage the agent's workflow allowlist, bind
  collections, and create/update/delete schedules. Every mutation shares one
  discipline — a caller idempotency key ledgered as a kernel external action
  *before* the wire call (committed duplicates replay the recorded outcome
  without a second effect), a version pin and rollback reference in the
  ledger detail, and transport-uncertain outcomes resolving `unknown` to
  reconcile by query (create ops adopt an existing same-name asset) rather
  than blind-retry. Minted/rotated bearers stream straight into the A2Actrl
  registry (`~/.capitol-a2a/agents.yaml`, 0600) under an OS-side reference;
  callers, the ledger, and logs only ever see a fingerprint.
- **Gates (all in tests; 1,359 in the routine suite on Python 3.9 and 3.14,
  ruff clean, plus 12 opt-in live tests):**
  - *Recorded fake-gateway contract tests* (`test_capitol_client.py`,
    `test_capitol_admin.py`, `test_capitol_bindings.py`): the fake mirrors
    the live 1.0.27 skill catalog and wire envelopes — invoke, HITL,
    artifacts, eval reads, duplicate-request idempotency, version-pin undo,
    and unknown-capability rejection with clear errors, plus every admin
    gate (default-off, policy veto, mandatory idempotency+ledger, bearer
    hygiene, the full create→verify→revert→clean-up drill).
  - *Mission gates* (`test_capitol_mission.py`): the bounded tool's
    spec-not-prompt authority, supervise-to-terminal, HITL↔approval with
    exactly-once reply, abort→cancel, replay==live, and one integrated
    end-to-end scenario (a mission binds a run → the daemon supervises it →
    a HITL checkpoint round-trips through a kernel approval → the run
    completes → binding-event replay equals live state).
  - *Unreachable-platform behavior*: pointing the runtime at a closed port
    degrades the binding with exponential backoff and parks the mission
    (never fails it, never uses an alternate provider); the cursor survives
    and supervision resumes cleanly when the platform returns.
  - *Live gates against the local dev stack* (`test_capitol_live.py`,
    opt-in `CONCH_CAPITOL_LIVE=1`, fail-closed skip otherwise): the
    disposable-asset drill through `CapitolAdmin` (create an orchestrator
    agent, publish + pin a `conch-phase3-*` workflow, manage its allowlist,
    create/update/delete a schedule, revert the publish to the prior
    version, clean everything up — each step ledgered and replay-verified),
    and invoke/supervise-to-terminal, cursor resume without replay, eval
    read, artifact round-trip, and a HITL round-trip through
    `CapitolRuntime`.
  - *Cross-client differential tests* (`test_capitol_crossclient.py`,
    opt-in): card / handshake / workflow list / describe / run start /
    status / events-since / cancel run through both the `a2actrl` CLI and
    the adapter against the local stack and compared on the semantic facts
    (volatile fields ignored). Accepted divergences are documented in the
    module's `DIVERGENCES` note.

**Capitol platform requests** (poll/resubscribe until these land; filed as
Capitol-side epics):

- **Signed push / webhook run notifications** so supervision does not have
  to poll — today the supervisor advances cursors on a timer and the adapter
  reconciles dropped streams by re-reading; a push callback (with an HMAC or
  signature Conch can verify) would make terminal/HITL wake-ups immediate.
- **A single idempotent unified management API.** Provisioning currently
  spans multiple services with inconsistent idempotency semantics. One
  management contract with first-class idempotency keys and a single
  authoritative version history would remove the reconcile-by-query and
  re-persist-to-undo workarounds documented in `admin.py`.
- **Process-graph diff / migration.** Publishing today re-persists a full
  payload and mints a new version with no server-side diff or safe
  migration between versions; a diff/migration API would let the builder
  profile reason about and gate structural changes.
- **First-class Gmail push ingestion** (Phase 4 dependency) so per-user mail
  intake is event-driven rather than polled.

**Verified live against a development Capitol stack:** the full
disposable-asset drill, invoke → supervise-to-`success`, SSE streaming and
cursor resume, eval roll-up reads (a passing suite), artifact
upload/download round-trips, a HITL pause + intervention reply, and the
a2actrl-vs-adapter comparisons — all creating only `conch-phase3-*` assets
and cleaning up. Accepted divergences between the adapter and the
reference client (including the publish-revert path) are documented in
`admin.py` and `test_capitol_crossclient.py`.

**Release 0.6.0 (September 12, 2026):** first tagged release from `edge` —
secure execution (terminal handoff, SSH control), `AgentSession`, the swarm
protocol and required-policy substrate, the mission kernel and `conch-edge`
daemon, the trusted SSH fleet, and the generic Capitol integration; notes
in `CHANGELOG.md`.

**Always-on daemon (September 2026): landed.** The roadmap's
always-on work item (Sequencing and critical path), building directly on
Milestone A. Three pieces. (1) *Daemon-hosted channel intake*: the edge
daemon hosts the remote channel loop (`conch/kernel/intake.py`), so
inbound Slack/SMS/email get full agent turns 24/7 with no interactive
shell; every remote-safety invariant is `RemoteLoop`'s own, unchanged —
fail-closed sender allowlists, the safe_auto cap, origin-bound expiring
approvals, `REMOTE_EXCLUDED_TOOLS`, bounded replies, thread ==
conversation — and each inbound turn builds a fresh fully-wired host
session from the daemon's config exactly like its mission sessions.
Single-consumer coordination is a kernel `channel_intake` lease every
would-be host must hold per polling pass (cursors advance only under the
lease → no drops, no double answers; zombies are epoch-fenced out
immediately) with `remote_host = daemon|shell` choosing the host —
daemon by default whenever `edge_daemon` is on, released on graceful stop
for instant handoff. (2) *Event-driven wakes*:
`MissionEngine.deliver_event` routes external events (channel messages,
webhooks, watches; `event.post` on the control socket) through the kernel
inbox idempotently and wakes timer-/input-parked missions the same call —
never paused missions or approval gates — and the daemon tick runs event
sources (timers, Capitol supervision, channel intake) ahead of the
session slot, so a `waiting_input` mission answered via `input msn-<id>
<text>` (or a Capitol HITL/terminal event on a bound run) fires its
session in the same tick, no timer wait. (3) *Managed uptime as default*:
`conch-edge install|uninstall|status` renders the launchd/systemd
templates with resolved paths and whitelist-only env (secrets stay by
reference; deploy/ files are the same templates rendered with
documentation defaults, kept byte-identical by test), loads, and verifies
health. Gates in tests (55 new): inbound answered with no shell attached
over a file-backed fake channel with real allowlist/cursor semantics,
same-tick event-to-wake latency asserted via kernel events, exactly one
reply per message with the lease contended in both directions plus
handoff both ways, zombie fencing, installer sequences/rendering, and the
existing kill -9/epoch/reconcile suites unchanged. Verified live on this
machine: the daemon now runs under launchd (`conch-edge install`
round-trip), and a fake-channel drill answered inbound messages and woke
a parked mission with no shell attached.

**Mission judgment and shared memory (September 2026): landed.** The
roadmap's second S–M work item (Sequencing), closing the judgment gap on
the Phase 1 kernel. Two pieces.

(1) *Critic/self-review sessions* (`conch/kernel/review.py`): every
standard mission owes a cheap review session every N work sessions or
daily — whichever first; spec `review` object overrides
`mission_review_*` config keys — on the weak model when configured,
falling back to the main model. Stall detection is deterministic and
computed from kernel events BEFORE the model sees anything: a mission is
stalled when ≥N sessions checkpointed with zero material events
(plan_recorded, task_created/transitioned, action_recorded/resolved,
artifact_recorded, approval_requested, binding_recorded/updated — notes,
checkpoints, and budget bookkeeping are deliberately non-material), or
when the same step failed repeatedly (≥2 failed attempts of one task, or
consecutive checkpoints carrying the same error). The critic scores each
success criterion (met/on-track/stalled/at-risk + one-line evidence) and
its verdict lands in one kernel transaction: `review_recorded` (a new
replayed `reviews` projection `/mission show` renders), re-plans as the
next numbered plan version through the existing plans machinery
journaled with an explicit `plan_revised` event + rationale, escalations
through the existing outbox notify path. Deterministic policy overrides
the model: a stalled mission may never "continue" (coerced to
escalation, never silent), unusable model output on a stalled mission
escalates, otherwise it is a journaled `review_skipped`. Reviews reuse
the session lease/reservation discipline (crash → lease expiry → normal
reconcile), never expose tools, never increment `runs` or write
checkpoints, honor a `reviews` budget line when declared (exhaustion is
a journaled skip that advances the cadence marker), and sit behind the
same STOP/pause gates as work sessions.

(2) *Cross-mission memory consolidation*
(`conch/kernel/consolidate.py`): after each work-session checkpoint (and
on completion), a weak-model pass distills durable lessons from that
session's journal delta into the shared memory store (`conch/memory.py`)
tagged `mission:<id>` + topic — deduplicated (normalized match + token
overlap), size-capped per lesson/pass/store (oldest mission lessons
evicted first; user memories untouched), and hard-scrubbed on OUTPUT:
lessons carrying credential-like tokens, secret keywords, org UUIDs,
channel/user identifiers, emails, or phone-like numbers are rejected
whole, extending the secret-canary discipline to the shared tier. Any
mission's rehydration then retrieves the top-K relevant lessons
(in-memory FTS5 over goal/plan/task keywords via
`MemoryStore.rank_entries`, own lessons excluded) into a clearly labeled
"Lessons from prior missions" block under a hard char cap, inside the
unchanged MAX_CONTEXT_CHARS bound. Consolidation is skippable
(`mission_consolidation=false`) and runs post-checkpoint on a worker
thread with a timeout — failure or timeout is a logged skip; the session
path never waits.

Gates in tests (38 new; 1,542 total on Python 3.9 and 3.14, ruff clean at
each commit): the stall fixture (N quiet sessions + a repeatedly failing
task) triggers a review whose re-plan is a journaled numbered plan
revision with replay == live; consolidation from mission A measurably
changes mission B's rehydrated context (exact lesson retrieval, size
bound held); an exhausted review budget skips with a journaled event and
never crashes; a credential planted in a journal never reaches
consolidated memories or another mission's context even when the model
echoes it; the no-daemon invariant is untouched. Verified live on this
machine against the launchd daemon's repo-digest mission (its model host
unreachable, so the daemon ran on the documented env-override provider):
a real review session scored both criteria `met` with journal evidence
(action `continue`, `review_recorded` seq 205, model
deepseek/deepseek-v4-flash) and `/mission show` renders the verdict; the
first live consolidation pass demonstrated the timeout-skip path (20s
cap, logged skip, session unaffected), a follow-up pass with a wider cap
landed three real tagged lessons, and a second mission's very next
rehydration retrieved them in its labeled lessons block with mission-id
provenance.

**Personal items P1 (September 2026): landed.** Kernel `items` aggregate
(event-sourced spaces: todo/recipes/papers/user-created, replay == live,
credential write-guard, computed urgency) + the `personal_items` builtin
(interactive and channel sessions; explicit-offer-only for sub-turns and
fleet) + `/todo` and `/list` commands with `/todo work` context loading
and `/todo escalate` mission linking + the memory fence — P2 (channel
capture provenance, morning digest) and P3 (space enrichment) remain.

**Notes N1 (September 2026): landed.** The `/notes` family (`/note`
aliases) on the items store — space `notes`, no new store. `new`/`open`
hand the terminal to the user's editor (`editor` config → $VISUAL →
$EDITOR → nano floor → vi; one shared resolver in config.py that the
multiline branch's /edit converges on at merge) through the
DirectTerminalRunner under the /terminal authority gate: interactive
local sessions with a real TTY only, remote/channel sessions refused
toward quick-add and personal_items. First-line `# Title` parsing with
argument and dated-Untitled fallbacks; abandoned empty buffers store
nothing; every save is an item_updated event and `show` surfaces the
edit history ("edited N times, last …"); quick-add rides the /todo add
grammar (tags supported); search covers titles+bodies; archive/reopen
complete the lifecycle. Credential write-guard (whole-note rejection,
editor text preserved on block) and stored-text inertness proven through
the command surface; replay == live. N2 (context pinning with caps,
`note:` references for missions/packs, templates) and N3 (Apple Notes
import, note→todo links, digest inclusion) remain.

**Natural-language Capitol layer (September 2026): landed.** The
`capitol_control` session builtin — the model-callable RUNTIME surface
(discover/workflows/describe/suggest/versions/stats/runs, keyed start
with digest-derived default keys, bounded watch-and-summarize, both HITL
kinds, outputs/evals, quarantine-bounded artifacts; admin and pack
mutations refused naming the user-explicit /capitol command; policy
events `capitol.run.start`/`capitol.hitl.respond`; personal_items
availability precedent, remote start = origin-bound approval whose
consume replays the pinned payload, mission sessions keep the
envelope-scoped tool) + the shipped-skills convention
(`conch/skills_data/`, user skills win by name) carrying the **capitol**
skill (core arc + hard rules, op reference, this-machine cookbook) and
the **pack-author** skill (invariants verbatim, the implemented
`conch.flow_pack.v1` grammar pinned to manifest constants by tests,
authoring loop + drill recipes). Live-drilled against the local stack
(the ingest smoke exposed and fixed the inputs-key heuristic for
multi-field workflows: text-input request nodes now resolve, ambiguity
refuses with the canonical keys named). 72 new tests (1,714 total on
Python 3.9 and 3.14, ruff clean).

**ProcessCompiler C1+C2 (September 2026): landed.** A stated goal becomes
governed, operating infrastructure through a reviewed compilation step
(`/compile`, process-compiler plan; C3 — mission-proposal door,
recompile-diff UX, auto-classes — deliberately not started). C1: the
versioned Architecture Card (`conch.architecture_card.v1`,
`conch/capitol/compiler/card.py`) with fail-closed validation,
code-enforced reuse-first, synthetic-only drill fixtures, secretguard,
deterministic uuid5 asset identities, and a generated
`conch.flow_pack.v1` manifest that must pass the pack loader; the bounded
compilation session (capitol + pack-author skills loaded,
`capitol_control` read/discovery only, the `compiler_workspace` emit tool
returning validation failures to the model, mission-session budgets); the
`compilations` kernel aggregate (events chain under the compilation's own
id: cards, versions, the origin-bound local-only approval pinning the
card digest, materialization receipts, drill results; replay == live);
and the `/compile` family (compile/list/show[--diff]/approve/reject/
revise/status; interactive-only — every non-local origin is refused,
which also forecloses self-approval). C2: materialization drives
`CapitolAdmin` in the card's declared order with per-step
`compile:{id}:{step}` idempotency keys (re-materializing replays;
partial failure records receipts and offers `/compile rollback`, which
reverts in reverse via the recorded rollback refs and never deletes
adopted assets), workflow payloads generated deterministically from the
approved stages against the live node catalog
(`conch/capitol/compiler/graph.py`, the together-funding uuid5 pattern
generalized), the generated pack + drill fixtures installed into the
packs dir, and the validation gate (fail-closed pack load → the new
`workflow_drill` acceptance kind, shared with `/capitol pack verify` →
dry-run supervising mission) advancing
compiled→materialized→verified→operating. Safety invariants tested:
materialization refuses without `capitol_admin`, non-local base URLs
refused (v1), approval origin-bound and local-only, fixtures never
reference real accounts, generated packs load fail-closed, cards are
credential-guarded whole. Live proof on the local stack: the plan's
candidate ("weekday 5pm — summarize the day's funding-ledger activity
into a short report and notify me") compiled, approved, materialized
with `conch-compile-*` assets, drilled green, and left operating with
the schedule DISABLED and the mission dry-run; the opt-in
`tests/test_compiler_live.py` additionally proves the rollback arc.

---

## Phase 0 — Correctness on local Ollama (do first)

These fix defects that make local sessions appear randomly broken today, plus
the tools-only enforcement. All are small and independent.

### 0.1 Discover effective context; keep `num_ctx` optional — **S** ✅
- **Problem:** `raw_ollama` / `stream_ollama` never pass `options.num_ctx`.
  Ollama defaults to 4k context under 24 GiB VRAM and truncates silently from
  the top (evicting system prompt + tool schemas), while
  `CONTEXT_LIMITS["ollama"] = 28000` in `runtime.py` assumes 28k. Result:
  tool calling "inexplicably" stops after a few exchanges.
- **Fix:** let Ollama choose `num_ctx` by default so Conch does not force a
  large KV allocation on unknown hardware. An explicit `ollama_num_ctx` is
  passed and clamped to the model maximum. Read the loaded effective context
  from `/api/ps`, use a conservative pre-load accounting fallback, and keep
  the model warm with `keep_alive`.
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
  and in the fallback chain. Cache the result by installed model digest so a
  retagged/replaced model is revalidated. Missing capability metadata is a
  rejection, not an optimistic guess.
- **Modules:** `providers.py`, `commands.py`, `tooling.py`, `config.py`.

### 0.5 Remove textual tool-call execution — **S** ✅
- **Fix:** remove every textual-call execution path from `chat_turn`.
  Native `tool_calls` only. A conservative detector remains for display
  suppression, diagnostics, and `/resettools`; it never supplies arguments to
  a tool.
- **Modules:** `runtime.py`.

### 0.6 Replace ask-mode regex extraction with structured output — **M** ✅
- **Fix:** delete `_SHELL_PREFIXES` / `extract_command` and the per-provider
  extraction heuristics. Ask mode requires a native `shell_command` call on
  Ollama, custom/llama.cpp, and cloud providers. `call_ollama` sends proper
  system+user roles and shares chat-mode request options.
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
  returns on every response, isolated by provider/model tokenizer; show a
  context-usage gauge in the per-turn usage line; warn at ~80% of the
  effective context.
- **Modules:** `runtime.py`, `app.py`.

### 1.4 Model-generated compaction (auto-compact) — **M** ✅
- **Problem:** `compress_context` char-slices messages (first 200 + last 100
  chars) and drops middles — garbage input for small models.
- **Fix:** at ~70% of `num_ctx`, summarize older history with the LLM into a
  single summary message; keep system + last N turns verbatim. Tool-call and
  tool-result groups are atomic and never split. Keep cheap
  char-capping only as a first layer for oversized single messages
  (Claude Code auto-compact / Hermes compressor pattern).
- **Modules:** `runtime.py`.

### 1.5 Token-aware tool-result truncation — **S** ✅
- **Fix:** replace fixed char caps with a budget scaled to context. Parallel
  results share one aggregate round budget, keeping head + tail, so many
  simultaneous calls cannot each consume 10% of the window.
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
  Capability gating (Phase 0.4) applies: enumerate `/v1/models` and retain only
  models that produce the required forced native tool call. llama.cpp context
  is discovered through `/v1/props` then `/props`; unavailable endpoints
  expose no models.
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
- **Extended (post-plan):** the `conch_introspect` tool reuses this
  machinery pointed at conch's *own* source (plus live capability/config
  reports and path-validated source reading), giving the model accurate
  self-knowledge on demand — the model-facing complement of `/status` and
  the 0.1-era self-description.

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

### 4.1 Skill system + in-chat skill builder — **M–L** ✅
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

### 4.2 Skill-scoped subagents — **M** (on top of 3.1) ✅
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

### 4.3 Remote agentic loop over Slack, SMS, email — **L** ✅
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

### Edge hardening gates — ✅
- **Per-request authority:** a model call may execute only a tool definition
  included in that exact request after profile/relevance selection. Retries,
  fallbacks, skill scopes, and remote sessions preserve that exact set.
- **Protocol integrity:** tool calls are canonicalized before execution,
  collision-free IDs are shared by assistant calls/results, malformed or
  schema-invalid arguments become tool errors, and repetitive batches stop.
- **Privacy:** local providers imply `local_only=auto`; cloud fallback,
  cloud model switches, cloud weak models, and startup IP geolocation are off
  unless explicitly enabled.
- **Execution isolation:** process-global shell/runtime state is serialized
  across local, scheduled, delegated, and remote turns. Remote approvals are
  single-use, expiring, origin-bound, and hook-gated.
- **Interactive credential boundary:** captured shell execution has no
  interactive stdin/controlling TTY. `/terminal` and SSH connect/shell actions
  require explicit local confirmation, suspend Conch's input reader, inherit
  the real terminal without capture, and restore terminal flags afterward.
  Remote channels never receive these tools.
- **Remote host operation:** OpenSSH ControlMaster sockets live in a private
  `0700` runtime directory; targets are option-injection validated, host-key
  policy remains OpenSSH's default, noninteractive remote commands retain
  permissions/hooks/timeouts/result budgets, and tracked masters are cleaned
  up on disconnect or best-effort exit.
- **Containers:** non-root wheel install, bridge networking, narrow workspace
  mount, named config/state volumes, read-only root filesystem, and documented
  host/LAN/Ollama-sidecar/llama.cpp-sidecar topologies. Entrypoint startup
  never rewrites config or deletes data.

---

## Phase 5 — Long-horizon autonomous missions

A **mission** is a goal conch pursues over hours/days/weeks with indeterminate
duration — e.g. "sell these items on eBay", "develop a long-term trading
strategy and execute it". This is a different regime from a chat session: the
context window is ephemeral but the mission is not, so all mission state lives
on disk and every model call reconstructs a small working context from it.

**Prior art / failure modes:** AutoGPT-style open loops are the cautionary
tale — aimless wandering (re-deciding the goal each iteration), context rot
(dragging a growing transcript until the model degrades), and infinite
retry loops. The mitigations, consistently rediscovered by Claude Code and
Hermes, are the design here: goal and plan externalized in durable stores
rather than the transcript; an append-only journal instead of transcript
replay; periodic explicit re-planning against success criteria; hard
iteration/spend budgets enforced in code; and human checkpoints on anything
irreversible. Conch already has most of the substrate (conversations, memory
facts, todo scratchpad 2.5, budgets 2.6, scheduler) — Phase 5 composes it
rather than inventing parallel stores.

### 5.1 Mission spec + state machine — **M**
- **What:** a durable mission record under
  `~/.local/state/conch/missions/<id>/`: `mission.json` holds the goal
  statement, success criteria, constraints and budgets (see 5.4), channel
  preferences, and status. State machine:
  `draft → active → paused → blocked (awaiting approval/input) →
  done | aborted | failed` — persisted on every transition so restarts of
  conch (or the machine) resume cleanly. `/mission new` runs a short
  conversational intake where conch drafts the spec (goal, measurable success
  criteria, budget lines) and the user confirms before activation.
- **Reuse, not parallel stores:** each mission owns a dedicated conversation
  in `ConversationManager` (its working transcript, compacted as usual), a
  per-mission todo scratchpad (the 2.5 tool, keyed by mission id), and writes
  durable facts to the 2.8 memory tiers. `mission.json` is the only new
  store; it is the source of truth for goal/status/budget.
- **Modules:** new `missions.py`, `commands.py` (`/mission`),
  `conversations.py` (mission↔conversation link), `tooling.py`
  (mission-scoped scratchpad keying).

### 5.2 Execution model: scheduler-driven work sessions — **M–L**
- **Decision — sessions, not a daemon:** the loop runs as discrete,
  checkpointed **work sessions** fired by `scheduler.py`, not a resident
  always-on agent process. Rationale: conch already has a persistent
  scheduler with on-disk tasks; sessions are naturally resumable and
  crash-tolerant (worst case loses one session, not the mission); and on a
  single local Ollama box a resident mission loop would pin VRAM and starve
  interactive use. Nothing about the mission needs to be "hot" between
  sessions — all state is on disk (5.1, 5.3).
- **Session shape:** each session re-hydrates a *fresh* context sized for a
  small local model (~2–3k tokens): the mission spec + current status, the
  latest self-review summary and journal tail (not the full journal), and the
  todo scratchpad. Never replay the full transcript. The session works for a
  bounded number of rounds/tokens (2.6 budgets), then **checkpoints**: journal
  entry, todo update, status transition if any, and — key — the model sets its
  own `next_run` ("auction ends in 6h, wake me then") via a mission tool,
  falling back to the mission's default cadence. Long or specialized steps go
  through skill-scoped subagents (4.2) so the mission session's context stays
  a coordinator, not a worker.
- **Concurrency:** mission sessions are serialized with interactive use and
  with each other (one Ollama server — same rule as 3.1); the scheduler skips
  a due session if an interactive turn is running and retries shortly after.
- **Modules:** `missions.py` (session runner, rehydration, checkpoint),
  `scheduler.py` (mission task type, model-settable next-run),
  `app.py` (executor wiring, interactive-use arbitration), `runtime.py`.

### 5.3 Progress journal + self-evaluation — **M**
- **Journal:** append-only JSONL per mission
  (`missions/<id>/journal.jsonl`): timestamped entries for actions taken,
  observations, decisions with one-line rationale, spend events, and session
  checkpoints. Sessions append; nothing rewrites history. The weak model
  (2.7) writes the per-session summary entry cheaply.
- **Self-review:** every N sessions (or daily), a review session runs with a
  dedicated prompt: score progress against the success criteria in
  `mission.json`, detect stalls (no material state change across the last N
  sessions, repeated failures of the same step), and either revise the
  strategy — recorded in the journal as a new numbered plan version, todo
  scratchpad rewritten to match — or escalate to the user over a channel
  (5.5) when blocked or when the criteria themselves look wrong. This is the
  anti-wandering mechanism: re-planning is an explicit, journaled event, not
  an every-iteration improvisation.
- **Modules:** `missions.py`, `prompts.py` (review prompt template).

### 5.4 Money and irreversible-action safety — **M–L** *(hard prerequisite for any real transactions)*
- **Budgets in the spec:** `mission.json` carries machine-enforced limits —
  max total spend, max per transaction, max per day, max transaction count —
  plus an action allowlist (which tools/commands the mission may use
  unattended). A spend ledger derived from journal entries is checked
  **in deterministic code** (mission gate + 2.2 `pre_tool_use` hooks) before
  any transaction-capable tool call; prompt instructions are not a control.
- **Approval checkpoints:** irreversible or financial actions (place listing,
  submit order, send payment, delete remote data) always route an
  approval request over the 4.3 channels (Slack/SMS/email) and park the
  mission in `blocked` until the operator replies `approve <id>` /
  `deny <id>`. Approvals are per-action, with the exact parameters (item,
  price, order size) in the request message.
- **Dry-run by default:** anything financial starts in dry-run/paper mode —
  eBay missions draft listings without publishing; trading missions
  paper-trade against live data and journal hypothetical fills. Going live
  requires the operator to explicitly set `live=true` **and** non-zero budget
  lines in the mission spec. **Full autonomy over money is never a model
  decision and never a default — it exists only to the extent the operator
  explicitly raises limits**, and even then per-action approval remains on
  unless the operator also allowlists specific action types under specific
  caps.
- **Kill switch:** `/abort <mission>` locally, `abort <mission>` over any
  authenticated channel (4.3 sender allowlist), and a stop-file
  (`missions/<id>/STOP`) checked by the scheduler before starting and by the
  gate before each tool call — so an abort takes effect mid-session, not at
  the next wake.
- **Audit log:** a separate append-only `missions/<id>/audit.jsonl` recording
  every gated action: what was requested, gate decision, who approved and
  over which channel, and the resulting spend. Distinct from the journal so
  the model can't compact or rewrite it (the model has no write access to it).
- **Modules:** `missions.py` (gate, ledger, stop-file), `tooling.py`
  (hook integration), `scheduler.py`, `channels.py` (approval flow),
  `config.py`.

### 5.5 Mission observability — **S–M**
- **Local:** `/missions` lists all missions with status, last activity, next
  wake, spend vs budget, and current blocker; `/mission show <id>` prints the
  spec, plan version, todo state, and journal tail; `/mission pause|resume|
  abort <id>`.
- **Remote:** proactive notifications over 4.3 channels on milestones
  (success-criterion met, listing sold, plan revision), blockers/approvals
  needed, budget thresholds (e.g. 80% of a limit), errors after retries, and
  a periodic digest built from the 5.3 self-review summary.
- **Modules:** `commands.py`, `missions.py`, `channels.py`.

### Phase 5 dependencies and ordering
- **Already in place (✅):** 2.1/2.2 (permission modes + hooks — the
  enforcement substrate for 5.4), 2.5 (todo scratchpad), 2.6 (budgets),
  2.7 (weak model for journaling/summaries), 2.8 (memory tiers), scheduler.
- **Hard dependency:** 4.3 (channels) for approval checkpoints, remote abort,
  and notifications — 5.4 and the remote half of 5.5 cannot ship without it.
  4.1/4.2 (skills/subagents) are strongly recommended for work quality in 5.2
  but not strictly blocking.
- **Within-phase order:** 5.1 → 5.2 → 5.3 form the core and can be
  prototyped *before* Phase 4 completes using local-only observability and
  non-financial missions (e.g. "keep this repo's deps updated"); 5.4 must land
  before any mission touches money or irreversible external actions; 5.5
  local half ships with 5.1, remote half with 4.3.

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
- **Phase 5** turns the accumulated substrate (scheduler, scratchpad,
  budgets, memory, permissions/hooks, and Phase 4's skills/subagents/channels)
  into long-horizon autonomous missions. Its core (5.1–5.3: durable mission
  spec, scheduler-driven checkpointed work sessions, journaled
  self-evaluation) is deliberately state-on-disk/context-fresh to avoid the
  AutoGPT failure modes of wandering and context rot, and can be prototyped
  on non-financial missions before Phase 4 lands. The money/irreversible-
  action gate (5.4) hard-depends on 4.3's approval-over-channel flow and must
  precede any real-world transacting; dry-run is the default and raising
  spend limits is an explicit operator act, never a model decision.

Coordination note: 0.2 and 0.7 overlap with the qwen tool-calling and live
model-list fixes in progress on the `curses` branch — check that diff before
implementing.

---

## Swarm Milestone A — live evidence (September 2026)

The daemon was enabled locally (`edge_daemon = true`) and run against a
real cloud backend. Legacy `tasks.json` migration did not apply on the
verification machine (it is covered by tests). Verified live:

- **Mission lifecycle:** a standard daily mission (dry-run, session and
  token budgets, daily cadence) activated; its first session executed
  real `git` commands via `local_shell` (agent mode), recorded a
  `mission_control` note, checkpointed, committed budgets, rescheduled
  the wake timer (+24h), and enqueued its digest — all in one kernel
  transaction. A separate `/schedule`-style run-once task had its timer
  fire live, ran, succeeded, and delivered its one-line answer.
- **Notifications:** with no channel configured, every session digest
  was delivered over the log transport and recorded as a `delivered`
  outbox row with a session-scoped dedupe key. Configuring
  `notify_channel` (Slack/SMS/email) upgrades the same path to a live
  channel with no mission changes.
- **kill -9 / resume proof:** the daemon was killed with SIGKILL
  mid-session. The restarted daemon adopted a new epoch and *honored
  the dead session's live lease* (no theft); at lease expiry,
  reconciliation released the budget reservation, appended
  `session_abandoned`, returned the mission to ready, and the retried
  session checkpointed normally. The killed session left no partial
  effects, no duplicate outbox rows, and no duplicate timer fires
  (exactly one `timer_fired` per scheduled occurrence).
- **Shell attach:** `/missions`, `/mission show`, and `/tasks` ran
  through the real command path over the daemon socket, showing live
  status, budgets, checkpoint, and the event tail.
- **Integrity:** `verify_integrity()` on the live kernel hash-chain
  verified the full journal, with replay == live across every replayed
  projection and daemon epochs advancing monotonically across the
  restarts.

Gates all live in tests (`tests/test_kernel_*.py`, plus the kernel
secret-canary sweep and the no-daemon compat gate): 1,183 tests green on
Python 3.14 and 3.9 with ruff clean at every commit.

## Sovereign phone layer — Matrix + ntfy (September 2026)

The remote loop gained a transport the user owns end to end: a
**MatrixChannel** (stdlib urllib against the client-server API of the
user's OWN homeserver — /sync long-poll with a persisted since-cursor,
m.room.message send with m.thread relations, thread_id = room + thread
root, full-user-id fail-closed allowlists, media-API attachment capture
into the existing quarantine with the Slack caps, access token strictly
by env reference) and an **NtfyNotifier** (outbound-only POST to a
self-hosted ntfy topic; `notify_push = ntfy` routes interrupts —
approval requests, digests, mission milestones — as real push
notifications with click deep-links into the Element room, while
`notify_channel = matrix` keeps the conversation on Matrix).

Invariants unchanged and tested per channel: Matrix sessions are remote
sessions (safe_auto cap, REMOTE_EXCLUDED_TOOLS, origin-bound expiring
approvals where origin = matrix + room/thread + sender, bounded
replies, thread == conversation), and the daemon's `channel_intake`
lease covers the Matrix poller exactly as the others.

E2EE is handled honestly: stdlib cannot do Olm/Megolm, so v1 documents
(a) unencrypted rooms on the user's own homeserver over TLS/tailnet as
the simple sovereign default and (b) pantalaimon as the E2EE proxy path
— noting plainly that upstream pantalaimon is archived (0.10.5, 2022,
libolm). Named upgrade paths, deliberately not built: a one-tap approve
button (needs an authenticated tailnet-only listener — never a public
endpoint) and UnifiedPush for Element's own notifications on Android.

Self-host deploy: `deploy/docker-compose.phone.yml` (Conduit + ntfy,
digest-pinned, named volumes, federation off, 127.0.0.1/tailnet binds)
plus `deploy/phone-bootstrap.sh` (registers the two users, mints
conch's token to a 0600 env file by reference, creates the DM room,
prints Element + ntfy app steps). README "Phone: sovereign setup"
walks the whole thing. The worktree/branch map is unchanged.

Live drill evidence (September 2026, isolated compose project
`conch-matrix-test` + isolated-XDG daemon; the live daemon untouched;
stack torn down after): a real Conduit + ntfy pair was bootstrapped
with three users. The phone-side user's message got a full daemon
answer in the room; a file-write request produced
"Approval needed [#1]" in the Matrix room AND a priority-4 ntfy push
(title "Conch: approval needed", matrix.to click deep-link, verified
over ntfy's subscribe API); `approve 1` from a room member who was NOT
allowlisted was dropped with no execution and no reply; `approve 1`
from the allowlisted user ran the exact pinned command and posted the
result; the /sync since-cursor survived a daemon restart (epoch 1→2)
with no message lost or double-answered, every pass under the
channel_intake lease.

## llama-idx — the inference registry integration (September 2026)

The self-hosted fleet got a control plane: **llama-idx**
(`~/llama-idx`, own public-safe repo), a stdlib+SQLite registry that
llama.cpp/Ollama/OpenAI-compatible boxes self-register with (one curl,
idempotent upsert, flavor autodetected), that health-polls them
(200/503 → up/degraded, 3-strike down with capped backoff, never waking
sleeping models) and pre-answers the tools-only question with conch's
own forced tool-call probe (identity-cached, serialized per provider,
loaded-models-first, router catalogs never force-loaded). Conch reads
one endpoint — `GET /v1/inference` — instead of per-box config.

Conch side (`conch/llamaidx.py`, ~R2 of the llama-idx plan):
`llamaidx_url` (+ optional `llamaidx_token_env`, name-only) config keys;
`/models` grows an llamaidx section of namespaced entries
(`llamaidx/provider/model`, degraded providers marked, down providers
absent); `/model llamaidx/gpubox/qwen3-32b` routes through the EXISTING
ollama/custom adapters by flavor — no new inference code. The registry
verdict gates listing; conch's probe-on-select still runs before
committing (belt and braces — a stale registry verdict is caught by the
component that talks to the model). Fallback chains gained a registry
tier (same-flavor first, largest-ctx first) between same-provider
alternates and any cloud hop, resolved at use in the runtime.
`local_only` applies to the registry URL and every discovered base_url;
`.ts.net` and CGNAT 100.64/10 now count as local. Unset = feature off,
zero new traffic.

Gates in tests (24 new in tests/test_llamaidx.py against a fake
registry + fake provider boxes: list/select/route/absent-when-down/
degraded-marker/probe-on-select/namespacing/local_only/fallback incl. a
live chat_turn rescue through a registry box). Live drill evidence
(2026-09-15, isolated XDG, no real inference hardware — the A6000 box
has no endpoint yet, the .152 box is hardware-broken): the real
registry served from its own repo, the test suite's fake llama.cpp
provider registered as a real HTTP process via the documented one-curl,
conch listed `llamaidx/gpubox/qwen3-32b`, selected it (its probe hit
the fake box), and routed a chat turn to it through the custom adapter.
R3 remains: push heartbeat, deregistration tombstones, fleet tie-in
(`labels.host` join to worker resource_groups).

## Capture monitoring — roadmap and the browser satellite (September 2026)

The computer-usage-monitoring feasibility work settled into three
phases, consent-first at every step (explicit opt-in surfaces, local
kernel only, no cloud, secretguard on every ingest path):

- **Phase 1 — journal-adjacent capture (done, merged).** The capture
  component (`/install capture`, `capture_enabled` gate): sessions,
  missions, shell history, a designated email folder, and Scribe import
  feed `/compile from-*` drafts; deterministic recurrence mining
  (`/compile suggestions`) computes "you've done this N times" over
  normalized step sequences. No new observation — only work conch
  already journaled or the user explicitly designated.
- **Phase 2 — browser-capture satellite (done, this increment).** The
  Scribe-equivalent for the user's own browser: an MV3 Chrome extension
  (`conch/satellites/browser_capture/`, plain JS, shipped as package
  data so pip/uv/pipx installs carry it; setup copies it to a stable
  user-data dir for load-unpacked) captures semantic DOM interaction —
  navigation paths,
  click role/label (never coordinates), form-submit field NAMES only,
  copy events without content — on origins the user explicitly
  allowlists (no `<all_urls>`; per-origin `chrome.permissions` grants;
  password/secret fields excluded at the source; badge + pause
  control). Transport is Chrome native messaging to
  `conch-capture-host` (stdio, 4-byte-length JSON framing — no
  listening ports): the host validates fail-closed (protocol version,
  pinned caller extension id, per-kind schema whitelist, http(s)
  origin), runs the authoritative secretguard scrub reject-whole, and
  journals accepted events via the existing `event.post` op with
  `source="browser"` (daemon socket → direct kernel store → bounded
  oldest-dropped spool, never blocking the browser). Ingestion feeds
  the same pipeline: `store.list_inbox` reads them back,
  `normalize_browser_step` gives mining `web:<host>:<action>` shapes,
  `/compile from-browser [origin]` follows the from-* conventions, and
  `/install capture browser` writes the NativeMessagingHosts manifests
  (Chrome/Chromium/Brave/Edge, macOS+Linux) and verifies the
  extension → host → kernel handshake. All behind `capture_enabled` +
  `capture_browser`. Firefox manifest: follow-up.
- **Phase 3 — OS-level usage monitoring (not started, deliberately
  last).** Window-focus/app-usage signals via platform accessibility
  APIs, the heaviest consent surface; only worth building if Phase 2's
  origin-scoped model proves the capture→mining→card loop earns its
  keep. Same shape when it comes: explicit per-app allowlist, a
  satellite process feeding `event.post`, secretguard at the boundary.

- [x] Browser-capture satellite: extension + native host + `event.post`
      ingestion + `from-browser` + mining integration + `/install
      capture browser` setup/handshake (September 2026).
- [ ] Firefox native-messaging manifest + extension port.
- [ ] Phase 3 scoping: OS-level focus/app-usage satellite, only after
      browser-capture proves recurrence value.

### The registry as a data source (September 2026, same week)

Discovery answered "what can I switch to"; this increment answers "what
is my fleet doing". `fetch_llamaidx_status` reads the registry's
`?status=all` view — down boxes with last_error/last_seen, degraded
boxes, per-model ctx/quant/loaded/tools/modalities, labels — parsed
fail-closed and rendered bounded (24 providers / 12 models / 6000
chars) by one shared renderer. Three consumers: the `llamaidx_registry`
builtin tool (fleet_status + list_models; present only when
`llamaidx_url` is set; read-only, never touches the boxes), the
`/llamaidx` command (the human's fleet view — the only place down boxes
appear, by design), and `conch_config` (list_models grows an llamaidx
section; `set_model llamaidx/provider/model` resolves through the
registry, runs probe-on-select against the box, and queues adapter
overrides the app loop applies between turns). Both registry views now
fail closed on unknown `registry_version` majors. Reporting and routing
stay separate: selection/fallback still consume only the tool-verified
catalog. 20 new gates in tests/test_llamaidx.py (status view incl.
down/untooled visibility, ?status=all request assertion, schema gate,
local_only on reporting, bounded rendering, env-name-only auth
rendering, tool injection gating, conch_config select/refuse/queue,
/llamaidx output). Live 2026-09-15: the real registry on burt served
the A6000 box's qwen3-14b through fleet_status and list_models.
