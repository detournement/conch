#!/usr/bin/env node
/**
 * Local stand-in for the Vercel runtime: routes /api/<path> to
 * api/<path>.js (default export handler) and serves public/ as static
 * files. Node 20+.
 *
 *   FILESTORE_BASE=... FILESTORE_ORG_TOKEN=... FILESTORE_ORG_ID=... FILESTORE_REPO=... \
 *   RECORDS_PREFIX=... SESSION_SECRET=... ALLOWLIST=... ALLOW_DEV_LOGIN=1 node serve.mjs [port]
 */
import { createServer } from "node:http";
import { readFile, access } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { pathToFileURL } from "node:url";

const port = Number(process.argv[2] || process.env.PORT || 4330);
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".html": "text/html" };
const handlers = new Map();

async function apiHandler(pathname) {
  const rel = normalize(pathname.replace(/^\/api\//, "")).replace(/^(\.\.[/\\])+/, "");
  if (rel.startsWith("_")) return null;
  if (!handlers.has(rel)) {
    const file = join(process.cwd(), "api", `${rel}.js`);
    try {
      await access(file);
      handlers.set(rel, (await import(pathToFileURL(file).href)).default);
    } catch {
      handlers.set(rel, null);
    }
  }
  return handlers.get(rel);
}

createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname.startsWith("/api/")) {
    const handler = await apiHandler(url.pathname);
    if (!handler) {
      res.writeHead(404, { "Content-Type": "application/json" });
      return res.end('{"error":"not_found"}');
    }
    return handler(req, res);
  }
  const rel = url.pathname === "/" ? "/index.html" : url.pathname;
  const file = join(process.cwd(), "public", normalize(rel).replace(/^(\.\.[/\\])+/, ""));
  try {
    const body = await readFile(file);
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end("not found");
  }
}).listen(port, () => console.log(`portal: http://localhost:${port}/`));
