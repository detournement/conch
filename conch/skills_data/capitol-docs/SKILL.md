---
name: capitol-docs
description: Answer Capitol platform questions from the release-true docs corpus and compile that corpus. Use when the user asks what something on Capitol is or how to do it (evals, pass/fail vs quantitative-check, guardrails, data collections and which tools read them, agents, connections, artifacts, triggers, schedules, HITL, API keys, A2A, org settings, releases, "what changed"); or asks to compile, update, re-crawl, or release the platform docs for a domain or a release (ENG-5630). Covers answer → cite doc ids + corpus_version; compile <domain> → crawl code at the release pins + live surface → draft → gates → coverage → PR; release <from> <to> → extract diff → what's-new + affected docs → removal gate → PR.
tools: local_shell, search_capitol_docs, how_do_i, explain_concept, get_doc, whats_new
rounds: 40
---

# Capitol docs: answer from the corpus, compile the corpus

Two jobs, one source of truth: the `Faction-V/capitol-docs` corpus, built
per release and served by the Capitol docs MCP server (`capitol-docs`,
tools `search_capitol_docs`, `how_do_i`, `explain_concept`, `get_doc`,
`whats_new`, node/tool reference). `reference.md` has the tool and repo
contracts; `cookbook.md` the worked recipes; `templates/` the doc
skeletons (copy, never retype).

## A. Answering a platform question

1. Pick the tool by question shape: "what is X" → `explain_concept
   {"term": "X"}`; "how do I X" → `how_do_i {"task": "X"}` then
   `get_doc {"id_or_slug": "<id>"}` for the full steps; anything else →
   `search_capitol_docs {"query": "...", "limit": 5}`; "what changed /
   since" → `whats_new {"since_version": "..."}`.
2. Read `corpus_version` and `error` in every response. `error` set or
   `corpus not loaded` → say so verbatim and stop; do not fill in from
   memory.
3. Answer from the doc text. End with: `Sources: <doc id>, <doc id>
   (corpus <corpus_version>)`. A doc marked "Not verified live" is
   repeated as such.
4. No hit after one rephrase → say the corpus has no doc for it and
   name the nearest domain from `list_capabilities`; offer `compile`.
5. Docs say how; the live catalog says what exists *now* for this org
   (`capitol_control op='workflows'`, workflow-builder
   `list_workflow_nodes`). When they disagree, report both and prefer
   the live catalog for existence, the docs for usage.

Docs tools absent from the session (`/tools`)? Say: set
`capitol_docs_url=<docs server /mcp URL>` in the conch config (or an
`mcp.json` entry named `capitol-docs`), then `/reload`. Stop there.

## B. `compile <domain>` — the release-tied domain compile

Preflight (fail closed; every line from real output):

```
REPO=${CAPITOL_DOCS_REPO:-$HOME/composer/capitol-docs}
git -C "$REPO" remote get-url origin        # must contain Faction-V/capitol-docs
git -C "$REPO" status --porcelain           # must be empty
cat "$REPO/reference/extracts/CURRENT"      # the release you document, e.g. 2026-10-01-release-all-prod
ls "$REPO/reference/extracts/$(cat $REPO/reference/extracts/CURRENT)"   # agentic-backend.json mcp-servers-runner.json platform-frontend.json
python3 "$REPO/gates/run_gates.py"          # PASS schema, reference-integrity, removal
```

Missing extracts → run the extract recipe (`cookbook.md` § Extract)
first; a dirty tree or a non-Faction-V remote → stop and say so.

1. **Scope.** `cat domains/<domain>/domain.yaml` → owned nodes, servers,
   tools, routes. `cat domains/<domain>/COVERAGE.md` → what is already
   covered. `AUTHORING.md` is the writing contract: read it.
2. **Crawl, do not recall.** For each owned node: the extract entry
   (`params[].info` are the UI help texts) + its `node.py`/`params.py`
   in the backend checkout at the pinned tag; each owned server:
   `src/servers/<x>_server.py` in mcp-servers-runner at its tag; each
   owned route: the page under `src/pages/` in platform-frontend at its
   tag (labels, dialogs, flags); the live API: `GET
   https://<service>-<env>.capitol.ai/openapi.json`. Keep a (file → fact)
   list; it becomes each doc's `## Sources`.
