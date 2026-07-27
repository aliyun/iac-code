import { t } from "../i18n.js?v=web-repl-ui-277";
import { renderMermaid, renderMermaidViews, renderDiagramPrice } from "../mermaid_render.js?v=arch-diagram-v5";

function text(value) {
  return value === undefined || value === null ? "" : String(value);
}

function asArray(value) {
  if (Array.isArray(value)) {
    return value;
  }
  if (value && typeof value === "object") {
    return Object.values(value);
  }
  return [];
}

function lastItem(items) {
  return items.length > 0 ? items[items.length - 1] : undefined;
}

function lastMatching(items, predicate) {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (predicate(items[index])) {
      return items[index];
    }
  }
  return undefined;
}

function dedupeBy(items, keyFn) {
  const deduped = new Map();
  for (const item of items) {
    const key = keyFn(item);
    deduped.set(key || `item-${deduped.size}`, item);
  }
  return [...deduped.values()];
}

function eventDataObject(event) {
  return event?.data && typeof event.data === "object" && !Array.isArray(event.data) ? event.data : {};
}

function normalizedEventKind(event = {}) {
  const kind = event.kind || event.eventType || "";
  const aliases = {
    candidate_selected: "candidate.selected",
    cleanup_started: "cleanup.started",
    cleanup_progress: "cleanup_progress",
    cleanup_completed: "cleanup.completed",
    cleanup_failed: "cleanup.failed",
    pipeline_handoff_ready: "pipeline_handoff_ready",
    stack_current_changed: "stack.progress",
    stack_progress: "stack.progress",
    stack_instances_progress: "stack.instances.progress",
  };
  return aliases[kind] || kind;
}

function normalizePipelineEvent(event = {}) {
  const data = eventDataObject(event);
  return {
    ...event,
    ...data,
    kind: normalizedEventKind(event),
    eventType: event.eventType,
    data: event.data,
    rawEvent: event,
  };
}

function normalizedPipelineEvents(state) {
  if (Array.isArray(state.normalizedPipelineEvents)) {
    return state.normalizedPipelineEvents;
  }
  return asArray(state.pipelineEvents).map(normalizePipelineEvent);
}

function isCleanupEvent(event = {}) {
  return ["cleanup.started", "cleanup_progress", "cleanup.completed", "cleanup.failed"].includes(event.kind);
}

function cleanupEventStatus(event = {}) {
  const statusByKind = {
    "cleanup.started": "started",
    cleanup_progress: "in_progress",
    "cleanup.completed": "completed",
    "cleanup.failed": "failed",
  };
  return event.status || statusByKind[event.kind] || "pending";
}

function hasPipelineData(state) {
  return Boolean(
    state?.pipelineSnapshot ||
      asArray(state?.pipelineEvents).length > 0 ||
      asArray(state?.candidateDetails).length > 0 ||
      asArray(state?.diagrams).length > 0 ||
      state?.pipelineDisplayReplay ||
      state?.cleanupStatus ||
      state?.pipelineNotice ||
      state?.pipelineActionResult ||
      state?.pipelineActionError ||
      state?.pipelineError,
  );
}

function displayReplayAttempts(state) {
  return asArray(state.pipelineDisplayReplay?.attempts);
}

function appendReplayMetric(parent, label, value) {
  const content = text(value);
  if (!content) {
    return;
  }
  const row = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = content;
  row.append(term, description);
  parent.append(row);
}

function appendReplayList(parent, label, items, itemText) {
  const values = asArray(items).map(itemText).map(text).filter(Boolean);
  if (values.length === 0) {
    return;
  }
  const wrapper = document.createElement("div");
  wrapper.className = "pipeline-replay-detail-list";
  const title = document.createElement("p");
  title.textContent = label;
  const list = document.createElement("ul");
  for (const value of values) {
    const item = document.createElement("li");
    item.textContent = value;
    list.append(item);
  }
  wrapper.append(title, list);
  parent.append(wrapper);
}

function valueAt(source, keys) {
  for (const key of keys) {
    const value = source?.[key];
    if (value !== undefined && value !== null && value !== "") {
      return value;
    }
  }
  return "";
}

function candidateDetail(candidate) {
  return candidate.detail && typeof candidate.detail === "object" ? candidate.detail : candidate;
}

function candidateName(candidate) {
  const detail = candidateDetail(candidate);
  return text(detail.candidateName || candidate.candidateName || candidate.name || candidate.title || candidate.id || candidate.runId);
}

function candidateIndex(candidate) {
  const detail = candidateDetail(candidate);
  return detail.candidateIndex ?? candidate.candidateIndex ?? candidate.index ?? null;
}

function candidateSummary(candidate) {
  const detail = candidateDetail(candidate);
  return text(detail.summary || candidate.summary || candidate.description || candidate.parentStepId || "");
}

