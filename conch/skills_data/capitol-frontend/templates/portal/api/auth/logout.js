import { json, setSessionCookie, withConfig } from "../_shared.js";

/** POST /api/auth/logout → clears the session cookie. */
export default withConfig(async (req, res, cfg) => {
  if (req.method !== "POST") return json(res, 405, { error: "method_not_allowed" });
  setSessionCookie(res, "", cfg, { clear: true });
  json(res, 200, { signed_in: false });
});
