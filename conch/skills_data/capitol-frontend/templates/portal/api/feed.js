import { encodePath, facadeFetch, json, requireSession, withConfig } from "./_shared.js";

/**
 * GET /api/feed → { records, source, fetched_at, stale }
 *
 * Lists RECORDS_PREFIX in the filestore, reads every *.json record, and
 * returns them newest-first. A failed refresh serves the last-known-good
 * snapshot with `stale: true` instead of an error; the next request retries.
 */
const CACHE_TTL_MS = 30_000;
const MAX_RECORDS = 200;

let cache = { records: null, fetchedAt: 0 };
let lastGood = null;

async function loadRecords(cfg) {
  const tree = await (
    await facadeFetch(
      `/v1/orgs/${cfg.orgId}/repos/${cfg.repo}/tree?recursive=true&path=${encodeURIComponent(cfg.recordsPrefix)}`,
      cfg,
    )
  ).json();
  const paths = (tree.entries || [])
    .filter((entry) => entry.type === "file" && entry.path.endsWith(".json") && entry.path.startsWith(cfg.recordsPrefix))
    .map((entry) => entry.path)
    .slice(0, MAX_RECORDS);

  const records = [];
  for (const path of paths) {
    try {
      const doc = await (await facadeFetch(`/v1/orgs/${cfg.orgId}/repos/${cfg.repo}/files/${encodePath(path)}`, cfg)).json();
      if (doc && typeof doc === "object") records.push({ ...doc, _path: path });
    } catch {
      // One unreadable record must not take the feed down.
    }
  }
  records.sort((a, b) => String(b.updated_at || b.last_seen_at || "").localeCompare(String(a.updated_at || a.last_seen_at || "")));
  return records;
}

export default withConfig(async (req, res, cfg) => {
  if (!requireSession(req, res, cfg)) return;
  const now = Date.now();
  if (cache.records && now - cache.fetchedAt < CACHE_TTL_MS) {
    return json(res, 200, { records: cache.records, source: "cached", fetched_at: new Date(cache.fetchedAt).toISOString(), stale: false });
  }
  try {
    const records = await loadRecords(cfg);
    cache = { records, fetchedAt: now };
    lastGood = { records, fetchedAt: now };
    json(res, 200, { records, source: "live", fetched_at: new Date(now).toISOString(), stale: false });
  } catch (err) {
    cache = { records: null, fetchedAt: 0 };
    if (!lastGood) throw err;
    json(res, 200, { records: lastGood.records, source: "last-known-good", fetched_at: new Date(lastGood.fetchedAt).toISOString(), stale: true });
  }
});
