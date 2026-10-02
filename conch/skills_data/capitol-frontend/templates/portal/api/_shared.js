/* Filestore portal — shared BFF helpers (server-side only).
 *
 * Environment (all required unless noted; the BFF refuses to start without them):
 *   FILESTORE_BASE          facade origin, e.g. http://127.0.0.1:19700
 *   FILESTORE_ORG_TOKEN     org-scoped bearer (sensitive, never sent to the browser)
 *   FILESTORE_ORG_ID        org uuid
 *   FILESTORE_REPO          repo name
 *   RECORDS_PREFIX          folder listed for records, e.g. gov-feed/records
 *   SESSION_SECRET          HMAC key for the session cookie (32+ random bytes)
 *   ALLOWLIST               comma-separated exact emails and *@domain entries
 *   GOOGLE_OAUTH_CLIENT_ID  Google Identity Services client id (optional only
 *                           while ALLOW_DEV_LOGIN is used locally)
 *   ALLOW_DEV_LOGIN         "1" lets POST /api/auth/login accept {dev_email}
 *                           WITHOUT Google — ignored when VERCEL_ENV=production
 */
import { createHmac, timingSafeEqual } from "node:crypto";

const COOKIE = "portal_session";
const SESSION_TTL_S = 12 * 60 * 60;
const FETCH_TIMEOUT_MS = 10_000;

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`BFF misconfigured: ${name} is not set`);
  return value;
}

export function config() {
  return {
    filestoreBase: required("FILESTORE_BASE").replace(/\/+$/, ""),
    filestoreToken: required("FILESTORE_ORG_TOKEN"),
    orgId: required("FILESTORE_ORG_ID"),
    repo: required("FILESTORE_REPO"),
    recordsPrefix: required("RECORDS_PREFIX").replace(/^\/+|\/+$/g, ""),
    sessionSecret: required("SESSION_SECRET"),
    allowlist: required("ALLOWLIST").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean),
    googleClientId: process.env.GOOGLE_OAUTH_CLIENT_ID || "",
    devLogin: process.env.ALLOW_DEV_LOGIN === "1" && process.env.VERCEL_ENV !== "production",
    production: process.env.VERCEL_ENV === "production",
  };
}

export function allowed(email, allowlist) {
  const value = String(email || "").toLowerCase();
  if (!value.includes("@")) return false;
  const domain = value.slice(value.indexOf("@") + 1);
  return allowlist.some((entry) => entry === value || (entry.startsWith("*@") && entry.slice(2) === domain));
}

// ---- session cookie -------------------------------------------------------

function sign(payload, secret) {
  return createHmac("sha256", secret).update(payload).digest("base64url");
}

export function issueSession(email, cfg) {
  const payload = Buffer.from(JSON.stringify({ email, exp: Math.floor(Date.now() / 1000) + SESSION_TTL_S })).toString("base64url");
  return `${payload}.${sign(payload, cfg.sessionSecret)}`;
}

export function readSession(req, cfg) {
  const cookies = Object.fromEntries(
    (req.headers.cookie || "")
      .split(";")
      .map((part) => part.trim().split("="))
      .filter((pair) => pair.length === 2),
  );
  const token = cookies[COOKIE];
  if (!token || !token.includes(".")) return null;
  const [payload, mac] = token.split(".");
  const expected = sign(payload, cfg.sessionSecret);
  if (mac.length !== expected.length || !timingSafeEqual(Buffer.from(mac), Buffer.from(expected))) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (!session.email || session.exp < Date.now() / 1000) return null;
    if (!allowed(session.email, cfg.allowlist)) return null; // allowlist edits revoke live sessions
    return session;
  } catch {
    return null;
  }
}

export function setSessionCookie(res, value, cfg, { clear = false } = {}) {
  const attrs = [`${COOKIE}=${clear ? "" : value}`, "Path=/", "HttpOnly", "SameSite=Lax"];
  if (cfg.production) attrs.push("Secure");
  attrs.push(clear ? "Max-Age=0" : `Max-Age=${SESSION_TTL_S}`);
  res.setHeader("Set-Cookie", attrs.join("; "));
}

export function requireSession(req, res, cfg) {
  const session = readSession(req, cfg);
  if (!session) {
    json(res, 401, { error: "sign_in_required" });
    return null;
  }
  return session;
}

// ---- Google ID token verification ------------------------------------------

export async function verifyGoogleIdToken(idToken, cfg) {
  if (!cfg.googleClientId) throw new Error("GOOGLE_OAUTH_CLIENT_ID is not set");
  const response = await fetch(`https://oauth2.googleapis.com/tokeninfo?id_token=${encodeURIComponent(idToken)}`);
  if (!response.ok) return null;
  const info = await response.json();
  if (info.aud !== cfg.googleClientId) return null;
  if (!["accounts.google.com", "https://accounts.google.com"].includes(info.iss)) return null;
  if (info.email_verified !== "true" && info.email_verified !== true) return null;
  return info.email;
}

// ---- facade access ---------------------------------------------------------

export async function facadeFetch(path, cfg) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(`${cfg.filestoreBase}${path}`, {
      headers: { Authorization: `Bearer ${cfg.filestoreToken}` },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`facade ${response.status} for ${path}`);
    return response;
  } finally {
    clearTimeout(timer);
  }
}

export function encodePath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

// ---- request helpers -------------------------------------------------------

export function json(res, status, body) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(body));
}

export async function readJsonBody(req) {
  if (req.body && typeof req.body === "object") return req.body; // Vercel pre-parses JSON
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

/** Wraps a handler so a misconfigured BFF answers 500 with the reason instead of serving data. */
export function withConfig(handler) {
  return async (req, res) => {
    let cfg;
    try {
      cfg = config();
    } catch (err) {
      json(res, 500, { error: "misconfigured", message: err.message });
      return;
    }
    try {
      await handler(req, res, cfg);
    } catch (err) {
      json(res, 502, { error: "upstream", message: err.message });
    }
  };
}