3. **Draft** into `domains/<domain>/<typedir>/<slug>.md` from
   `templates/<type>.md`: id `<domain>.<type>.<slug>`, `status: draft`,
   `owner: TBD`, `applies_to` = the pins, `refs` only to ids that exist,
   2–6 unique `aliases`, `## Sources` last. Anything you could not see
   running: write `Not verified live; documented from code at
   <repo>@<tag>.` Nothing invented: no field, label, enum or behaviour
   without a source line.
4. **Gate.** `python3 gates/run_gates.py` → three `PASS` lines;
   `python3 gates/coverage_report.py --domain <domain>` → updates
   `COVERAGE.md`; `python3 -m pytest -q` when the venv exists.
5. **PR.** Branch `docs/<domain>-<release>`, commit, `gh pr create
   --repo Faction-V/capitol-docs --title "<ticket>: docs(<domain>)
   compile @ <release>"` with the coverage summary in the body. Never
   push to `main`; never merge.

## C. `release <from> <to>` — the release delta

```
ls reference/extracts/<from> reference/extracts/<to>      # both present, else extract (cookbook § Extract)
python3 -m compiler diff --from <from> --to <to> --out /tmp/delta-<to>.md
python3 -m compiler diff --from <from> --to <to> --json > /tmp/delta-<to>.json
```

1. Write `domains/ops/release-notes/<to>.md` as `ops.release-note.<to>`
   (`release_version: <to>`) from the delta: added/removed/changed
   nodes, tools, routes — each line traceable to the diff.
2. For every "docs referencing" entry under changed/removed items, open
   the doc, update the fact, bump `applies_to`; removed surfaces lose
   their `refs`. New surfaces without a doc → list them in the PR body
   as follow-up compiles, do not pad.
3. `echo <to> > reference/extracts/CURRENT`; `python3
   gates/run_gates.py` (the removal gate now proves no doc references a
   vanished id); `python3 -m compiler build --release-name <to>` →
   `dist/<to>/manifest.json` shows `corpus_version: <to>`.
4. PR titled `<ticket>: docs release <from> → <to>`.

## Finish with the checklist

```
capitol-docs checklist
- mode: answer | compile <domain> | release <from>→<to>
- corpus_version / release: <value from output>
- sources crawled: <n files, n live endpoints> | n/a
- docs written/updated: <ids>  | cited: <ids>
- not verified live: <n docs>
- gates: <PASS/FAIL lines>   coverage: <summary line>
- PR: <url> | none
```

## Hard rules

- **No invented platform facts.** Every claim in an answer carries a
  doc id; every claim in a doc carries a source line. "I believe" is
  not a source.
- **Version-true.** Say the corpus/release the answer or doc is for;
  never blend releases. Local experimental stacks are not sources.
- **Read-only against live systems.** GETs only; a how-to is confirmed
  by reading the implementation, not by creating things in prod. If a
  test artifact is unavoidable, name it `docs-compile-test-<date>` and
  delete it.
- **Secrets never land in docs or output.** Tokens, bearers, env values:
  by reference only. Scan the diff before committing.
- **PRs only; Faction-V only.** Branch from the current base, never
  push `main`/`develop`, never force-push, never merge.
- **Use the tools you were given.** Missing tool → say which and how to
  enable it; do not curl the docs server or read conch's source.

## Failure modes

| Symptom | Cause | Do this |
|---|---|---|
| `error: corpus not loaded` | server not bound to a corpus | report it; the operator sets `DOCS_CORPUS_PATH` on the docs server |
| no docs tools in `/tools` | `capitol_docs_url` unset | user sets it (+ `/reload`); stop |
| `reference-integrity FAIL unknown node/tool` | id not in the release extracts | fix the id from the extract or drop the ref — never add it to the extract by hand |
| `removal FAIL` after `release` | a doc still references a removed surface | edit that doc (step C.2) |
| `ExtractsNotFound` | release has no extracts | cookbook § Extract at the pinned tags |
| docs and live catalog disagree | org enablement or newer release | report both; prefer live for existence |
