/**
 * Dependency-free browser client for a Capitol A2A gateway.
 *
 * - JSON-RPC 2.0 over fetch() against POST {gatewayUrl}
 * - SSE over fetch(): subscribe_workflow_events is opened with a
 *   `message/stream` POST, so EventSource (GET-only) cannot be used.
 *   The stream is parsed from the response ReadableStream instead,
 *   with auto-reconnect resuming from the last seen `sequence`.
 * - The bearer token is read at call time via the injected getToken()
 *   so it lives in localStorage, never in this file.
 */

export class A2AAuthError extends Error {
  constructor(status) {
    super(`Gateway rejected the token (HTTP ${status})`);
    this.name = "A2AAuthError";
    this.status = status;
  }
}

export class A2ARpcError extends Error {
  constructor(rpcError) {
    super(rpcError.message || `JSON-RPC error ${rpcError.code}`);
    this.name = "A2ARpcError";
    this.code = rpcError.code;
    this.data = rpcError.data || {};
    this.retryable = Boolean(this.data.retryable);
    this.actionableHint = this.data.actionable_hint || null;
  }
}

export class A2AClient {
  /**
   * @param {string} gatewayUrl full /a2a/{org_id}/{agent_id} endpoint
   * @param {() => string|null} getToken returns the cap_a2a_* bearer
   */
  constructor(gatewayUrl, getToken) {
    this.gatewayUrl = gatewayUrl.replace(/\/$/, "");
    this.getToken = getToken;
    this.contextId = null;
  }

  _headers() {
    const token = this.getToken();
    if (!token) throw new A2AAuthError(0);
    return {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    };
  }

  _envelope(method, skillData) {
    const message = {
      role: "user",
      parts: [{ type: "data", data: skillData }],
    };
    if (this.contextId) message.context_id = this.contextId;
    return {
      jsonrpc: "2.0",
      id: crypto.randomUUID(),
      method,
      params: { message },
    };
  }

  async rpc(method, skillData) {
    const resp = await fetch(this.gatewayUrl, {
      method: "POST",
      headers: this._headers(),
      body: JSON.stringify(this._envelope(method, skillData)),
    });
    if (resp.status === 401 || resp.status === 403) {
      throw new A2AAuthError(resp.status);
    }
    const body = await resp.json();
    if (body.error) throw new A2ARpcError(body.error);
    return body.result;
  }

  /** Send one skill call and return the response data part. */
  async sendSkill(skillData) {
    const task = await this.rpc("message/send", skillData);
    return extractDataPart(task);
  }

  async fetchAgentCard() {
    const resp = await fetch(`${this.gatewayUrl}/.well-known/agent-card.json`, {
      headers: this._headers(),
    });
    if (resp.status === 401 || resp.status === 403) {
      throw new A2AAuthError(resp.status);
    }
    if (!resp.ok) throw new Error(`agent card fetch failed: ${resp.status}`);
    return resp.json();
  }

  /** Handshake once per session; threads context_id onto later calls. */
  async handshake(system, version) {
    const data = await this.sendSkill({
      skill_id: "handshake",
      caller: { system, version },
      capabilities: { supports_sse: true },
    });
    this.contextId = data?.session?.context_id || this.contextId;
    return data;
  }

  async listWorkflows() {
    return this.sendSkill({ skill_id: "list_workflows" });
  }

  async getWorkflowDetails(workflowId) {
    return this.sendSkill({
      skill_id: "get_workflow_details",
      workflow_id: workflowId,
    });
  }

  /**
   * @param {string|null} workflowId omit only on single-workflow agents
   * @param {Object} inputs canonical "<node_id>.<field_id>" keys
   */
  async callWorkflow(workflowId, inputs, { artifacts = [], idempotencyKey } = {}) {
    const data = {
      skill_id: "call_workflow",
      inputs,
      artifacts,
      idempotency_key: idempotencyKey || crypto.randomUUID(),
    };
    if (workflowId) data.workflow_id = workflowId;
    return this.sendSkill(data);
  }

  async getWorkflowStatus(runId) {
    return this.sendSkill({ skill_id: "get_workflow_status", run_id: runId });
  }

  async getWorkflowOutput(runId) {
    return this.sendSkill({ skill_id: "get_workflow_output", run_id: runId });
  }

  async getWorkflowEvents(runId, sinceSequence = 0, types = []) {
    return this.sendSkill({
      skill_id: "get_workflow_events",
      run_id: runId,
      since_sequence: sinceSequence,
      types,
    });
  }

