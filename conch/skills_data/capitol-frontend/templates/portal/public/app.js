/**
 * Portal front end: sign-in state from /api/auth/me, Google Identity
 * Services button when a client id is configured, records from /api/feed.
 * The browser never talks to the filestore directly.
 */
const el = (id) => document.getElementById(id);

function showError(message) {
  const banner = el("error-banner");
  banner.textContent = message;
  banner.hidden = false;
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(body.message || body.error || `HTTP ${response.status}`);
    err.status = response.status;
    throw err;
  }
  return body;
}

async function login(payload) {
  try {
    await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await main();
  } catch (err) {
    showError(err.status === 403 ? "That account is not on the allowlist." : `Sign-in failed: ${err.message}`);
  }
}

function mountGoogleButton(clientId) {
  if (!clientId) return;
  const script = document.createElement("script");
  script.src = "https://accounts.google.com/gsi/client";
  script.async = true;
  script.onload = () => {
    window.google.accounts.id.initialize({
      client_id: clientId,
      callback: (response) => login({ credential: response.credential }),
    });
    window.google.accounts.id.renderButton(el("google-signin"), { theme: "outline", size: "large" });
  };
  document.head.appendChild(script);
}

function renderRecords(feed) {
  const list = el("records");
  list.textContent = "";
  el("feed-source").textContent = `${feed.source} · ${feed.records.length} records`;
  el("stale-note").hidden = !feed.stale;
  if (!feed.records.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No records yet.";
    list.appendChild(li);
    return;
  }
  for (const record of feed.records) {
    const li = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = record.title || record.name || record.id || record._path;
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = [record.status, record.source, record.updated_at || record.last_seen_at].filter(Boolean).join(" · ");
    li.append(title, meta);
    if (record.summary) {
      const summary = document.createElement("p");
      summary.textContent = record.summary;
      li.appendChild(summary);
    }
    list.appendChild(li);
  }
}

async function main() {
  el("error-banner").hidden = true;
  const me = await api("/api/auth/me");
  el("who").textContent = me.signed_in ? `Signed in as ${me.email}` : "";
  el("signin-panel").hidden = me.signed_in;
  el("feed-panel").hidden = !me.signed_in;
  if (!me.signed_in) {
    mountGoogleButton(me.google_client_id);
    const dev = el("dev-login");
    dev.hidden = !me.dev_login;
    dev.onsubmit = (event) => {
      event.preventDefault();
      login({ dev_email: el("dev-email").value.trim() });
    };
    return;
  }
  el("signout-btn").onclick = async () => {
    await api("/api/auth/logout", { method: "POST" });
    await main();
  };
  try {
    renderRecords(await api("/api/feed"));
  } catch (err) {
    showError(`Could not load records: ${err.message}`);
  }
}

main().catch((err) => showError(err.message));
