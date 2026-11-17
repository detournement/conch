/**
 * Live run progress + deliverables rendering.
 *
 * Feed it the call_workflow response (node roster) and then every
 * WorkflowEvent; it keeps the node list, the reasoning toggle and the
 * deliverables list current. HITL prompts are handled by app.js.
 */

const COT_TYPES = new Set([
  "node.thinking",
  "node.agent_message",
  "node.tool_call",
  "node.tool_result",
]);

export function createRunView(container) {
  container.textContent = "";

  const statusRow = document.createElement("p");
  statusRow.className = "status-row";
  const statusLine = document.createElement("span");
  statusLine.textContent = "Starting…";
  const connBadge = document.createElement("span");
  connBadge.className = "conn-badge";
  connBadge.hidden = true;
  statusRow.append(statusLine, connBadge);

  const progress = document.createElement("div");
  const cotToggle = document.createElement("label");
  cotToggle.className = "cot-toggle";
  const cotCheckbox = document.createElement("input");
  cotCheckbox.type = "checkbox";
  cotToggle.append(cotCheckbox, document.createTextNode(" show reasoning"));
  const nodeList = document.createElement("ul");
  nodeList.className = "node-list";
  const filesBox = document.createElement("div");
  filesBox.className = "files-box";
  container.append(statusRow, progress, cotToggle, nodeList, filesBox);

  cotCheckbox.addEventListener("change", () => {
    container.classList.toggle("show-cot", cotCheckbox.checked);
  });

  const CONN_LABELS = {
    live: "live stream",
    reconnecting: "stream interrupted — reconnecting",
    polling: "polling fallback",
  };
  const rows = new Map();
  const collected = [];
  let total = 0;
  let completed = 0;

  function setConnection(status) {
    const label = CONN_LABELS[status];
    if (!label) return;
    connBadge.textContent = label;
    connBadge.dataset.state = status;
    connBadge.hidden = false;
  }

  function addNodeRow(nodeId, displayName) {
    const li = document.createElement("li");
    li.dataset.status = "pending";
    const nameEl = document.createElement("span");
    nameEl.textContent = displayName || nodeId;
    const statusEl = document.createElement("em");
    statusEl.textContent = "pending";
    const detailEl = document.createElement("div");
    detailEl.className = "cot-detail";
    li.append(nameEl, statusEl, detailEl);
    nodeList.appendChild(li);
    rows.set(nodeId, { li, statusEl, detailEl });
    return rows.get(nodeId);
  }

  function setRoster(subAgents, totalSubAgents) {
    total = totalSubAgents || (subAgents || []).length;
    for (const node of subAgents || []) {
      addNodeRow(node.node_id || node.id, node.display_name || node.name);
    }
    renderProgress();
  }

  function renderProgress() {
    progress.textContent = total ? `${completed} / ${total} steps complete` : "";
  }

  function rowFor(event) {
    const nodeId = event.node?.node_id;
    if (!nodeId) return null;
    return rows.get(nodeId) || addNodeRow(nodeId, event.node?.display_name);
  }

  function appendDetail(row, text) {
    if (!row || !text) return;
    const p = document.createElement("p");
    p.textContent = text.length > 500 ? `${text.slice(0, 500)}…` : text;
    row.detailEl.appendChild(p);
  }

  function addFiles(files) {
    for (const raw of files || []) {
      const file = normalizeFile(raw);
      if (!file || collected.some((f) => f.id === file.id)) continue;
      collected.push(file);
      if (!filesBox.querySelector("h3")) {
        const heading = document.createElement("h3");
        heading.textContent = "Deliverables so far";
        filesBox.appendChild(heading);
      }
      filesBox.appendChild(fileRow(file));
    }
  }

  function applyEvent(event) {
    switch (event.event_type) {
      case "workflow.run_started":
        statusLine.textContent = "Running…";
        break;
      case "workflow.run_completed":
        statusLine.textContent = "Completed";
        break;
      case "workflow.run_failed":
        statusLine.textContent = `Failed: ${event.data?.error_message || "see status"}`;
        break;
      case "workflow.files_available":
        addFiles(event.data?.files);
        break;
      case "node.input_required":
        statusLine.textContent = "Waiting for your input";
        break;
      case "node.node_started": {
        const row = rowFor(event);
        if (row) {
          row.li.dataset.status = "running";
          row.statusEl.textContent = "running";
        }
        break;
      }
      case "node.node_completed": {
        const row = rowFor(event);
        if (row && row.li.dataset.status !== "done") {
          row.li.dataset.status = "done";
          row.statusEl.textContent = "done";
          completed += 1;
          renderProgress();
        }
        break;
      }
      case "node.node_failed": {
        const row = rowFor(event);
        if (row) {
          row.li.dataset.status = "failed";
          row.statusEl.textContent = "failed";
          appendDetail(row, event.data?.error_message);
        }
        break;
      }
      case "node.retry_scheduled": {
        const row = rowFor(event);
        if (row) row.statusEl.textContent = `retrying (attempt ${event.data?.attempt ?? "?"})`;
        break;
      }
      default:
        if (COT_TYPES.has(event.event_type)) {
          const data = event.data || {};
          appendDetail(rowFor(event), data.text || data.content || data.tool_name || data.message || "");
        }
      // Unknown event types are ignored on purpose (forward-compat).
    }
  }

  return { setRoster, applyEvent, addFiles, setConnection, getFiles: () => [...collected] };
}