  /**
   * Inline upload (<= 50 MB); returns the skill response whose `file_id`
   * is the reference to pass in workflow file inputs. The gateway
   * contract is top-level `filename` + `content_base64` (+ optional
   * `content_type`) -- NOT a nested artifact object.
   */
  async uploadFile(file) {
    const buf = await file.arrayBuffer();
    let binary = "";
    const bytes = new Uint8Array(buf);
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
    }
    return this.sendSkill({
      skill_id: "upload_file",
      filename: file.name,
      content_type: file.type || "application/octet-stream",
      content_base64: btoa(binary),
    });
  }

  /**
   * Live event stream with resume + reconnect.
   *
   * The first attach opens ``message/stream``; reconnects after a drop
   * use ``tasks/resubscribe`` (the A2A resume method -- this gateway
   * routes both to the same stream). ``since_sequence`` is an exclusive
   * cursor, so a resubscribe never re-delivers events already seen, and
   * a resubscribe after the run already ended still closes promptly on
   * the terminal frame.
   *
   * @param {string} runId
   * @param {(event: Object) => void} onEvent one call per WorkflowEvent
   * @param {Object} [opts]
   * @param {number} [opts.sinceSequence] resume cursor
   * @param {string[]} [opts.types] event_type filter ([] = all)
   * @param {(err: Error) => void} [opts.onError]
   * @param {(status: "live"|"reconnecting", attempts: number) => void} [opts.onStatus]
   * @returns {{ close: () => void }}
   */
  subscribeRunEvents(runId, onEvent, opts = {}) {
    const state = {
      closed: false,
      lastSequence: opts.sinceSequence || 0,
      attempts: 0,
      resubscribeUnsupported: false,
      controller: null,
    };
    const notify = (status) => {
      if (opts.onStatus) opts.onStatus(status, state.attempts);
    };

    const loop = async () => {
      let everConnected = false;
      while (!state.closed) {
        const method =
          everConnected || state.attempts > 0
            ? state.resubscribeUnsupported
              ? "message/stream"
              : "tasks/resubscribe"
            : "message/stream";
        try {
          await this._streamOnce(runId, state, onEvent, opts.types || [], method, () => {
            everConnected = true;
            notify("live");
          });
          return; // terminal event: clean close
        } catch (err) {
          if (state.closed) return;
          if (err instanceof A2ARpcError && err.code === -32601 && method === "tasks/resubscribe") {
            // Older gateway without the resubscribe alias: fall back to
            // re-opening message/stream (same resume cursor).
            state.resubscribeUnsupported = true;
            continue;
          }
          if (err instanceof A2AAuthError || err instanceof A2ARpcError) {
            if (opts.onError) opts.onError(err);
            return; // not recoverable by reconnecting
          }
          state.attempts += 1;
          notify("reconnecting");
          if (opts.onError) opts.onError(err);
          const backoffMs = Math.min(1000 * 2 ** (state.attempts - 1), 30000);
          await new Promise((r) => setTimeout(r, backoffMs));
        }
      }
    };
    loop();

    return {
      close: () => {
        state.closed = true;
        if (state.controller) state.controller.abort();
      },
    };
  }

  async _streamOnce(runId, state, onEvent, types, method = "message/stream", onOpen = () => {}) {
    state.controller = new AbortController();
    const envelope = this._envelope(method, {
      skill_id: "subscribe_workflow_events",
      run_id: runId,
      since_sequence: state.lastSequence,
      types,
    });
    const resp = await fetch(this.gatewayUrl, {
      method: "POST",
      headers: { ...this._headers(), Accept: "text/event-stream" },
      body: JSON.stringify(envelope),
      signal: state.controller.signal,
    });
    if (resp.status === 401 || resp.status === 403) {
      throw new A2AAuthError(resp.status);
    }
    if (!resp.ok || !resp.body) {
      throw new Error(`stream open failed: HTTP ${resp.status}`);
    }
    const contentType = resp.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) {
      // The gateway answered with a plain JSON-RPC envelope (e.g. method
      // not supported) instead of opening a stream.
      const body = await resp.json().catch(() => null);
      if (body && body.error) throw new A2ARpcError(body.error);
      throw new Error(`stream open failed: unexpected content-type "${contentType}"`);
    }

    state.attempts = 0;
    onOpen();
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawTerminal = false;

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const rawFrame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const event = parseSseFrame(rawFrame);
        if (!event) continue;
        if (typeof event.sequence === "number") {
          state.lastSequence = Math.max(state.lastSequence, event.sequence);
        }
        onEvent(event);
        if (
          event.event_type === "workflow.run_completed" ||
          event.event_type === "workflow.run_failed"
        ) {
          sawTerminal = true;
        }
      }
    }
    if (!sawTerminal) throw new Error("stream ended before the run terminal");
  }
}

/** First data part of a Task's status message (skill responses live there). */
export function extractDataPart(task) {
  const parts = task?.status?.message?.parts || [];
  for (const part of parts) {
    if (part && typeof part === "object" && part.data) return part.data;
  }
  return task;
}

/**
 * One SSE frame -> the WorkflowEvent it tunnels, or null for
 * keepalives / status frames / non-event payloads.
 */
export function parseSseFrame(rawFrame) {
  const dataLines = rawFrame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim());
  if (!dataLines.length) return null;
  let payload;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  const result = payload.result || payload;
  const artifact = result.artifact;
  if (!artifact) return null; // TaskStatusUpdateEvent frame
  const name = artifact.name || "";
  if (name.includes("keepalive")) return null;
  const parts = artifact.parts || [];
  for (const part of parts) {
    if (part && typeof part === "object" && part.data) return part.data;
  }
  return null;
}