function candidateCostItems(candidate) {
  const detail = candidateDetail(candidate);
  return asArray(detail.costItems || candidate.costItems);
}

function candidateTotalMonthlyCost(candidate) {
  const detail = candidateDetail(candidate);
  return detail.totalMonthlyCost ?? candidate.totalMonthlyCost ?? null;
}

function candidateDedupeKey(candidate) {
  const name = candidateName(candidate);
  const index = candidateIndex(candidate);
  if (name || index !== null) {
    return `${text(index ?? "")}:${name}`;
  }
  return text(candidate.detailId || candidate.toolUseId || candidate.runId || candidate.id);
}

function snapshotStepCandidates(state) {
  const candidates = [];
  for (const step of asArray(state.pipelineSnapshot?.steps)) {
    for (const candidate of asArray(step.candidates)) {
      candidates.push({
        ...candidate,
        candidateName: candidateName(candidate),
        candidateIndex: candidateIndex(candidate),
        parentStepId: step.id || step.runId,
      });
    }
  }
  return candidates;
}

function combinedCandidates(state) {
  return dedupeBy(
    [
      ...snapshotStepCandidates(state),
      ...asArray(state.pipelineSnapshot?.display?.candidateDetails),
      ...asArray(state.candidateDetails),
    ],
    candidateDedupeKey,
  );
}

function combinedDiagrams(state) {
  return dedupeBy(
    [
      ...asArray(state.pipelineSnapshot?.display?.diagrams),
      ...asArray(state.diagrams),
      ...asArray(state.webDiagrams),
    ],
    (diagram) =>
      text(
        diagram.diagramId ||
          diagram.id ||
          diagram.runId ||
          `${diagram.candidateIndex ?? ""}:${diagram.candidateName || ""}`,
      ),
  );
}

function appendMetric(parent, label, value) {
  const row = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value === undefined || value === null || value === "" ? "-" : text(value);
  row.append(term, description);
  parent.append(row);
}

function appendSection(container, titleText, className) {
  const section = document.createElement("section");
  section.className = className;
  const title = document.createElement("h3");
  title.textContent = titleText;
  section.append(title);
  container.append(section);
  return section;
}

function compactValue(value) {
  if (value === undefined || value === null || value === "") {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return text(value);
  }
  if (Array.isArray(value)) {
    return `${value.length} items`;
  }
  return text(
    value.title ||
      value.message ||
      value.summary ||
      value.status ||
      value.kind ||
      value.eventType ||
      value.name ||
      value.id ||
      value.toolUseId ||
      value.eventId ||
      JSON.stringify(value),
  );
}

function eventTitle(event) {
  return text(
    event.text ||
      event.message ||
      event.title ||
      event.kind ||
      event.eventType ||
      event.status ||
      event.eventId ||
      "Pipeline event",
  );
}

function eventMeta(event) {
  return [event.kind || event.eventType, event.status || event.judgeOutcome || event.outcome, event.eventId]
    .map(text)
    .filter(Boolean)
    .join(" · ");
}

function snapshotEvents(snapshot = {}) {
  return [
    ...asArray(snapshot.display?.messages),
    ...asArray(snapshot.display?.events),
    ...asArray(snapshot.control?.inputHistory),
    ...asArray(snapshot.events),
  ];
}

function recoveredActivityItems(state) {
  return [
    {
      key: "artifacts",
      label: "Artifacts",
      items: asArray(state.pipelineSnapshot?.display?.artifacts),
    },
    {
      key: "permissions",
      label: "Permissions",
      items: asArray(state.pipelineSnapshot?.display?.permissions),
    },
    {
      key: "tool-results",
      label: "Tool Results",
      items: asArray(state.pipelineSnapshot?.display?.toolResults),
    },
    {
      key: "pending-input",
      label: "Pending Input",
      items: state.pipelineSnapshot?.pendingInput ? [state.pipelineSnapshot.pendingInput] : [],
    },
    {
      key: "rollbacks",
      label: "Rollbacks",
      items: asArray(state.pipelineSnapshot?.control?.rollbackHistory),
    },
    {
      key: "restarts",
      label: "Restarts",
      items: asArray(state.pipelineSnapshot?.control?.candidateRestarts),
    },
    {
      key: "warnings",
      label: "Warnings",
      items: asArray(state.pipelineSnapshot?.control?.warningHistory),
    },
  ];
}

function appendRecoveryGroup(parent, group) {
  const article = document.createElement("article");
  article.className = "pipeline-recovery-entry";
  article.dataset.recoveryKey = group.key;

  const heading = document.createElement("p");
  heading.className = "pipeline-recovery-heading";
  heading.textContent = `${group.label} · ${group.items.length}`;
  article.append(heading);

  const list = document.createElement("ul");
  list.className = "pipeline-recovery-list";
  for (const item of group.items.slice(-3)) {
    const row = document.createElement("li");
    row.textContent = compactValue(item) || "-";
    list.append(row);
  }
  if (list.children.length === 0) {
    const row = document.createElement("li");
    row.textContent = "-";
    list.append(row);
  }
  article.append(list);
  parent.append(article);
}

