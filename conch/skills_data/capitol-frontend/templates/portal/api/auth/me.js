import { json, readSession, withConfig } from "../_shared.js";

/** GET /api/auth/me → who is signed in, plus what the sign-in button needs. */
export default withConfig(async (req, res, cfg) => {
  const session = readSession(req, cfg);
  json(res, 200, {
    signed_in: Boolean(session),
    email: session?.email || null,
    google_client_id: cfg.googleClientId || null,
    dev_login: cfg.devLogin,
  });
});
