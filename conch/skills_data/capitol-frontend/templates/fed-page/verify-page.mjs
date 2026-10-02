#!/usr/bin/env node
/**
 * Proves the served page carries the live document: fetches the page,
 * parses the app-data island, and reports which source the server used.
 *
 *   node verify-page.mjs [http://localhost:3000/] [--expect-source live]
 *
 * Exit 0 when the island parses (and matches --expect-source when given),
 * 1 otherwise. Run `node serve.mjs` first (with the FILESTORE_* env set).
 */
const args = process.argv.slice(2);
let url = "http://localhost:3000/";
let expectSource = null;
for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--expect-source") expectSource = args[++i];
  else url = args[i];
}

const response = await fetch(url);
const html = await response.text();
const source = response.headers.get("x-data-source") || "(no X-Data-Source header)";
const match = html.match(/<script id="app-data" type="application\/json">([\s\S]*?)<\/script>/);
if (!match) {
  console.error(`verify: FAIL — no app-data island in ${url}`);
  process.exit(1);
}
let doc;
try {
  doc = JSON.parse(match[1].replaceAll("<\\/", "</"));
} catch (err) {
  console.error(`verify: FAIL — island is not valid JSON: ${err.message}`);
  process.exit(1);
}
console.log(`status: ${response.status}`);
console.log(`source: ${source}`);
console.log(`island: id=${doc.id ?? "(none)"} keys=${Object.keys(doc).length} title=${JSON.stringify(doc.title ?? doc.name ?? null)}`);
if (expectSource && source !== expectSource) {
  console.error(`verify: FAIL — expected X-Data-Source=${expectSource}, got ${source}`);
  process.exit(1);
}
console.log("verify: PASS");
