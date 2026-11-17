/**
 * Reads the server-injected JSON island and renders it. Replace the
 * generic key/value rendering with the real page design; keep reading
 * data from the island only (no fetch from the browser).
 */
const island = document.getElementById("app-data");
let doc = {};
try {
  doc = JSON.parse(island.textContent || "{}");
} catch {
  doc = {};
}

const text = (id, value) => {
  const node = document.getElementById(id);
  if (node) node.textContent = value ?? "";
};

text("doc-title", doc.title || doc.name || doc.id || "No data yet");
text("doc-summary", doc.summary || doc.description || "");
text("page-updated", doc.updated_at || doc.last_seen_at ? `Updated ${doc.updated_at || doc.last_seen_at}` : "");

const fields = document.getElementById("doc-fields");
const skip = new Set(["title", "name", "summary", "description", "updated_at", "last_seen_at"]);
for (const [key, value] of Object.entries(doc)) {
  if (skip.has(key) || value === null || value === undefined || value === "") continue;
  const dt = document.createElement("dt");
  dt.textContent = key;
  const dd = document.createElement("dd");
  dd.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);
  fields.append(dt, dd);
}
