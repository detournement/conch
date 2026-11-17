# capitol-docs — reference

The contracts behind the procedure in `SKILL.md`: the docs MCP server's
tools and response shapes, the corpus repo layout, the frontmatter
schema, the gates, and the compiler commands. Observed against
`capitol-docs-server` (mcp-servers-runner, FastMCP 3.x) and the
`Faction-V/capitol-docs` repo at the ENG-5630 initial compile.

## 1. The docs MCP server

Mounted in conch as MCP server `capitol-docs` from the conch config key
`capitol_docs_url` (e.g. `http://localhost:8042/mcp`), or from an
`mcp.json` entry of the same name when headers are needed. Transport is
MCP Streamable HTTP: conch performs `initialize` → `mcp-session-id` →
`notifications/initialized` itself and renegotiates once if the server
restarts. Authentication type of the server is `none`.

Every tool returns a JSON object with `corpus_version` (the release the
corpus was built from, e.g. `2026-10-01-release-all-prod`) and `error`
(`null` when fine). `error` reads `corpus not loaded ...` when the
server has no `DOCS_CORPUS_PATH`; nothing else in that response is
meaningful.

| Tool | Arguments | Returns |
|---|---|---|
| `search_capitol_docs` | `query` (str), `domain?`, `doc_type?` (`concept|howto|reference|faq|runbook|release-note`), `limit?` (int), `version?` | `results[]`: `id`, `slug`, `title`, `type`, `domain`, `score`, `snippet`; `search_mode` (`keyword` today) |
| `get_doc` | `id_or_slug` (str), `version?` | the doc: `id`, `slug`, `title`, `type`, `domain`, `status`, `owner`, `aliases`, `applies_to`, `refs`, `content` (markdown body) |
| `explain_concept` | `term` (str), `version?` | the concept doc resolved through the alias table (exact, deterministic) or the best keyword hit; same shape as `get_doc` plus `matched_by` |
| `how_do_i` | `task` (str), `version?` | `results[]` of type `howto` (same shape as search) |
| `list_capabilities` | `domain?` | `domains{name: {summary, doc_counts{type: n}}}` |
| `list_node_reference` | `category?`, `version?` | generated node reference index (`node_type`, display name, category, description) — from the release extracts, not authored |
| `get_node_reference` | `node_type` (str), `version?` | full generated reference for one node: params with `info` texts, ports, usage notes |
| `list_tool_reference` | `server?`, `version?` | generated platform MCP tool index |
| `get_tool_reference` | `tool_name` (str), `version?` | one tool: description, input schema, hosting server |
| `list_workflow_patterns` | — | authoring guides (reference-type docs keyed by slug) |
| `get_guide` | `topic` (str), `version?` | one guide by slug |
| `whats_new` | `since_version?` | `release_notes[]` newest first, each a `release-note` doc with its `release_version` |
| `related_docs` | `doc_id` (str) | typed cross-references: nodes, guides, FAQs, release notes |

Argument names are exact: `how_do_i` takes `task` (not `question`),
`get_doc` takes `id_or_slug`, `explain_concept` takes `term`. A wrong
name comes back as a pydantic validation error in the tool text.

Citing: `Sources: evals.howto.create-an-eval, evals.concept.eval-types
(corpus 2026-10-01-release-all-prod)`. The id is the stable handle; the
title may change, the id does not.

## 2. The corpus repo (`Faction-V/capitol-docs`)

```
domains/<domain>/domain.yaml          summary + owned surfaces (nodes, servers, tools, routes)
domains/<domain>/COVERAGE.md          generated per-domain coverage report (checked in)
domains/<domain>/concepts/*.md        type concept
domains/<domain>/howtos/*.md          type howto
domains/<domain>/faqs/*.md            type faq
domains/<domain>/runbooks/*.md        type runbook
domains/<domain>/guides/*.md          type reference  (authoring patterns)
domains/<domain>/release-notes/*.md   type release-note (written by the release procedure)
reference/extracts/<release>/         agentic-backend.json, mcp-servers-runner.json, platform-frontend.json
reference/extracts/CURRENT            the release the gates and build default to
schema/frontmatter.schema.json        the frontmatter contract (additionalProperties: false)
gates/run_gates.py                    schema, reference-integrity, coverage (warn), removal
gates/coverage_report.py              writes domains/<d>/COVERAGE.md; --check in CI
compiler/{extract,diff,build,publish}.py
AUTHORING.md                          the writing contract (read before drafting)
```