function renderRecoveryActivity(container, state) {
  const groups = recoveredActivityItems(state);
  const hasRecoveredActivity = groups.some((group) => group.items.length > 0);
  if (!hasRecoveredActivity) {
    return;
  }
  const section = appendSection(container, t("Recovered State"), "pipeline-recovery");
  const grid = document.createElement("div");
  grid.className = "pipeline-recovery-grid";
  for (const group of groups) {
    appendRecoveryGroup(grid, group);
  }
  section.append(grid);
}

function renderPipelineError(container, state) {
  const message = state.pipelineActionError || state.pipelineError;
  if (!message) {
    return;
  }
  const error = document.createElement("p");
  error.className = "pipeline-error";
  error.textContent = text(message);
  container.append(error);
}

function actionResultMessage(result = {}) {
  if (!result || typeof result !== "object") {
    return "";
  }
  return [
    result.accepted === true ? "accepted" : result.status,
    result.action || result.message || result.detail,
  ]
    .map(text)
    .filter(Boolean)
    .join(" · ");
}

function renderPipelineNotice(container, state) {
  const message = state.pipelineNotice || actionResultMessage(state.pipelineActionResult);
  if (!message) {
    return;
  }
  const notice = document.createElement("p");
  notice.className = "pipeline-notice";
  notice.textContent = text(message);
  container.append(notice);
}

function stepTitle(step) {
  return text(step.title || step.name || step.label || step.id || step.runId || "Pipeline step");
}

function isActiveStep(step, snapshot = {}) {
  if (["working", "running", "waiting_input", "restarting"].includes(text(step.status))) {
    return true;
  }
  const activeRunIds = asArray(snapshot.control?.activeCandidateRunIds);
  return asArray(step.candidates).some((candidate) => activeRunIds.includes(candidate.runId));
}

function stepperItems(state) {
  const snapshot = state.pipelineSnapshot || {};
  const structuredSteps = asArray(state.pipelineSnapshot?.steps).map((step) => ({
    source: "step",
    eventKind: "pipeline.step",
    title: stepTitle(step),
    status: step.status,
    active: isActiveStep(step, snapshot),
    candidates: asArray(step.candidates),
    raw: step,
  }));
  const timelineItems = [...snapshotEvents(snapshot), ...normalizedPipelineEvents(state)].map((event) => ({
    source: "event",
    eventKind: event.kind || event.eventType || "display",
    title: eventTitle(event),
    status: event.status || event.judgeOutcome || event.outcome,
    meta: eventMeta(event),
    raw: event,
  }));
  return structuredSteps.length > 0 ? [...structuredSteps, ...timelineItems.slice(-6)] : timelineItems.slice(-12);
}

function renderDiagnostics(container, state) {
  const snapshot = state.pipelineSnapshot || {};
  const identity = snapshot.identity || {};
  const diagnostics = appendSection(container, t("Diagnostics"), "pipeline-diagnostics");
  const list = document.createElement("dl");
  list.className = "pipeline-metrics";
  appendMetric(list, t("Context"), valueAt(snapshot, ["contextId"]) || identity.contextId || state.currentSession?.contextId);
  appendMetric(list, t("Task"), valueAt(snapshot, ["taskId"]) || identity.taskId || state.currentSession?.taskId);
  appendMetric(list, t("Sequence"), valueAt(snapshot, ["lastSequence"]) || state.lastSequence);
  appendMetric(list, t("Pipeline"), valueAt(snapshot, ["pipelineName"]) || identity.pipelineName || snapshot.display?.pipelineName);
  appendMetric(list, t("Status"), valueAt(snapshot, ["status"]) || snapshot.status?.state || state.currentSession?.status);
  diagnostics.append(list);
}

function renderStepper(container, state) {
  const items = stepperItems(state);
  const stepper = appendSection(container, t("Timeline"), "pipeline-stepper");
  const list = document.createElement("ol");
  list.className = "pipeline-step-list";
  for (const stepItem of items) {
    const item = document.createElement("li");
    item.className = stepItem.active ? "pipeline-step is-active" : "pipeline-step";
    item.dataset.eventKind = text(stepItem.eventKind);
    item.dataset.stepStatus = text(stepItem.status || "");
    item.dataset.stepSource = text(stepItem.source);

    const title = document.createElement("p");
    title.className = "pipeline-step-title";
    title.textContent = stepItem.title;
    const meta = document.createElement("p");
    meta.className = "pipeline-step-meta";
    meta.textContent = [stepItem.status, stepItem.meta, stepItem.active ? t("Active") : ""].map(text).filter(Boolean).join(" · ");
    item.append(title, meta);

    if (asArray(stepItem.candidates).length > 0) {
      const candidateList = document.createElement("ul");
      candidateList.className = "pipeline-step-candidates";
      for (const candidate of asArray(stepItem.raw?.candidates || stepItem.candidates).slice(0, 4)) {
        const candidateItem = document.createElement("li");
        candidateItem.textContent = [candidateName(candidate), candidate.status].map(text).filter(Boolean).join(" · ");
        candidateList.append(candidateItem);
      }
      item.append(candidateList);
    }
    list.append(item);
  }
  if (list.children.length === 0) {
    const empty = document.createElement("p");
    empty.className = "pipeline-muted";
    empty.textContent = t("No pipeline events.");
    stepper.append(empty);
    return;
  }
  stepper.append(list);
}

