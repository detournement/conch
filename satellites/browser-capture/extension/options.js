// Conch Browser Capture — options page: the origin allowlist manager
// and the human-readable status surface.

async function refresh() {
  const status = await chrome.runtime.sendMessage({ type: "conch-status" });
  renderStatus(status);
  renderOrigins(status.origins);
  document.getElementById("pause").textContent = status.paused
    ? "Resume capture"
    : "Pause capture";
}

function renderStatus(status) {
  const el = document.getElementById("status");
  const bits = [];
  if (status.paused) {
    bits.push('<span class="status-bad">paused</span>');
  } else if (status.host.connected && status.host.capture) {
    bits.push('<span class="status-ok">capturing</span>');
  } else if (status.host.connected) {
    bits.push(
      '<span class="status-bad">host connected, capture disabled in' +
        " conch (/install capture browser)</span>"
    );
  } else {
    bits.push(
      '<span class="status-bad">native host not connected</span>' +
        " — run <code>/install capture browser</code> in conch"
    );
  }
  bits.push(
    status.host.daemon ? "edge daemon up" : "edge daemon down (events" +
      " land in the kernel directly)"
  );
  if (status.queued) bits.push(`${status.queued} queued`);
  if (status.dropped) bits.push(`${status.dropped} dropped`);
  el.innerHTML = bits.join(" · ");
}

function renderOrigins(origins) {
  const list = document.getElementById("origins");
  list.innerHTML = "";
  if (!origins.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No origins yet — nothing is being captured.";
    list.appendChild(li);
    return;
  }
  for (const origin of origins) {
    const li = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = origin;
    const btn = document.createElement("button");
    btn.textContent = "Remove";
    btn.addEventListener("click", () => removeOrigin(origin));
    li.appendChild(code);
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function normalizeOrigin(text) {
  let value = String(text || "").trim();
  if (!value) return null;
  if (!/^https?:\/\//.test(value)) value = `https://${value}`;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return url.origin;
  } catch (err) {
    return null;
  }
}

async function addOrigin() {
  const input = document.getElementById("origin-input");
  const origin = normalizeOrigin(input.value);
  if (!origin) {
    input.value = "";
    input.placeholder = "not a valid http(s) origin";
    return;
  }
  const granted = await chrome.permissions.request({
    origins: [`${origin}/*`],
  });
  if (!granted) return;
  const { origins } = await chrome.storage.local.get({ origins: [] });
  if (!origins.includes(origin)) {
    origins.push(origin);
    await chrome.storage.local.set({ origins });
  }
  input.value = "";
  await chrome.runtime.sendMessage({ type: "conch-sync" });
  refresh();
}

async function removeOrigin(origin) {
  const { origins } = await chrome.storage.local.get({ origins: [] });
  await chrome.storage.local.set({
    origins: origins.filter((o) => o !== origin),
  });
  try {
    await chrome.permissions.remove({ origins: [`${origin}/*`] });
  } catch (err) {
    // permission may already be gone; the allowlist is authoritative
  }
  await chrome.runtime.sendMessage({ type: "conch-sync" });
  refresh();
}

async function togglePause() {
  const { paused } = await chrome.storage.local.get({ paused: false });
  await chrome.storage.local.set({ paused: !paused });
  await chrome.runtime.sendMessage({ type: "conch-sync" });
  refresh();
}

document.getElementById("add").addEventListener("click", addOrigin);
document.getElementById("origin-input").addEventListener(
  "keydown",
  (event) => {
    if (event.key === "Enter") addOrigin();
  }
);
document.getElementById("pause").addEventListener("click", togglePause);

refresh();
