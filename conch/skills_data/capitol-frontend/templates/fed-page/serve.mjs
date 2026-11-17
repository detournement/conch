#!/usr/bin/env node
/**
 * Local stand-in for the Vercel runtime: mounts api/index.js at "/" and
 * serves public/ as static files. Node 20+.
 *
 *   FILESTORE_BASE=... FILESTORE_ORG_TOKEN=... FILESTORE_ORG_ID=... \
 *   FILESTORE_REPO=... DOC_PATH=... node serve.mjs [port]
 */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

const port = Number(process.argv[2] || process.env.PORT || 4320);
const { default: handler } = await import("./api/index.js");
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".html": "text/html" };

createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname === "/" || url.pathname === "/index.html") return handler(req, res);
  const file = join(process.cwd(), "public", normalize(url.pathname).replace(/^(\.\.[/\\])+/, ""));
  try {
    const body = await readFile(file);
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end("not found");
  }
}).listen(port, () => console.log(`fed-page: http://localhost:${port}/`));