function selectedCandidate(state) {
  const selected = lastMatching(normalizedPipelineEvents(state), (event) => event.kind === "candidate.selected");
  return {
    candidateName:
      selected?.candidateName ||
      state.pipelineSelectedCandidate?.candidateName ||
      state.pipelineSnapshot?.control?.selectedCandidate?.candidateName,
    candidateIndex:
      selected?.candidateIndex ??
      state.pipelineSelectedCandidate?.candidateIndex ??
      state.pipelineSnapshot?.control?.selectedCandidate?.candidateIndex ??
      null,
  };
}

function candidateKey(candidate) {
  return `${text(candidateIndex(candidate))}:${candidateName(candidate)}`;
}

function pipelineSessionId(state) {
  return text(state.currentSessionId || state.currentSession?.webSessionId || state.currentSession?.sessionId);
}

function parseParameterOverrides(value) {
  const trimmed = text(value).trim();
  if (!trimmed) {
    return {};
  }
  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (_error) {
    throw new Error("Parameter overrides must be a valid JSON object.");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Parameter overrides must be a valid JSON object.");
  }
  return parsed;
}

function setCandidateActionStatus(status, message, kind = "notice") {
  status.className =
    kind === "error"
      ? "pipeline-error pipeline-candidate-action-status"
      : "pipeline-notice pipeline-candidate-action-status";
  status.textContent = text(message);
}

function renderCandidateActions(card, candidate, state, callbacks, isSelected) {
  const actions = document.createElement("div");
  actions.className = "pipeline-candidate-actions";

  const overrides = document.createElement("details");
  overrides.className = "pipeline-candidate-overrides-panel";
  const overridesSummary = document.createElement("summary");
  overridesSummary.textContent = t("Parameter overrides");

  const label = document.createElement("label");
  label.className = "pipeline-candidate-override-label";

  const textarea = document.createElement("textarea");
  textarea.className = "pipeline-candidate-overrides";
  textarea.rows = 3;
  textarea.placeholder = '{"InstanceType":"ecs.g7.large"}';
  label.append(textarea);
  overrides.append(overridesSummary, label);

  const row = document.createElement("div");
  row.className = "pipeline-candidate-action-row";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "pipeline-candidate-select";
  button.textContent = isSelected ? t("Selected") : t("Select candidate");
  button.disabled = isSelected || typeof callbacks.onSelectCandidate !== "function" || !pipelineSessionId(state);

  const status = document.createElement("p");
  status.className = "pipeline-candidate-action-status";
  if (isSelected) {
    status.textContent = t("Selected");
  }

  const handleSelection = async () => {
    let parameterOverrides;
    try {
      parameterOverrides = parseParameterOverrides(textarea.value);
    } catch (error) {
      setCandidateActionStatus(status, error instanceof Error ? error.message : String(error), "error");
      return;
    }

    button.disabled = true;
    status.className = "pipeline-candidate-action-status";
    status.textContent = t("Submitting...");
    try {
      const result = await callbacks.onSelectCandidate({
        sessionId: pipelineSessionId(state),
        candidateName: candidateName(candidate),
        candidateIndex: candidateIndex(candidate),
        parameterOverrides,
      });
      card.className = "pipeline-candidate is-selected";
      button.textContent = t("Selected");
      setCandidateActionStatus(status, actionResultMessage(result) || "accepted", "notice");
    } catch (error) {
      button.disabled = false;
      setCandidateActionStatus(status, error instanceof Error ? error.message : String(error), "error");
    }
  };
  if (typeof button.addEventListener === "function") {
    button.addEventListener("click", handleSelection);
  }

  row.append(button, status);
  actions.append(overrides, row);
  card.append(actions);
}

