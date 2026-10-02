#!/usr/bin/env node
/**
 * Proves the BFF gates and serves: anonymous /api/feed → 401, a
 * non-allowlisted dev sign-in → 403, an allowlisted dev sign-in → session
 * cookie, /api/feed → records with a source, /api/auth/me reflects the
 * email, logout clears the session.
 *
 *   node verify-portal.mjs [http://localhost:3000] --email you@example.com [--deny other@example.org]
 *
 * Requires the server to run with ALLOW_DEV_LOGIN=1 (local only). Exit 0
 * on PASS, 1 on any failed check.
 */
const args = process.argv.slice(2);
let base = "http://localhost:3000";
let email = null;
let deny = "nobody@invalid.example";
for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--email") email = args[++i];
  else if (args[i] === "--deny") deny = args[++i];
  else base = args[i].replace(/\/+$/, "");
}
if (!email) {
  console.error("usage: node verify-portal.mjs [base-url] --email <allowlisted email>");
  process.exit(2);
}

let failures = 0;
const check = (label, ok, detail = "") => {
  console.log(`${ok ? "ok  " : "FAIL"} ${label}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures += 1;
};
const post = (path, body, cookie = "") =>
  fetch(`${base}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(cookie ? { Cookie: cookie } : {}) },
    body: JSON.stringify(body),
  });

const anon = await fetch(`${base}/api/feed`);
check("anonymous /api/feed is refused", anon.status === 401, `status ${anon.status}`);

const denied = await post("/api/auth/login", { dev_email: deny });
check("non-allowlisted sign-in is refused", denied.status === 403, `status ${denied.status}`);

const login = await post("/api/auth/login", { dev_email: email });
const cookie = (login.headers.get("set-cookie") || "").split(";")[0];
check("allowlisted dev sign-in issues a session", login.status === 200 && cookie.startsWith("portal_session="), `status ${login.status}`);

const me = await (await fetch(`${base}/api/auth/me`, { headers: { Cookie: cookie } })).json();
check("/api/auth/me reflects the signed-in email", me.signed_in === true && me.email === email, JSON.stringify(me));

const feedResponse = await fetch(`${base}/api/feed`, { headers: { Cookie: cookie } });
const feed = await feedResponse.json().catch(() => ({}));
check("/api/feed answers with records", feedResponse.status === 200 && Array.isArray(feed.records), `status ${feedResponse.status} ${feed.error || ""} ${feed.message || ""}`);
if (Array.isArray(feed.records)) console.log(`     source=${feed.source} records=${feed.records.length} stale=${feed.stale}`);

const logout = await post("/api/auth/logout", {}, cookie);
check("logout clears the session", logout.status === 200 && /Max-Age=0/.test(logout.headers.get("set-cookie") || ""));

console.log(failures ? `verify: FAIL (${failures})` : "verify: PASS");
process.exit(failures ? 1 : 0);
