const state = {
  snapshot: null,
  openNodes: new Set(),
  closedNodes: new Set(),
  lastRenderKey: "",
};

const RAW_RECORD_LIMIT = 120;
const DETAIL_ENTRY_LIMIT = 80;
const DETAIL_VALUE_LIMIT = 4000;
const DETAIL_PRIORITY_KEYS = [
  "gen_ai.input.messages",
  "gen_ai.system_instructions",
  "gen_ai.output.messages",
  "gen_ai.tool.call.arguments",
  "gen_ai.tool.call.result",
  "gen_ai.tool.name",
  "tool.name",
  "prompt",
  "user_prompt",
];
const RUN_EVIDENCE_GROUP_TITLES = {
  pipeline_lifecycle: "Pipeline lifecycle",
  normal_chat_after_pipeline: "Normal chat after pipeline",
  other_session_evidence: "Other session evidence",
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function attrEsc(value) {
  return esc(value).replaceAll('"', "&quot;");
}

function nodeId(...parts) {
  return parts.map(value => String(value ?? "")).join("::");
}

function isOpen(node, defaultOpen = false) {
  if (state.openNodes.has(node)) return true;
  if (state.closedNodes.has(node)) return false;
  return defaultOpen;
}

function detailsAttrs(node, defaultOpen = false) {
  return `data-node-id="${attrEsc(node)}"${isOpen(node, defaultOpen) ? " open" : ""}`;
}

async function refresh() {
  const rawMode = document.getElementById("raw-mode").value;
  const response = await fetch(`/api/snapshot?expected_raw_content=${encodeURIComponent(rawMode)}&record_limit=${RAW_RECORD_LIMIT}`);
  const snapshot = await response.json();
  const records = snapshot.records || [];
  const lastRecord = records[records.length - 1] || {};
  const renderKey = [rawMode, snapshot.health?.record_count || 0, records.length, lastRecord.id || ""].join(":");
  if (renderKey === state.lastRenderKey) return;
  state.lastRenderKey = renderKey;
  state.snapshot = snapshot;
  render();
}

function render() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const records = snapshot.records || [];
  renderHealth(snapshot.health);
  renderRuns(snapshot.pipeline.runs || []);
  renderPipeline(snapshot.pipeline.runs || [], records, snapshot.pipeline.unscoped_metrics || []);
  renderAssertions(snapshot.assertions || []);
  renderRaw(records, snapshot.health);
}

function renderHealth(health) {
  document.getElementById("health").innerHTML = `
    <div class="run">
      <strong>${health.record_count} records</strong>
      <div class="muted">memory limit ${health.memory_limit}</div>
      <div class="muted">${esc(health.jsonl_path)}</div>
    </div>`;
  document.getElementById("env").textContent = [
    `export IAC_CODE_TELEMETRY_ENDPOINT=${location.origin}`,
    "export IAC_CODE_ENABLE_LOCAL_TELEMETRY=1",
    "export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT",
  ].join("\n");
}

function renderRuns(runs) {
  document.getElementById("runs").innerHTML = runs.length
    ? runs.map(run => `
      <div class="run">
        <strong>${esc(run.pipeline_name)} / ${esc(run.session_id)}</strong>
        <div class="muted">${run.steps.length} steps · ${run.record_ids.length} records</div>
      </div>`).join("")
    : `<div class="muted">No pipeline records yet.</div>`;
}

function renderPipeline(runs, records, unscopedMetrics) {
  const recordsById = new Map(records.map(record => [record.id, record]));
  const runHtml = runs.length
    ? runs.map(run => {
      const runNode = nodeId("run", run.pipeline_name, run.session_id);
      return `
      <details class="node" ${detailsAttrs(runNode, true)}>
        <summary>${esc(run.pipeline_name)} / ${esc(run.session_id)} <span class="tag">${run.steps.length} steps</span></summary>
        <div class="children">
          ${renderRunEvidence(runNode, run, recordsById)}
          ${run.steps.map(step => renderStep(runNode, step, recordsById)).join("")}
        </div>
      </details>`;
    }).join("")
    : "";
  const metricsHtml = renderUnscopedMetrics(unscopedMetrics);
  document.getElementById("pipeline").innerHTML = runHtml || metricsHtml
    ? `${runHtml}${metricsHtml}`
    : `<div class="muted">Waiting for iac.pipeline.* records.</div>`;
}

function renderRunEvidence(runNode, run, recordsById) {
  const groups = run.evidence_groups || legacyRunEvidenceGroups(run, recordsById);
  return groups
    .filter(group => (group.records || []).length)
    .map(group => renderRunEvidenceGroup(runNode, group))
    .join("");
}

function legacyRunEvidenceGroups(run, recordsById) {
  const stepRecordIds = new Set((run.steps || []).flatMap(step => step.record_ids || []));
  const records = run.evidence_records || (run.record_ids || [])
    .filter(id => !stepRecordIds.has(id))
    .map(id => recordsById.get(id))
    .filter(Boolean);
  return records.length ? [{ id: "other_session_evidence", title: "Other session evidence", records }] : [];
}

