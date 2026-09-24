# capitol cookbook — worked recipes for this machine

Recipes against the assets actually deployed here: the local Capitol
stack (workflow-api on `:8300`, org/agent pinned in the conch config),
the **together-funding** pipeline (`together-funding-ingest` on a
5-minute schedule, `together-funding-packet` launched per funding
match, ledger collection `together-funding-requests`), and the
**ebay-listing** flow pack. Every recipe is a `capitol_control` call
sequence; ids are always discovered fresh, never copied from here.

## Recipe 1 — funding ingest backfill over a date window

*"Backfill funding intake for the first week of September."*

1. Find the workflow — never assume the id:

```json
{"op": "workflows"}
```

Pick the row whose name is `together-funding-ingest`. If it is not
there, this agent doesn't run funding intake — say so and stop.

2. Check the input contract:

```json
{"op": "describe", "workflow_id": "<ingest-id-from-step-1>"}
```

The single text input ("Window Override") accepts three shapes:
`incremental` (the scheduled default — do not backfill with this), a
Gmail date window like `after:2026/09/01 before:2026/09/08`, or a JSON
array of synthetic email objects (the acceptance seam). Backfills use
the date window.

3. Start it keyed. `input_value` wraps the window under the discovered
   input key; the derived key makes a retried backfill replay instead
   of double-ingesting:

```json
{"op": "start", "workflow_id": "<ingest-id>",
 "input_value": "after:2026/09/01 before:2026/09/08"}
```

Report the printed run id and idempotency key to the user.

4. Watch bounded, then report (recipe 2). Triage launches
   `together-funding-packet` runs for matches — their dedupe key is
   `together:funding:{gmail_message_id}`, so re-running the same window
   never duplicates packets (`replayed: true` = the packet already
   exists = success).

## Recipe 2 — watch a run and summarize it

*"How's that run doing?"*

```json
{"op": "watch", "run_id": "<run-id>", "deadline_seconds": 120}
```

The summary carries: terminal state (or "still running at the
deadline"), event counts by type, the last sequence, recent node lines,
and any `NEEDS INPUT` checkpoint verbatim. Then:

- terminal `success` → `{"op": "outputs", "run_id": …}` and report the
  facts from the outputs, not from memory;
- terminal `failed` → the summary includes the error message; report it
  and stop (do not auto-restart an effectful run);
- still running → tell the user, and resume later with
  `{"op": "watch", "run_id": …, "since_sequence": <printed cursor>}` —
  a cheap `{"op": "status", …}` also works between watches.

## Recipe 3 — answer a clarification

A watch summary shows:

```
NEEDS INPUT [clarification] request_id=req-42 node=Research Agent: Which fund vintage should the packet target?
```

1. Relay that question to the user **verbatim**. Do not answer it
   yourself, even when the answer seems obvious.
2. Deliver their answer:

```json
{"op": "respond", "run_id": "<run-id>", "request_id": "req-42",
 "response": "<the user's words>"}
```

Declines: `{"op": "respond", …, "decline": true}`. Continue/stop panels
are the *other* kind — `{"op": "respond", …, "kind": "intervention",
"response": "continue"}` with the literal token only.

## Recipe 4 — fetch a docx artifact from a packet run

*"Get me the funding packet document from that run."*

1. `{"op": "outputs", "run_id": "<packet-run-id>"}` — packet runs put
   the generated docx in their outputs; find the file entry (filename
   `*.docx`) and its file/artifact id.
2. Download it into the quarantine dir (the only place the tool
   writes):

```json
{"op": "download", "file_id": "<file-id>", "filename": "packet.docx"}
```

3. Verify before trusting: the result prints the sha256 digest —
   compare it against the producer's recorded digest when one exists,
   and check the bytes are a real docx (`PK` zip magic) with the shell:

```bash
head -c 2 "<printed path>"   # expect: PK
```

4. Tell the user the quarantine path; move/copy it only if they ask.

## Recipe 5 — an eBay draft via the pack intake

*"List this lamp on eBay."*

The eBay flow is a **flow pack** (`ebay-listing`) with its own governed
pipeline: caps-clamped publish gate, exact-approval challenge, byte-
pinned publish request. Intake belongs to the pack, not to raw
workflow starts:

- The user runs `/ebay <photo.jpg> [-- notes]` in the shell, or posts
  the photos to the bound Slack channel — the thread then carries the
  whole session (clarifications, the publish approval).
- Your role through `capitol_control` is **observation and relay**:
  `runs`/`status`/`watch` on the draft and publish workflows, relaying
  clarification questions, reporting the drafted revision and the
  publish effect.
- Do not start the pack's publish workflow directly: publishing goes
  through the pack's `ebay_publish` approval (challenge
  `POST r{rev} {hash[-12:]}`) so the user approves exact bytes. A raw
  `start` would bypass nothing — Capitol's approval gate re-verifies —
  but it wastes a run and confuses the session; point the user at the
  pack instead.

## Recipe 6 — inspect or adopt an existing Procedure

*"Show me the exact SOP for the daily snapshot, then bring it under Conch
review."*

1. Discover without writes:

```json
{"op": "procedure_search", "query": "daily snapshot", "limit": 10}
```

2. Confirm the workflow id and exact version, then read it:

```json
{"op": "procedure_show", "workflow_id": "<workflow-id>",
 "version_number": 2}
```

The returned Markdown is bounded inert documentation. Its reviewed/accredited
status does not authorize any infrastructure action.

3. The user—not the model—starts the review flow in the shell:

```text
/compile from-procedure <workflow-id> --version 2
```

That command separately fetches the exact workflow payload, records immutable
digests/provenance, and creates a normal draft Architecture Card. It never
auto-approves and never treats Procedure prose as executable semantics.
