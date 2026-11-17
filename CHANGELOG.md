# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 0.6.0 predate this changelog.

## [Unreleased]

### Added

- In-flight guidance at safe model/tool boundaries. Enter while busy steers the
  current turn; unstarted stale tools are skipped without interrupting running
  commands or bypassing approvals, history invariants, or turn budgets.
- Alt+Q queues an explicit follow-up; Ctrl+Q edits or deletes pending messages
  while preserving the composer draft and protecting edits from concurrent delivery.
- Provider reasoning and generation progress appear by default, separately from
  final answers and tool output. Alt+R hides/shows the bounded detail panel;
  opaque reasoning and native tool arguments are never displayed.
- First-run setup shows whether each Claude/ChatGPT seat is signed in, installed,
  or missing its CLI, and defaults to the first usable one. A signed-in seat is
  selected without a second browser login.

### Changed

- First-run setup no longer ends on a choice that cannot be completed. A missing
  seat CLI, failed sign-in, empty or rejected API key, unreachable Ollama, or
  empty custom URL saves nothing and returns to the provider menu; a rejected
  key is saved only on explicit confirmation. Setup text wraps to the terminal
  and shows `~`-relative paths.
- The key-hint row drops its least important hints on narrow terminals instead
  of cutting text mid-word, and lists pending-message and detail keys only when
  they apply. Command-palette and pending rows stay on one line each.
- A failed model request is reported once, without a repeated detail line or a
  trailing `[no response]`. Generation progress clears when the turn ends;
  published reasoning stays readable until the next turn.
- Ctrl+C during a turn returns pending guidance and follow-ups to the composer
  instead of letting them start new work after the stop.
- When first-run model verification fails, setup offers another provider and
  discards the failed attempt's config and key instead of exiting with a saved
  configuration that cannot start.
- The `/provider` picker lists the current provider first and shows what each
  other provider needs; picker descriptions align in one column. `/status`
  wraps settings on boundaries and shows `~`-relative paths.
- A command you decline is shown as `declined` with your feedback, not as an
  error with the time spent at the prompt. Model-check failure text wraps on
  words.

### Fixed

- Model/tool guidance now routes animations, full-screen programs, and
  cursor-updating displays through the explicitly approved direct terminal
  handoff, rather than claiming captured shell output was displayed.
- Captured shell output removes fragmented private CSI and terminal control
  payloads before applying output budgets, preserving readable text.
- Ordinary output publication stays synchronous with raw input attached.
  Live panels account for wrapped display cells and no longer leak repeated
  headings or typed drafts into scrollback through deferred print handoffs.
- Guidance sent while a final answer is empty or is a rejected textual tool call
  no longer stores that answer in history; blank guidance is ignored.
- Steering messages appear in the transcript after the reply they followed.
- Escape cancels approvals and pickers without a one-second delay; Backspace
  removes a pending message like Delete.
- Claude seat turns survive the CLI's transient API retries, and a seat stream
  error on macOS is no longer replaced by a cleanup `PermissionError`.
- Captured output keeps one copy of overstruck man-page text, and an
  unterminated terminal control string can no longer swallow later lines.
- Non-streamed OpenAI and Cerebras replies strip inline `<think>` blocks.
- Tool relevance on tool-capped providers follows the turn's original request
  plus guidance, not the latest guidance message alone.
- Seat turns no longer fail with `Codex returned malformed JSON` on commands
  containing quotes. Tool arguments are now typed objects under the client's
  schema enforcement instead of JSON written inside a string; arguments that
  are still invalid are rejected per call so the model can correct them.
- Codex's per-turn "Code Mode host is disabled" diagnostic no longer prints
  into the chat; it is kept in debug logging.

## [0.8.0] - 2026-11-16

### Added

- A rotating ASCII shell with phase labels and elapsed time makes startup model
  checks and tool loading visible without interfering with setup prompts.
- The terminal status line shows cumulative session cost independently of token
  statistics, preserving totals across conversation/model changes and explicitly
  distinguishing API estimates, subscription seats, local inference, and unknown
  pricing. `/cost`, introspection, and the exit summary use the same accounting.
- An inline `prompt_toolkit` chat interface keeps native scrollback alongside a
  live multiline composer, model/permission/session status, elapsed activity,
  and compact tool results. Search commands with Ctrl+P, models with `/model`,
  providers with `/provider`, and conversations with `/convos`.
