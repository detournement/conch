# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 0.6.0 predate this changelog.

## [1.0.3] - 2026-11-25

### Added

- `grep` and `glob` tools for searching file contents and names. They are
  read-only, never ask for approval, respect `.gitignore`, use ripgrep
  when it is installed (with a Python fallback), cap their results, and
  redact credentials. Matches inside credential files are listed by name
  only.
- `post_edit_check` runs your linter or type checker after each
  successful `edit_file`/`write_file` and shows the model its exit status
  and output, so it can fix what it just broke. Per-extension entries
  (`post_edit_check.py=ruff check --quiet {path}`) override the default,
  and an empty value turns the check off for that extension.
- Project instructions now load from CONCH.md, AGENTS.md, CLAUDE.md,
  .claude/CLAUDE.md, .github/copilot-instructions.md, .cursorrules and
  always-applied .cursor/rules/*.mdc, at every level from the git root
  down to the current directory. Symlinked or duplicate files load once,
  each block is labelled with its path, and the size limit grows with the
  model's context window (6,000 to 24,000 characters). `/status` lists the
  files that were loaded.
- Inside a git repository the system prompt carries short coding
  guidance: search before changing, read before editing, keep diffs
  minimal, run the project's own tests and linters before calling work
  done, report what was not verified, and commit, push or open pull
  requests only when asked.

### Changed

- `edit_file` and `write_file` refuse to change an existing file that was
  not read this session or that changed on disk since it was read, and
  ask the model to read it (again) first. Set
  `require_read_before_edit=false` to turn this off.
- Output from `post_tool_use` hooks now reaches the model, appended to the
  tool result.
- The tool cap for local models rises from 17 to 19 so that `grep` and
  `glob` can be pinned without crowding out relevant tools.

### Security

- A per-project `.conchrc` can no longer run commands, widen permissions
  or redirect traffic and credentials. Before, a cloned repository's
  `.conchrc` could set `permission_mode=yolo`, hook commands, `base_url` or
  `api_key_env` and take effect as soon as Conch ran inside it. Project
  files now apply only model choice, limits and display settings. Conch
  names any key it ignored; put such keys in `~/.config/conch/config` if
  you trust the project.

## [1.0.2] - 2026-11-25

### Performance

- Conch reaches its prompt without waiting on the network. MCP tool schemas
  are cached per server (keyed on a hash of its `mcp.json` entry, never on
  secret values) and servers connect in the background; a tool called
  before its server is ready waits for it. With a slow MCP server and a
  slow provider, launch-to-prompt went from 8.2s to 0.1s (1.9s on a first
  run with no cache). One-shot runs, scheduled tasks and missions use the
  cache and start no server until one of its tools is called.
- A passing model check is remembered for 24 hours per provider, model,
  endpoint and key fingerprint (the key itself is never stored). Otherwise
  the check runs in the background and settles before your first message.
  An offline provider no longer stops Conch from starting.
- Provider calls reuse HTTP keep-alive connections per host, including
  streaming responses, which removes a TCP and TLS handshake from every
  model call and tool round (about 100-185 ms per call measured against
  api.openai.com). A stale idle connection is retried once, and only if
  no response byte had arrived; proxied hosts use plain urllib. Set
  `http_keepalive=false` to opt out.
- Conversation saves are incremental: unchanged messages are not scrubbed
  again and the search index updates only new or changed messages,
  including across compaction. A save on a 1.6 MB conversation dropped
  from about 340 ms to about 8 ms.
- The fleet controller keeps one SSH ControlMaster per worker host
  (started on first use, 5-minute idle timeout, restarted on failure)
  instead of a new SSH connection per call: a steady tick with 10 tasks
  went from 30 handshakes to none. A worker that fails a connection is
  skipped for the rest of the tick and backed off, and the duplicate
  `worker.status` call is gone. Artifacts stream through one
  `conch-hostctl` session into a digest-checked `.part` file on a
  background thread. Tested against a simulated transport.
- The kernel daemon verifies the event journal incrementally from a
  verified-through checkpoint: at 300k events, startup verification went
  from 1.7s to about 2 ms. `conch mission verify` still checks every hash.
- Capitol pack flows flush run cursors every 100 events or 10 seconds and
  when a run parks, finishes or fails, instead of rewriting the state file
  on every event (0.3 ms per event instead of 138 ms at 2,000 sessions).
  `capitol_control watch` streams events within its deadline and checks
  run status less often, and the agent card is cached for 120 seconds;
  the bearer is still resolved on every call.

### Added

- `kernel_retention_days` (default 90, `0` turns it off) prunes processed
  or unaddressed inbox rows and delivered outbox rows older than that
  through journaled events, so replay stays exact. The event journal is
  never pruned. Once rows are pruned, versions before 1.0.2 cannot replay
  the journal.
- `capitol_pack_retention_days` (default 30) and `capitol_pack_history_cap`
  (default 50) prune finished pack sessions and cap their history.

### Fixed

- A mission session that crashed after writing more than 50 events now
  releases its token budget reservation. Before, it was abandoned with an
  unknown session id and kept up to 2,000,000 tokens reserved.
- Two shells saving conversations at the same time no longer overwrite
  each other's entries in `index.json`.
- Resuming a Capitol event stream from an older cursor no longer delivers
  events that were already processed.
- The kernel `status` command and folder-watch polling no longer load whole
  tables.

## [1.0.1] - 2026-11-25

### Fixed

- Evaluations and tool-less requests on `gpt-6.1-terra`, `gpt-6-sol`,
  `gpt-6-luna` and `gpt-5.6-sol`/`-terra`/`-luna` no longer fail with
  "transport failed (unsupported)". These models accept a custom
  temperature only with `reasoning_effort: "none"`, which Conch sends only
  on tool calls; other requests now use the model's default temperature.
  Verified live for all six models.
- Output from `local_shell` keeps multibyte characters (CJK, emoji, box
  drawing) intact when they straddle a read boundary, instead of turning
  them into replacement characters.

### Performance

- Anthropic requests use prompt caching (automatic `cache_control`), so
  each tool round reads the tool schemas, system prompt and earlier history
  from cache instead of reprocessing them. This lowers time to first token
  and input cost on long agent turns. Turn it off with
  `anthropic_prompt_cache=false`. Token counts, the context gauge and
  calibration still use the full prompt size, and the session cost line
  prices cached input at its discounted rate.
- Commands that print a lot of output run up to about 15x faster in
  `local_shell` (3M lines: 5.4s to 1.7s, the speed of the terminal itself).
  The terminal-control filter skips its per-character pass for plain text,
  and output is read in 64 KiB chunks.
- The fleet controller asks SQLite for in-flight dispatches only, instead
  of decoding the whole dispatch history on every one-second tick.
- Kernel databases get indexes for the folder-watch inbox poll and the
  outbox delivery claim, which ran as full table scans every tick. They
  are added automatically the next time the kernel opens.
- Unattended SSH commands (fleet RPCs and captured `ssh_remote` exec) set
  `ConnectTimeout=10` and server-alive checks, so a host that is down no
  longer holds up the controller for the whole RPC timeout.

## [1.0.0] - 2026-11-25

### Added

- Newer cloud models, each verified live for native tool calling on
  2026-11-25. OpenRouter: Kimi K3.1 Pro, GLM-5.3, Gemini 4 Neon, Gemini 3.8
  Flash, Qwen3.8 2.4T, Grok 4.7, MiniMax M3, Meta Muse Wonder, and Mistral
  Large 4. OpenAI: `gpt-6.1-terra`. Bedrock: Kimi K3 (US inference profile),
  MiniMax M2.5, DeepSeek V3.2, and Mistral Large 3.

### Changed

- Switching models with `/model` or the model picker takes effect at once.
  The live model check now runs in the background (a ChatGPT or Claude seat
  check is a full generation that took 3-7 seconds), and your next message
  waits for it only if it is still running. If the check fails, Conch
  switches back to the last working model and returns your message to the
  composer unsent.
- OpenRouter prices and the OpenAI, OpenRouter, and Bedrock audit dates are
  refreshed. `gpt-6.1-sol` and `gpt-6.2-sol`/`-astra` stay out of the OpenAI
  catalog because they need v1/responses for tool calls.

### Removed

- OpenAI `gpt-4.1-nano`, `o4-mini`, `o3-mini`, and `o1`: the API no longer
  lists them and rejects them as deprecated.

### Fixed

- Conch no longer loses the request it is working on in the middle of a
  turn. Automatic summarization now covers only history from before the
  current request, so the request and its tool exchanges stay verbatim.
  Fallback compression gives up earlier turns first, then the current
  turn's earliest tool exchanges, and never drops your latest message.
  Repeated compression keeps one cumulative note instead of stacking notes.
- Large tool catalogs no longer crowd the conversation out of the context
  window. Tool schemas are held to about 35% of the context budget per
  request, keeping the pinned tools plus the most relevant others. With
  200+ MCP tools on a 128K-window model such as a ChatGPT seat, requests
  previously kept little more than the latest tool exchange.

## [0.9.9] - 2026-11-23

### Changed

- Foreground chat and one-shot `conch "..."` tool workflows no longer stop
  after 25 rounds by default. Existing automatic context summarization and
  fallback compression keep long turns within the context budget.
- `/rounds 0` and `conch_config set_rounds=0` restore unlimited rounds;
  positive limits remain available. Skills can raise finite limits without
  capping unlimited sessions. Background and evaluation budgets, token
  caps, cancellation, and repeated-call protection remain in place.

### Fixed

- Apply fallback compression when tool results shrink without changing the
  number of messages.
- Stop repeated oversized tool-call batches instead of letting their
  parallel-call rejection bypass repeated-call protection.

## [0.9.8] - 2026-11-22

### Added

- Interactive startup checks for updates in the background using the running
  installation's source, with a persistent update notice and Option+U / Alt+U
  shortcut. The shortcut protects unfinished input, saves the conversation,
  and restarts into that same conversation only after a successful update.
  Notices fit compact terminal layouts, terminal handoffs cancel queued
  updates, and restart excludes implicit working-directory imports.
  Restoring unsent input or a picked command cancels any queued update and
  keeps the composer editable.
  Updates wait for live picker selections to be restored as input or queued.

## [0.9.7] - 2026-11-20

### Added

- Evaluation summaries give a Conch suitability verdict and identify deficient
  capabilities. Trust failures override the aggregate score; incomplete runs,
  unsupported suites, and model fallback stay inconclusive. Saved reports
  receive the same assessment without extra model calls.
- `/eval` briefly explains the battery before confirmation and describes each
  case while it runs.

## [0.9.6] - 2026-11-19

### Added

- `/eval` selects a model without changing chat settings and runs 12 fixed
  Conch Lite cases through the real adapters. Per-case fixture subprocesses,
  inference/tool budgets, a whole-run deadline, and cancellation bound the run.
- Private, atomic evaluation reports and `/eval results` browsing, including
  partial-run recovery, explicit transport errors, observed model identity,
  unknown-usage handling, and separate trust-case failures.
- Opt-in public sharing through GitHub issues after an exact payload/account
  preview. Trusted ingestion validates bounded self-reported measurements and
  stores immutable data on `eval-results`; retries retain their issue and cannot
  overwrite an accepted record.
- Selection-only discovery does not probe models or launch seat CLIs. Evaluation
  disables fallback, retries, helper inference, hooks, ambient context, and
  real-world tools. Claude Code/Codex seat evaluations fail before inference
  while native isolation remains unverifiable; normal seat chat is unchanged.
- Reports use Ollama's clamped request context rather than its configured
  override. Cancelling sharing terminates and reaps the GitHub CLI request.

## [0.9.5] - 2026-11-18

### Added

- `/shortcuts` opens a searchable in-terminal keyboard reference. Shortcut hints
  use Option on macOS and Alt on Linux. `/notifications` enables or disables
  long-turn alerts without losing the configured delay.
- `conch setup [--check]` verifies the effective primary and offers native
  Claude/ChatGPT sign-in recovery, optional connection of both subscriptions,
  and primary selection. Failed or interrupted saves roll back config, keys,
  and file permissions; incomplete restoration is reported explicitly.

### Fixed

- macOS alert clicks no longer open Script Editor. Apple Terminal banners
  target the originating tab via `terminal-notifier`; supported terminal
  emulators deliver their own notifications. Linux desktops retain `notify-send`.
- Enter selects the highlighted primary model and closes the picker; Ctrl+P
  keeps it open. A switch selected during a turn applies before pending chat
  at the turn boundary, with the new model reflected in the session tools.
- Installers verify model access even when credentials already exist and
  reopen onboarding for rejected keys. No interactive login runs headlessly.
- Ollama recovery verifies and saves the discovery endpoint instead of reusing
  another provider's stale URL.
- API-key checks classify provider rejection codes before redacting diagnostics,
  so HTTP 400/422 authentication failures cannot appear as accepted credentials.
- Launcher repair leaves profiles with ambiguous multiline shell data unchanged
  and reports manual action rather than modifying quoted or generated scripts.
- Source checkouts expose companion commands through Conch-only shims without
  adding their private Python or pip to PATH, including symlinked launchers.
- Upgrades reconcile recognized stale shell launchers with private backups,
  preserving credentials, helper integrations, customized PATH entries, and
  valid shell syntax inside conditional/function bodies. Experimental checkouts
  migrate only with consent and retain their local work. Existing helpers that
  cannot be inspected are not overwritten.
- Launcher conflict checks match complete command names, so unrelated helpers
  such as `conch-helper` and `old-conch` no longer block an upgrade.
- Old pip is upgraded within the selected environment for editable
  `pyproject.toml` builds, avoiding the missing `setup.py` upgrade failure.
- Subscription inference no longer conflates Codex's read-only native sandbox
  with Conch's host file tools or an MCP server's allowed-directory list.
  Requested host paths remain available under OS permissions and existing
  approval guards; saved-file claims require a successful tool result.

## [0.9.4] - 2026-11-17

### Added

- `conch update` and `/update`: update the running installation from its Git
  upstream or recorded package source. Checkouts fast-forward or merge by
  default, with explicit `--rebase`, preservation of local/index work,
  conflict recovery, and editable/dependency refresh. `--check` previews;
  chat saves and closes before an actual update.
- Release source distributions and wheels with SHA-256 sidecars.

### Changed

- Submitted prompts are distinct themed **You** blocks, with terminal-width
  separators above and below, including multiline and queued input.
- The website installer detects existing managed or source installations and
  asks on the controlling terminal before updating the same copy. Decline,
  empty input, and no terminal leave it unchanged; fresh installs stay
  unattended. Older source checkouts bootstrap the safe updater rather than
  passing an unsupported `update` prompt to a model.

### Fixed

- `/new` carries a bounded set of recent file references immediately, even
  when session summaries are disabled or still running. Follow-ups can inspect
  generated artifacts instead of relying on a lossy summary. The old transcript
  and file contents are not carried over, and `/clear` removes the references.

## [0.9.3] - 2026-11-17

### Changed

- Maintenance release with updated version metadata and current-release
  documentation. Functionality is unchanged from 0.9.2.

## [0.9.2] - 2026-11-17

### Added

- An inline slash-command browser opens on `/` or Ctrl+P, filters names and
  descriptions, and supports arrow/page navigation, Enter to run, Tab to fill,
  and Escape to dismiss without losing the draft.
- `/ebrake [on|off|status]` pauses and resumes the session's agents at safe
  boundaries, preserving pending work and fan-out timeout budgets.
- `/colorscheme [name]` selects and saves one of six shared color schemes for
  the composer and panels, with monochrome and light-terminal support. Hover or
  Up/Down previews a scheme; click or Enter saves it, and Escape restores the
  configured colors.
- `/infobar [detail|progress]` selects and saves a persistent detailed stats bar
  (tokens, cost, throughput, context, model) or a context-utilization ruler.
- Startup loading has a smoother, colorful shell-and-wave animation, with compact
  rendering on narrow terminals and monochrome/ASCII fallbacks.

- Codex model discovery now lists the signed-in native client's named models
  (including available Sol, Astra, Terra, and Luna versions) in the model picker
  and textual `/models`, instead of offering only `default`. Discovery preserves
  native visibility, pagination, and subscription billing; `default` remains an
  option.

- Ctrl+P in the model browser sets the highlighted model as primary without
  closing the page or clearing its filter. It uses the same validation as Enter;
  switches requested during a running turn stay queued until a safe boundary.

- Model picker: Ctrl+E steps a model's reasoning effort and Ctrl+W its context
  window, where the provider takes them (Claude and ChatGPT seats, Anthropic,
  OpenAI reasoning models, OpenRouter, Ollama thinking and `num_ctx`). Choices
  are per model, saved as `model_effort` and `model_context`, shown on the row
  and in the status bar, and never sent to a model that does not accept them.

### Changed

- Repository preflight now requires documentation updates and a version increment
  before every merge to `main`, including docs-only and maintenance changes.

### Fixed

- Claude seat streaming accepts the CLI's text-only user echoes and synthetic
  structured-output reminders instead of failing with "unexpected user message".
  Native tool results remain checked, and only the validated final answer is shown.

- Custom and llamaidx reasoning models are no longer rejected during model
  switching because the tool-call probe ran out of tokens. The bounded probe
  now allows reasoning before the required native tool call; prose-only
  responses still fail validation.

- Explicit `ssh …` input now offers a choice between secure interactive terminal
  handoff and model-selected execution. The model receives the original command
  when selected; cancelling sends and executes nothing. SSH options and absolute
  paths are preserved, and multiline pasted blocks stay chat.

- Claude seat turns no longer fail with "Claude returned an unsupported system
  event": status events from newer Claude Code versions (thinking progress,
  notices, model fallback) are shown as progress, while events that mean
  native execution still stop the turn.
- Claude seat turns no longer fail with "Claude attempted a native tool" when
  the model calls a Conch tool by name instead of returning it: the CLI offers
  no native tools, refuses the call, and the model answers again. A native tool
  the CLI could actually run still stops the turn.

## [0.9.1] - 2026-11-17

### Added

- Repository awareness: inside a git checkout the status bar and startup banner
  show the repository and branch (`🐙 owner/repo ⎇ branch` on GitHub, with the
  branch's open PR and a check-state icon). On GitHub, one background `gh`
  request loads the description, default branch, latest release, the branch's
  pull request and checks, and recent open pull requests and issues into the
  model's context before the first turn. `github_context = false` turns the
  fetch off.

### Changed

- README and the docs under `docs/` describe the current release: the release
  link, configuration reference, tools, commands, and source layout match the
  code again.

## [0.9.0] - 2026-11-17

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
- Fan-out: the `fan_out` tool runs 2 to 8 independent subtasks in parallel
  worker processes and returns one summary per task. `fanout_model` gives
  workers a different model from the session's, and `fanout_model=auto` picks
  one per task from the models usable right now, matched to the task's effort
  and never on a costlier kind of billing than the session's. `/fanout` shows
  and sets it. Workers cannot prompt: an ask-first session confirms once, then
  workers run read-only commands and refuse the rest.
- Conversations page: `conch convos` (or `/convos`, or `/switch` with no id, in
  the shell) opens a full-screen browser over saved conversations. Type to
  search titles, ids, models, and full message text; read transcripts; sort by
  recent, cost, or size; delete after confirmation; Enter resumes. The header
  totals messages, tokens, and estimated cost overall and across matches.
- Tokens and estimated cost are recorded per conversation (`usage` in the
  transcript file and index), fan-out workers and channel conversations
  included. Conversations saved earlier show no cost.
- Desktop notification when an interactive turn runs 30 seconds or longer
  (`notify_after_seconds`; `0` disables): macOS via `osascript`, Linux via
  `notify-send`. It names the conversation and quotes the reply's first line.
- File tools: `read_file`, `edit_file`, and `write_file`. Edits and writes show
  a colored diff and ask before applying (`a` applies file edits for the rest
  of the session; agent mode applies without asking). Writes are atomic,
  preserve line endings and mode, and are covered by `/undo`. Credential-looking
  and system paths need confirmation even in agent mode.
- `web_fetch` reads an http(s) page as text, asking first in `prompt_all`
  (`web_fetch_allow_domains` skips the question for listed hosts). URLs that
  carry credentials are refused.
- Image input: image files dragged into the terminal or typed as paths are
  attached to the message, and `/image` attaches the clipboard. Every API
  provider receives them in its own format.
- `/rename`, `/export` (Markdown, credentials redacted), `/retry`, `/fork`, and
  `/compact [focus]`. The conversations page gains `r` rename, `e` export, and
  `f` fork.
- Model picker (`/models`, or `/model` with no name): every model across
  providers, seats, local servers, the llama-idx registry, and your additions
  in one type-to-filter list with context, price, tier, and roles. Enter switches
  the chat model; Ctrl+F and Ctrl+T assign fan-out workers and side tasks;
  Ctrl+A adds a model the catalog lacks (`extra_models`), Ctrl+X removes it.
  `/model <id> --provider <name>` switches to an exact provider.
- Panels open during a running turn: `/models`, `/provider`, `/help`, `/convos`,
  and read-only reports (`/status`, `/cost`, `/tools`, ...) no longer wait for
  the turn. Turn output is held behind a full-screen panel and shown when it
  closes; picks that change the session run as soon as the turn ends.
- A large ASCII conch heads the startup banner again on terminals with room for
  it; pipes and small windows skip it.
- Conch changes its own settings when asked: `conch_config` gains
  `list_settings`, `set_setting`, and `reset_setting` over a registry of
  validated settings (saved and applied live where possible). Changes that let
  more run unattended, and turning agent mode on, ask you first.
- `conch_introspect docs <topic>` returns the documentation sections that
  explain a feature, and `conch_introspect settings` lists what Conch can change
  about itself.
- Subagent panel (`/agents`, `/subagents`): fan-out worker and `delegate_task`
  settings (model, parallel workers, round budget, side-task model) edited in
  place or through the model picker, the skills a subagent can be scoped to, and
  live activity of this session's workers and subagents; `x` stops running
  fan-out workers. It opens during a turn as well.

### Changed

- CI pins ruff (an unpinned upgrade had been failing lint across the tree), and
  the secret-scan allowlist covers the last synthetic test fixtures, so both
  checks pass again. `delegate_task` honours a `provider/model` value in
  `subagent_model`.
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
- `/convos` and a bare `/switch` open the conversations page instead of the
  one-line picker, and switching conversations confirms which one resumed.
- Local models (Ollama, custom endpoints) are offered up to 17 tools per
  request instead of 12: the file tools, `web_fetch`, and `conch_config` joined
  the always-on set, and the 4 slots for tools picked by relevance are kept.
- The repository map in `conch_introspect` and the system prompt caps each
  file's symbol list (with a `+N` count) and keeps naming files once symbols no
  longer fit, so one large module cannot hide the rest.
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

[1.0.3]: https://github.com/detournement/conch/releases/tag/v1.0.3
[1.0.2]: https://github.com/detournement/conch/releases/tag/v1.0.2
[1.0.1]: https://github.com/detournement/conch/releases/tag/v1.0.1
[1.0.0]: https://github.com/detournement/conch/releases/tag/v1.0.0
[0.9.9]: https://github.com/detournement/conch/releases/tag/v0.9.9
[0.9.8]: https://github.com/detournement/conch/releases/tag/v0.9.8
[0.9.7]: https://github.com/detournement/conch/releases/tag/v0.9.7
[0.9.6]: https://github.com/detournement/conch/releases/tag/v0.9.6
[0.9.5]: https://github.com/detournement/conch/releases/tag/v0.9.5
[0.9.4]: https://github.com/detournement/conch/releases/tag/v0.9.4
[0.9.3]: https://github.com/detournement/conch/releases/tag/v0.9.3
[0.9.2]: https://github.com/detournement/conch/releases/tag/v0.9.2
[0.9.1]: https://github.com/detournement/conch/releases/tag/v0.9.1
[0.9.0]: https://github.com/detournement/conch/releases/tag/v0.9.0
[0.7.0]: https://github.com/detournement/conch/compare/v0.6.0...edge
[0.6.0]: https://github.com/detournement/conch/releases/tag/v0.6.0
