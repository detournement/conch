import { allowed, issueSession, json, readJsonBody, setSessionCookie, verifyGoogleIdToken, withConfig } from "../_shared.js";

/**
 * POST /api/auth/login  { credential: <Google ID token> }
 * Dev only (ALLOW_DEV_LOGIN=1, never in production): { dev_email: "me@example.com" }
 */
export default withConfig(async (req, res, cfg) => {
  if (req.method !== "POST") return json(res, 405, { error: "method_not_allowed" });
  const body = await readJsonBody(req);

  let email = null;
  if (body.credential) {
    email = await verifyGoogleIdToken(body.credential, cfg);
    if (!email) return json(res, 401, { error: "invalid_google_token" });
  } else if (body.dev_email && cfg.devLogin) {
    email = String(body.dev_email);
  } else {
    return json(res, 400, { error: "credential_required" });
  }

  if (!allowed(email, cfg.allowlist)) return json(res, 403, { error: "not_allowed", email });
  setSessionCookie(res, issueSession(email, cfg), cfg);
  json(res, 200, { signed_in: true, email });
});
