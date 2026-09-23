# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 0.6.0 predate this changelog.

## [0.7.0] - 2026-09-23

### Added

- **Installable product seams.** A plugin registry now supplies product tools,
  slash commands, components, and session wiring without coupling the core
  shell to Capitol or fleet adapters; static import-boundary tests enforce the
  split. First-run interactive launches can configure a provider and a
  permission-tight API-key file, while `/install` reports and sets up the
  shipped shell, edge, fleet, works, and capture components.
- **Owned-model discovery through llama-idx.** `/registry` and the
  `conch_config` tool validate and persist one llama-idx endpoint, add its
  tool-verified models to selection and local-first fallback, and route each
  model through the existing Ollama or OpenAI-compatible adapter.
  `/llamaidx` and the read-only `llamaidx_registry` tool expose the registry as
  a bounded fleet-status data source, including degraded and down providers.
- **Capture to compile.** The reviewed Architecture Card pipeline can now
  draft from bounded mission journals, saved conversations, shell history,
  allowlisted read-only email, Scribe MCP guides, and browser events.
  `/install capture` keeps the surface off until enabled; provenance is
  journaled, captured text is inert evidence, and every draft still requires
  the normal review and approval path. `/compile suggestions` mines recurring
  normalized work shapes with deterministic ranking and stable tie-breaks,
  then `/compile from-suggestion` carries the source evidence into a draft.
- **Local browser-capture satellite.** The wheel ships an MV3 Chrome extension
  and native-messaging host. `/install capture browser` makes a stable
  load-unpacked copy and a reinstall-safe launcher; explicit origin
  allowlists, semantic rather than coordinate capture, field-name-only form
  events, source-side secret exclusion, host validation, and bounded spooling
  keep the capture path local and fail closed.
- **Sandboxed execution.** `/sandbox docker` and `/sandbox e2b` route approved
  shell commands to ephemeral Docker or E2B environments without changing the
  existing permission and destructive-command gates. Host environment
  variables are not forwarded, Docker mount policy is explicit, and E2B does
  not receive local files.
- **Git checkpoints and undo.** Changed worktrees receive bounded turn-level
  snapshot refs through a temporary index, leaving the branch, real index,
  stash, and history untouched. `/checkpoint` lists, diffs, and restores
  snapshots; `/undo` restores the latest one after confirmation. Ignored and
  secret-shaped paths are excluded.
- **Visible token accounting.** A default-on per-message line shows input and
  output tokens, cost, context use, model, and measured or honestly marked
  estimated throughput; `/tks` toggles it. Custom OpenAI-compatible streams
  request usage data so local llama.cpp-style providers report consistently.
- **Current model catalogs.** Live-audited catalogs add Claude Opus 5.5 and
  GPT-6 Sol/Luna with their verified Chat Completions constraints, context
  windows, and pricing. The catalog continues to exclude models whose tool
  use requires an API Conch does not implement.
- **Governed processes and fleet operations.** The generic FlowPack engine,
  `/capitol` runtime and pack commands, shipped Capitol/pack-author skills,
  ProcessCompiler review/materialize/drill/rollback flow, durable mission
  controller, owner-granted worker ceilings, artifact publishing,
  `fleet_delegate`, and `/fleet` operations turn the earlier kernel and SSH
  worker foundation into user-facing operating surfaces.
- **Everyday shell and remote workflows.** Multiline paste and editor
  composition, `python -m conch`, `/new` and `conch --new`, `/todo`, `/list`,
  editor-backed `/notes`, Matrix conversation transport, and ntfy push support
  were added. Conversation writes are atomic, corrupt files are salvaged or
  quarantined, and model/fallback switches are recorded in history.

### Changed

- `conch-edge` uses supervised install and startup checks, channel intake is
  daemon-owned under a lease, mission events wake work immediately, scheduled
  critic reviews detect stalls deterministically, and mission learnings can be
  consolidated into credential-guarded shared memory.
- The public installer and documentation use the website `/install` route for
  an isolated, no-sudo install or upgrade, and installed/runtime diagnostics
  report the single-sourced package version.

### Safety and behavior

- Capture sources, shared memory, and retrieval reject credential material;
  email additionally requires an explicit folder and sender allowlist, and
  shell-history import drops credential-shaped lines with a disclosed count.
- Capture cannot approve or materialize its own output. Sandboxes change where
  a command runs, not whether it is allowed. Checkpoint restores require local
  confirmation and cannot restore excluded secrets.
- Product components remain gated until configured. Unknown registry/schema
  versions, unsupported catalog models, invalid browser callers/events, and
  uncertain external effects fail closed or reconcile by query instead of
  being retried blindly.

### Known limitations

- Scribe import requires a live Scribe MCP endpoint and OAuth credential; E2B
  execution requires a live account and API key. Their contract paths are
  covered by deterministic fakes when live credentials are unavailable.
- Browser capture is proven first on Chrome for macOS. Linux host paths are
  supported, Firefox is not yet supported, and Conch has no native Windows
  installer or runtime support (WSL2 follows the Linux path).
- Responses API and computer-use support were research only and are not part
  of this release.