- Chat drafts remain editable during streaming; queued multiline messages stay
  intact. Approval inputs are isolated from drafts and queues, and credential
  handoffs suspend the editor and discard pending input before releasing the
  terminal. Existing readline history is imported without overwriting it.
- Source installation uses a checkout-local virtual environment, including the
  terminal dependency, and stops on dependency failures instead of claiming a
  working PATH-only install.
- First launch explains provider/sign-in/model setup, lets local users select
  a model, and opens chat with example requests and approval guidance. Successful
  setup verification is reused rather than repeated. Cancellation and failed
  sign-in exit cleanly; optional integration keys no longer interrupt installation.
- Seat-backed `claude-code` and `codex` providers use the unmodified official
  CLIs with provider-owned Claude/ChatGPT sign-in and refresh; Conch never
  collects subscription tokens. `conch auth login/status/logout` manages
  native sign-in, and successful login selects the keyless provider.
- Seat-first onboarding and defaults, structured chat/tool/ask responses,
  native model-access preflight, bounded subprocess cleanup, and seat-aware
  usage labels. Existing Conch approval gates remain in charge of tool
  dispatch; seat sessions have no automatic API-key-billed fallback.
- Explicit API-key providers and existing configured provider selections are
  retained. Official CLIs and subscription login are required on each runner.
- New-user setup now includes official CLI installation commands, per-provider
  login checks, model-access verification, and troubleshooting in the README
  and `conch auth --help`. The source installer preserves existing credentials
  and delegates provider setup to the shared seat-first wizard instead of
  prompting for API keys.
- `docs/LOCAL-SUBAGENTS.md` documents LAN endpoints, registry discovery,
  seat-parent/local-child Conch skills, and separate named OMP agent setup.

### Fixed

- Pin the test matrix to Ubuntu 22.04 so Python 3.9 remains installable after
  the `ubuntu-latest` runner moves to Ubuntu 26.04.

- Codex subscription startup accepts documented nonfatal diagnostic items
  instead of misclassifying them as native tool execution. Warnings remain
  visible and credential-redacted; fatal stream errors and native execution
  events still fail closed.
- Seat startup failures distinguish detected sign-in from model/transport
  errors, strip API-error display brackets from diagnostics, and explain
  that slash commands require a running Conch session.

## [0.7.1] - 2026-10-22

### Added

- **`conch keys` — rotate any credential with one command.** `conch keys
  rotate` (also `/keys` in a session, `conch-keys`, `python3 -m conch keys`)
  runs a doctor, live-verifies every credential conch uses against its
  service, prompts (hidden) for what failed or imports it from the
  environment (`--from-env`, `--stdin`, `--yes`), saves it atomically to
  `~/.config/conch/env` (0600), and reloads every consumer — shell, conch-ask,
  the edge daemon, workers, MCP servers. `list` shows consumer, effective
  source, fingerprint, last verification and flags stale duplicates and inline
  `mcp.json` secrets; `check` has a `shell` and a `daemon` column and exits 0
  only when the active config is green in both; `required` prints what to ask
  the key admin for; `migrate-mcp` moves inline secrets to `${NAME}`
  references with a backup; `doctor` explains multiple `conch` binaries on
  PATH, a stale installed copy, env-file problems, stale profile exports by
  `file:line` (profiles, sourced files, fish, direnv), and a daemon running
  older code — with the restart line for launchd or systemd. Every command
  starts with an identity line naming version, commit, path, and install shape.
- **Precedence rule.** Keys saved through `conch keys` or first-run setup are
  conch-managed (manifest next to the env file) and override a shell export of
  the same name; hand-added lines keep the old environment-wins rule.
- **Hot reload.** Sessions re-read the env file on `/keys set`, `/reload`, and
  after a provider 401 (one retry); the daemon re-reads it when it changes, on
  `SIGHUP`, and on the new `keys.reload` control op (`keys.status` reports
  which keys it sees, fingerprints only).
- **`${NAME}` references in `mcp.json`** are honoured in `env`, `headers`,
  `url`, and `args`; an unset variable is a loud per-server error naming the
  server and the variable instead of a blank header.
- **Auth failures name the key and the fix** everywhere a 401/403 surfaces:
  provider adapters (chat, streaming, ask mode), the startup model check and the
  daemon's parked-mission reason, MCP servers, the Capitol client (registry
  bearers point at re-bootstrap), and the status-page exporter.

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