function renderRunEvidenceGroup(runNode, group) {
  const records = group.records || [];
  if (!records.length) return "";
  const metrics = records.filter(record => record.kind === "metric");
  const signals = records.filter(record => record.kind !== "metric");
  const evidenceNode = nodeId(runNode, "run-evidence", group.id || group.title);
  const title = group.title || RUN_EVIDENCE_GROUP_TITLES[group.id] || "Other session evidence";
  return `
    <details class="node" ${detailsAttrs(evidenceNode)}>
      <summary>${esc(title)} <span class="tag">${records.length}</span></summary>
      <div class="children">
        ${metrics.length ? `<div class="section-label">Metrics</div>${renderMetricTable(metrics)}` : ""}
        ${signals.length ? `<div class="section-label">Spans & logs</div>` : ""}
        ${signals.map(record => `
          <div class="node">
            <strong>${esc(record.name)}</strong>
            ${renderRecordMeta(record)}
            ${renderRecordDetails("Attributes", record)}
          </div>`).join("")}
      </div>
    </details>`;
}

function renderStep(runNode, step, recordsById) {
  const stepNode = nodeId(runNode, "step", step.step_instance_id || step.step_id, step.step_attempt);
  const tags = [
    `attempt=${esc(step.step_attempt)}`,
    step.sub_pipeline_id ? `sub=${esc(step.sub_pipeline_id)}` : "",
  ].filter(Boolean).map(item => `<span class="tag">${item}</span>`).join(" ");
  return `
    <details class="node" ${detailsAttrs(stepNode)}>
      <summary>${esc(step.step_id)} ${tags}</summary>
      <div class="children">
        <div class="muted">${step.record_ids.length} linked signals</div>
        ${renderLinkedRecords(step, recordsById)}
        ${step.agent_rounds.map(round => renderRound(stepNode, round)).join("") || `<div class="muted">No AgentLoop rounds linked yet.</div>`}
      </div>
    </details>`;
}

function renderRound(stepNode, round) {
  const roundNode = nodeId(stepNode, "round", round.round, round.record_id);
  return `
    <details class="node" ${detailsAttrs(roundNode)}>
      <summary>AgentLoop round ${esc(round.round)} <span class="tag">${recordRef(round)}</span></summary>
      <div class="children">
        ${renderRecordMeta(round)}
        ${renderRecordDetails("Round attributes", round)}
        ${(round.children || []).map(child => renderRoundChild(child)).join("")}
      </div>
    </details>`;
}

function renderRoundChild(child) {
  return `
    <div class="node">
      <strong>${esc(child.name)}</strong>
      ${renderRecordMeta(child)}
      ${renderRecordDetails("Attributes", child)}
    </div>`;
}

function renderLinkedRecords(step, recordsById) {
  const records = step.evidence_records || step.record_ids.map(id => recordsById.get(id)).filter(Boolean);
  if (!records.length) return "";
  const metrics = records.filter(record => record.kind === "metric");
  const signals = records.filter(record => record.kind !== "metric");
  return `
    <details>
      <summary>Step evidence <span class="tag">${records.length}</span></summary>
      <div class="children">
        ${metrics.length ? `<div class="section-label">Metrics</div>${renderMetricTable(metrics)}` : ""}
        ${signals.length ? `<div class="section-label">Spans & logs</div>` : ""}
        ${signals.map(record => `
          <div class="node">
            <strong>${esc(record.name)}</strong>
            ${renderRecordMeta(record)}
            ${renderRecordDetails("Attributes", record)}
          </div>`).join("")}
      </div>
    </details>`;
}

function renderUnscopedMetrics(metrics) {
  if (!metrics.length) return "";
  const records = metrics.map(metric => ({
    id: metric.record_id,
    kind: "metric",
    name: metric.name,
    value: metric.value,
    attributes: metric.attributes || {},
  }));
  return `
    <details class="node" ${detailsAttrs("unscoped-pipeline-metrics")}>
      <summary>Unscoped pipeline metrics <span class="tag">${records.length}</span></summary>
      <div class="children">
        <div class="muted">Metrics without a session_id are shown here instead of creating a selling / unknown run.</div>
        ${renderMetricTable(records)}
      </div>
    </details>`;
}

