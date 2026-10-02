/**
 * App bootstrap: token setup → intake form → keyed call_workflow →
 * live progress (+ HITL relay) → deliverables. a2a-client.js is the
 * transport; run.js holds the environment-neutral run logic.
 */

import { A2AClient, A2AAuthError, A2ARpcError } from "./a2a-client.js";
import { createRunView, renderFileOutputs, extractOutputFiles } from "./run-view.js";
import { answerInputRequired, composeInputs, idempotencyKey, waitForTerminal } from "./run.js";

const TOKEN_KEY = "capitol-console-token";
const el = (id) => document.getElementById(id);
const panels = ["setup-panel", "form-panel", "run-panel", "output-panel"];

function show(panelId) {
  for (const id of panels) el(id).hidden = id !== panelId;
}

function showError(message) {
  const banner = el("error-banner");
  banner.textContent = message;
  banner.hidden = false;
}

function clearError() {
  el("error-banner").hidden = true;
}

function renderIntake(container, fields) {
  container.textContent = "";
  const controls = new Map();
  const advanced = document.createElement("details");
  advanced.className = "advanced";
  const summary = document.createElement("summary");
  summary.textContent = "Optional fields";
  advanced.appendChild(summary);

  for (const field of fields) {
    const row = document.createElement("div");
    row.className = "field-row";
    const label = document.createElement("label");
    label.textContent = field.required ? `${field.label || field.key} *` : field.label || field.key;
    const input = document.createElement(field.type === "textarea" ? "textarea" : "input");
    if (field.type === "textarea") input.rows = 6;
    else input.type = field.type === "number" ? "number" : "text";
    input.value = field.default ?? "";
    if (field.placeholder) input.placeholder = field.placeholder;
    row.append(label, input);
    if (field.help) {
      const help = document.createElement("small");
      help.textContent = field.help;
      row.appendChild(help);
    }
    const issue = document.createElement("div");
    issue.className = "field-issue";
    issue.hidden = true;
    row.appendChild(issue);
    (field.required ? container : advanced).appendChild(row);
    controls.set(field.key, { input, issue });
  }
  if (advanced.childElementCount > 1) container.appendChild(advanced);

  return {
    collect() {
      const values = {};
      for (const [key, { input, issue }] of controls) {
        issue.hidden = true;
        values[key] = input.value;
      }
      const { inputs, missing } = composeInputs(fields, values);
      for (const key of missing) {
        const control = controls.get(key);
        control.issue.textContent = "Required";
        control.issue.hidden = false;
      }
      return missing.length ? null : inputs;
    },
    showIssues(issues) {
      const messages = (issues || []).map((item) => item.message || String(item)).filter(Boolean);
      if (messages.length) showError(messages.join(" "));
    },
  };
}

