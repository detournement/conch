// Conch Browser Capture — content script (allowlisted origins only;
// the background worker registers it per origin, never <all_urls>).
//
// Deliberately thin: extract a SEMANTIC target (role + accessible
// label), never coordinates, never input values, never clipboard
// content, and never anything from password/secret fields. The Python
// native host re-validates and secret-scrubs everything — this script
// is the first net, not the last.

(() => {
  "use strict";

  const MAX_LABEL = 120;
  const SECRET_NAME = /pass|secret|token|pwd|otp|auth|cvv|card|ssn|pin|credential|api.?key|private/i;

  // Simple rate bound so a busy page can't flood the pipeline.
  let allowance = 5;
  let lastRefill = Date.now();
  function allow() {
    const now = Date.now();
    allowance = Math.min(5, allowance + ((now - lastRefill) / 1000) * 5);
    lastRefill = now;
    if (allowance < 1) return false;
    allowance -= 1;
    return true;
  }

  function send(kind, detail) {
    if (!allow()) return;
    try {
      chrome.runtime.sendMessage({ type: "conch-event", kind, detail });
    } catch (err) {
      // extension reloaded / context invalidated: stay silent
    }
  }

  function clip(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, MAX_LABEL);
  }

  function isSecretInput(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if (el.type === "password") return true;
    const hints = `${el.name || ""} ${el.id || ""} ${el.autocomplete || ""}`;
    return SECRET_NAME.test(hints);
  }

  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return clip(explicit).slice(0, 40);
    const tag = el.tagName ? el.tagName.toLowerCase() : "element";
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const type = (el.type || "text").toLowerCase();
      return ["submit", "button", "checkbox", "radio"].includes(type)
        ? type
        : "input";
    }
    if (tag === "select") return "select";
    if (tag === "textarea") return "textarea";
    return tag;
  }

  function labelOf(el) {
    // Accessible-name-ish, values excluded for anything the user types
    // into. Button/submit "value" is the visible caption, not input.
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria) return clip(aria);
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    if (tag === "input") {
      if (["submit", "button"].includes((el.type || "").toLowerCase())) {
        return clip(el.value);
      }
      return clip(el.name || el.placeholder || el.id);
    }
    if (tag === "select" || tag === "textarea") {
      return clip(el.name || el.id);
    }
    return clip(
      el.innerText || el.textContent || el.title || el.alt || el.name
    );
  }

  function semanticTarget(node) {
    if (!node || !node.closest) return null;
    return (
      node.closest(
        "button, a, [role], input, select, textarea, summary, label"
      ) || node
    );
  }

  // -- navigation: this script loading on an allowlisted origin IS the
  // navigation signal (path only, no query string, no fragment).
  send("nav", { path: location.pathname || "/" });

  // -- clicks: semantic target only.
  document.addEventListener(
    "click",
    (event) => {
      const el = semanticTarget(event.target);
      if (!el || isSecretInput(el)) return; // secret inputs: nothing at all
      send("click", { role: roleOf(el), label: labelOf(el) });
    },
    { capture: true, passive: true }
  );

  // -- form submits: field NAMES only, secret-named/password fields
  //    excluded at the source.
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!form || !form.elements) return;
      const fields = [];
      for (const el of form.elements) {
        if (fields.length >= 40) break;
        if (isSecretInput(el)) continue;
        const name = clip(el.name || el.id).slice(0, 80);
        if (name && !SECRET_NAME.test(name)) fields.push(name);
      }
      send("submit", {
        form: clip(form.name || form.id || "form"),
        fields,
      });
    },
    { capture: true, passive: true }
  );

  // -- copy: the fact of a copy and its semantic source — NEVER the
  //    copied text.
  document.addEventListener(
    "copy",
    () => {
      const el = semanticTarget(document.activeElement);
      if (el && isSecretInput(el)) return;
      send("copy", {
        role: el ? roleOf(el) : "",
        label: el ? labelOf(el) : "",
      });
    },
    { capture: true, passive: true }
  );
})();