function renderMetricTable(metrics) {
  return `
    <table class="metric-table">
      <thead>
        <tr><th>Name</th><th>Value</th><th>Status</th><th>Step</th><th>Attempt</th><th>Evidence</th></tr>
      </thead>
      <tbody>
        ${metrics.map(metric => {
          const attrs = metric.attributes || {};
          return `
            <tr>
              <td>${esc(metric.name)}</td>
              <td>${formatInlineValue(metric.value)}</td>
              <td>${esc(attrs.status || "")}</td>
              <td>${esc(attrs.step_id || attrs.sub_step_id || attrs.parent_step_id || "")}</td>
              <td>${esc(attrs.step_attempt || "")}</td>
              <td>${esc(shortId(metric.id || metric.record_id))}</td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

function renderRecordMeta(record) {
  const spanPart = record.span_id ? ` · span ${esc(shortId(record.span_id))}` : "";
  return `<div class="record-meta">${esc(record.kind || "span")}${spanPart} · Local evidence id ${esc(shortId(record.record_id || record.id))}</div>`;
}

function recordRef(record) {
  if (record.span_id) return `span ${shortId(record.span_id)}`;
  return `evidence ${shortId(record.record_id || record.id)}`;
}

function renderRecordDetails(title, record) {
  const attrs = record?.attributes || {};
  const entries = sortedAttributeEntries(attrs).slice(0, DETAIL_ENTRY_LIMIT);
  if (!entries.length) return `<div class="muted">No attributes.</div>`;
  const hiddenCount = Math.max(Object.keys(attrs).length - entries.length, 0);
  return `
    <details class="attrs">
      <summary>${esc(title)} <span class="tag">${entries.length}${hiddenCount ? `+${hiddenCount}` : ""}</span></summary>
      <div class="attr-grid">
        ${entries.map(([key, value]) => `
          <div class="attr-key">${esc(key)}</div>
          <pre class="attr-value">${formatAttrValue(value)}</pre>
        `).join("")}
      </div>
    </details>`;
}

function sortedAttributeEntries(attrs) {
  return Object.entries(attrs).sort(([left], [right]) => attributeRank(left) - attributeRank(right) || left.localeCompare(right));
}

function attributeRank(key) {
  const exactIndex = DETAIL_PRIORITY_KEYS.indexOf(key);
  if (exactIndex >= 0) return exactIndex;
  if (key.startsWith("gen_ai.tool.") || key.startsWith("tool.")) return 20;
  if (key.startsWith("gen_ai.") || key.includes("prompt")) return 30;
  return 100;
}

function formatAttrValue(value) {
  let text;
  if (typeof value === "string") {
    text = value;
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch (_error) {
      text = String(value);
    }
  }
  if (text.length > DETAIL_VALUE_LIMIT) {
    text = `${text.slice(0, DETAIL_VALUE_LIMIT)}\n... truncated ${text.length - DETAIL_VALUE_LIMIT} chars ...`;
  }
  return esc(text);
}

function formatInlineValue(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return esc(value);
  return esc(String(value));
}

function shortId(value) {
  return String(value || "").slice(0, 12);
}

function renderAssertions(assertions) {
  const intro = `
    <div class="assertion-note">
      <strong>Smoke checks</strong>
      <div class="muted">These checks validate debug content capture and pipeline attempt attribution. Evidence IDs are local receiver IDs.</div>
    </div>`;
  document.getElementById("assertions").innerHTML = intro + assertions.map(item => `
    <div class="assertion ${esc(item.status)}">
      <strong>${esc(item.label)}</strong>
      <div>${esc(item.message)}</div>
      <div class="muted">${(item.evidence_ids || []).length ? `Evidence IDs: ${(item.evidence_ids || []).map(esc).join(", ")}` : ""}</div>
    </div>`).join("");
}

function renderRaw(records, health) {
  const total = health?.record_count || records.length;
  const hiddenCount = Math.max(total - records.length, 0);
  const header = hiddenCount
    ? `Showing latest ${records.length} of ${total} records. Use Export JSONL for full payload.\n\n`
    : "";
  const compactRecords = records.slice().reverse().map(compactRecord);
  document.getElementById("raw").textContent = `${header}${JSON.stringify(compactRecords, null, 2)}`;
}

function compactRecord(record) {
  return {
    ...record,
    attributes: compactAttributes(record.attributes || {}),
  };
}

function compactAttributes(attrs) {
  return Object.fromEntries(Object.entries(attrs).map(([key, value]) => [key, compactValue(value)]));
}

function compactValue(value) {
  if (typeof value !== "string") return value;
  if (value.length <= DETAIL_VALUE_LIMIT) return value;
  return `${value.slice(0, DETAIL_VALUE_LIMIT)}\n... truncated ${value.length - DETAIL_VALUE_LIMIT} chars ...`;
}

document.getElementById("pipeline").addEventListener("toggle", event => {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement)) return;
  const node = details.dataset.nodeId;
  if (!node) return;
  if (details.open) {
    state.openNodes.add(node);
    state.closedNodes.delete(node);
  } else {
    state.closedNodes.add(node);
    state.openNodes.delete(node);
  }
}, true);
document.getElementById("clear").addEventListener("click", async () => {
  await fetch("/api/clear", { method: "POST" });
  await refresh();
});
document.getElementById("demo-off").addEventListener("click", async () => {
  document.getElementById("raw-mode").value = "off";
  await fetch("/api/demo?raw_content=off", { method: "POST" });
  await refresh();
});
document.getElementById("demo-on").addEventListener("click", async () => {
  document.getElementById("raw-mode").value = "on";
  await fetch("/api/demo?raw_content=on", { method: "POST" });
  await refresh();
});
document.getElementById("raw-mode").addEventListener("change", refresh);
refresh();
setInterval(refresh, 1000);