async function main() {
  const config = await (await fetch("config/app.config.json")).json();
  el("app-title").textContent = config.app_name;
  el("app-intro").textContent = config.intro || "";
  document.title = config.app_name;

  const client = new A2AClient(config.gateway_url, () => localStorage.getItem(TOKEN_KEY));

  el("save-token-btn").addEventListener("click", async () => {
    const token = el("token-input").value.trim();
    if (!token) return;
    localStorage.setItem(TOKEN_KEY, token);
    el("token-input").value = "";
    await start();
  });
  el("restart-btn").addEventListener("click", () => {
    clearError();
    start();
  });

  async function start() {
    clearError();
    if (!localStorage.getItem(TOKEN_KEY)) {
      show("setup-panel");
      return;
    }
    try {
      await client.handshake(config.app_slug, config.app_version);
    } catch (err) {
      if (err instanceof A2AAuthError) {
        localStorage.removeItem(TOKEN_KEY);
        showError("That token was rejected. Paste a current agent token.");
        show("setup-panel");
        return;
      }
      showError(`Could not reach the agent: ${err.message}`);
      return;
    }
    showForm();
  }

  function showForm() {
    const form = renderIntake(el("form-fields"), config.fields);
    show("form-panel");
    el("workflow-form").onsubmit = async (submitEvent) => {
      submitEvent.preventDefault();
      clearError();
      const inputs = form.collect();
      if (!inputs) return;
      el("run-btn").disabled = true;
      try {
        await runWorkflow(inputs);
      } catch (err) {
        if (err instanceof A2ARpcError && err.code === -32008) {
          form.showIssues(err.data?.issues || []);
          show("form-panel");
        } else {
          showError(err.message);
        }
      } finally {
        el("run-btn").disabled = false;
      }
    };
  }

  async function runWorkflow(inputs) {
    const runView = createRunView(el("run-view"));
    const hitl = el("hitl-panel");
    hitl.hidden = true;
    show("run-panel");

    const versionId = config.workflow_version_id || "";
    const key = await idempotencyKey(config.idempotency_prefix || config.app_slug, config.workflow_id, inputs, versionId);
    const call = await client.callWorkflow(config.workflow_id, inputs, { idempotencyKey: key, versionId });
    // The gateway answers a repeated key with the stored original response
    // (same run_id, no "replayed" flag), so detect replay by the run id.
    const replayed = sessionStorage.getItem(`run:${key}`) === call.run_id;
    sessionStorage.setItem(`run:${key}`, call.run_id);
    el("run-meta").textContent = replayed
      ? `Run ${call.run_id} (same inputs as before — showing the existing run, nothing new was started)`
      : `Run ${call.run_id}`;
    runView.setRoster(call.sub_agents, call.total_sub_agents);

    const status = await waitForTerminal(client, call.run_id, {
      onEvent: (event) => {
        runView.applyEvent(event);
        if (event.event_type === "node.input_required") showHitl(call.run_id, event);
      },
      onConnection: (state) => runView.setConnection(state),
    });
    hitl.hidden = true;

    if (status.status === "failed") {
      const failure = status.failure || {};
      const hint = failure.actionable_hint ? ` (${failure.actionable_hint})` : "";
      throw new Error(`Run failed: ${status.error_message || "unknown error"}${hint}`);
    }
    const output = await client.getWorkflowOutput(call.run_id);
    renderFileOutputs(el("output-view"), [...runView.getFiles(), ...extractOutputFiles(output)]);
    show("output-panel");
  }

  function showHitl(runId, event) {
    const panel = el("hitl-panel");
    const controls = el("hitl-controls");
    el("hitl-prompt").textContent = event.data?.prompt || "The workflow is waiting for a response.";
    controls.textContent = "";
    const buttons = document.createElement("div");
    buttons.className = "buttons";

    const send = async (response, declined = false) => {
      try {
        await answerInputRequired(client, runId, event, response, { declined });
        panel.hidden = true;
      } catch (err) {
        showError(`Could not send the response: ${err.message}`);
      }
    };

    if (event.data?.input_kind === "clarification") {
      const area = document.createElement("textarea");
      area.rows = 3;
      const sendBtn = document.createElement("button");
      sendBtn.textContent = "Send";
      sendBtn.onclick = () => area.value.trim() && send(area.value);
      const skipBtn = document.createElement("button");
      skipBtn.className = "secondary";
      skipBtn.textContent = "Decline";
      skipBtn.onclick = () => send("", true);
      buttons.append(sendBtn, skipBtn);
      controls.append(area, buttons);
    } else {
      const continueBtn = document.createElement("button");
      continueBtn.textContent = "Continue";
      continueBtn.onclick = () => send("continue");
      const stopBtn = document.createElement("button");
      stopBtn.className = "secondary";
      stopBtn.textContent = "Stop";
      stopBtn.onclick = () => send("stop");
      buttons.append(continueBtn, stopBtn);
      controls.appendChild(buttons);
    }
    panel.hidden = false;
  }

  await start();
}

main().catch((err) => showError(err.message));
