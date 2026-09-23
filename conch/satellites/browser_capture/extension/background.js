// Conch Browser Capture — background service worker.
//
// Owns the native messaging port to conch-capture-host, the origin
// allowlist (mirrored into dynamically registered content scripts),
// the pause state, and the action badge. The Python host is the trust
// boundary — everything sent here is re-validated and secret-scrubbed
// there — but this worker still forwards only events from allowlisted
// origins while unpaused, and drops (with a count) when the host is
// unreachable rather than ever queueing unboundedly.

const HOST_NAME = "com.conch.capture";
const PROTOCOL_VERSION = 1;
const EXT_VERSION = chrome.runtime.getManifest().version;
const MAX_QUEUE = 200;

let port = null;
let hostInfo = { connected: false, capture: false, daemon: false };
let queue = [];
let dropped = 0;
let seq = 0;

async function getState() {
  const stored = await chrome.storage.local.get({
    origins: [],
    paused: false,
  });
  return stored;
}

// ---------------------------------------------------------------------------
// Badge: the always-visible on/off surface.
// ---------------------------------------------------------------------------

async function updateBadge() {
  const { origins, paused } = await getState();
  let text = "OFF";
  let color = "#777777";
  if (paused) {
    text = "II";
    color = "#c9a227";
  } else if (origins.length && hostInfo.connected && hostInfo.capture) {
    text = "ON";
    color = "#1a7f37";
  } else if (origins.length) {
    // Origins allowlisted but the host is down or capture is disabled
    // in conch config: visibly not capturing.
    text = "OFF";
    color = "#b3261e";
  }
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// ---------------------------------------------------------------------------
// Native messaging port.
// ---------------------------------------------------------------------------

function connectHost() {
  if (port) return port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (err) {
    port = null;
    hostInfo = { connected: false, capture: false, daemon: false };
    updateBadge();
    return null;
  }
  port.onMessage.addListener((msg) => {
    if (msg && msg.type === "hello") {
      hostInfo = {
        connected: true,
        capture: Boolean(msg.capture),
        daemon: Boolean(msg.daemon),
      };
      updateBadge();
      flushQueue();
    }
    // acks with ok:false carry a reason label only; nothing to do here
    // beyond keeping the port healthy.
  });
  port.onDisconnect.addListener(() => {
    port = null;
    hostInfo = { connected: false, capture: false, daemon: false };
    updateBadge();
  });
  port.postMessage({
    type: "hello",
    v: PROTOCOL_VERSION,
    ext_version: EXT_VERSION,
  });
  return port;
}

function flushQueue() {
  if (!port || !hostInfo.capture) return;
  const pending = queue;
  queue = [];
  for (const event of pending) {
    try {
      port.postMessage(event);
    } catch (err) {
      queue.push(event);
      break;
    }
  }
}

function sendEvent(event) {
  const p = connectHost();
  if (p && hostInfo.connected) {
    try {
      p.postMessage(event);
      return;
    } catch (err) {
      // fall through to the bounded queue
    }
  }
  queue.push(event);
  if (queue.length > MAX_QUEUE) {
    queue.shift();
    dropped += 1;
  }
}

// ---------------------------------------------------------------------------
// Content-script registration: one registration per allowlisted origin,
// nothing anywhere else (no <all_urls> — origins are explicit opt-in).
// ---------------------------------------------------------------------------

async function syncContentScripts() {
  const { origins } = await getState();
  const existing = await chrome.scripting.getRegisteredContentScripts();
  if (existing.length) {
    await chrome.scripting.unregisterContentScripts({
      ids: existing.map((s) => s.id),
    });
  }
  if (!origins.length) {
    updateBadge();
    return;
  }
  await chrome.scripting.registerContentScripts(
    origins.map((origin, index) => ({
      id: `conch-capture-${index}`,
      js: ["content.js"],
      matches: [`${origin}/*`],
      runAt: "document_idle",
      world: "ISOLATED",
    }))
  );
  updateBadge();
}

// ---------------------------------------------------------------------------
// Wiring.
// ---------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  syncContentScripts();
  connectHost();
});
chrome.runtime.onStartup.addListener(() => {
  syncContentScripts();
  connectHost();
});

// The action button is the pause control.
chrome.action.onClicked.addListener(async () => {
  const { paused } = await getState();
  await chrome.storage.local.set({ paused: !paused });
  updateBadge();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "conch-status") {
    // options page asking for status
    getState().then(({ origins, paused }) => {
      sendResponse({
        origins,
        paused,
        host: hostInfo,
        queued: queue.length,
        dropped,
      });
    });
    return true;
  }
  if (msg && msg.type === "conch-sync") {
    // options page changed the allowlist
    syncContentScripts().then(() => sendResponse({ ok: true }));
    connectHost();
    return true;
  }
  if (msg && msg.type === "conch-event") {
    handleCapturedEvent(msg, sender);
  }
  return false;
});

async function handleCapturedEvent(msg, sender) {
  const { origins, paused } = await getState();
  if (paused) return;
  const origin = sender.origin || "";
  if (!origins.includes(origin)) return; // never forward off-list events
  seq += 1;
  sendEvent({
    type: "event",
    v: PROTOCOL_VERSION,
    origin,
    ts: Date.now() / 1000,
    seq,
    kind: msg.kind,
    detail: msg.detail || {},
    ext_version: EXT_VERSION,
  });
}
