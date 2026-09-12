# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 0.6.0 predate this changelog.

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

[0.6.0]: https://github.com/detournement/conch/releases/tag/v0.6.0
