#!/usr/bin/env node
/**
 * Headless end-to-end check for the console: agent card → handshake →
 * keyed call_workflow → live events → terminal status → deliverables.
 * Uses the same client, inputs and idempotency key as the browser app,
 * so running it twice with the same inputs replays one run instead of
 * starting two.
 *
 *   CAPITOL_A2A_BEARER=... node verify-run.mjs [--config config/app.config.json]
 *                                                [--input KEY=VALUE ...] [--timeout 2400]
 *
 * Exit codes: 0 run reached success and produced deliverables (or none
 * were expected), 1 run failed / timed out / transport error, 2 setup
 * error (missing token, placeholder config, bad arguments).
 */

import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const major = Number(process.versions.node.split(".")[0]);
if (major < 20) {
  console.error(`setup: Node 20+ required (fetch + WebCrypto globals); found ${process.versions.node}`);
  process.exit(2);
}

const { A2AClient } = await import(pathToFileURL(resolve(here, "js/a2a-client.js")).href);
const { composeInputs, idempotencyKey, waitForTerminal } = await import(
  pathToFileURL(resolve(here, "js/run.js")).href
);
const { extractOutputFiles, normalizeFile } = await import(pathToFileURL(resolve(here, "js/run-view.js")).href);

const args = process.argv.slice(2);
const opt = { config: "config/app.config.json", inputs: {}, timeout: 2400 };
for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--config") opt.config = args[++i];
  else if (args[i] === "--timeout") opt.timeout = Number(args[++i]);
  else if (args[i] === "--input") {
    const pair = args[++i] || "";
    const eq = pair.indexOf("=");
    if (eq < 1) fail(2, `setup: --input expects KEY=VALUE, got "${pair}"`);
    opt.inputs[pair.slice(0, eq)] = pair.slice(eq + 1);
  } else fail(2, `setup: unknown argument ${args[i]}`);
}

const token = process.env.CAPITOL_A2A_BEARER;
if (!token) fail(2, "setup: CAPITOL_A2A_BEARER is not set (export it from your shell; never write it into the app)");

const config = JSON.parse(await readFile(resolve(here, opt.config), "utf8"));
for (const [key, value] of Object.entries({ gateway_url: config.gateway_url, workflow_id: config.workflow_id })) {
  if (!value || String(value).includes("REPLACE_")) fail(2, `setup: config.${key} still holds a placeholder (${value})`);
}
const placeholderField = (config.fields || []).find((f) => String(f.key).includes("REPLACE_"));
if (placeholderField) fail(2, `setup: config.fields key still holds a placeholder (${placeholderField.key})`);

const values = {};
for (const field of config.fields || []) values[field.key] = opt.inputs[field.key] ?? field.default ?? "";
const { inputs, missing } = composeInputs(config.fields || [], values);
if (missing.length) fail(2, `setup: required inputs missing (pass --input KEY=VALUE): ${missing.join(", ")}`);

const client = new A2AClient(config.gateway_url, () => token);
const card = await step("card", () => client.fetchAgentCard());
console.log(`card: ${card.name || "(unnamed)"} streaming=${Boolean(card.capabilities?.streaming)}`);

const hs = await step("handshake", () => client.handshake(config.app_slug, config.app_version));
console.log(`handshake: context_id=${hs?.session?.context_id || "(none)"}`);

const key = await idempotencyKey(config.idempotency_prefix || config.app_slug, config.workflow_id, inputs);
const call = await step("call_workflow", () => client.callWorkflow(config.workflow_id, inputs, { idempotencyKey: key }));
// A repeated key returns the stored original response (same run_id, no
// flag); the status check below tells whether that run already finished.
const before = await step("get_workflow_status", () => client.getWorkflowStatus(call.run_id));
const ageMs = before.started_at ? Date.now() - Date.parse(before.started_at) : 0;
const replayed = Boolean(before.completed_at) || ageMs > 15000;
console.log(`run: ${call.run_id} key=${key} nodes=${call.total_sub_agents ?? "?"} status=${before.status}${replayed ? " (existing run — replayed, nothing new started)" : " (new)"}`);

const seen = new Map();
const files = new Map();
const timer = setTimeout(() => fail(1, `timeout: no terminal event after ${opt.timeout}s (run ${call.run_id})`), opt.timeout * 1000);
const status = await step("follow", () =>
  waitForTerminal(client, call.run_id, {
    onConnection: (state) => console.log(`stream: ${state}`),
    onEvent: (event) => {
      seen.set(event.event_type, (seen.get(event.event_type) || 0) + 1);
      if (event.event_type === "workflow.files_available") {
        for (const raw of event.data?.files || []) {
          const file = normalizeFile(raw);
          if (file) files.set(file.id, file);
        }
      }
      if (event.event_type === "node.input_required") {
        console.log(`hitl: ${event.data?.input_kind || "intervention"} "${event.data?.prompt || ""}" (answer it in the console UI)`);
      }
    },
  }),
);
clearTimeout(timer);
console.log(`events: ${[...seen].map(([k, v]) => `${k}=${v}`).join(" ") || "(none — polling path)"}`);
console.log(`terminal: ${status.status}${status.error_message ? ` — ${status.error_message}` : ""}`);

if (status.status === "failed") fail(1, `run ${call.run_id} failed`);
const output = await step("get_workflow_output", () => client.getWorkflowOutput(call.run_id));
for (const file of extractOutputFiles(output)) files.set(file.id, file);
for (const file of files.values()) console.log(`deliverable: ${file.filename} ${file.mimeType || ""} ${file.href ? "url=yes" : "url=no"}`);
console.log(`deliverables: ${files.size}`);
console.log("verify: PASS");

function fail(code, message) {
  console.error(message);
  process.exit(code);
}

async function step(name, fn) {
  try {
    return await fn();
  } catch (err) {
    fail(1, `${name}: ${err.name || "Error"} ${err.message || err}`);
  }
}