Domains: `workflows evals guardrails collections agents connections
artifacts triggers apps admin ops`. Every shipped node has exactly one
owning domain (a test enforces it).

### Frontmatter (all keys below; nothing else is allowed)

```yaml
---
id: evals.howto.create-an-eval          # <domain>.<type>.<slug>, must match path + slug
slug: create-an-eval
type: howto                              # concept|howto|reference|faq|runbook|release-note
domain: evals
title: "Create an eval"
status: draft                            # draft|reviewed (compile writes draft)
owner: TBD                               # @handle or TBD
applies_to:                              # the release pins the doc was written against
  agentic-backend: "v4.6.0"
  platform-frontend: "v1.81.0"
refs:
  nodes: [evals_node]                    # exact node ids present in the release extracts
  tools: []                              # exact MCP tool names present in the extracts
  docs: [evals.concept.eval-types]       # existing doc ids
aliases: ["make an eval", "new eval"]    # 2–6 lower-case user phrasings, corpus-unique
release_version: 2026-10-01-release-all-prod   # release-note docs only
---
```

### Body conventions (from `AUTHORING.md`)

- H1 = title; the first paragraph answers the question.
- `howto`: numbered steps with exact UI labels or exact tool calls
  (tool name + real parameter names), then **Verify**.
- `concept`: definition → behaviour → when to use vs the alternatives.
- `faq`: **bold question** + short sourced answer, 8–15 pairs.
- `runbook`: symptoms → diagnosis → action → verification.
- `reference` (guides): a reusable authoring pattern.
- Cross-links by id: `[Eval types](evals.concept.eval-types)`.
- Last section is always `## Sources`: one line per file read as
  `<repo>@<tag> \`<path>\`` and per live surface as `Live: <host>
  <what> (<date>)`; plus the verbatim sentence `Not verified live;
  documented from code at <repo>@<tag>.` when it applies.

## 3. Gates (`python3 gates/run_gates.py [--release R]`)

| Gate | Blocking | Checks |
|---|---|---|
| schema | yes | frontmatter validates; path dir ↔ `domain`/`type`; id ↔ path |
| reference-integrity | yes | `refs.nodes`/`refs.tools` exist in the release extracts; `refs.docs` exist |
| coverage | warn | every shipped node/tool is referenced by at least one doc |
| removal | yes | no doc references a surface removed from the current extracts |

Extracts are resolved explicit `--release` → `$DOCS_EXTRACTS_RELEASE` →
`reference/extracts/CURRENT` → dev-sample. The gate prints `NOTE
extracts: reference/extracts/<release>/ (blocking)`.

`python3 gates/coverage_report.py --domain <d>` rewrites that domain's
`COVERAGE.md` (docs per type/status, "not verified live" count, owned
nodes/tools referenced yes/elsewhere/NO, routes mentioned). `--check`
fails when a committed report is stale (CI runs it).

## 4. Compiler commands

```
python3 -m compiler extract agentic-backend     --checkout <path@tag> --python <poetry venv python> --tag vX.Y.Z --release <release>
python3 -m compiler extract mcp-servers-runner  --checkout <path@tag> --python <poetry venv python> --tag vX.Y.Z --release <release>
python3 -m compiler extract platform-frontend   --checkout <path@tag> --tag vX.Y.Z --release <release>
python3 -m compiler diff  --from <release A> --to <release B> [--json] [--out FILE]
python3 -m compiler build --release-name <release> [--extracts-release <release>]   → dist/<release>/{manifest.json,registry.json,docs/}
python3 -m compiler publish --release-name <release> [--dry-run]                     → S3 docs-corpus/<release>/ + Qdrant (config-gated)
```

`extract` runs a discovery probe inside the service's own virtualenv
(backend node registry; MSR `list_tools()`), so the checkout must be
installable (`poetry install`) at the tag. It records `{repo, tag,
commit}` provenance and never the local path.

The release name is the gofigure_terraform release file name
(`releases/archive/<name>.json`); its `services.<svc>.tags.image_tag.tag`
values are the pins to check out.

## 5. Serving a built corpus locally (verification)

From an mcp-servers-runner checkout containing `src/servers/docs_server.py`:

```
cd <msr>/src && DOCS_CORPUS_PATH=<capitol-docs>/dist/<release> ENVIRONMENT=test \
  poetry run fastmcp run servers/docs_server.py --transport streamable-http --host 127.0.0.1 --port 8042
```

Then `capitol_docs_url=http://127.0.0.1:8042/mcp` in the conch config
and `/reload`; `/tools` lists the 13 docs tools.
