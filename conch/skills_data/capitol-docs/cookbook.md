# capitol-docs — cookbook

Worked recipes. Commands are verbatim; expected output is what the
ENG-5630 initial compile actually printed.

## Answer: the question battery

The shapes users actually ask, and the call that answers each:

| Question | Call | Then |
|---|---|---|
| "How do I make an eval?" | `how_do_i {"task": "create an eval"}` | `get_doc` the top `howto`; relay the numbered steps |
| "What is a pass/fail eval vs a quantitative check?" | `explain_concept {"term": "pass/fail eval"}` and `{"term": "quantitative check"}` | compare from both docs; cite both ids |
| "How do I add a guardrail?" | `how_do_i {"task": "add a guardrail"}` | |
| "What is a data collection? Which kinds exist?" | `explain_concept {"term": "data collection"}`; `search_capitol_docs {"query": "collection kinds", "domain": "collections"}` | |
| "Which tools must an agent have to read a collection?" | `search_capitol_docs {"query": "tools to read a collection", "domain": "collections"}` | the concept `collections.concept.reading-collections-from-agents` |
| "What changed in the last release?" | `whats_new {}` | the newest `release-note`; `since_version` for a range |
| "What does the `agent_node` `model` param accept?" | `get_node_reference {"node_type": "agent_node"}` | generated, version-true |
| "What can the platform do?" | `list_capabilities {}` | one line per domain |

Answer template:

```
<answer in the user's words, steps numbered when it is a how-to>
Sources: <id>, <id> (corpus <corpus_version>)
```

If a result's `status` is `draft`, say "draft doc". If the body contains
"Not verified live", repeat it.

## Compile: one domain end to end (as run for ENG-5630)

```
REPO=$HOME/composer/capitol-docs; cd "$REPO"
git remote get-url origin                      # git@github.com:Faction-V/capitol-docs.git
git status --porcelain                         # (empty)
cat reference/extracts/CURRENT                 # 2026-10-01-release-all-prod
python3 gates/run_gates.py
#   PASS  schema (… docs)
#   PASS  reference-integrity
#   WARN  coverage …           (warn-only)
#   PASS  removal
cat domains/guardrails/domain.yaml             # owned: guardrails_node, pdf_redaction_node, text_redaction_node; route GUARDRAILS.ROOT
python3 - <<'EOF'
import json; d = json.load(open("reference/extracts/2026-10-01-release-all-prod/agentic-backend.json"))
print(json.dumps(d["nodes"]["guardrails_node"], indent=1)[:3000])
EOF
```

Source checkouts at the pins (read-only worktrees; the tags come from
the release JSON):

```
git -C ~/composer/agentic-backend     worktree add --detach /tmp/capitol-extract/agentic-backend-v4.6.0 v4.6.0
git -C ~/composer/mcp-servers-runner  worktree add --detach /tmp/capitol-extract/mcp-servers-runner-v3.14.7 v3.14.7
git -C ~/composer/platform-frontend   worktree add --detach /tmp/capitol-extract/platform-frontend-v1.81.0 v1.81.0
git -C ~/composer/platform-api        worktree add --detach /tmp/capitol-extract/platform-api-1.78.0 1.78.0
rg -n 'node_id = "guardrails_node"' /tmp/capitol-extract/agentic-backend-v4.6.0/src   # → nodes/governance/guardrails/node.py
```

Live surface (unauthenticated, read-only; dev runs the release pins):

```
curl -s https://agentic-backend-development.capitol.ai/openapi.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["info"]["version"]);[print(m.upper(),p) for p,o in d["paths"].items() for m in o if "guardrail" in p]'
curl -s https://platform-api-development.capitol.ai/public/openapi.json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["info"]["version"])'
```

The frontend is behind Auth0: UI labels come from
`platform-frontend@<tag> src/pages/<area>/…` and are cited as "UI labels
from platform-frontend@v1.81.0", not as "verified live".

Draft from the templates beside this file:

```
cp "<skill assets dir>/templates/howto.md" domains/guardrails/howtos/add-a-guardrail.md
# fill every REPLACE_ marker; delete the HTML comments; keep ## Sources last
```

Gate, report, PR:

```
python3 gates/run_gates.py                              # 3× PASS
python3 gates/coverage_report.py --domain guardrails    # writes domains/guardrails/COVERAGE.md
git checkout -b docs/guardrails-2026-10-01-release-all-prod
git add domains/guardrails && git commit -m "ENG-5630: docs(guardrails) compile @ 2026-10-01-release-all-prod"
git push -u origin HEAD
gh pr create --repo Faction-V/capitol-docs --title "ENG-5630: docs(guardrails) compile @ 2026-10-01-release-all-prod" --body-file /tmp/pr-body.md
```