## [0.6.0] - 2026-09-12

### Added

- **Secure interactive terminal handoff.** `/terminal <command>` (and the
  model-facing `interactive_terminal` tool) hands the real terminal to
  credential-aware programs — `sudo`, SSH, key passphrases — after an
  explicit local confirmation that holds even in agent/yolo mode. Conch
  never captures, logs, or returns handoff input or output; insecure
  forwarding forms such as `sshpass` and `sudo -S` are rejected; remote
  channel sessions never see these tools.
- **Validated SSH ControlMaster remote operation.** `/ssh
  connect/exec/shell/status/disconnect` manage OpenSSH ControlMaster
  sessions without storing credentials: authentication happens in an
  interactive handoff, captured `exec` reuses the control socket in
  BatchMode while still passing permission modes, allowlists, lifecycle
  hooks, timeouts, and result budgets, user/host/port fields are strictly
  validated and passed behind OpenSSH's option terminator, and control
  sockets live in `0700` runtime directories that are cleaned up on
  disconnect or exit.
- **Per-session `AgentSession` architecture.** Provider/model config, tool
  clients, permission and agent mode, cwd, and budgets are owned per
  session, so interactive, scheduled, delegated, and remote turns cannot
  leak policy, cwd, or tool state into each other. `conch/bootstrap.py`
  exposes the same startup wiring headlessly.
- **Versioned swarm protocol and fail-closed required-policy layer.** Task
  envelope, event, receipt, and lease dataclasses with canonical JSON and
  canonical IDs that fail closed on unknown fields and unknown or newer
  versions; a required-policy registry that denies on exception, timeout,
  or invalid decision, consulted ahead of the user `pre_tool_use` hook in
  every dispatch path.
- **SQLite mission kernel.** One transactional WAL kernel (single-writer)
  holds missions, plans, tasks, leases, checkpoints, integer-unit budgets
  (reserve/commit/release; child scopes strict subsets), origin-bound
  expiring one-use approvals, timers with generation fencing and
  skip/coalesce/bounded-catch-up misfire policies, a transactional
  inbox/outbox with dedupe keys and exactly-once ledger effects, an
  external-action ledger with idempotency keys and query-before-retry
  unknown outcomes, and an immutable hash-chained event journal whose
  projections replay exactly (replay == live is a tested invariant).
- **`conch-edge` daemon.** Opt-in via `edge_daemon=true`: owns the kernel
  exclusively (OS lock plus controller-epoch fencing), fires timers,
  mission sessions, and outbox deliveries, migrates legacy `tasks.json`
  schedules idempotently, stops gracefully on SIGTERM, and serves shell
  attach (`/missions`, `/mission`, `/approvals`, `/approve`, `/deny`, and
  the unchanged `/schedule` UX) over a `0700`/`0600` local socket.
  Missions run as bounded, checkpointed work sessions with fresh
  rehydrated context and hard wall/token/round caps; launchd and systemd
  templates ship in `deploy/`.
- **Trusted SSH fleet layer.** Signed, reproducible single-file worker
  artifacts (stdlib `zipapp` with `sshsig` signatures; verification is
  mandatory and fail-closed) plus an optional docker/OCI profile with
  identical delivery semantics; the stdlib-only `conch-hostctl` host agent
  (probe, content-addressed artifact store, idempotent
  deploy/activate/rollback with operation receipts, worker supervision,
  bounded RPC relay over SSH stdio); an event-sourced fleet registry with
  leases, fencing tokens, and heartbeat deadlines; filter→score task
  scheduling with shared resource-group caps; effectively-once external
  side effects through the kernel ledger; and controller-brokered
  delegation that turns a worker's `delegate_task` into a validated child
  dispatch.
- **Generic Capitol integration.** `CapitolRuntime` — AgentCard discovery
  with fail-closed capability gating, idempotent workflow calls, resumable
  SSE run watch with polling fallback, HITL replies, artifact
  upload/download, and eval reads; `CapitolAdmin` — a default-off,
  required-policy-checked provisioning profile whose every mutation is
  ledgered with a caller idempotency key, version pin, and rollback
  reference before the wire call; and mission-bound run supervision from
  the daemon — persisted event cursors, HITL checkpoints mapped to
  origin-bound kernel approvals whose decisions flow back exactly once,
  mission abort mapped to run cancel, and exponential backoff that parks
  (never fails) missions while the platform is unreachable.

### Security

- Secret-scanning gate: a gitleaks CI workflow, `scripts/check-secrets.sh`
  for local full-history and working-tree scans, a repo `.gitleaks.toml`
  allowlisting only synthetic fixtures, `SECURITY.md`, and hardened
  `.gitignore` rules for credentials, runtime state, and backups.
- Documentation, test fixtures, and plan prose use RFC 5737 / example
  values in place of private deployment identifiers, and the live-test org
  id is now supplied by the environment rather than a hardcoded default.

[0.7.0]: https://github.com/detournement/conch/compare/v0.6.0...edge
[0.6.0]: https://github.com/detournement/conch/releases/tag/v0.6.0
