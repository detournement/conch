# Workflow-fed page (filestore data island)

A static page whose `<script id="app-data" type="application/json">`
island is filled at serve time with the latest document a Capitol
workflow published to the filestore. The org token stays server-side.
If the live read fails or the document is unusable, the last-known-good
document is served, then the baked `data/baseline.json`; the next request
retries. The response header `X-Data-Source` says which one was used
(`live`, `cached`, `last-known-good`, `baseline`).

## Environment (server-side only)

| Variable | Meaning |
|---|---|
| `FILESTORE_BASE` | facade origin, e.g. `http://127.0.0.1:19700` |
| `FILESTORE_ORG_TOKEN` | org-scoped bearer (sensitive) |
| `FILESTORE_ORG_ID` | org uuid |
| `FILESTORE_REPO` | repo name |
| `DOC_PATH` | document path inside the repo (plain, e.g. `feed/latest.json`) |
| `EXPECTED_ID` | optional — the document's `id` must equal this to be trusted |
| `EXPECTED_SCHEMA` | optional — the document's `schema` must equal this to be trusted |

## Local run and verification (Node 20+)

```
curl -sf -H "Authorization: Bearer $FILESTORE_ORG_TOKEN" \
  "$FILESTORE_BASE/v1/orgs/$FILESTORE_ORG_ID/repos/$FILESTORE_REPO/files/<url-encoded DOC_PATH>" > data/baseline.json
node serve.mjs 4320 &
node verify-page.mjs http://localhost:4320/ --expect-source live     # → "verify: PASS"
```

Fallback drill: restart with `FILESTORE_ORG_TOKEN=bad` and run
`node verify-page.mjs http://localhost:4320/ --expect-source baseline`.

## Deploy

`vercel --prod` with the same environment variables set in the project.
`vercel.json` routes `/` and `/index.html` to `api/index.js` and bundles
`template/index.html` + `data/baseline.json` with the function.