function appendCandidateDiagram(card, candidate, diagrams) {
  // Match by index first — candidate_index is the duplicate-name discriminator
  // (see show_architecture_diagram tool schema); name is only a fallback.
  // 同一候选可能同时命中「无价的 snapshot/live 图」与「带价的 webDiagram」(dedupeBy
  // 键不同 → 两者共存)。优先挑带价的那张,让 mermaid 与询价同源;无价时回退任意命中,
  // 保证仍能出图(仅显示「暂无询价信息」)。
  const idx = candidateIndex(candidate);
  const nm = candidateName(candidate);
  const byIndex = (d) => idx !== null && d.candidateIndex === idx;
  const byName = (d) => nm && d.candidateName === nm;
  const hasCost = (d) => Boolean(d && (d.totalMonthlyCost || (Array.isArray(d.costItems) && d.costItems.length)));
  const match =
    diagrams.find((d) => byIndex(d) && hasCost(d)) ||
    diagrams.find(byIndex) ||
    diagrams.find((d) => byName(d) && hasCost(d)) ||
    diagrams.find(byName);
  const details = document.createElement("details");
  details.className = "pipeline-candidate-diagram";
  const summary = document.createElement("summary");
  summary.textContent = t("Architecture diagram");
  details.append(summary);
  const body = document.createElement("div");
  body.className = "pipeline-candidate-diagram-body";
  details.append(body);
  // 折叠默认收起;展开时才懒加载 + 渲染(避免一次渲染 N 张)
  details.addEventListener("toggle", async () => {
    if (!details.open || body.dataset.rendered) return;
    body.dataset.rendered = "1";
    if (match?.mermaidSource) {
      // 必须先 await:renderMermaid 以 replaceChildren 收尾,不等它会抹掉后面 append 的询价块。
      if (Array.isArray(match.views) && match.views.length > 1) {
        await renderMermaidViews(body, match.views);
      } else {
        await renderMermaid(body, match.mermaidSource);
      }
      body.append(renderDiagramPrice(match));
    } else {
      body.textContent = t("No architecture diagram");
    }
  });
  card.append(details);
}

