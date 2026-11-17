/* Workflow-fed page — serve-time data injection.
 *
 * The page HTML (template/index.html) is served byte-identical except for
 * the content of the <script id="app-data" type="application/json">
 * island, which is swapped for the latest document in the Capitol
 * filestore facade. Read-only: the org token stays server-side and is
 * never in the page. If the live read fails or the document is unusable,
 * the last-known-good document is served, falling back to the baked-in
 * baseline; the next request retries.
 *
 * Environment (server-side only):
 *   FILESTORE_BASE        facade origin, e.g. http://127.0.0.1:19700
 *   FILESTORE_ORG_TOKEN   org-scoped bearer (sensitive)
 *   FILESTORE_ORG_ID      org uuid
 *   FILESTORE_REPO        repo name
 *   DOC_PATH              document path inside the repo (URL-encoded per segment here)
 *   EXPECTED_ID           optional: the document's `id` must equal this to be trusted
 *   EXPECTED_SCHEMA       optional: the document's `schema` must equal this to be trusted
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ISLAND_OPEN = '<script id="app-data" type="application/json">';
const ISLAND_CLOSE = "</script>";
const CACHE_TTL_MS = 15_000;
const FETCH_TIMEOUT_MS = 10_000;

function loadAsset(relative) {
  return readFileSync(join(process.cwd(), relative));
}

const template = loadAsset("template/index.html").toString("utf8");
const openAt = template.indexOf(ISLAND_OPEN);
if (openAt < 0) throw new Error("template/index.html is missing the app-data island");
const contentStart = openAt + ISLAND_OPEN.length;
const contentEnd = template.indexOf(ISLAND_CLOSE, contentStart);
if (contentEnd < 0) throw new Error("template island not closed");
const prefix = Buffer.from(template.slice(0, contentStart), "utf8");
const suffix = Buffer.from(template.slice(contentEnd), "utf8");

const baseline = loadAsset("data/baseline.json").toString("utf8");
if (!usable(baseline)) throw new Error("data/baseline.json does not pass the usable() check");

let cache = { text: null, fetchedAt: 0, source: "baseline" };
let lastGood = { text: baseline, source: "baseline" };

export function usable(text) {
  if (typeof text !== "string" || !text.trim()) return false;
  try {
    const doc = JSON.parse(text);
    if (!doc || typeof doc !== "object") return false;
    const expectedId = process.env.EXPECTED_ID;
    const expectedSchema = process.env.EXPECTED_SCHEMA;
    if (expectedId && doc.id !== expectedId) return false;
    if (expectedSchema && doc.schema !== expectedSchema) return false;
    return true;
  } catch {
    return false;
  }
}

// "</" inside the island would end the script element early; "<\/" is the
// equivalent JSON escape, so this is content-preserving.
export function htmlSafe(text) {
  return text.includes("</") ? text.replaceAll("</", "<\\/") : text;
}

export function documentUrl(env = process.env) {
  const base = (env.FILESTORE_BASE || "").replace(/\/+$/, "");
  const org = env.FILESTORE_ORG_ID || "";
  const repo = env.FILESTORE_REPO || "";
  const docPath = env.DOC_PATH || "";
  if (!base || !org || !repo || !docPath) return null;
  const encodedPath = docPath.split("/").map(encodeURIComponent).join("/");
  return `${base}/v1/orgs/${org}/repos/${repo}/files/${encodedPath}`;
}

async function fetchLive() {
  const url = documentUrl();
  const token = process.env.FILESTORE_ORG_TOKEN || "";
  if (!url || !token) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    });
    if (!response.ok) return null;
    return await response.text();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function currentData() {
  const now = Date.now();
  if (cache.text && now - cache.fetchedAt < CACHE_TTL_MS) return { text: cache.text, source: "cached" };
  const live = await fetchLive();
  if (live !== null && usable(live)) {
    cache = { text: live, fetchedAt: now, source: "live" };
    lastGood = { text: live, source: "last-known-good" };
    return { text: live, source: "live" };
  }
  cache = { text: null, fetchedAt: 0, source: lastGood.source };
  return lastGood;
}

export default async function handler(req, res) {
  const { text, source } = await currentData();
  const body = Buffer.concat([prefix, Buffer.from(htmlSafe(text), "utf8"), suffix]);
  res.statusCode = 200;
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.setHeader("Cache-Control", "public, max-age=0, s-maxage=15, must-revalidate");
  res.setHeader("X-Data-Source", source);
  res.end(body);
}