Secret scan before the commit (the configs you read contain bearers):

```
git diff --cached | rg -n "cap_a2a_[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.|sk-[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{20,}"   # → no output
```

## Extract: a release that has no extracts yet

```
R=2026-10-01-release-all-prod
git -C ~/composer/gofigure_terraform show origin/main:releases/archive/$R.json | python3 -c '
import json,sys; d=json.load(sys.stdin)
for s,v in d["services"].items(): print(s, v.get("tags",{}).get("image_tag",{}).get("tag"))'
#   agentic-backend v4.6.0 / mcp-servers-runner v3.14.7 / platform-frontend v1.81.0 / platform-api 1.78.0 …
ABPY=$(cd /tmp/capitol-extract/agentic-backend-v4.6.0 && poetry env info --path)/bin/python
MSRPY=$(cd /tmp/capitol-extract/mcp-servers-runner-v3.14.7 && poetry env info --path)/bin/python
python3 -m compiler extract agentic-backend    --checkout /tmp/capitol-extract/agentic-backend-v4.6.0    --python $ABPY  --tag v4.6.0  --release $R
python3 -m compiler extract mcp-servers-runner --checkout /tmp/capitol-extract/mcp-servers-runner-v3.14.7 --python $MSRPY --tag v3.14.7 --release $R
python3 -m compiler extract platform-frontend  --checkout /tmp/capitol-extract/platform-frontend-v1.81.0  --tag v1.81.0 --release $R
ls reference/extracts/$R      # agentic-backend.json  mcp-servers-runner.json  platform-frontend.json
```

Expected sizes at that release: 37 nodes, 87 tools / 24 servers, 63
routes. A backend extract with ~30 nodes means modules failed to import
(the probe needs the repo root on `PYTHONPATH`; the compiler sets it —
check `poetry install` ran at that tag).

## Release: `2026-09-28-release-all-prod → 2026-10-01-release-all-prod`

```
python3 -m compiler diff --from 2026-09-28-release-all-prod --to 2026-10-01-release-all-prod --out /tmp/delta.md
# nodes: +wordpress_events_node; changed docx_chat_node/pptx_chat_node/ey_pptx_chat_node/xlsx_chat_node (+ai_disclaimer param),
#        human_intervention_node (timeout info), agent_node (model options) …
# tools: none ; routes: +ACCOUNT.CONNECTORS /account/connectors
```

Then `domains/ops/release-notes/2026-10-01-release-all-prod.md`
(`ops.release-note.2026-10-01-release-all-prod`,
`release_version: 2026-10-01-release-all-prod`) with one bullet per
delta line, the affected docs updated (`workflows.howto.generate-office-
documents` gains the AI-disclaimer option, `connections.*` gains
`/account/connectors`), `CURRENT` moved, gates green, build:

```
python3 -m compiler build --release-name 2026-10-01-release-all-prod
python3 -c 'import json;print(json.load(open("dist/2026-10-01-release-all-prod/manifest.json"))["corpus_version"])'
```

## Verify a built corpus through conch (the product path)

```
cd <msr>/src && DOCS_CORPUS_PATH=$REPO/dist/<release> ENVIRONMENT=test poetry run fastmcp run servers/docs_server.py --transport streamable-http --host 127.0.0.1 --port 8042 &
# conch config: capitol_docs_url=http://127.0.0.1:8042/mcp   then /reload, /tools → search_capitol_docs …
/skill capitol-docs
how do I make an eval?
```

The answer must end with `Sources: … (corpus <release>)`. Run the
battery above; every row without a hit is a compile gap to report.

## Troubleshooting

| Seen | Meaning | Fix |
|---|---|---|
| `FAIL  schema … additional properties` | a frontmatter key outside the schema | remove it (common: `tags`, `summary`) |
| `FAIL  schema … path dir 'faqs' vs type 'concept'` | file in the wrong type directory | move it |
| `FAIL  reference-integrity … unknown node 'json_input_node'` | id from a feature branch / local stack | it is not in the release; drop it |
| `WARN  coverage: nodes never referenced: …` | shipped surfaces without docs | compile those domains next; never pad |
| `ExtractsNotFound: no extracts for release X` | `CURRENT`/`--release` names a missing dir | § Extract |
| docs server `Bad Request: Missing session ID` | a client without the Streamable HTTP handshake | conch ≥ this skill's release does it; other clients must `initialize` first |
