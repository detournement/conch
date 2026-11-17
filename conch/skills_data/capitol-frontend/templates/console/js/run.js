/**
 * Environment-neutral run helpers shared by the browser app (app.js) and
 * the headless verifier (verify-run.mjs). Works in browsers and Node 20+.
 */

export const TERMINAL_STATUSES = new Set(["success", "completed", "failed", "stopped", "cancelled"]);
export const TERMINAL_EVENTS = new Set(["workflow.run_completed", "workflow.run_failed"]);

/** Stable key order so the same form contents always hash the same. */
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Idempotency key derived from the form contents, never from the click:
 * re-submitting the same inputs replays the existing run (``replayed:
 * true`` on the call_workflow response) instead of starting a second one.
 */
export async function idempotencyKey(prefix, workflowId, inputs, versionId = "") {
  // A pinned version is part of the key: moving the pin must start a new
  // run, while an unpinned app keeps replaying the same key for the same
  // inputs.
  const hash = await sha256Hex(canonicalJson(versionId ? { inputs, version_id: versionId } : inputs));
  return `${prefix}:${workflowId}:${hash.slice(0, 16)}`;
}

/**
 * Compare the app's pinned workflow version with what the gateway holds.
 * Returns {pinned, latest, state} where state is "unpinned", "current",
 * "behind" (a newer version exists — see the cookbook's Upgrading
 * section) or "missing" (the pin is not a saved version: fail closed).
 */
export async function checkVersionPin(client, workflowId, pinnedVersionId) {
  const details = await client.getWorkflowDetails(workflowId);
  const latest = details?.version_id || "";
  if (!pinnedVersionId) return { pinned: "", latest, state: "unpinned" };
  if (pinnedVersionId === latest) return { pinned: pinnedVersionId, latest, state: "current" };
  const history = await client.getWorkflowVersions(workflowId);
  const known = (history?.versions || []).some((v) => (v.version_id || v.id) === pinnedVersionId);
  return { pinned: pinnedVersionId, latest, state: known ? "behind" : "missing" };
}

/** Build the inputs map from the config fields and the collected values. */
export function composeInputs(fields, values) {
  const inputs = {};
  const missing = [];
  for (const field of fields) {
    const raw = values[field.key];
    const value = typeof raw === "string" ? raw.trim() : raw;
    if (value === undefined || value === null || value === "") {
      if (field.required) missing.push(field.key);
      continue;
    }
    inputs[field.key] = value;
  }
  return { inputs, missing };
}

/**
 * Follow a run to its terminal state: live SSE via subscribeRunEvents,
 * polling get_workflow_status when the stream is unavailable. Resolves
 * with the final get_workflow_status payload.
 *
 * The status is checked before subscribing and every ``statusCheckMs``
 * while the stream is open: a keyed re-submit returns a run that may
 * already be finished, and a finished run's stream does not always
 * replay its terminal frame.
 */
export async function waitForTerminal(
  client,
  runId,
  { onEvent, onConnection, pollMs = 5000, statusCheckMs = 30000 } = {},
) {
  const initial = await client.getWorkflowStatus(runId);
  if (TERMINAL_STATUSES.has(initial.status)) return initial;

  return new Promise((resolve, reject) => {
    let pollTimer = null;
    let streamErrors = 0;
    let pollErrors = 0;
    let settled = false;

    const finish = async (ok, value) => {
      if (settled) return;
      settled = true;
      clearInterval(pollTimer);
      clearInterval(safetyTimer);
      subscription.close();
      if (!ok) return reject(value);
      try {
        resolve(await client.getWorkflowStatus(runId));
      } catch (err) {
        reject(err);
      }
    };

    const checkStatus = async (onError) => {
      try {
        const status = await client.getWorkflowStatus(runId);
        pollErrors = 0;
        if (TERMINAL_STATUSES.has(status.status)) finish(true);
      } catch (err) {
        onError?.(err);
      }
    };

    const startPolling = () => {
      if (pollTimer || settled) return;
      onConnection?.("polling");
      pollTimer = setInterval(
        () =>
          checkStatus((err) => {
            pollErrors += 1;
            if (err.name === "A2AAuthError" || pollErrors >= 6) finish(false, err);
          }),
        pollMs,
      );
    };

    const safetyTimer = setInterval(() => checkStatus(), statusCheckMs);

    const subscription = client.subscribeRunEvents(
      runId,
      (event) => {
        onEvent?.(event);
        if (TERMINAL_EVENTS.has(event.event_type)) finish(true);
      },
      {
        onStatus: (status) => onConnection?.(status),
        onError: (err) => {
          streamErrors += 1;
          if (err.name === "A2AAuthError") finish(false, err);
          else if (err.name === "A2ARpcError" || streamErrors >= 3) startPolling();
        },
      },
    );
  });
}

/** Answer a node.input_required event with the user's words, verbatim. */
export function answerInputRequired(client, runId, event, response, { declined = false } = {}) {
  const data = event.data || {};
  if (data.input_kind === "clarification") {
    return client.sendSkill({
      skill_id: "submit_clarification_response",
      run_id: runId,
      request_id: data.request_id,
      response,
      declined,
    });
  }
  return client.sendSkill({
    skill_id: "submit_intervention_response",
    run_id: runId,
    node_id: event.node?.node_id,
    request_id: data.request_id,
    response,
  });
}