/**
 * Accepts both wire shapes -- ``files_available`` frames
 * ({file_id, filename, download_url, mime_type, size_bytes}) and the
 * terminal output's ``files[]`` ({id, name, presigned_url, mime_type}).
 */
export function normalizeFile(raw) {
  if (!raw || typeof raw !== "object") return null;
  const href = raw.href || raw.download_url || raw.presigned_url || raw.url || null;
  const id = raw.id || raw.file_id || href;
  if (!id) return null;
  return {
    id,
    filename: raw.filename || raw.name || String(id),
    href,
    mimeType: raw.mimeType || raw.mime_type || raw.content_type || "",
    sizeBytes: Number(raw.sizeBytes ?? raw.size_bytes ?? raw.size) || 0,
  };
}

function fileRow(file) {
  const row = document.createElement("div");
  row.className = "file-row";
  const link = document.createElement("a");
  link.textContent = file.filename;
  if (file.href) {
    link.href = file.href;
    link.target = "_blank";
    link.rel = "noopener";
  }
  const meta = document.createElement("small");
  meta.textContent = file.mimeType || "file";
  row.append(link, meta);
  return row;
}

/** File entries carried on the terminal get_workflow_output payload. */
export function extractOutputFiles(finalOutput) {
  const outputs = finalOutput?.outputs || {};
  const found = [];
  for (const entry of Object.values(outputs)) {
    if (!entry || typeof entry !== "object") continue;
    for (const raw of entry.files || []) {
      const file = normalizeFile(raw);
      if (file) found.push(file);
    }
    for (const port of Object.values(entry.output_ports_data || {})) {
      for (const raw of port?.files || []) {
        const file = normalizeFile(raw);
        if (file) found.push(file);
      }
    }
  }
  return found;
}

/** Results area: downloadable deliverables only, deduped by id. */
export function renderFileOutputs(container, files) {
  container.textContent = "";
  const seen = new Set();
  const unique = [];
  for (const raw of files || []) {
    const file = normalizeFile(raw);
    if (!file || seen.has(file.id)) continue;
    seen.add(file.id);
    unique.push(file);
  }
  if (!unique.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "The run completed but produced no downloadable files.";
    container.appendChild(empty);
    return;
  }
  const box = document.createElement("div");
  box.className = "files-box";
  for (const file of unique) box.appendChild(fileRow(file));
  container.appendChild(box);
}