function renderCandidates(container, state, callbacks) {
  const candidates = combinedCandidates(state);
  const diagrams = combinedDiagrams(state);
  const selected = selectedCandidate(state);
  const section = appendSection(container, t("Candidates"), "pipeline-candidates");
  if (candidates.length === 0) {
    const empty = document.createElement("p");
    empty.className = "pipeline-muted";
    empty.textContent = t("No candidates.");
    section.append(empty);
    return;
  }

  const list = document.createElement("div");
  list.className = "pipeline-candidate-list";
  for (const candidate of candidates) {
    const card = document.createElement("article");
    const isSelected =
      selected.candidateIndex !== null
        ? candidateIndex(candidate) === selected.candidateIndex
        : Boolean(selected.candidateName && candidateName(candidate) === selected.candidateName);
    card.className = isSelected ? "pipeline-candidate is-selected" : "pipeline-candidate";
    card.dataset.candidateKey = candidateKey(candidate);
    card.dataset.candidateName = candidateName(candidate);

    const heading = document.createElement("h4");
    heading.textContent = candidateName(candidate) || t("Candidate {n}", { n: candidateIndex(candidate) ?? "" }).trim();
    const meta = document.createElement("p");
    meta.className = "pipeline-candidate-meta";
    meta.textContent = [
      candidateIndex(candidate) === null
        ? ""
        : `#${candidateIndex(candidate)}`,
      candidateTotalMonthlyCost(candidate) === null
        ? ""
        : t("Monthly {cost}", { cost: candidateTotalMonthlyCost(candidate) }),
      candidate.status || "",
      isSelected ? t("Selected") : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const summary = document.createElement("p");
    summary.className = "pipeline-candidate-summary";
    summary.textContent = candidateSummary(candidate);
    card.append(heading, meta, summary);

    const costItems = candidateCostItems(candidate);
    if (costItems.length > 0) {
      const costs = document.createElement("ul");
      costs.className = "pipeline-cost-items";
      for (const item of costItems.slice(0, 4)) {
        const cost = document.createElement("li");
        cost.textContent = text(item.name || item.resourceType || item.resource || item.description || JSON.stringify(item));
        costs.append(cost);
      }
      card.append(costs);
    }
    appendCandidateDiagram(card, candidate, diagrams);
    renderCandidateActions(card, candidate, state, callbacks, isSelected);
    list.append(card);
  }
  section.append(list);
}

function renderDiagrams(container, state) {
  const diagrams = combinedDiagrams(state);
  const selected = selectedCandidate(state);
  // Match by index first (duplicate-name discriminator), name as fallback.
  const diagram =
    diagrams.find((item) => selected.candidateIndex !== null && item.candidateIndex === selected.candidateIndex) ||
    diagrams.find((item) => selected.candidateName && item.candidateName === selected.candidateName) ||
    lastItem(diagrams);
  const section = appendSection(container, t("Diagram"), "pipeline-diagram");
  const preview = document.createElement("pre");
  preview.className = "pipeline-diagram-preview";
  preview.textContent = text(diagram?.mermaidSource || diagram?.templateContent || t("No diagram."));
  preview.dataset.candidateName = text(diagram?.candidateName);
  section.append(preview);
}

function progressEvents(state) {
  return normalizedPipelineEvents(state).filter((event) =>
    ["stack.progress", "stack.instances.progress"].includes(event.kind),
  );
}

function snapshotProgressEvents(state) {
  const stacks = state.pipelineSnapshot?.stacks || {};
  const stackGroups = state.pipelineSnapshot?.stackGroups || {};
  return dedupeBy(
    [
      stacks.current,
      ...asArray(stacks.byId),
      ...asArray(stacks.history),
      stackGroups.current,
      ...asArray(stackGroups.byId),
      ...asArray(stackGroups.history),
    ].filter(Boolean),
    (item) => text(item.eventId || item.stackId || item.stackGroupName || item.operationId || item.id || item.runId),
  ).map((item) => ({
    kind: item.kind || (item.stackGroupName || item.operationId ? "stack.instances.progress" : "stack.progress"),
    ...item,
  }));
}

function renderProgress(container, state) {
  const section = appendSection(container, t("Deployment"), "pipeline-progress");
  const list = document.createElement("div");
  list.className = "pipeline-progress-list";
  for (const event of [...snapshotProgressEvents(state), ...progressEvents(state)]) {
    const row = document.createElement("article");
    row.className = "pipeline-progress-item";
    row.dataset.progressKind = text(event.kind);
    const title = document.createElement("p");
    title.className = "pipeline-progress-title";
    title.textContent = text(event.stackName || event.stackGroupName || event.stackId || event.operationId || event.kind);
    const meta = document.createElement("p");
    meta.className = "pipeline-progress-meta";
    const progressStatus = event.status || event.stackStatus || event.progressStatus;
    meta.textContent = [
      event.kind,
      event.stackId || event.operationId,
      event.regionId,
      progressStatus,
      event.progressPercentage ?? event.progress,
      event.deploymentSucceeded === true ? "deploymentSucceeded" : "",
      event.deploymentComplete === true ? "deploymentComplete" : "",
    ]
      .map(text)
      .filter(Boolean)
      .join(" · ");
    row.append(title, meta);
    list.append(row);
  }
  if (list.children.length === 0) {
    const empty = document.createElement("p");
    empty.className = "pipeline-muted";
    empty.textContent = t("No deployment progress.");
    section.append(empty);
    return;
  }
  section.append(list);
}

function cloneCleanupSource(cleanup = {}) {
  return {
    ...cleanup,
    resources: asArray(cleanup.resources).map((resource) => ({ ...resource })),
    errors: asArray(cleanup.errors || cleanup.currentErrors),
  };
}

function optionalFieldMatches(existing, incoming, ...keys) {
  const existingValue = keys.map((key) => existing[key]).find((value) => value !== undefined && value !== null && value !== "");
  const incomingValue = keys.map((key) => incoming[key]).find((value) => value !== undefined && value !== null && value !== "");
  return !existingValue || !incomingValue || existingValue === incomingValue;
}

function mergeCleanupResource(resources, incoming) {
  const resourceId = incoming.resourceId || incoming.stackId || incoming.name || incoming.logicalResourceId;
  if (!resourceId) {
    return resources;
  }
  const incomingResource = { ...incoming, resourceId };
  const index = resources.findIndex((resource) => {
    const sameId =
      (resource.resourceId || resource.stackId || resource.name || resource.logicalResourceId) === resourceId;
    if (!sameId) {
      return false;
    }
    return (
      optionalFieldMatches(resource, incomingResource, "provider") &&
      optionalFieldMatches(resource, incomingResource, "resourceType", "resource_type") &&
      optionalFieldMatches(resource, incomingResource, "regionId", "region")
    );
  });
  if (index >= 0) {
    resources[index] = { ...resources[index], ...incomingResource };
    return resources;
  }
  resources.push(incomingResource);
  return resources;
}

function cleanupResourceStatus(resource = {}) {
  const primary = resource.cleanupStatus || resource.status || resource.resourceStatus;
  const detail = resource.stackStatus || resource.progressStatus;
  if (primary && detail && primary !== detail) {
    return `${primary} · ${detail}`;
  }
  return primary || detail;
}

function isCompletedCleanupStatus(status) {
  const normalized = text(status).toLowerCase();
  return normalized === "completed" || normalized === "skipped" || normalized.includes("complete");
}

function aggregateCleanupStatus(resources, fallback) {
  const statuses = resources.map(cleanupResourceStatus).map((status) => text(status).toLowerCase()).filter(Boolean);
  if (statuses.some((status) => status.includes("fail") || status.includes("error"))) {
    return "failed";
  }
  if (statuses.some((status) => status.includes("progress") || status === "running" || status === "started")) {
    return "in_progress";
  }
  if (statuses.some((status) => status === "pending" || status.includes("pending"))) {
    return "pending";
  }
  if (resources.length > 0 && statuses.length === resources.length && statuses.every(isCompletedCleanupStatus)) {
    return "completed";
  }
  return fallback || "none";
}

function reduceCleanupState(state) {
  const base = cloneCleanupSource(state.cleanupStatus || state.pipelineSnapshot?.cleanup || {});
  let cleanup = {
    status: base.status || base.rawStatus || "none",
    resourceCount: base.resourceCount,
    resources: base.resources,
    errors: base.errors,
    statusMessage: base.statusMessage || base.message,
    blocksNormalChat: base.blocksNormalChat,
  };
  for (const event of normalizedPipelineEvents(state).filter(isCleanupEvent)) {
    const eventStatus = cleanupEventStatus(event);
    cleanup = {
      ...cleanup,
      ...event,
      status: eventStatus,
      resources: cleanup.resources,
      errors: cleanup.errors,
    };
    const eventResources = asArray(event.resources);
    if (eventResources.length > 0) {
      for (const resource of eventResources) {
        mergeCleanupResource(cleanup.resources, resource);
      }
    } else {
      mergeCleanupResource(cleanup.resources, event);
    }
    if (event.error || event.errorMessage || event.errorSummary || event.lastError) {
      cleanup.errors = [
        ...cleanup.errors,
        event.error || event.errorMessage || event.errorSummary || event.lastError,
      ];
    }
    cleanup.resourceCount = Number.isFinite(event.resourceCount) ? event.resourceCount : cleanup.resources.length;
    cleanup.status = aggregateCleanupStatus(cleanup.resources, eventStatus || cleanup.status);
  }
  cleanup.resourceCount = cleanup.resourceCount ?? cleanup.resources.length;
  cleanup.status = aggregateCleanupStatus(cleanup.resources, cleanup.status);
  return cleanup;
}

function cleanupSource(state) {
  return reduceCleanupState(state);
}

function renderCleanup(container, state) {
  const cleanup = cleanupSource(state);
  const resources = asArray(cleanup.resources);
  const section = appendSection(container, t("Cleanup"), "pipeline-cleanup");
  section.dataset.cleanupStatus = text(cleanup.status || cleanup.rawStatus || "none");
  const summary = document.createElement("p");
  summary.className = "pipeline-cleanup-summary";
  summary.textContent = [
    cleanup.status || cleanup.rawStatus || "none",
    t("{n} resources", { n: cleanup.resourceCount ?? resources.length }),
    cleanup.blocksNormalChat ? "blocking" : "",
    cleanup.message || cleanup.statusMessage,
  ]
    .map(text)
    .filter(Boolean)
    .join(" · ");
  section.append(summary);

  const table = document.createElement("table");
  table.className = "pipeline-cleanup-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of [t("Resource"), t("Status"), t("Region")]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const resource of resources.slice(0, 8)) {
    const row = document.createElement("tr");
    for (const value of [
      resource.resourceId || resource.stackId || resource.name || resource.logicalResourceId,
      cleanupResourceStatus(resource),
      resource.regionId || resource.region,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = text(value || "-");
      row.append(cell);
    }
    body.append(row);
  }
  if (body.children.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = t("No cleanup resources.");
    row.append(cell);
    body.append(row);
  }
  table.append(body);
  section.append(table);

  const errors = asArray(cleanup.errors || cleanup.currentErrors);
  if (errors.length > 0) {
    const errorList = document.createElement("ul");
    errorList.className = "pipeline-cleanup-errors";
    for (const error of errors.slice(0, 4)) {
      const item = document.createElement("li");
      item.textContent = text(error.message || error.error || error);
      errorList.append(item);
    }
    section.append(errorList);
  }
}

function renderDisplayReplay(container, state) {
  const replay = state.pipelineDisplayReplay;
  const attempts = displayReplayAttempts(state);
  if (!replay || attempts.length === 0) {
    return;
  }
  const section = appendSection(container, replay.pipelineName || t("Pipeline replay"), "pipeline-display-replay");
  const meta = document.createElement("p");
  meta.className = "pipeline-replay-meta";
  meta.textContent = [
    replay.completed ? "completed" : replay.failed ? "failed" : replay.interrupted ? "interrupted" : "",
    replay.durationS ? `${Math.round(Number(replay.durationS))}s` : "",
    t("{n} steps", { n: attempts.length }),
  ]
    .map(text)
    .filter(Boolean)
    .join(" · ");
  section.append(meta);

  const list = document.createElement("ol");
  list.className = "pipeline-replay-list";
  for (const attempt of attempts) {
    const item = document.createElement("li");
    item.className = "pipeline-replay-step";
    item.dataset.stepStatus = text(attempt.status || "");

    const title = document.createElement("p");
    title.className = "pipeline-replay-title";
    title.textContent = [
      attempt.index && attempt.total ? `${attempt.index}/${attempt.total}` : "",
      attempt.stepId || attempt.stepType || "step",
    ]
      .map(text)
      .filter(Boolean)
      .join(" ");

    const summary = document.createElement("p");
    summary.className = "pipeline-replay-summary";
    summary.textContent = [attempt.status, attempt.summary, attempt.rollbackReason].map(text).filter(Boolean).join(" · ");

    item.append(title, summary);
    const tools = asArray(attempt.tools);
    if (tools.length > 0) {
      const toolLine = document.createElement("p");
      toolLine.className = "pipeline-replay-tools";
      toolLine.textContent = tools.map((tool) => text(tool.name || tool.toolName)).filter(Boolean).join(" · ");
      item.append(toolLine);
    }

    const details = document.createElement("details");
    details.className = "pipeline-replay-details";
    const detailsSummary = document.createElement("summary");
    detailsSummary.textContent = t("Details");
    const metrics = document.createElement("dl");
    metrics.className = "pipeline-replay-detail-metrics";
    appendReplayMetric(metrics, t("Step"), attempt.stepId);
    appendReplayMetric(metrics, t("Attempt"), attempt.attemptNo);
    appendReplayMetric(metrics, t("Type"), attempt.stepType);
    appendReplayMetric(metrics, t("Mode"), attempt.uiMode);
    appendReplayMetric(metrics, t("Transcript"), attempt.transcriptId);
    appendReplayMetric(metrics, t("Rollback"), attempt.rollbackReason);
    appendReplayMetric(metrics, t("Error"), attempt.error);
    details.append(detailsSummary, metrics);
    appendReplayList(details, t("Tools"), tools, (tool) => [tool.name || tool.toolName, tool.toolUseId].map(text).filter(Boolean).join(" · "));
    appendReplayList(details, t("Sub-pipelines"), Object.values(attempt.subPipelines || {}), (sub) =>
      [sub.subPipelineName || sub.subPipelineId, sub.status].map(text).filter(Boolean).join(" · "),
    );
    const candidates = Object.values(attempt.candidateSelection?.candidates || {});
    appendReplayList(details, t("Candidates"), candidates, (candidate) =>
      [candidate.candidateIndex, candidate.name, candidate.totalMonthlyCost].map(text).filter(Boolean).join(" · "),
    );
    if (details.children.length > 1) {
      item.append(details);
    }
    list.append(item);
  }
  section.append(list);
}

function handoffSource(state) {
  const snapshot = state.pipelineSnapshot || {};
  const normalHandoff = state.pipelineSnapshot?.normalHandoff;
  const handoffEvent =
    lastMatching(normalizedPipelineEvents(state), (event) => event.kind === "pipeline_handoff_ready") ||
    lastMatching(normalizedPipelineEvents(state), (event) => event.kind === "pipeline.interrupt.judged");
  return (
    handoffEvent ||
    normalHandoff ||
    snapshot.control?.handoff ||
    snapshot.display?.handoff ||
    {}
  );
}

function renderHandoff(container, state) {
  const handoff = handoffSource(state);
  const section = appendSection(container, t("Handoff"), "pipeline-handoff");
  const list = document.createElement("dl");
  list.className = "pipeline-metrics";
  appendMetric(list, t("Mode"), handoff.targetNormalMode || handoff.targetMode || handoff.mode);
  appendMetric(list, t("Outcome"), handoff.outcome || handoff.judgeOutcome || handoff.status);
  appendMetric(list, t("Summary"), handoff.summary || handoff.message || handoff.reason);
  section.append(list);
}

export function renderPipelineWorkspace(state = {}, callbacks = {}) {
  const renderState = {
    ...state,
    normalizedPipelineEvents: normalizedPipelineEvents(state),
  };
  const container = document.createElement("div");
  container.className = "pipeline-workspace";
  if (!hasPipelineData(renderState)) {
    const empty = document.createElement("p");
    empty.className = "pipeline-workspace-empty";
    empty.textContent = t("No pipeline data.");
    container.append(empty);
    return container;
  }

  renderPipelineError(container, renderState);
  renderPipelineNotice(container, renderState);
  const main = document.createElement("div");
  main.className = "pipeline-workspace-main";
  const leftColumn = document.createElement("div");
  leftColumn.className = "pipeline-column pipeline-column-side";
  const rightColumn = document.createElement("div");
  rightColumn.className = "pipeline-column pipeline-column-main";
  main.append(leftColumn, rightColumn);
  container.append(main);

  renderDiagnostics(leftColumn, renderState);
  renderStepper(leftColumn, renderState);
  renderDisplayReplay(rightColumn, renderState);
  renderCandidates(rightColumn, renderState, callbacks);
  renderDiagrams(rightColumn, renderState);
  renderProgress(rightColumn, renderState);
  renderRecoveryActivity(rightColumn, renderState);
  renderCleanup(rightColumn, renderState);
  renderHandoff(rightColumn, renderState);
  return container;
}
