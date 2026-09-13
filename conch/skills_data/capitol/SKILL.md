---
name: capitol
description: Run and steer Capitol AI workflows in plain language via the capitol_control tool. Use when the user asks to run, drive, check, watch, resume, or backfill a Capitol workflow, mentions runs, HITL checkpoints, clarifications, interventions, artifacts, docx outputs, or evals, names a workflow (funding ingest, funding packet, eBay draft), or asks "what can the Capitol agent do?". Covers discover → confirm → start keyed → watch bounded → report outputs.
tools: capitol_control, local_shell
---

# Capitol workflows in plain language

You drive the org's Capitol AI agent through the `capitol_control` tool —
discovery, keyed starts, bounded watching, HITL answers, outputs, evals,
and quarantine-bounded artifacts. This skill is the operating procedure;
the exact op semantics, input-schema conventions, and error meanings live
in `reference.md`, and worked recipes for this machine's real workflows
live in `cookbook.md` (both beside this file — read them when needed).

If a step here doesn't work against the live agent, the skill is the bug:
report what the tool actually returned rather than improvising around it.

## The core arc

Every "run X for me" request follows one arc. Do not skip steps.

1. **Discover.** `capitol_control op='workflows'` (or `op='discover'` for
   the card and org directory). Workflow ids come from this catalog and
   nowhere else. If the user's words don't map cleanly to one workflow,
   `op='suggest'` with their goal — suggestions are ranked guesses, so
   confirm the pick with the user before starting anything.
2. **Describe before starting.** `op='describe'` shows the input fields
   and the request-input key. If a required input is not derivable from
   what the user said, ask — never guess values into an effectful start.
3. **Start keyed.** `op='start'` with either `inputs` (the full map) or
   `input_value` (one value; the tool wraps it under the discovered
   key). The tool requires an idempotency key and derives one from
   workflow+inputs when you omit it — report the printed key to the
   user. To retry the same request, reuse the same key; the gateway
   replays the original run (`replayed` = success, not an error).
4. **Watch bounded.** `op='watch'` polls to a hard deadline (default
   120s, max 600s) and returns a summary. A run still going at the
   deadline is normal: report progress and resume later with the
   printed `since_sequence` cursor — never spin in a loop of watches
   without telling the user what's happening.
5. **Report outputs.** On terminal success, `op='outputs'` (and
   `op='evals'` when the user cares about quality gates). Read machine
   facts from outputs, never out of assistant prose.

## Hard rules

- **Never invent workflow ids.** Only ids returned by
  `op='workflows'`/`op='suggest'` are real. A remembered or guessed id
  is wrong even if it looks plausible.
- **Always key effectful starts.** Omitting the key is fine (the tool
  derives one deterministically); *varying* inputs to dodge a replay is
  not — a fresh run needs the user to ask for one.
- **Park on ambiguity.** Missing required inputs, several plausible
  workflows, an unclear goal: ask the user and stop. An idle question
  costs seconds; a wrong run costs a real external effect.
- **Relay HITL questions verbatim.** When a watch summary shows
  `NEEDS INPUT`, put the run's exact question to the user, then answer
  with `op='respond'` using their words. Never answer a clarification
  from your own judgment. Interventions are the literal tokens
  `continue` or `stop` — map the user's decision to exactly one.
- **Admin is user-explicit.** Creating/publishing/rolling back
  workflows, allowlists, schedules, collections, bearers: the tool
  refuses these and names the `/capitol admin …` command — tell the
  user to run it themselves; do not work around the refusal.
- **Artifacts live in the quarantine dir.** Uploads only read from it,
  downloads only write into it. Verify a downloaded file's digest
  against the producer's record before trusting the bytes.

## Errors you will see (full table in reference.md)

- **Credential needed (401):** park. The user sets `$CAPITOL_A2A_BEARER`
  (or the env named by `capitol_bearer_env`) or adds the agent to
  `~/.capitol-a2a/agents.yaml`. There is no automatic re-auth.
- **"card does not advertise …":** this agent genuinely lacks that
  capability (fail closed). `op='discover'` shows what it has; say so.
- **IdempotencyConflict:** same key, different inputs. Reuse the key
  with the original inputs, or ask the user before minting a new run.

## Channel sessions

Over Slack/SMS, reads and HITL answers work normally; an effectful
start becomes an origin-bound approval in the thread ("approve N" /
"deny N"). Say it is pending and continue with reads — never retry the
start.
