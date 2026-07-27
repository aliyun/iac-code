import * as api from "./api.js?v=web-repl-ui-159";
import { createComposerController } from "./components/composer.js?v=session-model-v15";
import { renderBlockingPanels } from "./components/blocking.js?v=blocking-keys-v4";
import { renderPipelineWorkspace } from "./components/pipeline.js?v=pipeline-arch-v5";
import { renderToolCards, applyShimmerPhase, applySpinPhase } from "./components/tool_cards.js?v=live-inline-tools-v21";
import { createWorkspaceController } from "./components/workspace.js?v=cloud-creds-v48";
import { createOutputController } from "./components/output_panel.js?v=output-panel-v16";
import { reduceEvent } from "./events.js?v=web-repl-ui-196";
import { applyDomI18n, t } from "./i18n.js?v=web-repl-ui-277";

const root = document.getElementById("iac-code-web-root");
const COMMAND_PALETTE_ITEMS = [
  { id: "new-thread", label: t("New chat"), detail: t("Start a new working conversation.") },
  // 「设置」拆成三条独立命令,分别直达常规/模型/云凭证配置页(对应 workspace tab id
  // other/model/cloud);流水线、状态不再作为命令项(各有其编程式入口)。
  { id: "settings-general", label: t("General configuration"), detail: t("General options and foreign session visibility."), tab: "other" },
  { id: "settings-model", label: t("Models"), detail: t("Configure models and providers."), tab: "model" },
  { id: "settings-cloud", label: t("Cloud credentials"), detail: t("Configure cloud account access credentials."), tab: "cloud" },
  { id: "memory", label: t("Memory"), detail: t("Edit project and user memory."), tab: "memory" },
  { id: "skills", label: t("Plugins"), detail: t("View and enable IaC plugins."), tab: "skills" },
];
// 聊天分组最多展示 9 条,恰好对应 ⌘1–9 快捷键,避免结果过长且每条都有快捷键。
const PALETTE_CHAT_LIMIT = 9;
// spotlight 面板的当前扁平结果(进行中→聊天→命令),用于 ⌘1–9 选中;
// paletteSearchToken 做异步竞态守卫,晚到的旧响应丢弃。
let paletteResults = [];
let paletteSearchToken = 0;
let paletteSearchTimer = null;
const PROJECT_THREAD_PREVIEW_LIMIT = 5;
const PROJECT_THREAD_EXPANDED_LIMIT = 200;
const DEFAULT_PIPELINE_NAME = "selling";
const PIPELINE_OPTIONS = [
  { id: DEFAULT_PIPELINE_NAME, label: t("Sales pipeline"), detail: t("Pipeline planning, generation, and validation for sales scenarios") },
];
const markdownRenderer =
  typeof window !== "undefined" && typeof window.markdownit === "function"
    ? window.markdownit({
        html: false,
        linkify: true,
        typographer: false,
      })
    : null;

function byShell(name) {
  return root?.querySelector(`[data-app-shell="${name}"]`) || null;
}

function text(value) {
  return value === undefined || value === null ? "" : String(value);
}

// 后端在首屏把新会话默认(权限模式 / 会话模式)注入 <body> data 属性,读一次做模块级常量,
// 让页面加载时创建的草稿即刻采用,避免异步拉取造成的闪烁。值由服务端 get_session_defaults 校验过;
// Node 静态测试无 body dataset → 全部回落安全默认(权限=请求批准、模式=普通)。
const SESSION_DEFAULTS = readInjectedSessionDefaults();

// 权限空→default、模式仅 pipeline/normal、流水线空→缺省;首屏注入与保存回写共用同一套规范化,防两处漂移。
export function normalizeSessionDefaults(raw = {}) {
  return {
    permissionMode: text(raw.permissionMode).trim() || "default",
    mode: text(raw.mode).trim() === "pipeline" ? "pipeline" : "normal",
    pipelineName: text(raw.pipelineName).trim() || DEFAULT_PIPELINE_NAME,
  };
}

function readInjectedSessionDefaults() {
  const dataset = (typeof document !== "undefined" && document.body?.dataset) || {};
  return normalizeSessionDefaults({
    permissionMode: dataset.defaultPermissionMode,
    mode: dataset.defaultMode,
    pipelineName: dataset.defaultPipelineName,
  });
}

// 设置面板保存「新会话默认」后回写前端:①更新内存常量,供之后无草稿时 makeNewSessionDraft 回落;
// ②当前若有活跃草稿,立即用新默认覆盖其 mode/permissionMode/pipelineName——否则残留草稿的旧字段会在
// makeNewSessionDraft 里抢先于 SESSION_DEFAULTS,造成「改了默认要刷新页面才生效」。草稿已选的 cwd 保留。
export function applySessionDefaults(next = {}) {
  const normalized = normalizeSessionDefaults(next);
  Object.assign(SESSION_DEFAULTS, normalized);
  if (state.newSessionDraft?.active) {
    state.newSessionDraft = {
      ...state.newSessionDraft,
      mode: normalized.mode,
      permissionMode: normalized.permissionMode,
      pipelineName: normalized.pipelineName,
    };
    render(state);
  }
}

function displaySessionId(session) {
  return session?.webSessionId || session?.sessionId || "";
}

function currentThreadTitle(session = {}) {
  return text(session.title || session.sessionId || "Ready");
}

// 后端 currentSession.title 只反映手动重命名,新会话恒为 "(empty)";侧边栏却用「最后一条、
// 退而取首条用户 prompt」派生标题(见 services/session_index 的 auto_title)。header/重命名框
// 若直接用 currentSession.title 就会一直显示 "(empty)" 或退化成 sessionId,与侧边栏不一致。
function hasMeaningfulThreadTitle(session = {}) {
  const raw = text(session.title);
  return Boolean(raw) && raw !== "(empty)";
}

// 按侧边栏同样的规则从已加载消息派生标题:取显示顺序最靠后的用户消息(stored 在前、再按 sequence),
// 换行折成空格、去空白、200 字符封顶——与 session_index._trim_title 对齐。
export function deriveThreadTitleFromMessages(state = {}) {
  let best = "";
  let bestKey = null;
  for (const message of Object.values(state.messages || {})) {
    if (message.role !== "user") {
      continue;
    }
    const flat = text(message.text ?? message.content ?? "").replace(/\n/g, " ").trim();
    if (!flat) {
      continue;
    }
    const group = message.stored ? 0 : 1;
    const sequence = Number.isFinite(message.sequence) ? message.sequence : 0;
    const key = group * 1e12 + sequence;
    if (bestKey === null || key >= bestKey) {
      bestKey = key;
      best = flat;
    }
  }
  if (!best) {
    return "";
  }
  return best.length > 200 ? `${best.slice(0, 200).trimEnd()}…` : best;
}

// 从已加载消息派生「本对话」输入历史(oldest→newest),供 composer 方向键召回。
// 排序与 deriveThreadTitleFromMessages 对齐:先 stored(在前)再 sequence 升序。
export function orderedUserInputs(messages = {}) {
  return Object.values(messages)
    .filter((message) => message.role === "user")
    .map((message) => ({
      group: message.stored ? 0 : 1,
      sequence: Number.isFinite(message.sequence) ? message.sequence : 0,
      value: text(message.text ?? message.content ?? "").trim(),
    }))
    .filter((entry) => entry.value !== "")
    .sort((left, right) => left.group - right.group || left.sequence - right.sequence)
    .map((entry) => entry.value);
}

// header 与重命名框统一用这个解析器:有意义的后端标题优先,否则回退到消息派生标题,保持与侧边栏一致。
export function resolveThreadTitle(state = {}) {
  const session = state.currentSession || {};
  if (hasMeaningfulThreadTitle(session)) {
    return currentThreadTitle(session);
  }
  return deriveThreadTitleFromMessages(state) || currentThreadTitle(session);
}

function sameSession(left = {}, right = {}, fallbackId = "") {
  const leftDisplayId = displaySessionId(left);
  const rightDisplayId = displaySessionId(right);
  if (leftDisplayId && rightDisplayId && leftDisplayId === rightDisplayId) {
    return true;
  }
  if (fallbackId && leftDisplayId === fallbackId) {
    return true;
  }
  return Boolean(left.sessionId && right.sessionId && left.sessionId === right.sessionId && left.cwd === right.cwd);
}

function replaceUpdatedSessionInState(currentState, updatedSession) {
  const fallbackId = currentState.currentSessionId || "";
  const mergedSession = {
    ...(currentState.currentSession || {}),
    ...(updatedSession || {}),
  };
  const replaceSession = (session) => (sameSession(session, mergedSession, fallbackId) ? { ...session, ...mergedSession } : session);
  return {
    ...currentState,
    currentSession: mergedSession,
    currentSessionId: displaySessionId(mergedSession) || fallbackId,
    sessions: (currentState.sessions || []).map(replaceSession),
    pinnedSessions: (currentState.pinnedSessions || []).map(replaceSession),
    pinnedProjects: (currentState.pinnedProjects || []).map((group) => ({
      ...group,
      sessions: (group.sessions || []).map(replaceSession),
    })),
    projectGroups: (currentState.projectGroups || []).map((group) => ({
      ...group,
      sessions: (group.sessions || []).map(replaceSession),
    })),
  };
}

function structuredFallbackText(value) {
  try {
    const rendered = JSON.stringify(value, null, 2);
    return rendered === undefined ? "" : rendered;
  } catch (_error) {
    if (typeof value === "object") {
      return "[unserializable object]";
    }
    return text(value);
  }
}

function structuredContentText(value) {
  if (value === undefined || value === null || value === "") {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.map(structuredContentText).filter(Boolean).join("\n");
  }
  if (typeof value !== "object") {
    return text(value);
  }

  const pieces = [];
  for (const key of ["text", "content", "input", "value", "message", "summary", "description"]) {
    if (value[key] !== undefined && value[key] !== null && value[key] !== "") {
      const rendered = structuredContentText(value[key]);
      if (rendered) {
        pieces.push(rendered);
      }
    }
  }
  if (pieces.length > 0) {
    return pieces.join("\n");
  }
  return structuredFallbackText(value);
}

export function messageText(message = {}) {
  if (typeof message.text === "string") {
    return message.text;
  }
  if (typeof message.content === "string") {
    return message.content;
  }
  const content = structuredContentText(message.content);
  if (content) {
    return content;
  }
  return "";
}

export function renderMarkdownInto(target, source) {
  const content = text(source);
  if (!target) {
    return;
  }
  if (!content) {
    target.replaceChildren();
    return;
  }
  if (markdownRenderer) {
    target.innerHTML = markdownRenderer.render(content);
    return;
  }
  target.textContent = content;
}

function normalizeStoredMessage(message, index) {
  const role = message.role === "user" ? "user" : "assistant";
  const content = messageText(message);
  return {
    messageId: message.id || message.messageId || `stored-${index}`,
    role,
    text: content,
    content,
    kind: typeof message.kind === "string" ? message.kind : "",
    pipelineStep: message.pipelineStep && typeof message.pipelineStep === "object" ? message.pipelineStep : null,
    thinking: typeof message.thinking === "string" ? message.thinking : "",
    toolUseIds: Array.isArray(message.toolUseIds) ? message.toolUseIds.map(text).filter(Boolean) : [],
    blocks: Array.isArray(message.blocks) ? message.blocks : [],
    status: "completed",
    sequence: index + 1,
    stored: true,
    elapsedSeconds:
      typeof message.elapsedSeconds === "number" && Number.isFinite(message.elapsedSeconds)
        ? message.elapsedSeconds
        : 0,
  };
}

function dedupeReplayMessages(messages = {}) {
  return { ...messages };
}

// 把 /messages(load_visible_transcript)响应规整成 { messages, tools },loadSession 与
// 流水线实时轮询共用,保证两条路径的消息/工具形态完全一致。
function buildStoredTranscript(storedMessages) {
  const messages = {};
  for (const [index, message] of (storedMessages?.messages || []).entries()) {
    const normalized = normalizeStoredMessage(message, index);
    messages[normalized.messageId] = normalized;
  }
  const tools =
    storedMessages?.tools && typeof storedMessages.tools === "object" && !Array.isArray(storedMessages.tools)
      ? storedMessages.tools
      : {};
  return { messages, tools };
}

function reduceAndDedupe(state, event) {
  const reduced = reduceEvent(state, event);
  return {
    ...reduced,
    messages: dedupeReplayMessages(reduced.messages),
  };
}

function emptyState() {
  return {
    messages: {},
    tools: {},
    localShell: {},
    permissions: {},
    questions: {},
    resolvedPermissions: {},
    resolvedQuestions: {},
    queuedInputs: [],
    queuedInputsSeedSequence: 0,
    commands: [],
    pinnedSessions: [],
    pinnedProjects: [],
    projectGroups: [],
    sessions: [],
    currentTurnActive: false,
    lastSequence: 0,
    newSessionDraft: null,
    inlineSessionStatus: null,
    inlineMcpStatus: null,
    webDiagrams: [],
    webCandidates: [],
  };
}

function setField(name, value) {
  const target = root?.querySelector(`[data-field="${name}"]`);
  if (target) {
    target.textContent = text(value);
  }
}

function openWorkspaceModal(tab = "settings") {
  workspace?.setActiveTab(tab);
  const modal = byShell("workspace-modal");
  if (!modal) {
    return;
  }
  modal.hidden = false;
  document.body?.classList?.add("workspace-modal-open");
  byShell("workspace-modal-close")?.focus?.();
}

function closeWorkspaceModal() {
  const modal = byShell("workspace-modal");
  if (!modal) {
    return;
  }
  modal.hidden = true;
  document.body?.classList?.remove("workspace-modal-open");
  // 设置弹窗里可能刚配置/激活了 provider，回到会话时让 composer 重取整表，
  // 使新变绿的 provider 立即出现在会话切换菜单，无需刷新整页。
  composer?.refreshProviders?.();
}

function formatStatusNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "0";
  }
  return new Intl.NumberFormat("en-US").format(number);
}

function statusSessionId(status = {}) {
  return text(status.sessionId || status.webSessionId || state.currentSessionId || displaySessionId(state.currentSession));
}

function statusContextUsage(status = {}) {
  const contextUsage = status.contextUsage || status.context_usage;
  return contextUsage && typeof contextUsage === "object" ? contextUsage : {};
}

function deriveContextUsageWindows(state = {}) {
  const windows = state.activeContextWindows;
  if (!windows || typeof windows !== "object") {
    return [];
  }
  return Object.values(windows)
    .filter((win) => win && typeof win === "object" && win.contextUsage)
    .sort((a, b) => String(a.groupId).localeCompare(String(b.groupId)));
}

// 无活跃步骤窗口(选择门 / 步骤间隙 / reload 后 —— 上下文用量不持久化)时,composer 会回退到单主环。
// 普通会话该环标「普通会话」；但流水线会话此刻并非普通对话,标「普通会话」会误导(见选择门问题)。
// 流水线会话回退时:优先用正在等待用户输入的步骤名(方案选择 / 提问),否则退到流水线名。
// 返回空串表示「用 composer 默认的普通会话标签」。
export function deriveContextFallbackLabel(state = {}, session = {}) {
  if (session?.mode !== "pipeline") {
    return "";
  }
  const messages = state?.messages && typeof state.messages === "object" ? Object.values(state.messages) : [];
  const awaitingStep = messages
    .filter((m) => m?.pipelineStep && String(m.pipelineStep.status || "") === "input" && m.pipelineStep.title)
    .sort((a, b) => (Number(b.sequence) || 0) - (Number(a.sequence) || 0))[0];
  if (awaitingStep) {
    return String(awaitingStep.pipelineStep.title);
  }
  return typeof session.pipelineName === "string" ? session.pipelineName : "";
}

function statusUsage(status = {}) {
  const usage = status.usage && typeof status.usage === "object" ? status.usage : {};
  const contextUsage = statusContextUsage(status);
  // 普通会话的 contextUsage 经后端 _camelize 是 camelCase;而流水线每步窗口(pipeline.step.context)
  // 的 contextUsage 是 ContextManager.get_usage() 原样 dict,SSE 不改键名 → snake_case。此处两种都读,
  // 与 composer 圆环读取器口径一致,令 /status 文字版对流水线步骤也能算出百分比。
  const totalTokens = Number(
    contextUsage.totalTokens ||
      contextUsage.total_tokens ||
      contextUsage.usedTokens ||
      contextUsage.used_tokens ||
      usage.totalTokens ||
      0,
  );
  const inputTokens = Number(usage.inputTokens || 0);
  const outputTokens = Number(usage.outputTokens || 0);
  const contextLimit = Number(
    contextUsage.contextWindow ||
      contextUsage.context_window ||
      contextUsage.maxTokens ||
      contextUsage.max_tokens ||
      status.contextWindowTokens ||
      status.contextLimitTokens ||
      status.contextLimit ||
      usage.contextWindowTokens ||
      0,
  );
  const percent = contextLimit > 0 && totalTokens >= 0 ? Math.min(100, Math.round((totalTokens / contextLimit) * 100)) : 0;
  return {
    totalTokens,
    inputTokens,
    outputTokens,
    recordedEvents: Number(usage.recordedEvents || 0),
    contextLimit,
    percent,
  };
}

function formatCompactTokenLimit(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) {
    return "0";
  }
  if (number >= 1000) {
    return `${Math.round(number / 1000)}K`;
  }
  return formatStatusNumber(number);
}

export function statusUsageText(status = {}) {
  const usage = statusUsage(status);
  if (usage.totalTokens <= 0 && usage.recordedEvents <= 0) {
    return t("No context usage recorded yet");
  }
  if (usage.contextLimit > 0) {
    const remainingPercent = Math.max(0, 100 - usage.percent);
    return t("{percent} left (used {used} of {limit})", {
      percent: `${remainingPercent}%`,
      used: formatStatusNumber(usage.totalTokens),
      limit: formatCompactTokenLimit(usage.contextLimit),
    });
  }
  const tokenText = t("Used {n} tokens", { n: formatStatusNumber(usage.totalTokens) });
  const details = [
    usage.inputTokens > 0 ? t("Input {n}", { n: formatStatusNumber(usage.inputTokens) }) : "",
    usage.outputTokens > 0 ? t("Output {n}", { n: formatStatusNumber(usage.outputTokens) }) : "",
  ]
    .filter(Boolean)
    .join(" / ");
  return `${tokenText}${details ? `（${details}）` : ""}`;
}

// 流水线会话:每个活跃步骤/候选窗口渲染一行,与 composer 圈圈同源(deriveContextUsageWindows)。
// 标签固定为「背景信息」(t("Context:")),步骤/候选名(圈圈 tooltip 的「候选名 · 步骤名」拼法)挪到
// value 前缀,后接面板既有 statusUsageText(「剩余%」措辞)——否则长步骤名塞进窄 <dt> 会折行,把
// 「剩余%」挤到错位(排版问题)。无活跃窗口(普通会话 / 步骤间隙 / reload 后上下文不持久化)时退回
// 单条会话级 Context 行,与圈圈退化行为一致。
function statusContextRows(status = {}, windows = []) {
  if (!Array.isArray(windows) || !windows.length) {
    return [{ label: t("Context:"), value: statusUsageText(status) }];
  }
  return windows.map((win) => {
    const title = typeof win?.title === "string" ? win.title : "";
    const candidateName = typeof win?.candidateName === "string" ? win.candidateName : "";
    const base = candidateName ? `${candidateName} · ${title}` : title;
    const usageText = statusUsageText({ contextUsage: win?.contextUsage || {} });
    const value = base ? `${base} · ${usageText}` : usageText;
    return { label: t("Context:"), value };
  });
}

export function statusPanelRows(status = {}, options = {}) {
  const windows = Array.isArray(options?.contextWindows) ? options.contextWindows : [];
  return [
    { label: t("Session:"), value: statusSessionId(status), copyable: true },
    ...statusContextRows(status, windows),
  ];
}

// 复制文本到剪贴板:优先异步 Clipboard API,不可用(非安全上下文/旧浏览器)时回退到
// 临时 textarea + execCommand。返回是否成功,供调用方给出反馈。
async function copyStatusTextToClipboard(value) {
  const textValue = value == null ? "" : String(value);
  if (!textValue) {
    return false;
  }
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(textValue);
      return true;
    }
  } catch (_error) {
    // 落到下方回退路径。
  }
  try {
    const scratch = document.createElement("textarea");
    scratch.value = textValue;
    scratch.setAttribute("readonly", "");
    scratch.style.position = "fixed";
    scratch.style.opacity = "0";
    scratch.style.pointerEvents = "none";
    document.body.append(scratch);
    scratch.select();
    const ok = document.execCommand("copy");
    scratch.remove();
    return ok;
  } catch (_error) {
    return false;
  }
}

// 会话 ID 复制按钮:复用 workspace 状态页同款视觉(.workspace-status-copy),
// 复制成功后临时切换对勾图标并在 1.6s 后复位。内联面板每次 render 全量重建,
// 按钮随之新建,复位定时器仅在自身生命周期内有效。
function makeSessionStatusCopyButton(copyValue) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "workspace-status-copy session-status-copy";
  button.setAttribute("aria-label", t("Copy session ID"));
  button.title = t("Copy session ID");
  const icon = document.createElement("span");
  icon.className = "workspace-status-copy-icon";
  icon.setAttribute("aria-hidden", "true");
  button.append(icon);
  let resetTimer = null;
  button.addEventListener("click", async () => {
    if (!copyValue) {
      return;
    }
    const ok = await copyStatusTextToClipboard(copyValue);
    button.dataset.copied = ok ? "yes" : "no";
    button.setAttribute("aria-label", ok ? t("Session ID copied") : t("Copy failed"));
    button.title = ok ? t("Session ID copied") : t("Copy failed");
    if (resetTimer !== null) {
      clearTimeout(resetTimer);
    }
    resetTimer = setTimeout(() => {
      delete button.dataset.copied;
      button.setAttribute("aria-label", t("Copy session ID"));
      button.title = t("Copy session ID");
      resetTimer = null;
    }, 1600);
  });
  return button;
}

function makeSessionStatusRow(labelText, valueText, { copyValue = "" } = {}) {
  const row = document.createElement("div");
  row.className = "session-status-row";
  const label = document.createElement("dt");
  label.textContent = labelText;
  const value = document.createElement("dd");
  if (copyValue) {
    row.classList.add("session-status-row-copy");
    const valueSpan = document.createElement("span");
    valueSpan.className = "session-status-value";
    valueSpan.textContent = valueText || "—";
    value.append(valueSpan, makeSessionStatusCopyButton(copyValue));
  } else {
    value.textContent = valueText || "—";
  }
  row.append(label, value);
  return row;
}

function hideInlineSessionStatus() {
  state = { ...state, inlineSessionStatus: null };
  render(state);
}

function showInlineSessionStatus(status = {}) {
  // 状态与 MCP 两个内联面板互斥,避免在输入框上方同时堆两张卡片。
  state = { ...state, inlineSessionStatus: status && typeof status === "object" ? status : {}, inlineMcpStatus: null };
}

function renderInlineSessionStatusPanel(currentState = {}) {
  const target = byShell("session-status-panel");
  if (!target) {
    return;
  }
  target.replaceChildren();
  const status = currentState.inlineSessionStatus;
  target.hidden = !status;
  if (!status) {
    return;
  }

  const card = document.createElement("section");
  card.className = "session-status-card";
  card.setAttribute("role", "status");

  const header = document.createElement("header");
  header.className = "session-status-header";
  const title = document.createElement("h3");
  title.textContent = t("Status");
  const close = document.createElement("button");
  close.type = "button";
  close.className = "session-status-close";
  close.textContent = t("Close");
  close.addEventListener("click", hideInlineSessionStatus);
  header.append(title, close);

  const list = document.createElement("dl");
  list.className = "session-status-list";
  // 流水线会话:/status 展示与 composer 圈圈同源的每步上下文用量(文字版);其余会话保持单条会话级用量。
  const contextWindows =
    currentState.currentSession?.mode === "pipeline" ? deriveContextUsageWindows(currentState) : [];
  list.append(
    ...statusPanelRows(status, { contextWindows }).map((row) =>
      makeSessionStatusRow(row.label, row.value, { copyValue: row.copyable ? row.value : "" }),
    ),
  );

  card.append(header, list);
  target.append(card);
}

const MCP_STATUS_AUTH_LABELS = {
  authenticated: t("Authenticated"),
  configured: t("Authenticated"),
  "needs-auth": t("Authentication required"),
  needs_auth: t("Authentication required"),
  error: t("Authentication failed"),
};

function mcpStatusServers(mcp = {}) {
  const servers = mcp && typeof mcp === "object" && Array.isArray(mcp.servers) ? mcp.servers : [];
  return servers.filter((server) => server && typeof server === "object");
}

const MCP_REMOTE_TRANSPORTS = new Set(["http", "sse", "ws"]);
function mcpServerSupportsAuth(server = {}) {
  // 远程传输(http/sse/ws)才会走 OAuth;本地 stdio 命令不涉及身份验证。
  // 远程服务器常用动态客户端注册,配置里未必声明 oauth 段,故不能只看 oauth_configured。
  const transport = text(server.transport).toLowerCase();
  if (MCP_REMOTE_TRANSPORTS.has(transport)) {
    return true;
  }
  const oauth = server.oauth_client_state && typeof server.oauth_client_state === "object" ? server.oauth_client_state : {};
  return Boolean(oauth.oauth_configured === true || oauth.stored_client_id === true || oauth.configured_client_id === true);
}
function mcpStatusAuthLabel(server = {}) {
  const authState = text(server.auth_state);
  if (MCP_STATUS_AUTH_LABELS[authState]) {
    return MCP_STATUS_AUTH_LABELS[authState];
  }
  // not-configured:区分「支持身份验证但尚未进行」与「本身不支持身份验证」。
  return mcpServerSupportsAuth(server) ? t("Not authenticated") : t("Authentication not supported");
}

function makeMcpStatusRow(server = {}) {
  const row = document.createElement("div");
  row.className = "mcp-status-row";

  const name = document.createElement("span");
  name.className = "mcp-status-name";
  name.textContent = text(server.name) || "—";

  const auth = document.createElement("span");
  auth.className = "mcp-status-auth";
  auth.textContent = mcpStatusAuthLabel(server);

  const enabled = document.createElement("span");
  const isDisabled = server.disabled === true;
  enabled.className = `mcp-status-enabled ${isDisabled ? "is-disabled" : "is-enabled"}`;
  enabled.textContent = isDisabled ? t("Disabled") : t("Enabled");

  row.append(name, auth, enabled);
  return row;
}

function hideInlineMcpStatus() {
  state = { ...state, inlineMcpStatus: null };
  render(state);
}

function showInlineMcpStatus(mcp = {}) {
  // 与「状态」面板互斥(见 showInlineSessionStatus)。
  state = { ...state, inlineMcpStatus: mcp && typeof mcp === "object" ? mcp : {}, inlineSessionStatus: null };
}

function renderInlineMcpStatusPanel(currentState = {}) {
  const target = byShell("mcp-status-panel");
  if (!target) {
    return;
  }
  target.replaceChildren();
  const mcp = currentState.inlineMcpStatus;
  target.hidden = !mcp;
  if (!mcp) {
    return;
  }

  const card = document.createElement("section");
  card.className = "mcp-status-card";
  card.setAttribute("role", "status");

  const header = document.createElement("header");
  header.className = "mcp-status-header";
  const title = document.createElement("h3");
  title.textContent = "MCP";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "mcp-status-close";
  close.textContent = t("Close");
  close.addEventListener("click", hideInlineMcpStatus);
  header.append(title, close);

  const servers = mcpStatusServers(mcp);
  const list = document.createElement("div");
  list.className = "mcp-status-list";
  if (servers.length) {
    list.append(...servers.map((server) => makeMcpStatusRow(server)));
  } else {
    const empty = document.createElement("p");
    empty.className = "mcp-status-empty";
    empty.textContent = t("No MCP servers configured");
    list.append(empty);
  }

  card.append(header, list);
  target.append(card);
}

function commandPaletteMatches(item, query) {
  const normalized = text(query).trim().toLowerCase();
  if (!normalized) {
    return true;
  }
  return [item.label, item.detail, item.id].map(text).some((value) => value.toLowerCase().includes(normalized));
}

function runCommandPaletteItem(item = {}) {
  closeCommandPalette();
  if (item.id === "new-thread") {
    void startNewSessionDraft();
    return;
  }
  if (item.tab) {
    openWorkspaceModal(item.tab);
  }
}

function paletteSessionRow(session, index) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "command-palette-item command-palette-session";
  button.dataset.commandPaletteSession = displaySessionId(session);

  const icon = document.createElement("span");
  // 复用侧栏 .thread-mode-icon 的字形(普通对话方框 / 流水线分叉 SVG),否则面板里
  // .command-palette-mode-icon 无尺寸/字形,图标不可见。
  icon.className = `command-palette-mode-icon thread-mode-icon ${sessionModeIconClass(session)}`;
  icon.setAttribute("aria-hidden", "true");

  const body = document.createElement("span");
  body.className = "command-palette-session-body";
  const title = document.createElement("strong");
  title.textContent = text(session.title || session.sessionId || t("Conversation"));
  body.append(title);
  // 项目名作为次级信息移到会话名下方,不再挤占右侧快捷键列(否则单列网格会把 ⌘N 芯片拉宽)。
  const projectLabel = text(session.projectLabel || "");
  if (projectLabel) {
    const project = document.createElement("small");
    project.className = "command-palette-project";
    project.textContent = projectLabel;
    body.append(project);
  }

  const right = document.createElement("span");
  right.className = "command-palette-session-right";

  // 进行中转圈;等待批准额外显示 pill(复用侧栏活动判定)。
  const activity = sessionActivityState(session, state);
  // 未读圆点:与侧栏一致——会话被标为未读、非当前正在看、且非进行中/等待(那两态走转圈)。
  const isActiveRow = displaySessionId(session) === state.currentSessionId;
  const showUnread = Boolean(session.unread) && !isActiveRow && activity === "";
  if (showUnread) {
    const unreadDot = document.createElement("span");
    unreadDot.className = "session-unread-dot";
    unreadDot.setAttribute("aria-hidden", "true");
    const srLabel = document.createElement("span");
    srLabel.className = "sr-only";
    srLabel.textContent = t("Unread");
    unreadDot.append(srLabel);
    right.append(unreadDot);
  }
  if (activity) {
    const status = document.createElement("span");
    status.className = "command-palette-status thread-status";
    status.dataset.activity = activity;
    if (activity === "awaiting") {
      const pill = document.createElement("span");
      pill.className = "thread-status-pill";
      pill.textContent = t("Awaiting approval");
      status.append(pill);
    }
    const spinner = document.createElement("span");
    spinner.className = "thread-spinner";
    spinner.setAttribute("aria-hidden", "true");
    applySpinPhase(spinner, 1.4); // 命令面板每次开合/重渲染都重建此行，相位对齐避免转圈复位
    status.append(spinner);
    // 读屏播报活动状态(与侧栏一致:spinner 仅视觉,状态文字走 sr-only)。
    const srLabel = document.createElement("span");
    srLabel.className = "sr-only";
    srLabel.textContent = activity === "awaiting" ? t("Awaiting approval") : t("In progress");
    status.append(srLabel);
    right.append(status);
  }

  const shortcut = document.createElement("kbd");
  if (index < 9) {
    shortcut.textContent = `⌘${index + 1}`;
  } else {
    shortcut.hidden = true;
  }
  right.append(shortcut);

  button.append(icon, body, right);
  button.addEventListener("click", () => activatePaletteResult({ type: "session", session }));
  return button;
}

function paletteCommandRow(item, index) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "command-palette-item";
  button.dataset.commandPaletteAction = item.id;

  const body = document.createElement("span");
  const label = document.createElement("strong");
  label.textContent = item.label;
  const detail = document.createElement("small");
  detail.textContent = item.detail;
  body.append(label, detail);

  const shortcut = document.createElement("kbd");
  if (index < 9) {
    shortcut.textContent = `⌘${index + 1}`;
  } else {
    shortcut.textContent = item.shortcut || "";
    if (!shortcut.textContent) {
      shortcut.hidden = true;
    }
  }

  button.append(body, shortcut);
  button.addEventListener("click", () => activatePaletteResult({ type: "command", item }));
  return button;
}

function renderPaletteGroups({ chats, unread, commands }) {
  const list = byShell("command-palette-list");
  if (!list) {
    return;
  }
  list.replaceChildren();
  paletteResults = [];

  // 分组顺序:聊天 → 未读聊天 → 命令(不设「进行中」组,进行中的会话在聊天组内以行内转圈
  // 表示)。聊天组含全部会话,未读聊天组是其未读子集(两组可重叠);组内均按 updated_at 倒排。
  const sections = [
    { title: t("Chats"), sessions: chats },
    { title: t("Unread chats"), sessions: unread },
  ];
  for (const section of sections) {
    if (!section.sessions.length) {
      continue;
    }
    const head = document.createElement("p");
    head.className = "command-palette-group-title";
    head.textContent = section.title;
    list.append(head);
    for (const session of section.sessions) {
      const index = paletteResults.length;
      paletteResults.push({ type: "session", session });
      list.append(paletteSessionRow(session, index));
    }
  }
  if (commands.length) {
    const head = document.createElement("p");
    head.className = "command-palette-group-title";
    head.textContent = t("Commands");
    list.append(head);
    for (const item of commands) {
      const index = paletteResults.length;
      paletteResults.push({ type: "command", item });
      list.append(paletteCommandRow(item, index));
    }
  }
  if (paletteResults.length === 0) {
    const empty = document.createElement("p");
    empty.className = "command-palette-empty";
    empty.textContent = t("No matching results.");
    list.append(empty);
  }
}

function activatePaletteResult(result) {
  if (!result) {
    return;
  }
  if (result.type === "session") {
    closeCommandPalette();
    switchSession(displaySessionId(result.session));
    setMobileSidebarOpen(false);
    return;
  }
  if (result.type === "command") {
    runCommandPaletteItem(result.item);
  }
}

async function refreshPalette(query = "") {
  const list = byShell("command-palette-list");
  if (!list) {
    return;
  }
  const token = ++paletteSearchToken;
  const commands = COMMAND_PALETTE_ITEMS.filter((item) => commandPaletteMatches(item, query));
  let payload;
  try {
    payload = await api.searchSessions(query, { limit: query ? 50 : 20 });
  } catch (_error) {
    if (token !== paletteSearchToken) {
      return;
    }
    list.replaceChildren();
    paletteResults = [];
    const failed = document.createElement("p");
    failed.className = "command-palette-empty";
    failed.textContent = t("Failed to search sessions. Please try again.");
    list.append(failed);
    return;
  }
  if (token !== paletteSearchToken) {
    return;
  }
  const sessions = Array.isArray(payload?.results) ? payload.results : [];
  const chats = [];
  const unread = [];
  for (const session of sessions) {
    // 聊天组收录全部会话(进行中的仍在此,行上显示转圈,不再单独成组);未读聊天组是
    // 其中「未读且非当前查看」的子集——两组不互斥,一条未读会话同时出现在两组里。
    chats.push(session);
    const isCurrent = displaySessionId(session) === state.currentSessionId;
    if (Boolean(session.unread) && !isCurrent) {
      unread.push(session);
    }
  }
  // 聊天与未读聊天各自最多展示 9 条(⌘1–9 覆盖前 9 个结果),均沿用后端的时间倒排。
  renderPaletteGroups({
    chats: chats.slice(0, PALETTE_CHAT_LIMIT),
    unread: unread.slice(0, PALETTE_CHAT_LIMIT),
    commands,
  });
}

function openCommandPalette() {
  const palette = byShell("command-palette");
  const search = byShell("command-palette-search");
  if (!palette) {
    return;
  }
  palette.hidden = false;
  document.body?.classList?.add("command-palette-open");
  // 丢弃上次输入未触发的防抖回调,避免它晚到后覆盖刚重置的空态。
  if (paletteSearchTimer) {
    clearTimeout(paletteSearchTimer);
    paletteSearchTimer = null;
  }
  if (search) {
    search.value = "";
  }
  void refreshPalette("");
  search?.focus?.();
}

function closeCommandPalette() {
  const palette = byShell("command-palette");
  if (!palette) {
    return;
  }
  palette.hidden = true;
  document.body?.classList?.remove("command-palette-open");
}

function isCommandPaletteOpen() {
  return byShell("command-palette")?.hidden === false;
}

function setMobileSidebarOpen(open) {
  root?.classList?.toggle("sidebar-open", Boolean(open));
  const toggle = byShell("sidebar-drawer-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(Boolean(open)));
    toggle.setAttribute("aria-label", open ? t("Close navigation") : t("Open navigation"));
  }
}

function toggleMobileSidebar() {
  setMobileSidebarOpen(!root?.classList?.contains("sidebar-open"));
}

function pathParts(value) {
  const normalized = text(value).replace(/\\/g, "/").replace(/\/+$/u, "");
  return normalized.split("/").filter(Boolean);
}

function basenamePath(value) {
  const parts = pathParts(value);
  return parts.at(-1) || text(value) || "Local project";
}

function projectSuffixLabel(key, depth) {
  const parts = pathParts(key);
  if (parts.length === 0) {
    return "Local project";
  }
  return parts.slice(-Math.min(depth, parts.length)).join("/");
}

export function projectDisplayLabels(keys = []) {
  const normalizedKeys = [...new Set(keys.map(text).filter(Boolean))];
  return Object.fromEntries(
    normalizedKeys.map((key) => {
      const parts = pathParts(key);
      let depth = 1;
      let label = projectSuffixLabel(key, depth);
      while (
        depth < Math.max(1, parts.length) &&
        normalizedKeys.filter((candidate) => projectSuffixLabel(candidate, depth) === label).length > 1
      ) {
        depth += 1;
        label = projectSuffixLabel(key, depth);
      }
      return [key, label];
    }),
  );
}

function sessionProjectKey(session, state) {
  return text(session.cwd || session.projectPath || state.currentSession?.cwd || "local-project");
}

function sessionProjectLabel(session, state) {
  return basenamePath(sessionProjectKey(session, state));
}

export function applyProjectDisplayLabels(groups = []) {
  const labels = projectDisplayLabels(groups.map((group) => group.key));
  return groups.map((group) => ({
    ...group,
    label: group.label || labels[group.key] || basenamePath(group.key),
  }));
}

function groupSessionsByProject(sessions = [], state = {}) {
  if (Array.isArray(state.projectGroups) && state.projectGroups.length > 0) {
    return applyProjectDisplayLabels(
      state.projectGroups
        .map((project) => {
          const key = text(project.cwd || project.key || project.projectPath);
          if (!key) {
            return null;
          }
          const sessions = Array.isArray(project.sessions) ? project.sessions : [];
          const total = Number.isFinite(Number(project.total)) ? Number(project.total) : sessions.length;
          return {
            key,
            label: project.label || basenamePath(key),
            sessions,
            total,
            hasMore: project.hasMore === true || total > sessions.length,
            pinned: project.pinned === true,
            pinnedAt: project.pinnedAt || null,
            archived: project.archived === true,
            collapsed: project.collapsed === true,
          };
        })
        .filter(Boolean),
    );
  }

  const groups = new Map();
  for (const session of sessions) {
    const key = sessionProjectKey(session, state);
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        label: sessionProjectLabel(session, state),
        sessions: [],
        total: 0,
      });
    }
    const group = groups.get(key);
    group.sessions.push(session);
    group.total += 1;
  }
  return applyProjectDisplayLabels([...groups.values()]);
}

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object || {}, key);
}

function projectKeyFromGroup(group = {}) {
  return text(group.cwd || group.key || group.projectPath);
}

function draftDefaultProjectKey(options = {}) {
  if (hasOwn(options, "cwd")) {
    return text(options.cwd);
  }
  if (state.newSessionDraft?.active) {
    return text(state.newSessionDraft.cwd);
  }
  return text(state.currentSession?.cwd || projectKeyFromGroup((state.projectGroups || [])[0]) || "");
}

export function makeNewSessionDraft(options = {}) {
  // 优先级:显式入参 > 上一草稿的选择 > 首屏注入的用户默认。新会话据此预置权限/模式,免去每次重选。
  const requestedMode = text(options.mode || state.newSessionDraft?.mode || SESSION_DEFAULTS.mode);
  const mode = requestedMode === "pipeline" ? "pipeline" : "normal";
  const permissionMode = text(
    options.permissionMode || state.newSessionDraft?.permissionMode || SESSION_DEFAULTS.permissionMode,
  );
  return {
    active: true,
    cwd: draftDefaultProjectKey(options),
    mode,
    permissionMode,
    pipelineName: text(
      options.pipelineName || state.newSessionDraft?.pipelineName || SESSION_DEFAULTS.pipelineName,
    ),
  };
}

export function newSessionCreatePayload(draft = {}) {
  const mode = draft.mode === "pipeline" ? "pipeline" : "normal";
  const payload = { mode };
  const cwd = text(draft.cwd).trim();
  if (cwd) {
    payload.cwd = cwd;
  }
  if (mode === "pipeline") {
    payload.pipelineName = text(draft.pipelineName || DEFAULT_PIPELINE_NAME);
  }
  const permissionMode = text(draft.permissionMode).trim();
  if (permissionMode) {
    payload.permissionMode = permissionMode;
  }
  const selection = draft.providerSelection;
  if (selection && text(selection.provider).trim() && text(selection.model).trim()) {
    payload.provider = text(selection.provider).trim();
    payload.model = text(selection.model).trim();
    const effort = text(selection.effort).trim();
    if (effort) {
      payload.effort = effort;
    }
  }
  return payload;
}

function draftProjectLabel(draft = {}) {
  return draft.cwd ? basenamePath(draft.cwd) : t("No project");
}

function pipelineOptionLabel(value) {
  const option = PIPELINE_OPTIONS.find((item) => item.id === value);
  return option?.label || text(value || DEFAULT_PIPELINE_NAME);
}

export function relativeTimeLabel(session) {
  const raw = session.updatedAt || session.updated_at || session.createdAt || session.created_at;
  const time = raw ? new Date(raw).getTime() : NaN;
  if (!Number.isFinite(time)) {
    return "";
  }
  const diffSeconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (diffSeconds < 60) {
    return t("Just now");
  }
  const diffMinutes = Math.floor(diffSeconds / 60);
  if (diffMinutes < 60) {
    return t("{n}m", { n: diffMinutes });
  }
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) {
    return t("{n}h", { n: diffHours });
  }
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) {
    return t("{n}d", { n: diffDays });
  }
  const diffWeeks = Math.floor(diffDays / 7);
  if (diffWeeks < 52) {
    return t("{n}w", { n: diffWeeks });
  }
  return t("{n}y", { n: Math.floor(diffWeeks / 52) });
}

function threadStatusLabel(session) {
  if (session.currentTurnActive || session.status === "running") {
    return "running";
  }
  if (session.status && !["idle", "normal"].includes(session.status)) {
    return session.status;
  }
  return relativeTimeLabel(session);
}

export function sessionModeIconClass(session = {}) {
  // 交接给普通对话后 session.mode 会翻转为 "normal"(Issue 4),但 sidecar 仍保留
  // contextId/taskId 指向流水线历史。侧栏图标须以「曾是流水线」为准(与 load_visible_transcript
  // 的 reload 回放、loadPipelineState 同一套解耦),否则交接后图标错误地变回普通对话。
  const wasPipeline = session.mode === "pipeline" || Boolean(session.contextId) || Boolean(session.taskId);
  return wasPipeline ? "is-pipeline-mode" : "is-normal-mode";
}

// 会话在侧边栏的活动状态：等待批准 > 进行中 > 空闲。
// 当前会话用实时 state（事件驱动），其它会话用列表接口带回的快照字段。
function sessionActivityState(session, state = {}) {
  if (!session) {
    return "";
  }
  const isCurrent =
    !state.newSessionDraft?.active && Boolean(state.currentSessionId) && displaySessionId(session) === state.currentSessionId;
  let awaiting;
  let running;
  if (isCurrent) {
    const pendingPermissions = Object.keys(state.permissions || {}).length;
    const pendingQuestions = Object.keys(state.questions || {}).length;
    awaiting = pendingPermissions + pendingQuestions > 0;
    running = Boolean(state.currentTurnActive);
  } else {
    awaiting = (Number(session.pendingPermissionCount) || 0) + (Number(session.pendingQuestionCount) || 0) > 0;
    running = Boolean(session.currentTurnActive) || session.status === "running";
  }
  if (awaiting) {
    return "awaiting";
  }
  if (running) {
    return "running";
  }
  return "";
}

function attachThreadActionTooltip(button) {
  const show = () => {
    const row = button.closest(".thread-item");
    button.classList.add("is-tooltip-open");
    row?.classList.add("is-action-hovered");
  };
  const hide = () => {
    const row = button.closest(".thread-item");
    button.classList.remove("is-tooltip-open");
    row?.classList.remove("is-action-hovered");
  };
  button.addEventListener("mouseenter", show);
  button.addEventListener("mouseleave", hide);
  button.addEventListener("focus", show);
  button.addEventListener("blur", hide);
  button.addEventListener("click", hide);
}

function setThreadActionTooltip(button, label) {
  button.setAttribute("aria-label", label);
  button.setAttribute("data-tooltip", label);
  const tooltip = document.createElement("span");
  tooltip.className = "thread-action-tooltip";
  tooltip.textContent = label;
  button.append(tooltip);
}

async function toggleSessionPinned(session, event) {
  event?.stopPropagation?.();
  const sessionId = displaySessionId(session);
  if (!sessionId) {
    return;
  }
  await api.updateSession(sessionId, { pinned: !session.pinned });
  await loadSessions();
  render(state);
}

async function archiveSession(session, event) {
  event?.stopPropagation?.();
  const sessionId = displaySessionId(session);
  if (!sessionId) {
    return;
  }
  const updated = await api.updateSession(sessionId, { archived: true });
  state = replaceUpdatedSessionInState(state, updated);
  await loadSessions();
  if (state.currentSessionId === sessionId) {
    startNewSessionDraft({ cwd: session.cwd });
    return;
  }
  render(state);
}

function createThreadRow(session, state) {
  const row = document.createElement("div");
  row.className =
    displaySessionId(session) === state.currentSessionId ? "session-item thread-item is-active" : "session-item thread-item";
  row.dataset.sessionId = displaySessionId(session);
  row.setAttribute("role", "button");
  row.tabIndex = 0;

  const modeIcon = document.createElement("span");
  modeIcon.className = `thread-mode-icon ${sessionModeIconClass(session)}`;
  modeIcon.setAttribute("aria-hidden", "true");

  const title = document.createElement("span");
  title.className = "thread-title";
  title.dataset.threadTitle = "true";
  title.textContent = text(session.title || session.sessionId || "Thread");

  if (session.readOnly) {
    const badge = document.createElement("span");
    badge.className = "thread-readonly-badge";
    badge.textContent = t("Read-only");
    title.append(badge);
  }

  const activity = sessionActivityState(session, state);
  const isActiveRow = displaySessionId(session) === state.currentSessionId;
  // 未读圆点:仅当会话被标为未读、非当前正在看的行、且非进行中/等待(那两态走转圈)时显示。
  const showUnread = Boolean(session.unread) && !isActiveRow && activity === "";
  const metaText = activity ? "" : threadStatusLabel(session);
  const meta = document.createElement("small");
  meta.className = "thread-meta";
  meta.textContent = metaText;
  if (!metaText || showUnread) {
    // 未读时以圆点替代时间戳(二者同占第 3 列)。
    meta.hidden = true;
  }

  // 进行中转圈；等待批准时额外显示绿色提示。
  let statusNode = null;
  if (activity) {
    statusNode = document.createElement("span");
    statusNode.className = "thread-status";
    statusNode.dataset.activity = activity;
    if (activity === "awaiting") {
      const pill = document.createElement("span");
      pill.className = "thread-status-pill";
      pill.textContent = t("Awaiting approval");
      statusNode.append(pill);
    }
    const spinner = document.createElement("span");
    spinner.className = "thread-spinner";
    spinner.setAttribute("aria-hidden", "true");
    applySpinPhase(spinner, 1.4); // 侧栏列表每次 render / 后台刷新都 replaceChildren 重建，相位对齐避免转圈复位
    statusNode.append(spinner);
    const srLabel = document.createElement("span");
    srLabel.className = "sr-only";
    srLabel.textContent = activity === "awaiting" ? t("Awaiting approval") : t("In progress");
    statusNode.append(srLabel);
  }

  let unreadDot = null;
  if (showUnread) {
    unreadDot = document.createElement("span");
    unreadDot.className = "session-unread-dot";
    unreadDot.setAttribute("aria-hidden", "true");
    const srLabel = document.createElement("span");
    srLabel.className = "sr-only";
    srLabel.textContent = t("Unread");
    unreadDot.append(srLabel);
  }

  const actions = document.createElement("span");
  actions.className = "thread-actions";

  const pinAction = document.createElement("button");
  pinAction.type = "button";
  pinAction.className = session.pinned ? "thread-action thread-action-pin is-pinned" : "thread-action thread-action-pin";
  const pinTooltip = session.pinned ? t("Unpin conversation") : t("Pin conversation");
  setThreadActionTooltip(pinAction, pinTooltip);
  pinAction.addEventListener("click", (event) => {
    void toggleSessionPinned(session, event);
  });
  attachThreadActionTooltip(pinAction);

  const archiveAction = document.createElement("button");
  archiveAction.type = "button";
  archiveAction.className = "thread-action thread-action-archive";
  setThreadActionTooltip(archiveAction, t("Archive conversation"));
  archiveAction.addEventListener("click", (event) => {
    void archiveSession(session, event);
  });
  attachThreadActionTooltip(archiveAction);

  actions.append(pinAction, archiveAction);

  row.append(modeIcon, title, meta);
  if (statusNode) {
    row.append(statusNode);
  }
  if (unreadDot) {
    row.append(unreadDot);
  }
  row.append(actions);
  row.addEventListener("click", () => {
    switchSession(displaySessionId(session));
    setMobileSidebarOpen(false);
  });
  row.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    switchSession(displaySessionId(session));
    setMobileSidebarOpen(false);
  });
  return row;
}

function replaceProjectGroup(projectGroups = [], key, patch = {}) {
  const nextGroups = [];
  let replaced = false;
  for (const group of projectGroups) {
    const groupKey = text(group.cwd || group.key || group.projectPath);
    if (groupKey === key) {
      nextGroups.push({ ...group, cwd: key, ...patch });
      replaced = true;
    } else {
      nextGroups.push(group);
    }
  }
  if (!replaced) {
    nextGroups.unshift({ cwd: key, key, sessions: [], total: 0, ...patch });
  }
  return nextGroups;
}

function mergeSessionIntoProjectGroups(projectGroups = [], session) {
  const key = sessionProjectKey(session, {});
  if (!key) {
    return projectGroups;
  }
  const nextGroups = replaceProjectGroup(projectGroups, key, {});
  return nextGroups.map((group) => {
    const groupKey = text(group.cwd || group.key || group.projectPath);
    if (groupKey !== key) {
      return group;
    }
    const sessions = Array.isArray(group.sessions) ? group.sessions : [];
    const sessionId = displaySessionId(session);
    const withoutDuplicate = sessions.filter((item) => displaySessionId(item) !== sessionId);
    return {
      ...group,
      cwd: key,
      sessions: [session, ...withoutDuplicate].slice(0, Math.max(PROJECT_THREAD_PREVIEW_LIMIT, sessions.length + 1)),
      total: Number.isFinite(Number(group.total)) ? Number(group.total) + (sessions.length === withoutDuplicate.length ? 1 : 0) : sessions.length + 1,
    };
  });
}

async function expandProjectThreads(group) {
  const key = group.key;
  expandedProjectKeys.add(key);
  if ((group.total || 0) <= (group.sessions || []).length) {
    renderProjectThreadNavigation(state);
    return;
  }

  loadingProjectKeys.add(key);
  renderProjectThreadNavigation(state);
  try {
    const limit = Math.min(Math.max(group.total || 0, PROJECT_THREAD_PREVIEW_LIMIT), PROJECT_THREAD_EXPANDED_LIMIT);
    const payload = await api.listSessions({ cwd: key, limit });
    state = {
      ...state,
      projectGroups: replaceProjectGroup(state.projectGroups || [], key, {
        sessions: payload.sessions || [],
        total: Number.isFinite(Number(payload.total)) ? Number(payload.total) : (payload.sessions || []).length,
        hasMore: payload.hasMore === true,
      }),
      sessions: [...(payload.sessions || []), ...(state.sessions || []).filter((session) => sessionProjectKey(session, state) !== key)],
    };
  } finally {
    loadingProjectKeys.delete(key);
    renderProjectThreadNavigation(state);
  }
}

function normalizePinnedProjects(projects = []) {
  return applyProjectDisplayLabels(
    projects
      .map((project) => {
        const key = text(project.cwd || project.key || project.projectPath);
        if (!key) {
          return null;
        }
        const sessions = Array.isArray(project.sessions) ? project.sessions : [];
        const total = Number.isFinite(Number(project.total)) ? Number(project.total) : sessions.length;
        return {
          key,
          label: project.label || basenamePath(key),
          sessions,
          total,
          hasMore: project.hasMore === true || total > sessions.length,
          pinned: true,
          pinnedAt: project.pinnedAt || null,
          archived: project.archived === true,
          collapsed: project.collapsed === true,
        };
      })
      .filter(Boolean),
  );
}

function setProjectCollapsedInState(currentState, key, collapsed) {
  const patch = (list) =>
    (list || []).map((group) =>
      text(group.cwd || group.key || group.projectPath) === key ? { ...group, collapsed } : group,
    );
  return {
    ...currentState,
    projectGroups: patch(currentState.projectGroups),
    pinnedProjects: patch(currentState.pinnedProjects),
  };
}

async function toggleProjectCollapsed(group) {
  const key = group.key;
  const collapsed = !group.collapsed;
  state = setProjectCollapsedInState(state, key, collapsed);
  renderProjectThreadNavigation(state);
  try {
    await api.updateProject(key, { collapsed });
  } catch (_error) {
    // Collapse is a cosmetic preference; ignore persistence failures.
  }
}

let appModalState = null;

function setAppModalError(message = "") {
  const el = byShell("app-modal-error");
  if (!el) {
    return;
  }
  el.textContent = message;
  el.hidden = !message;
}

function closeAppModal() {
  const backdrop = byShell("app-modal-backdrop");
  if (backdrop) {
    backdrop.hidden = true;
  }
  document.body?.classList?.remove("app-modal-open");
  appModalState = null;
}

function isAppModalOpen() {
  return byShell("app-modal-backdrop")?.hidden === false;
}

function openAppModal(options = {}) {
  const {
    title = "",
    subtitle = "",
    kind = "input",
    initialValue = "",
    confirmLabel = t("Save"),
    cancelLabel = t("Cancel"),
    danger = false,
    multiline = false,
    onConfirm,
  } = options;
  const backdrop = byShell("app-modal-backdrop");
  const modal = byShell("app-modal");
  const form = byShell("app-modal-form");
  const input = byShell("app-modal-input");
  const textarea = byShell("app-modal-textarea");
  const cancelButton = byShell("app-modal-cancel");
  const confirmButton = byShell("app-modal-confirm");
  if (!backdrop || !modal || !form || !input) {
    return;
  }
  // 单行 <input> 与多行 <textarea> 二选一:input 类弹窗默认单行,排队消息编辑用多行。
  const useTextarea = kind === "input" && Boolean(multiline);
  appModalState = { onConfirm, kind, multiline: useTextarea };
  const titleEl = byShell("app-modal-title");
  const subtitleEl = byShell("app-modal-subtitle");
  if (titleEl) {
    titleEl.textContent = title;
  }
  if (subtitleEl) {
    subtitleEl.textContent = subtitle;
    subtitleEl.hidden = !subtitle;
  }
  input.hidden = kind !== "input" || useTextarea;
  input.value = kind === "input" && !useTextarea ? initialValue : "";
  if (textarea) {
    textarea.hidden = !useTextarea;
    textarea.value = useTextarea ? initialValue : "";
  }
  if (confirmButton) {
    confirmButton.textContent = confirmLabel;
    confirmButton.classList.toggle("is-danger", Boolean(danger));
    confirmButton.disabled = false;
  }
  if (cancelButton) {
    cancelButton.textContent = cancelLabel;
  }
  modal.classList.toggle("is-confirm", kind !== "input");
  setAppModalError("");
  backdrop.hidden = false;
  document.body?.classList?.add("app-modal-open");
  if (kind === "input") {
    const activeField = useTextarea ? textarea : input;
    setTimeout(() => {
      activeField?.focus();
      activeField?.select?.();
    }, 0);
  } else if (confirmButton) {
    setTimeout(() => confirmButton.focus(), 0);
  }
}

async function submitAppModal(event) {
  event?.preventDefault?.();
  if (!appModalState) {
    return;
  }
  const { onConfirm, kind, multiline } = appModalState;
  const field = multiline ? byShell("app-modal-textarea") : byShell("app-modal-input");
  const value = kind === "input" ? text(field?.value).trim() : "";
  if (kind === "input" && !value) {
    setAppModalError(multiline ? t("Please enter content") : t("Please enter a name"));
    field?.focus();
    return;
  }
  const confirmButton = byShell("app-modal-confirm");
  if (confirmButton) {
    confirmButton.disabled = true;
  }
  try {
    await onConfirm?.(value);
    closeAppModal();
  } catch (error) {
    setAppModalError(error?.message || t("Operation failed"));
    if (confirmButton) {
      confirmButton.disabled = false;
    }
  }
}

async function toggleProjectPinned(group) {
  closeProjectMenu();
  try {
    await api.updateProject(group.key, { pinned: !group.pinned });
    await loadSessions();
    render(state);
  } catch (error) {
    window.alert?.(error?.message || t("Operation failed"));
  }
}

async function archiveProjectGroup(group) {
  closeProjectMenu();
  try {
    // 归档整个项目 = 归档其下全部会话(会话级 archived=True),使它们成组出现在
    // 「已归档对话」面板、可逐条撤销;项目本身不设归档标志,撤销任一会话即回到侧栏。
    await api.archiveProjectSessions(group.key);
    await loadSessions();
    render(state);
  } catch (error) {
    window.alert?.(error?.message || t("Archive failed"));
  }
}

function removeProjectGroup(group) {
  closeProjectMenu();
  openAppModal({
    title: t("Remove {label}?", { label: group.label }),
    subtitle: t("This will remove the project from Codex. Files on disk will not be deleted."),
    kind: "confirm",
    confirmLabel: t("Remove"),
    danger: true,
    onConfirm: async () => {
      await api.updateProject(group.key, { hidden: true });
      await loadSessions();
      render(state);
    },
  });
}

async function revealProjectGroup(group) {
  closeProjectMenu();
  try {
    await api.revealProject(group.key);
  } catch (error) {
    window.alert?.(error?.message || t("Unable to reveal the project in Finder"));
  }
}

function startProjectRename(group) {
  closeProjectMenu();
  openAppModal({
    title: t("Rename project"),
    subtitle: t("Keep it short and recognizable"),
    kind: "input",
    initialValue: group.label,
    confirmLabel: t("Save"),
    onConfirm: async (value) => {
      await api.updateProject(group.key, { label: value });
      await loadSessions();
      render(state);
    },
  });
}

function ensureProjectMenu() {
  let menu = byShell("project-context-menu");
  if (menu) {
    return menu;
  }
  menu = document.createElement("div");
  menu.className = "project-menu-popover";
  menu.dataset.appShell = "project-context-menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  menu.addEventListener("click", (event) => event.stopPropagation());
  root?.append(menu);
  return menu;
}

function closeProjectMenu() {
  const menu = byShell("project-context-menu");
  if (menu) {
    menu.hidden = true;
  }
  projectMenuKey = "";
}

function makeProjectMenuItem(iconClass, label, onClick, { danger = false } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = danger ? "project-menu-popover-item is-danger" : "project-menu-popover-item";
  button.setAttribute("role", "menuitem");
  const icon = document.createElement("span");
  icon.className = `project-menu-popover-icon ${iconClass}`;
  icon.setAttribute("aria-hidden", "true");
  const labelNode = document.createElement("span");
  labelNode.className = "project-menu-popover-label";
  labelNode.textContent = label;
  button.append(icon, labelNode);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  return button;
}

function projectHasRealDirectory(key) {
  return /[\\/]/.test(text(key));
}

function openProjectMenu(group, anchor) {
  const menu = ensureProjectMenu();
  projectMenuKey = group.key;
  menu.replaceChildren();
  menu.append(
    makeProjectMenuItem(
      group.pinned ? "pmi-unpin" : "pmi-pin",
      group.pinned ? t("Unpin project") : t("Pin project"),
      () => void toggleProjectPinned(group),
    ),
  );
  if (projectHasRealDirectory(group.key)) {
    menu.append(makeProjectMenuItem("pmi-folder", t("Reveal in Finder"), () => void revealProjectGroup(group)));
  }
  menu.append(makeProjectMenuItem("pmi-rename", t("Rename project"), () => startProjectRename(group)));
  menu.append(makeProjectMenuItem("pmi-archive", t("Archive conversation"), () => void archiveProjectGroup(group)));
  menu.append(makeProjectMenuItem("pmi-remove", t("Remove"), () => void removeProjectGroup(group), { danger: true }));

  menu.hidden = false;
  const anchorRect = anchor.getBoundingClientRect();
  const rootRect = root.getBoundingClientRect();
  const menuWidth = menu.offsetWidth || 208;
  let left = anchorRect.left - rootRect.left;
  left = Math.min(left, rootRect.width - menuWidth - 8);
  left = Math.max(8, left);
  menu.style.left = `${left}px`;
  menu.style.top = `${anchorRect.bottom - rootRect.top + 4}px`;
}

function createProjectSection(group, state) {
  const collapsed = group.collapsed === true;
  const section = document.createElement("section");
  section.className = collapsed ? "project-group is-collapsed" : "project-group";
  section.dataset.projectKey = group.key;

  const projectRow = document.createElement("div");
  projectRow.className = "project-row";

  const projectCollapse = document.createElement("button");
  projectCollapse.type = "button";
  projectCollapse.className = "project-collapse";
  projectCollapse.setAttribute("aria-expanded", String(!collapsed));
  projectCollapse.setAttribute("aria-label", `${collapsed ? t("Expand") : t("Collapse")} ${group.label}`);
  projectCollapse.addEventListener("click", (event) => {
    event.stopPropagation();
    void toggleProjectCollapsed(group);
  });

  const projectName = document.createElement("span");
  projectName.className = "project-name";
  const projectIcon = document.createElement("span");
  projectIcon.setAttribute("aria-hidden", "true");
  // Collapsed projects reuse the exact new-session project-picker glyph;
  // expanded projects show the open-notebook icon.
  projectIcon.className = collapsed ? "draft-session-menu-icon is-project" : "project-row-icon";
  const projectLabel = document.createElement("span");
  projectLabel.className = "project-name-label";
  projectLabel.textContent = group.label;
  // The collapse chevron sits directly after the name (revealed on row hover),
  // separate from the far-right menu/new-thread actions.
  projectName.append(projectIcon, projectLabel, projectCollapse);

  const projectCount = document.createElement("small");
  projectCount.className = "project-count";
  projectCount.textContent = String(group.total || group.sessions.length);
  projectCount.hidden = (group.total || group.sessions.length) === 0;

  const projectActions = document.createElement("span");
  projectActions.className = "project-actions";

  const projectMenu = document.createElement("button");
  projectMenu.type = "button";
  projectMenu.className = "project-menu";
  projectMenu.setAttribute("aria-label", t("{label} project actions", { label: group.label }));
  projectMenu.setAttribute("title", t("{label} project actions", { label: group.label }));
  projectMenu.addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = byShell("project-context-menu");
    if (projectMenuKey === group.key && menu && menu.hidden === false) {
      closeProjectMenu();
      return;
    }
    openProjectMenu(group, projectMenu);
  });

  const projectNewThread = document.createElement("button");
  projectNewThread.type = "button";
  projectNewThread.className = "project-new-thread";
  projectNewThread.setAttribute("aria-label", t("New chat in {label}", { label: group.label }));
  projectNewThread.setAttribute("title", t("New chat in {label}", { label: group.label }));
  projectNewThread.addEventListener("click", (event) => {
    event.stopPropagation();
    void startNewSessionDraft({ cwd: group.key });
  });

  projectActions.append(projectMenu, projectNewThread);
  projectRow.append(projectName, projectCount, projectActions);
  projectRow.addEventListener("click", () => void toggleProjectCollapsed(group));

  const threadList = document.createElement("div");
  threadList.className = "thread-list";
  threadList.hidden = collapsed;
  const expanded = expandedProjectKeys.has(group.key);
  const visibleSessions = expanded ? group.sessions : group.sessions.slice(0, PROJECT_THREAD_PREVIEW_LIMIT);
  for (const session of visibleSessions) {
    threadList.append(createThreadRow(session, state));
  }
  const canToggleMore = (group.total || group.sessions.length) > PROJECT_THREAD_PREVIEW_LIMIT;
  if (canToggleMore) {
    const showMore = document.createElement("button");
    showMore.type = "button";
    showMore.className = "project-show-more";
    showMore.textContent = loadingProjectKeys.has(group.key) ? t("Loading...") : expanded ? t("Show less") : t("Show more");
    showMore.disabled = loadingProjectKeys.has(group.key);
    showMore.addEventListener("click", (event) => {
      event.stopPropagation();
      if (expandedProjectKeys.has(group.key)) {
        expandedProjectKeys.delete(group.key);
        renderProjectThreadNavigation(state);
        return;
      }
      void expandProjectThreads(group);
    });
    threadList.append(showMore);
  }

  section.append(projectRow, threadList);
  return section;
}

function renderProjectThreadNavigation(state) {
  const list = byShell("session-list");
  const pinnedList = byShell("pinned-session-list");
  if (!list) {
    return;
  }
  list.replaceChildren();

  const pinnedSessions = (state.pinnedSessions || []).filter((session) => !session.archived);
  const pinnedProjects = normalizePinnedProjects(state.pinnedProjects || []);
  const pinnedItems = [
    ...pinnedSessions.map((session) => ({
      kind: "session",
      pinnedAt: session.pinnedAt || session.updatedAt || "",
      session,
    })),
    ...pinnedProjects.map((group) => ({ kind: "project", pinnedAt: group.pinnedAt || "", group })),
  ].sort((left, right) => String(right.pinnedAt).localeCompare(String(left.pinnedAt)));

  if (pinnedList) {
    pinnedList.replaceChildren();
    pinnedList.hidden = pinnedItems.length === 0;
    for (const item of pinnedItems) {
      if (item.kind === "session") {
        pinnedList.append(createThreadRow(item.session, state));
      } else {
        pinnedList.append(createProjectSection(item.group, state));
      }
    }
  }

  const groups = groupSessionsByProject(state.sessions || [], state);
  updateProjectNavHeader(groups);

  list.hidden = projectsSectionCollapsed;
  if (projectsSectionCollapsed) {
    return;
  }

  for (const group of groups) {
    list.append(createProjectSection(group, state));
  }
}

function updateProjectNavHeader(groups) {
  const toggle = byShell("projects-section-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(!projectsSectionCollapsed));
  }
  const collapseAll = byShell("projects-collapse-all");
  if (collapseAll) {
    // "Expanded" here means a project is showing its session list (not collapsed).
    const anyExpanded = groups.some((group) => group.collapsed !== true);
    collapseAll.hidden = projectsSectionCollapsed || groups.length === 0;
    // Arrows-apart icon invites expansion; shown only when everything is collapsed.
    collapseAll.classList.toggle("is-expand", !anyExpanded);
    const label = anyExpanded ? t("Collapse all sessions") : t("Expand all sessions");
    collapseAll.setAttribute("aria-label", label);
    collapseAll.title = label;
  }
}

function toggleProjectsSectionCollapsed() {
  projectsSectionCollapsed = !projectsSectionCollapsed;
  renderProjectThreadNavigation(state);
}

async function toggleAllProjectsCollapsed() {
  const groups = groupSessionsByProject(state.sessions || [], state);
  if (groups.length === 0) {
    return;
  }
  // Collapse everything if any project is still showing its sessions,
  // otherwise expand everything.
  const collapsed = groups.some((group) => group.collapsed !== true);
  for (const group of groups) {
    state = setProjectCollapsedInState(state, group.key, collapsed);
  }
  renderProjectThreadNavigation(state);
  await Promise.all(
    groups.map((group) =>
      api.updateProject(group.key, { collapsed }).catch(() => {
        // Collapse is a cosmetic preference; ignore persistence failures.
      }),
    ),
  );
}

function renderSessions(state) {
  renderProjectThreadNavigation(state);
}

function toolStateForMessage(message, state) {
  const toolUseIds = Array.isArray(message.toolUseIds) ? message.toolUseIds : [];
  if (toolUseIds.length === 0) {
    return { tools: {}, localShell: {} };
  }
  return {
    tools: Object.fromEntries(
      toolUseIds
        .map((toolUseId) => [toolUseId, state.tools?.[toolUseId]])
        .filter(([, tool]) => tool && typeof tool === "object"),
    ),
    localShell: Object.fromEntries(
      toolUseIds
        .map((toolUseId) => [toolUseId, state.localShell?.[toolUseId]])
        .filter(([, shell]) => shell && typeof shell === "object"),
    ),
  };
}

function hasRenderableTools(toolState) {
  return (
    Object.keys(toolState.tools || {}).length > 0 ||
    Object.keys(toolState.localShell || {}).length > 0
  );
}

function detachedToolState(state) {
  return {
    tools: Object.fromEntries(Object.entries(state.tools || {}).filter(([, tool]) => !tool?.messageId)),
    localShell: Object.fromEntries(Object.entries(state.localShell || {}).filter(([, shell]) => !shell?.messageId)),
  };
}

function pipelineMarkerDepth(message) {
  const depth = Number(message.pipelineStep?.depth);
  if (Number.isFinite(depth)) {
    return depth;
  }
  if (message.kind === "pipeline_sub_step") {
    return 2;
  }
  if (message.kind === "pipeline_candidate") {
    return 1;
  }
  return 0;
}

function pipelineMarkerIcon(message) {
  if (message.kind === "pipeline_sub_step") {
    return "↳";
  }
  if (message.kind === "pipeline_candidate") {
    return "◇";
  }
  return "◎";
}

function pipelineMarkerTitle(message) {
  return messageText(message).replace(/^[●·◆↪]\s*/u, "");
}

// 「选择该方案」按钮的两击确认:模块级记录当前已武装的按钮,点击别处时复位。
// 每次转录全量重建都会造出新按钮,故复位后即作废,不留悬垂引用。
let armedSelectButton = null;
// 全局提交锁:任一「选择该方案」进入提交态后,所有候选按钮都拒绝点击(Issue 2——此前只锁单个按钮,
// 提交中其它候选仍可点)。提交失败复位;成功则流水线推进、转录重建令按钮随之消失,无需显式清锁。
let selectSubmitting = false;
function disarmSelectButton() {
  const btn = armedSelectButton;
  armedSelectButton = null;
  if (btn) {
    btn.className = "pipeline-step-select-button";
    btn.textContent = t("Select this option");
  }
}
// 点击已武装按钮之外的任意处即复位确认态(仿 workspace.js 的一次性外部点击监听;
// 无头测试的 document 桩没有 addEventListener,guard 使其不注册,两击确认仍由按钮自身驱动)。
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("click", (event) => {
    if (armedSelectButton && !armedSelectButton.contains?.(event.target)) {
      disarmSelectButton();
    }
  });
}

// 判断某架构图候选是否为「已选方案」:优先按候选序号(candidateIndex)匹配,回退候选名。
// selected 由 resolvePipelineSelectedCandidate 解析,可能只带其一。
function isSelectedDiagramCandidate(item, selected) {
  if (!item || !selected) {
    return false;
  }
  const si = selected.candidateIndex;
  const ii = item.candidateIndex;
  if (si !== undefined && si !== null && ii !== undefined && ii !== null) {
    return String(si) === String(ii);
  }
  const sn = text(selected.candidateName);
  return sn !== "" && sn === text(item.candidateName);
}

export function overlayDiagramOptimization(diagrams, state) {
  const optimizing = (state && state.diagramOptimizing) || {};
  const optimized = (state && state.diagramOptimized) || {};
  return (diagrams || []).map((d) => {
    const idx = String(d.candidateIndex);
    if (Object.prototype.hasOwnProperty.call(optimized, idx)) {
      const views = optimized[idx];
      const first = Array.isArray(views) && views.length ? views[0].mermaidSource : d.mermaidSource;
      return { ...d, views, mermaidSource: first, optimized: true, optimizing: false };
    }
    if (optimizing[idx]) {
      return { ...d, optimizing: true };
    }
    // 事件态被 resync 清空后回退后端权威 inflight 标志;后端缓存已 done 则不再标优化中。
    if (d.optimizing && !d.optimized) {
      return { ...d, optimizing: true };
    }
    return { ...d, optimizing: false };
  });
}

// 架构图优化三态(供输出面板行/预览头挂徽标):
//  - "optimizing":该候选正在后台优化(diagram.optimizing 事件在途,或 resync 后由 /outputs 的
//    后端 inflight 标志恢复) → 「优化中」
//  - "pending":已识别为方案草图但尚未优化(未优化过、当前不在优化) → 「待优化」
//  - "done":优化已完成(本轮 optimized 事件产出 或 后端缓存命中 optimized=true) → 无徽标
//  - "none":非候选架构图(部署产物等,无 candidateIndex) → 无徽标
// 仅对「候选方案架构图」(带 candidateIndex)判态;优化只在 step4 触发,故草图在生成
// 阶段(step1-3)即以 pending 呈现,正是用户「早就识别出方案架构图」的那段窗口。
export function diagramOptimizationState(item, state) {
  const idx = item && item.candidateIndex;
  if (idx === undefined || idx === null) return "none";
  const key = String(idx);
  const optimizing = (state && state.diagramOptimizing) || {};
  const optimized = (state && state.diagramOptimized) || {};
  if (optimizing[key]) return "optimizing";
  if (Object.prototype.hasOwnProperty.call(optimized, key)) return "done";
  // 事件态缺失(resync 清空)时认后端权威:缓存命中 → done(优先于滞留的 inflight);
  // 后端 inflight → optimizing,使正在优化的候选跨 resync 保持「优化中」而非倒退成「待优化」。
  if (item.optimized) return "done";
  if (item.optimizing) return "optimizing";
  return "pending";
}

export function renderPipelineMarkerGroup(message, options = {}) {
  const details = document.createElement("details");
  const groupClass =
    message.kind === "pipeline_sub_step"
      ? "pipeline-sub-step-group"
      : message.kind === "pipeline_candidate"
        ? "pipeline-candidate-group"
        : "pipeline-step-group";
  details.className = `message-pipeline-step pipeline-transcript-group ${groupClass}`;
  const status = text(message.pipelineStep?.status || "");
  // status === "input"：该步骤正等待用户输入（方案选择 / ask_user_question，含 step1）。
  // 后端在 input_required 时把步骤 marker 重发为 "input"（即便它已 step_completed），
  // 前端据此强制展开并给出「等待输入」提示——绝不能收起，否则用户会以为流水线卡住。
  const awaitingInput = status === "input";
  // 进行中的步骤保持展开、结束（completed / canceled 等终态）后自动收起，跟 normal 轮次一致。
  // 稳定键（markerId，live 与 reload 同源）让用户的展开/收起态跨帧重建保留，不再被自动收起。
  // 等待输入的步骤打上 forceOpen 标记，applyDetailsOpenOverrides 会跳过它，保证强制展开。
  details.dataset.openKey = `mk:${text(message.messageId || message.id || "")}`;
  details.open = status === "working" || status === "" || awaitingInput;
  if (awaitingInput) {
    details.dataset.forceOpen = "1";
  }

  const summary = document.createElement("summary");
  summary.className = "pipeline-step-summary";

  const icon = document.createElement("span");
  icon.className = "pipeline-step-icon";
  icon.textContent = pipelineMarkerIcon(message);

  const title = document.createElement("span");
  title.className = "pipeline-step-title";
  title.textContent = pipelineMarkerTitle(message);

  const statusNode = document.createElement("span");
  statusNode.className = "pipeline-step-status";
  if (awaitingInput) {
    // 等待输入：转圈 + 显式「等待输入」文案，明确告诉用户该步骤在等他操作而非卡死。
    const spinner = document.createElement("span");
    spinner.className = "thread-spinner pipeline-step-spinner";
    spinner.setAttribute("aria-hidden", "true");
    const hint = document.createElement("span");
    hint.className = "pipeline-step-input-hint";
    hint.textContent = t("Waiting for input");
    applySpinPhase(spinner, 1.4); // 转录区每帧 replaceChildren 重建，相位对齐避免转圈复位
    statusNode.append(spinner, hint);
  } else if (status === "working") {
    // 进行中：与侧栏一致的转圈特效，替代原来的「working」文字。
    const spinner = document.createElement("span");
    spinner.className = "thread-spinner pipeline-step-spinner";
    spinner.setAttribute("aria-hidden", "true");
    const srLabel = document.createElement("span");
    srLabel.className = "sr-only";
    srLabel.textContent = t("In progress");
    applySpinPhase(spinner, 1.4); // 转录区每帧 replaceChildren 重建，相位对齐避免转圈复位
    statusNode.append(spinner, srLabel);
  } else {
    // 已结束：显示「已处理 <时长>」，与 normal 轮次的计时展示对齐。
    const durationSeconds = Number(message.pipelineStep?.durationS);
    const elapsed =
      Number.isFinite(durationSeconds) && durationSeconds > 0 ? formatTurnDuration(durationSeconds * 1000) : "";
    statusNode.textContent = elapsed ? t("Processed {elapsed}", { elapsed }) : "";
  }

  const chevron = document.createElement("span");
  chevron.className = "pipeline-step-chevron";
  chevron.textContent = "›";

  const body = document.createElement("div");
  body.className = "pipeline-step-body";
  // 让步骤 body 自描述状态与稳定键，供心跳在事件静默期（无 SSE→无 render）从 live DOM 定位
  // 「进行中叶子步骤」并跨帧续算等待秒数（stepStatus/stepKey 三类步骤共用此唯一 body 出口）。
  body.dataset.stepStatus = status;
  body.dataset.stepKey = details.dataset.openKey;
  // 步骤体打上 groupId,供 syncPipelineThinking 按 compaction.started 带来的 groupId 精确定位
  // 运行态压缩条的宿主步骤(并行候选下唯一正确),与边界条的 groupId 归属同源。
  body.dataset.groupId = String(message.pipelineStep?.groupId || "");

  summary.append(icon, title, statusNode, chevron);
  details.append(summary, body);

  const stepId = message.pipelineStep?.stepId;
  const diagrams = Array.isArray(options.diagrams) ? options.diagrams : [];
  // confirm_and_select 是「候选选择器」:只列候选架构图(带 candidateIndex 者)。会话结束后重载时,
  // state.webDiagrams 还含部署步骤按真实路径写出的最终模板(其 envelope 无 candidate 字段,
  // candidateIndex 为 null)——它与被选候选同构,却会额外冒出一条「查看架构图 · <路径>」造成重复按钮。
  // 据 candidateIndex 过滤,把部署产物挡在候选选择器之外(输出面板「架构图」区仍展示全量,不受影响)。
  const candidateDiagrams = diagrams.filter(
    (item) => item && item.candidateIndex !== null && item.candidateIndex !== undefined,
  );
  // 权威候选表(input_required.options，见 outputs.py:pipeline_candidate_options)。候选清单
  // 必须来自权威提问信封而非「架构图能否渲染」——某候选模板 YAML 损坏时无 mermaid、diagram_items
  // 会丢弃它,若据 candidateDiagrams 渲染就会少一行(出了 2 个方案却只有 1 个可选)。有 candidates
  // 时按它渲染、架构图按 candidateIndex 合并;缺图候选仍可选(只是无「查看架构图」)。
  const candidates = Array.isArray(options.candidates) ? options.candidates : [];
  const diagramForIndex = (index) => {
    if (index === null || index === undefined) {
      return null;
    }
    return candidateDiagrams.find((d) => String(d.candidateIndex) === String(index)) || null;
  };
  // 统一成 {candidateName, candidateIndex, diagram} 的行描述:优先权威候选表(合并同序号架构图),
  // 为空时(老会话/未含 options 的流程)回退到「按可渲染架构图」的旧逻辑,零回归。
  const candidateRows = candidates.length
    ? candidates.map((c) => ({
        candidateName: c.candidateName,
        candidateIndex: c.candidateIndex,
        diagram: diagramForIndex(c.candidateIndex),
      }))
    : candidateDiagrams.map((d) => ({
        candidateName: d.candidateName,
        candidateIndex: d.candidateIndex,
        diagram: d,
      }));
  // 查看架构图:链接观感,点一下开预览、再点一下关（切换）。toggleDiagram 返回切换后的
  // 开启态（true=已打开），据此给链接加/去 is-open;真正的开/关判定在输出面板控制器内
  // 依 activePreviewPath 决定，故即便转录全量重建丢了 is-open 类，切换语义仍不受影响。
  const toggleDiagram = typeof options.toggleDiagram === "function" ? options.toggleDiagram : () => {};
  // 选择该方案:把用户对该候选的选择直接回传服务端（两击确认）。仅在该步骤仍等待用户
  // 输入时提供;选定后流水线推进、marker status 离开 "input"，按钮随下次重建自然消失，
  // 无需额外持久化「已选」态（贴合「选择后按钮消失」）。
  const onSelectCandidate = typeof options.onSelectCandidate === "function" ? options.onSelectCandidate : null;
  // 已选方案:选定后该候选行显示一枚绿色对勾。选择后 marker 离开 "input"，「选择该方案」按钮消失，
  // 唯留一枚对勾标出用户所选(实时选择态或重载时的快照/事件均可解析,见 resolvePipelineSelectedCandidate)。
  const selectedCandidate = options.selectedCandidate || null;
  // 「查看架构图」按钮组只在选方案步骤且有图时构建。此处只建不挂：调用方在该步骤的
  // 提示文字等所有内联消息落进 body 之后再把它 append 到 body 末尾，保证按钮位于提示文字
  // 下方（marker 构造时 body 为空、prompt 后到流式追加，若在此直接挂载会浮在提示之上）。
  let diagramGroup = null;
  if (stepId === "confirm_and_select" && candidateRows.length) {
    diagramGroup = document.createElement("div");
    diagramGroup.className = "pipeline-step-diagrams";
    for (const item of candidateRows) {
      const row = document.createElement("div");
      row.className = "pipeline-step-diagram-item";
      const isSelected = isSelectedDiagramCandidate(item, selectedCandidate);
      if (isSelected) {
        row.className = "pipeline-step-diagram-item is-selected";
      }

      // 候选名总要显示;「查看架构图」链接只在该候选有可渲染架构图时提供(缺图候选仍可选)。
      const diagram = item.diagram || null;
      if (diagram) {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "pipeline-step-diagram-link";
        link.textContent = t("View diagram") + " · " + (item.candidateName || diagram.sourceRelPath || "");
        link.addEventListener("click", () => {
          const open = toggleDiagram(diagram);
          link.className = open === true ? "pipeline-step-diagram-link is-open" : "pipeline-step-diagram-link";
        });
        row.append(link);
      } else {
        // 无架构图的候选:用纯文本标签占位,保证该方案照样成行、可选。
        const label = document.createElement("span");
        label.className = "pipeline-step-diagram-name";
        label.textContent = item.candidateName || "";
        row.append(label);
      }

      // 架构图优化的三态标识：正在后台优化 → “优化中”转圈徽标；已识别为方案草图但尚未
      // 开始优化（未优化过、当前不在优化） → 静态“待优化”徽标；优化完成（本轮事件产出或
      // 后端缓存命中 optimized=true） → 不挂任何徽标。
      if (diagram && diagram.optimizing) {
        const badge = document.createElement("span");
        badge.className = "diagram-optimizing";
        badge.textContent = t("Optimizing");
        row.append(badge);
      } else if (diagram && !diagram.optimized) {
        const badge = document.createElement("span");
        badge.className = "diagram-pending";
        badge.textContent = t("Pending optimization");
        row.append(badge);
      }

      // 已选方案:在链接后追加绿色对勾(带无障碍标签)。仅所选候选出现,标出用户最终选择。
      if (isSelected) {
        const check = document.createElement("span");
        check.className = "pipeline-step-diagram-check";
        check.textContent = "✓";
        check.setAttribute("aria-label", t("Selected"));
        check.setAttribute("title", t("Selected"));
        row.append(check);
      }

      // 仅在「待输入且尚未解析出已选候选」时提供「选择该方案」按钮。已选后(live 的
      // pipelineSelectedCandidate 或重载时快照/事件解析出的 selectedCandidate)只留对勾,
      // 绝不再整排复活按钮(修复:确认后按钮全部复位成可选)。
      if (awaitingInput && onSelectCandidate && !selectedCandidate) {
        const selectBtn = document.createElement("button");
        selectBtn.type = "button";
        selectBtn.className = "pipeline-step-select-button";
        selectBtn.textContent = t("Select this option");
        selectBtn.addEventListener("click", () => {
          // 已有候选正在提交:锁定全部按钮,忽略点击(含本按钮)。
          if (selectSubmitting) {
            return;
          }
          // 首击:武装本按钮(先复位其它已武装者),进入「确认选择?」。
          if (armedSelectButton !== selectBtn) {
            disarmSelectButton();
            selectBtn.className = "pipeline-step-select-button is-confirming";
            selectBtn.textContent = t("Confirm selection?");
            armedSelectButton = selectBtn;
            return;
          }
          // 再击:确认。清武装态、上全局提交锁并进入提交态,失败则复位并解锁交由全局错误呈现。
          armedSelectButton = null;
          selectSubmitting = true;
          selectBtn.className = "pipeline-step-select-button is-submitting";
          selectBtn.disabled = true;
          selectBtn.textContent = t("Selecting…");
          Promise.resolve(onSelectCandidate(item)).catch(() => {
            selectSubmitting = false;
            selectBtn.className = "pipeline-step-select-button";
            selectBtn.disabled = false;
            selectBtn.textContent = t("Select this option");
          });
        });
        row.append(selectBtn);
      }

      diagramGroup.append(row);
    }
  }

  return { body, details, diagramGroup };
}

// 流水线四种终态 → 中文文案 / 颜色类 / 图标。与后端 handoff.py 的 TerminalOutcome
// 枚举一一对应（completed/failed/canceled/early_exit）。前端硬编码中文，与本前端其余
// 硬编码中文一致，避免为几条短标签走 gettext 全量 .po/.mo。
const PIPELINE_OUTCOME_META = {
  completed: { cls: "success", icon: "✓", label: t("Pipeline completed") },
  failed: { cls: "failed", icon: "✕", label: t("Pipeline failed") },
  canceled: { cls: "canceled", icon: "⊘", label: t("Pipeline canceled") },
  early_exit: { cls: "early-exit", icon: "↦", label: t("Pipeline exited early") },
};

function renderPipelineOutcomeMarker(message) {
  const outcome = text(message.pipelineStep?.outcome || "");
  const meta = PIPELINE_OUTCOME_META[outcome] || { cls: "ended", icon: "◼", label: t("Pipeline ended") };
  const article = document.createElement("article");
  article.className = `message pipeline-outcome pipeline-outcome--${meta.cls}`;

  const icon = document.createElement("span");
  icon.className = "pipeline-outcome-icon";
  icon.textContent = meta.icon;

  const title = document.createElement("span");
  title.className = "pipeline-outcome-title";
  title.textContent = meta.label;

  article.append(icon, title);
  return article;
}

// 上文压缩分隔条（复刻 Codex）：默认收起的「⊟ 上下文已自动压缩」左对齐标记，展开可见压缩摘要。
// 图标与文案对齐消息正文左缘、无横贯分隔线；普通与流水线转录复用（renderMessages 共享）；
// 稳定键让展开/收起态跨帧重建保留。
function renderCompactionBoundaryMarker(message) {
  const details = document.createElement("details");
  details.className = "context-compaction-boundary";
  details.dataset.openKey = `compaction:${text(message.messageId || message.id || "")}`;
  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "context-compaction-icon";
  icon.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "context-compaction-boundary-label";
  label.textContent = t("Context automatically compacted");
  summary.append(icon, label);
  details.append(summary);
  const body = document.createElement("div");
  body.className = "context-compaction-boundary-body";
  body.textContent = messageText(message) || "";
  details.append(body);
  return details;
}

function renderPipelineBoundaryMarker(message) {
  const article = document.createElement("article");
  article.className = "message pipeline-normal-boundary";

  const icon = document.createElement("span");
  icon.className = "pipeline-normal-boundary-icon";
  icon.textContent = "↪";

  const title = document.createElement("span");
  title.className = "pipeline-normal-boundary-title";
  title.textContent = pipelineMarkerTitle(message);

  article.append(icon, title);
  return article;
}

// 思考是否仍在进行：当前轮次活跃、该消息仍在流式生成，
// 且尚未产出正文或工具调用（一旦开始输出正文/调用工具，思考即告一段落）。
function isThinkingActive(message, state) {
  if (!state || state.currentTurnActive !== true) {
    return false;
  }
  if (message.stored === true || message.status !== "streaming") {
    return false;
  }
  if (messageText(message)) {
    return false;
  }
  return !(Array.isArray(message.toolUseIds) && message.toolUseIds.length > 0);
}

// 构建一条助手消息的「思考」折叠块（无思考内容时返回 null）。
function buildThinkingElement(message, state) {
  if (!message.thinking) {
    return null;
  }
  const thinking = document.createElement("details");
  thinking.className = "message-thinking";
  // 展开态键：思考块同样跨帧重建，缺 openKey 时 toggle 记录器直接 return（见 ensureMessageStackToggleSync），
  // 用户展开/收起意图不被记录、每帧重建后回落默认收起——即"思考中无法展开"。挂上稳定键后其展开态
  // 被 detailsOpenOverrides 记录并在重建后回放。
  thinking.dataset.openKey = `think:${text(message.messageId || message.id || "")}`;
  const active = isThinkingActive(message, state);
  if (active) {
    thinking.classList.add("is-thinking");
  }
  const summary = document.createElement("summary");
  const summaryLabel = document.createElement("span");
  summaryLabel.className = "message-thinking-label";
  // 进行中用独立 msgid「Thinking…」(zh「正在思考」),与意图开关按钮的「Thinking」(zh「思考」)
  // 解耦——二者曾共用 msgid,导致进行中指示器被开关的译文锁成「思考」。
  summaryLabel.textContent = active ? t("Thinking…") : t("Thinking done");
  if (active) {
    // 「正在思考」流光：对齐相位，避免每帧重建把动画重置到不可见起点（见 applyShimmerPhase）。
    applyShimmerPhase(summaryLabel);
  }
  summary.append(summaryLabel);
  const thinkingBody = document.createElement("pre");
  thinkingBody.textContent = text(message.thinking);
  thinking.append(summary, thinkingBody);
  return thinking;
}

// 构建一条助手消息的正文（markdown）块（无正文时返回 null）。
function buildMessageBodyElement(message) {
  const content = messageText(message);
  if (!content) {
    return null;
  }
  const body = document.createElement("div");
  body.className = "message-body markdown-body";
  renderMarkdownInto(body, content);
  return body;
}

function buildMessageAttachmentsElement(message, state) {
  const imageIds = Array.isArray(message.imageIds) ? message.imageIds.map(text).filter(Boolean) : [];
  const fileRefs = Array.isArray(message.fileRefs) ? message.fileRefs.map(text).filter(Boolean) : [];
  if (imageIds.length === 0 && fileRefs.length === 0) {
    return null;
  }

  const attachments = document.createElement("div");
  attachments.className = "message-attachments";
  const sessionId = text(state?.currentSessionId || state?.currentSession?.webSessionId || "");
  for (const imageId of imageIds) {
    const chip = document.createElement("span");
    chip.className = "attachment-chip attachment-chip-image message-attachment-image";
    chip.title = imageId;
    const image = document.createElement("img");
    image.className = "attachment-chip-preview";
    image.src = `/api/images/${encodeURIComponent(imageId)}?sessionId=${encodeURIComponent(sessionId)}`;
    image.alt = t("Attached image");
    image.draggable = false;
    chip.append(image);
    attachments.append(chip);
  }
  for (const fileRef of fileRefs) {
    const chip = document.createElement("span");
    chip.className = "attachment-chip message-attachment-file";
    chip.title = fileRef;
    chip.textContent = `@ ${fileRef}`;
    attachments.append(chip);
  }
  return attachments;
}

// 是否为「流水线」转录：live 时 mode==="pipeline"；交接普通对话后 mode 翻转但 sidecar 仍留
// contextId/taskId（与侧栏图标、load_visible_transcript reload 回放同一套「曾是流水线」解耦）；
// 草稿态用 newSessionDraft.mode。流水线里只展开 complete_step、其余工具卡收起以消除闪烁。
export function isPipelineTranscript(state = {}) {
  const session = state.currentSession || {};
  return (
    session.mode === "pipeline" ||
    Boolean(session.contextId) ||
    Boolean(session.taskId) ||
    state.newSessionDraft?.mode === "pipeline"
  );
}

// 构建一条助手消息的工具卡片块（无可渲染工具时返回 null）。
// openToolUseId：转录尾部最新一张工具卡的 id，令其保持展开直到下一条消息/工具到来（Issue 3）。
function buildToolCardsElement(message, state, openToolUseId = "") {
  const messageToolState = toolStateForMessage(message, state);
  if (!hasRenderableTools(messageToolState)) {
    return null;
  }
  const messageTools = renderToolCards(messageToolState, {
    grouped: true,
    turnActive: !!state.currentTurnActive,
    openToolUseId,
    collapseNonComplete: isPipelineTranscript(state),
  });
  messageTools.classList.add("message-tool-cards");
  return messageTools;
}

// 返回一条消息里"最后一个可渲染工具"的 toolUseId（无则空串）——按 toolUseIds 顺序取末位。
function lastRenderableToolUseId(message, state) {
  const toolState = toolStateForMessage(message, state);
  const ids = Array.isArray(message.toolUseIds) ? message.toolUseIds.map(text).filter(Boolean) : [];
  for (let i = ids.length - 1; i >= 0; i -= 1) {
    const id = ids[i];
    if (toolState.tools[id] || toolState.localShell[id]) {
      return id;
    }
  }
  return "";
}

// 计算整段转录里"最新一张工具卡"的 toolUseId（Issue 3）：从末尾向前找第一条"有意义"的消息——
// 有可渲染工具、或有正文/思考、或是流水线标记。若它带工具→取其末位工具 id 保持展开；否则（正文/
// 思考/新标记已到来）返回空串，让此前的工具卡收起。messages 需已按转录顺序排好。
function latestToolUseIdForTranscript(messages, state) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    const toolId = lastRenderableToolUseId(message, state);
    if (toolId) {
      // 该消息虽带工具，但已产出最终正文（工具跑完后助手开始作答，是"非工具事件"）→ 不再把它的
      // 工具视为转录最新，返回空串让工具组随即自动收起（Issue：所有工具完成且下一事件非工具相关）。
      // 未产出正文时仍返回其末位工具 id，保持"进行中/刚完成"的工具组展开。
      // 注意：同一条消息里"思考在前、工具在后"，此处工具优先命中并返回，故不会被下方思考边界误收。
      if (messageText(message)) {
        return "";
      }
      return toolId;
    }
    const isMarker =
      message.kind === "pipeline_step" ||
      message.kind === "pipeline_candidate" ||
      message.kind === "pipeline_sub_step" ||
      message.kind === "pipeline_outcome" ||
      message.kind === "normal_chat_boundary";
    // 思考（含"正在思考"进行中）同样是非工具事件：工具组全部跑完后助手转入思考，此前的工具组即应
    // 收起（用户反馈：思考中就该收起，不必等思考完成）。仅对"无工具的后续消息"生效，故不影响本条自带工具。
    if (isMarker || messageText(message) || text(message.thinking)) {
      return "";
    }
  }
  return "";
}

// 某个流水线步骤 body 内是否正渲染「实时活动」：进行中的工具卡/工具组、真实思考折叠块，或此刻
// 确在流式产出的助手消息。有实时活动就显示其自身流光，只有事件间隙才补占位。
// 注意：流水线里段消息只在步骤 completed 才落 assistant.message.end，故其 .message-agent.is-streaming
// 会在整步存活——正文吐完、后端静默后仍挂着。若直接把它当实时活动，事件间隙占位会被这枚陈旧标记
// 永久压制（核心 bug）。因此该标记仅当最近 PIPELINE_STREAM_SILENCE_MS 内确有 delta 抵达才算「正在
// 流式」；超过静默阈值即视为停顿，放行占位。工具卡/真实思考块各有自身流光，仍无条件压制。
function stepBodyHasLiveActivity(body) {
  if (body.querySelector(".tool-card.is-active, .tool-group.is-active, .message-thinking.is-thinking")) {
    return true;
  }
  if (body.querySelector(".message-agent.is-streaming")) {
    return Date.now() - lastStreamDeltaAt < PIPELINE_STREAM_SILENCE_MS;
  }
  return false;
}

// 单一事实源：对 stackRoot 内所有「进行中叶子步骤 body」维护事件间隙占位——render 快照注入与
// 心跳静默补建共用同一函数。每个「进行中」的叶子步骤（status==="working" 且内部没有更深的进行中
// 子步骤）在事件间隙各补一枚流光占位（多个并行步骤各自独立显示）；进行中的父步骤把占位让给进行中
// 的子步骤（叶子优先），步骤内已有实时活动（工具/流式/真实思考）时则撤除占位。计时基准：每个叶子
// 各记「进入静默」时刻于 pipelineThinkingSince（键=stepKey，跨帧稳定不归零），活动恢复或离开
// working 即删键、下段静默重新起算（语义＝「距上次可见进度已等待 N 秒」）。
export function syncPipelineThinking(stackRoot) {
  const workingBodies = Array.from(stackRoot.querySelectorAll('[data-step-status="working"]'));
  const compacting = state.compaction?.status === "running";
  // 压缩条的宿主步骤体:优先按 groupId 精确匹配触发压缩的那个 step/候选组(started SSE 由后端带上,
  // 与边界条同源经 _group_id_for 解析),并行候选下唯一正确;缺 groupId / 未匹配时才退回首个进行中
  // 叶子。旧「首个 working 叶子」启发式在并行候选阶段会把方案2的压缩条错挂到方案1(用户反馈)。
  // 压缩发生在进行中的步骤内(该步骤压缩后仍继续),故宿主必在 working 步骤体中查找。
  const compactionGroupId = compacting ? String(state.compaction.groupId || "") : "";
  const leafBodies = workingBodies.filter(
    (body) => !workingBodies.some((child) => child !== body && body.contains(child))
  );
  let compactionHost = null;
  if (compacting) {
    if (compactionGroupId) {
      compactionHost = workingBodies.find((body) => body.dataset.groupId === compactionGroupId) || null;
    }
    if (!compactionHost) {
      compactionHost = leafBodies[0] || null;
    }
  }
  // 撤除所有非宿主进行中步骤体内的残留压缩条(切换宿主 / 上一帧的误挂 / 压缩已结束)。
  for (const body of workingBodies) {
    if (body !== compactionHost) {
      body.querySelector(":scope > .context-compaction")?.remove();
    }
  }
  if (compactionHost) {
    // 宿主步骤体:撤普通流光占位、清该步骤静默计时,把压缩条挂进体内(幂等)。
    compactionHost.querySelector(":scope > .pipeline-thinking")?.remove();
    const hostKey = compactionHost.dataset.stepKey || "";
    if (hostKey) {
      pipelineThinkingSince.delete(hostKey);
    }
    if (!compactionHost.querySelector(":scope > .context-compaction")) {
      compactionHost.append(buildCompactionIndicator(state.compaction));
    }
  }
  const liveKeys = new Set();
  for (const body of workingBodies) {
    // 宿主步骤体已由压缩条接管,不再叠加事件间隙占位。
    if (body === compactionHost) {
      continue;
    }
    // 叶子优先：含更深 working 子 body 的父步骤让位。
    if (workingBodies.some((child) => child !== body && body.contains(child))) {
      continue;
    }
    const key = body.dataset.stepKey || "";
    const existing = body.querySelector(":scope > .pipeline-thinking");
    if (stepBodyHasLiveActivity(body)) {
      // 有实时活动：撤除残留占位、重置该步骤静默计时。
      if (existing) {
        existing.remove();
      }
      if (key) {
        pipelineThinkingSince.delete(key);
      }
      continue;
    }
    if (key) {
      liveKeys.add(key);
      if (!pipelineThinkingSince.has(key)) {
        pipelineThinkingSince.set(key, Date.now());
      }
    }
    const elapsed = key ? Date.now() - pipelineThinkingSince.get(key) : 0;
    if (existing) {
      // 已有占位：原地改文本（不换节点），保住流光相位与布局。
      const label = existing.querySelector(".message-thinking-label");
      if (label) {
        label.textContent = pipelineThinkingLabel(elapsed);
      }
    } else {
      body.append(buildPipelineThinkingIndicator(elapsed));
    }
  }
  // 回收已不再是 working 叶子的计时键，防泄漏。
  for (const key of [...pipelineThinkingSince.keys()]) {
    if (!liveKeys.has(key)) {
      pipelineThinkingSince.delete(key);
    }
  }
  // 压缩进行中但无宿主步骤体(无匹配 groupId 且当前无进行中叶子):退回栈底挂一枚,避免压缩条彻底消失;
  // 否则撤除栈底残留。流水线模式压缩条完全由本函数负责,renderMessages 尾部(app.js:3486)已按模式跳过。
  if (compacting && !compactionHost) {
    if (!stackRoot.querySelector(":scope > .context-compaction")) {
      stackRoot.append(buildCompactionIndicator(state.compaction));
    }
  } else {
    stackRoot.querySelector(":scope > .context-compaction")?.remove();
  }
}

// 普通模式一次活跃回合内是否有「实时活动」：正文区(内联工具卡/流式/真实思考,复用 stepBodyHasLiveActivity)
// 或独立工具活动区(无 messageId 的工具落在 tool-activity-stack,不在 message-stack 内)里任一进行中信号。
// 后者经 byShell 取(测试环境 root 未设 → 返回 null，安全跳过)。有活动即不补底部占位。
function normalTurnHasLiveActivity(stackRoot) {
  if (stepBodyHasLiveActivity(stackRoot)) {
    return true;
  }
  const activity = byShell("tool-activity-stack");
  return Boolean(activity && activity.querySelector(".tool-card.is-active, .tool-group.is-active"));
}

// 普通(非流水线)模式的单枚事件间隙占位:与 syncPipelineThinking 同源复用文案/占位/流光,但没有步骤体,
// 故只在 message-stack 底部维护一枚。lastError / 压缩进行中时各有专属提示,撤占位并归零计时不叠加。
// 有实时活动(内联/独立工具、流式、真实思考)时撤占位、重置静默计时;否则起算/续算 normalThinkingSince
// 并补建或原地改文本。render 快照与心跳静默补建共用本函数。
export function syncNormalThinking(stackRoot) {
  const existing = stackRoot.querySelector(":scope > .pipeline-thinking");
  if (state.lastError?.message || state.compaction?.status === "running" || normalTurnHasLiveActivity(stackRoot)) {
    if (existing) {
      existing.remove();
    }
    normalThinkingSince = 0;
    return;
  }
  if (normalThinkingSince === 0) {
    normalThinkingSince = Date.now();
  }
  const elapsed = Date.now() - normalThinkingSince;
  if (existing) {
    // 已有占位:原地改文本(不换节点),保住流光相位与布局。
    const label = existing.querySelector(".message-thinking-label");
    if (label) {
      label.textContent = pipelineThinkingLabel(elapsed);
    }
  } else {
    stackRoot.append(buildPipelineThinkingIndicator(elapsed));
  }
}

// 事件间隙占位的轮换文案：每 3 秒在【处理中/执行中/进行中/运行中】间切换，附已等待整秒数
// （类似 Claude Code，如「处理中… 6s」）。elapsedMs<0 归零。
export const PIPELINE_THINKING_WORDS = [t("Processing"), t("Executing"), t("In progress"), t("Running")];

export function pipelineThinkingLabel(elapsedMs = 0) {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const word = PIPELINE_THINKING_WORDS[Math.floor(totalSeconds / 3) % PIPELINE_THINKING_WORDS.length];
  return `${word}… ${totalSeconds}s`;
}

// 流水线事件间隙的流光占位（复用 iac-shimmer-sweep，见 styles.css）。
function buildPipelineThinkingIndicator(elapsedMs = 0) {
  const wrap = document.createElement("div");
  wrap.className = "pipeline-thinking is-thinking";
  const label = document.createElement("span");
  label.className = "message-thinking-label";
  label.textContent = pipelineThinkingLabel(elapsedMs);
  applyShimmerPhase(label);
  wrap.append(label);
  return wrap;
}

// 上下文压缩进行中指示器（复刻 Codex）：顶部分隔线 + 收拢图标 + 流光文案，
// 贴在转录底部、紧邻输入框上方。自动压缩显示「正在自动压缩上下文」，
// 手动 /compact 显示「正在压缩上下文」。
function buildCompactionIndicator(compaction) {
  const wrap = document.createElement("div");
  wrap.className = "context-compaction is-compacting";
  const icon = document.createElement("span");
  icon.className = "context-compaction-icon";
  icon.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "context-compaction-label";
  label.textContent = compaction?.auto ? t("Auto-compacting context") : t("Compacting context");
  applyShimmerPhase(label);
  wrap.append(icon, label);
  return wrap;
}

// 压缩结束后的一次性结果提示。成功不走这里（handleStreamEvent 已重载出持久分隔条）,
// 只在未产生新边界的情形下告知用户结果,避免动画消失后毫无反馈。未知/成功状态返回 null。
const COMPACTION_NOTICE_TEXT = {
  too_short: t("The conversation is short; no context compaction needed yet."),
  empty: t("There is no context to compact."),
  failed: t("Context compaction failed. Please try again later."),
  blocked: t("A task is already running; this compaction was skipped."),
};

function buildCompactionNotice(compaction) {
  const state = text(compaction?.state || "");
  const message = COMPACTION_NOTICE_TEXT[state];
  if (!message) {
    return null;
  }
  const wrap = document.createElement("div");
  wrap.className = "context-compaction-notice";
  if (state === "failed") {
    wrap.classList.add("is-error");
  }
  const label = document.createElement("span");
  label.className = "context-compaction-notice-label";
  label.textContent = message;
  wrap.append(label);
  return wrap;
}

// 把毫秒时长格式化为「14m 8s」「1h 2m」「8s」形式；无有效值返回空串。
function formatTurnDuration(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) {
    return "";
  }
  const totalSeconds = Math.round(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

function renderConversationMessage(message, state, options = {}) {
  const article = document.createElement("article");
  article.className = message.role === "user" ? "message message-user" : "message message-agent";
  // 流式中的助手消息打 is-streaming，供流水线步骤判断该步是否仍有「实时活动」
  // （有则显示其自身流光，事件间隙才补「正在思考」）。
  if (message.role !== "user" && message.status === "streaming" && message.stored !== true) {
    article.classList.add("is-streaming");
  }

  // 管线分组内部按步骤切成多段（text→工具→text），每段都挂「IaC Code」标签会很吵，
  // 由分组头（步骤标题）统一标识，这里隐藏逐段标签。
  if (!options.hideLabel) {
    const label = document.createElement("p");
    label.className = "message-label";
    label.textContent = message.role === "user" ? "You" : "IaC Code";
    article.append(label);
  }
  if (message.role !== "user") {
    const thinking = buildThinkingElement(message, state);
    if (thinking) {
      article.append(thinking);
    }
  }

  if (message.role === "user") {
    const messageAttachments = buildMessageAttachmentsElement(message, state);
    if (messageAttachments) {
      article.append(messageAttachments);
    }
  }

  const body = buildMessageBodyElement(message);
  if (body) {
    article.append(body);
  }
  if (message.role !== "user") {
    const messageTools = buildToolCardsElement(message, state, options.openToolUseId || "");
    if (messageTools) {
      article.append(messageTools);
    }
  }
  return article;
}

// 渲染一个「已结束」轮次：中间过程（思考 + 工具卡片）折进「已处理 <时间>」可展开块，
// 最终回答正文留在分割线下方始终显示。若该轮没有任何过程内容，则退化为普通渲染。
function renderCollapsedTurn(container, agentMessages, state, turnId, boundaries = []) {
  const processNodes = [];
  const answerNodes = [];
  // 中途压缩分隔线按「处理完前 afterCount 条助手消息后」登记；折进过程区(「已处理」组)内对应位置，
  // 使中途压缩的整段工具循环仍是一个组、而非被分隔线切成上下两个。
  const dividersByCount = new Map();
  for (const boundary of boundaries) {
    const list = dividersByCount.get(boundary.afterCount) || [];
    list.push(boundary.message);
    dividersByCount.set(boundary.afterCount, list);
  }
  const pushDividers = (count) => {
    const list = dividersByCount.get(count);
    if (!list) {
      return;
    }
    for (const dividerMessage of list) {
      processNodes.push(renderCompactionBoundaryMarker(dividerMessage));
    }
  };
  // 预先构建每条消息的工具卡，定位最后一条含工具调用的助手消息。
  // 只有它之后的连续纯文本才是本轮「最终回答」；其余中间文本(调用工具前的旁白)
  // 连同思考、工具卡一起折进「已处理」，避免把所有 text delta 平铺成答案。
  const toolElements = agentMessages.map((message) => buildToolCardsElement(message, state));
  let lastToolIndex = -1;
  for (let i = 0; i < toolElements.length; i += 1) {
    if (toolElements[i]) {
      lastToolIndex = i;
    }
  }
  for (let i = 0; i < agentMessages.length; i += 1) {
    pushDividers(i); // 分隔线插在第 i 条助手消息的各节点之前
    const message = agentMessages[i];
    const thinking = buildThinkingElement(message, state);
    if (thinking) {
      processNodes.push(thinking);
    }
    const body = buildMessageBodyElement(message);
    const isFinalAnswer = i > lastToolIndex;
    // 中间步骤的文本旁白折进「已处理」，排在该步骤的工具卡之前(先说明后调用)。
    if (body && !isFinalAnswer) {
      processNodes.push(body);
    }
    const tools = toolElements[i];
    if (tools) {
      processNodes.push(tools);
    }
    if (body && isFinalAnswer) {
      answerNodes.push(body);
    }
  }
  // afterCount==全部消息数(压缩发生在本轮所有助手消息之后):附在过程区末尾，仍留在「已处理」组内。
  pushDividers(agentMessages.length);

  // 没有中间过程可折叠时，直接按普通消息渲染，避免出现空折叠头。
  // (登记了压缩分隔线时 processNodes 至少含一条分隔线，不会命中此退化分支。)
  if (processNodes.length === 0) {
    for (const message of agentMessages) {
      container.append(renderConversationMessage(message, state));
    }
    return;
  }

  const article = document.createElement("article");
  article.className = "message message-agent message-turn";

  const label = document.createElement("p");
  label.className = "message-label";
  label.textContent = "IaC Code";
  article.append(label);

  const details = document.createElement("details");
  details.className = "turn-process";
  const summary = document.createElement("summary");
  summary.className = "turn-process-summary";
  const summaryLabel = document.createElement("span");
  summaryLabel.className = "turn-process-title";
  let elapsedMs = turnId ? state.turns?.[turnId]?.elapsedMs : null;
  // 服务器重启后 state.turns 为空，改用存档消息上持久化的 elapsedSeconds 回填（取该轮最大值）。
  if (typeof elapsedMs !== "number" || !Number.isFinite(elapsedMs)) {
    let maxSeconds = 0;
    for (const message of agentMessages) {
      const seconds = message?.elapsedSeconds;
      if (typeof seconds === "number" && Number.isFinite(seconds) && seconds > maxSeconds) {
        maxSeconds = seconds;
      }
    }
    if (maxSeconds > 0) {
      elapsedMs = maxSeconds * 1000;
    }
  }
  const duration = formatTurnDuration(elapsedMs);
  summaryLabel.textContent = duration ? t("Processed {elapsed}", { elapsed: duration }) : t("Processed");
  const chevron = document.createElement("span");
  chevron.className = "turn-process-chevron";
  chevron.textContent = "›";
  summary.append(summaryLabel, chevron);
  const processBody = document.createElement("div");
  processBody.className = "turn-process-body";
  for (const node of processNodes) {
    processBody.append(node);
  }
  details.append(summary, processBody);
  article.append(details);

  for (const node of answerNodes) {
    article.append(node);
  }
  container.append(article);
}

function setElementClassFlag(element, className, active) {
  if (element?.classList?.toggle) {
    element.classList.toggle(className, active);
    return;
  }
  const classes = new Set(text(element?.className).split(/\s+/).filter(Boolean));
  if (active) {
    classes.add(className);
  } else {
    classes.delete(className);
  }
  if (element) {
    element.className = [...classes].join(" ");
  }
}

// 贴底阈值:距底部不超过这个像素数就算「仍在跟读最新消息」，渲染后继续滚到底；
// 一旦用户上翻超过该距离，就视为在看历史，流式更新不再把视口拽回底部。
const MESSAGE_STACK_STICKY_BOTTOM_PX = 120;

function isMessageStackNearBottom(stack) {
  const distance =
    Number(stack.scrollHeight || 0) - Number(stack.scrollTop || 0) - Number(stack.clientHeight || 0);
  return distance <= MESSAGE_STACK_STICKY_BOTTOM_PX;
}

function refreshMessageStackOverflow(stack) {
  // 先强制切到 align-content:start 再测量：align-content:end 时，溢出的内容会顶到
  // 容器顶部之外，scrollHeight 会等于 clientHeight，导致检测不到溢出。切到 start 后
  // 测量才准确，随后再写回真实的溢出标记。
  setElementClassFlag(stack, "is-overflowing", true);
  const isOverflowing = Number(stack.scrollHeight || 0) > Number(stack.clientHeight || 0) + 1;
  setElementClassFlag(stack, "is-overflowing", isOverflowing);
  return isOverflowing;
}

function syncMessageStackOverflow(stack) {
  if (!stack) {
    return;
  }
  const overflowing = refreshMessageStackOverflow(stack);
  // 只有本轮渲染前判定应「贴底」时才滚到底（见 renderMessages 里对 stickBottom 的记录）；
  // 用户上翻看历史时 stickBottom 为 "0"，流式重渲染不再把他拽回底部。默认（无标记）仍滚到底，
  // 保持首屏/切换会话落在最新消息的既有行为。
  if (overflowing && stack.dataset.stickBottom !== "0") {
    stack.scrollTop = stack.scrollHeight;
  }
}

// 展开/收起状态存储：renderMessages 每帧 replaceChildren() 全量重建，<details>.open 只是 DOM
// 态，重建即丢失——于是用户展开的卡片/分组下一帧被打回默认（收起）。这里按稳定 id
// (data-open-key) 记住每个 details 的用户意图，重建后恢复；只记录“用户主动点击”产生的态，
// 不记录渲染时的程序化默认（toggle 事件异步派发，无法用标志位区分，故改用 click 捕获）。
const detailsOpenOverrides = new Map();

// 清空用户展开态（切换/重载会话时调用）：不同流水线会话可能复用同一 markerId（如
// plmk-step-intent_parsing-1），不清会串台。
function clearDetailsOpenOverrides() {
  detailsOpenOverrides.clear();
}

// 渲染后统一回放用户展开态：凡带 data-open-key 且用户有过记录的 details，覆盖其默认 open。
function applyDetailsOpenOverrides(stack) {
  if (!stack || detailsOpenOverrides.size === 0) {
    return;
  }
  for (const details of stack.querySelectorAll("details[data-open-key]")) {
    // 等待输入的步骤强制展开，忽略用户此前记录的折叠态（见 renderPipelineMarkerGroup）。
    if (details.dataset.forceOpen === "1") {
      continue;
    }
    const key = details.dataset.openKey;
    if (key && detailsOpenOverrides.has(key)) {
      details.open = detailsOpenOverrides.get(key);
    }
  }
}

function ensureMessageStackToggleSync(stack) {
  if (!stack || stack.dataset.toggleSyncBound === "1") {
    return;
  }
  stack.dataset.toggleSyncBound = "1";
  // 工具卡片是 <details>，展开/收起会改变内容高度；toggle 事件不冒泡，需在捕获阶段监听。
  // 这里只重算溢出标记（不强制滚到底部），否则展开后 align-content:end 会把卡片顶部
  // 顶出可视区且整页无法滚动——正是用户反馈的“命令展开后没法滚动页面”。
  stack.addEventListener(
    "toggle",
    (event) => {
      if (!(event.target instanceof HTMLDetailsElement)) {
        return;
      }
      refreshMessageStackOverflow(stack);
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => refreshMessageStackOverflow(stack));
      }
    },
    true,
  );
  // 记录用户主动展开/收起。键为 details.dataset.openKey（markerId/toolUseId/分组键）。
  // 必须同步落库：capture 阶段 details.open 仍是翻转前的态，点击 summary 必翻转（无 preventDefault），
  // 故目标态为 !details.open。曾用 rAF 读翻转后真实态——但流式渲染同样走 rAF（scheduleStreamRender），
  // 若渲染 rAF 抢先，会按默认（收起）重建并 applyDetailsOpenOverrides 时尚无记录 → 收起，
  // 随后 record 落后一帧却已无后续渲染回放，表现为「点击展开无效」。同步落库消除该竞态。
  stack.addEventListener(
    "click",
    (event) => {
      const summary = event.target?.closest?.("summary");
      if (!summary) {
        return;
      }
      const details = summary.parentElement;
      if (!(details instanceof HTMLDetailsElement) || !details.dataset.openKey) {
        return;
      }
      detailsOpenOverrides.set(details.dataset.openKey, !details.open);
    },
    true,
  );
  // 指针悬停在转录区时，把流式全量重建(replaceChildren)节流到低频(见 scheduleStreamRender)：
  // 每帧重建会销毁/重建光标下的节点，令 :hover 反复通断("一闪闪")并打断点击手势(工具卡/思考块
  // "点不动")。pointerenter/leave 不随子节点冒泡，只在进出 stack 边界时触发；指针移开时补一帧追平。
  stack.addEventListener("pointerenter", () => {
    messageStackPointerInside = true;
  });
  stack.addEventListener("pointerleave", () => {
    messageStackPointerInside = false;
    clearHoverThrottle();
    scheduleStreamRender();
  });
}

function scheduleMessageStackOverflowSync(stack) {
  syncMessageStackOverflow(stack);
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(() => syncMessageStackOverflow(stack));
  }
}

// 流水线并行候选修复(Issue 3):候选之间并发执行,其 step 标记与内容事件按序号交错灌进
// state.messages。下方渲染循环靠「depth 栈 + 文档顺序」嵌套,把内容归到当前最内层 body——一旦
// 方案0的子步骤标记与方案1的内容交错,方案0的内容就会被错挂到方案1名下(模板生成空、成本估算错位)。
// 本预处理不改任何内容,只按 groupId/parentGroupId 的父子关系把每棵候选子树重排成连续段:
// DFS 输出「标记→其全部子孙→下一同级」,使渲染循环的顺序嵌套与真实归属一致。普通对话的消息全部
// 落到 TOP 且保持原序号次序,输出与输入完全一致,零风险。
const PIPELINE_CONTAINER_KINDS = new Set(["pipeline_step", "pipeline_candidate", "pipeline_sub_step"]);
export function regroupPipelineMessages(messages) {
  if (!Array.isArray(messages) || messages.length === 0) {
    return messages;
  }
  const idOf = (message) => message.messageId || message.id || null;
  const markerById = new Map();
  const markerIdByGroupId = new Map();
  for (const message of messages) {
    if (!PIPELINE_CONTAINER_KINDS.has(message.kind)) {
      continue;
    }
    const id = idOf(message);
    if (!id) {
      continue;
    }
    markerById.set(id, message);
    const groupId = message.pipelineStep?.groupId;
    if (groupId) {
      markerIdByGroupId.set(String(groupId), id);
    }
  }
  // 没有任何流水线容器标记 → 与原数组等价,直接返回,避免多余工作。
  if (markerById.size === 0) {
    return messages;
  }
  const TOP = Symbol("pipeline-top");
  const parentKeyOf = (message) => {
    const id = idOf(message);
    if (PIPELINE_CONTAINER_KINDS.has(message.kind)) {
      // 容器:挂到其 parentGroupId 对应的标记下;无父(顶层 step)→ TOP。
      const parentGroupId = message.pipelineStep?.parentGroupId;
      const parentMarkerId = parentGroupId ? markerIdByGroupId.get(String(parentGroupId)) : null;
      return parentMarkerId && markerById.has(parentMarkerId) ? parentMarkerId : TOP;
    }
    if (message.kind === "context_compaction_boundary") {
      // 压缩边界:后端已把它挂在发生压缩的 step/候选组上(groupId 指向该组标记)。
      const groupId = message.pipelineStep?.groupId;
      const ownerId = groupId ? markerIdByGroupId.get(String(groupId)) : null;
      return ownerId && markerById.has(ownerId) ? ownerId : TOP;
    }
    // 内容气泡:id 形如 pl-{...}[#n],与其所属标记 plmk-{...} 一一对应(去掉 #n 序号后缀)。
    if (typeof id === "string" && id.startsWith("pl-") && !id.startsWith("plmk-")) {
      const ownerId = "plmk-" + id.slice(3).split("#")[0];
      if (markerById.has(ownerId)) {
        return ownerId;
      }
    }
    return TOP;
  };
  const children = new Map();
  children.set(TOP, []);
  for (const message of messages) {
    const key = parentKeyOf(message);
    if (!children.has(key)) {
      children.set(key, []);
    }
    children.get(key).push(message);
  }
  const ordered = [];
  const emit = (message) => {
    ordered.push(message);
    const id = idOf(message);
    const kids = id ? children.get(id) : null;
    if (kids) {
      for (const kid of kids) {
        emit(kid);
      }
    }
  };
  for (const message of children.get(TOP)) {
    emit(message);
  }
  // 防御:若某些容器因数据异常成环/失联导致未被 DFS 覆盖,补回原序,绝不丢消息。
  if (ordered.length !== messages.length) {
    const seen = new Set(ordered);
    for (const message of messages) {
      if (!seen.has(message)) {
        ordered.push(message);
      }
    }
  }
  return ordered;
}

function renderMessages(state) {
  const stack = byShell("message-stack");
  if (!stack) {
    return;
  }
  ensureMessageStackToggleSync(stack);
  // 排序以转录序号(sequence)为主：种子(stored)与 live 事件都带单调序号，同进程 reload 里流水线
  // 步骤内容会被缓冲区回放的 assistant.message.start 翻成 stored=false——若以 stored 为主键排序，
  // 这些内容就会被甩到所有 stored 行之后(步骤体清空、内容错位到底部)。改以 sequence 为主键后，翻
  // 转 stored 也不影响其转录位置；stored 仅作次键，保证尚未拿到序号(seq 0/缺失→视为无穷大排最后)
  // 的 live 消息里，历史(stored)仍排在新流式消息之前。
  const orderRank = (message) =>
    Number.isFinite(message.sequence) && message.sequence > 0 ? message.sequence : Number.POSITIVE_INFINITY;
  const messages = Object.values(state.messages || {}).sort((left, right) => {
    const leftOrder = orderRank(left);
    const rightOrder = orderRank(right);
    if (leftOrder !== rightOrder) {
      return leftOrder - rightOrder;
    }
    return (left.stored ? 0 : 1) - (right.stored ? 0 : 1);
  });
  // 序号排序后再按父子关系把并行候选子树重排成连续段（修复方案子 step 错位；见 regroupPipelineMessages）。
  const orderedMessages = regroupPipelineMessages(messages);
  // 转录尾部最新一张工具卡：保持展开直到下一条消息/工具到来才收起，避免工具"闪一下"（Issue 3）。
  const latestToolUseId = latestToolUseIdForTranscript(orderedMessages, state);
  // 流水线会话:进度不再走轮询。executor 发出的细粒度 A2A 信封由后端翻译器(pipeline_transcript)
  // 转成 pipeline.step.marker / assistant.message.* / tool.* 事件——live 时经 SSE 实时转发、reload
  // 时由 load_visible_transcript 回放成 pipeline_step / pipeline_candidate / pipeline_sub_step 气泡,
  // 两路同源灌进 state.messages,复用下方既有的恢复态嵌套渲染。
  // 清空重建前记录用户是否贴着底部——只有原本就在底部、首屏（还没有子节点）或刚切换/重载会话
  // （pendingScrollToBottom）时，渲染后才自动滚到底；否则保留其上翻位置，避免流式更新拽回底部。
  const existingChildCount = Number(stack.childElementCount ?? stack.children?.length ?? 0);
  const stickToBottom =
    pendingScrollToBottom || existingChildCount === 0 || isMessageStackNearBottom(stack);
  pendingScrollToBottom = false;
  stack.dataset.stickBottom = stickToBottom ? "1" : "0";
  stack.replaceChildren();
  // 乐观切换态：正文尚未拉到时显示加载动画，而不是「开始构建」引导块（后者是空会话的落点）。
  if (state.loadingSession && messages.length === 0 && !state.lastError?.message) {
    const loading = document.createElement("div");
    loading.className = "message-loading";
    const spinner = document.createElement("span");
    spinner.className = "message-loading-spinner";
    spinner.setAttribute("aria-hidden", "true");
    applySpinPhase(spinner, 0.85); // 转录区每帧 replaceChildren 重建，相位对齐避免转圈复位
    const copy = document.createElement("span");
    copy.className = "message-loading-copy";
    copy.textContent = t("Loading session…");
    loading.append(spinner, copy);
    stack.append(loading);
    setElementClassFlag(stack, "is-overflowing", false);
    return;
  }
  if (messages.length === 0 && !state.lastError?.message) {
    const empty = document.createElement("div");
    empty.className = "message-empty";
    const title = document.createElement("strong");
    title.textContent = t("Start building your infrastructure");
    const copy = document.createElement("span");
    copy.textContent = t("Describe a task, command, or infrastructure change and hand it to IaC Code.");
    empty.append(title, copy);
    stack.append(empty);
    setElementClassFlag(stack, "is-overflowing", false);
    return;
  }
  const pipelineStack = [{ depth: -1, body: stack }];
  // 本次渲染创建的所有流水线步骤分组（含 status），供事件间隙给进行中步骤注入「正在思考」。
  const stepGroups = [];
  // 本次渲染是否越过「↪ 普通对话」分隔：交接后 session 仍保留 contextId/taskId（isPipelineTranscript
  // 恒真），但尾部已是普通回合、没有 working 步骤体，占位须走 syncNormalThinking 而非 syncPipelineThinking。
  let sawNormalBoundary = false;
  // 累积当前轮次的助手消息，遇到下一轮 / 管线标记 / 循环结束时统一渲染：
  // 已结束的轮次把中间过程折进「已处理 <时间>」，进行中的轮次保持逐条展开。
  let pendingTurn = null;
  const flushPendingTurn = (active) => {
    if (!pendingTurn) {
      return;
    }
    // 中途压缩边界登记为 {afterCount, message}:处理完前 afterCount 条助手消息后画分隔线。
    const boundaries = pendingTurn.boundaries || [];
    if (active) {
      let bi = 0;
      for (let i = 0; i < pendingTurn.agentMessages.length; i += 1) {
        while (bi < boundaries.length && boundaries[bi].afterCount === i) {
          stack.append(renderCompactionBoundaryMarker(boundaries[bi].message));
          bi += 1;
        }
        stack.append(renderConversationMessage(pendingTurn.agentMessages[i], state, { openToolUseId: latestToolUseId }));
      }
      while (bi < boundaries.length) {
        stack.append(renderCompactionBoundaryMarker(boundaries[bi].message));
        bi += 1;
      }
    } else {
      renderCollapsedTurn(stack, pendingTurn.agentMessages, state, pendingTurn.turnId, boundaries);
    }
    pendingTurn = null;
  };

  for (let messageIndex = 0; messageIndex < orderedMessages.length; messageIndex += 1) {
    const message = orderedMessages[messageIndex];
    if (message.kind === "pipeline_outcome") {
      // 流水线结局彩条：收起流水线嵌套栈后作为独立整行插入，落在「↪ 普通对话」分隔的
      // 紧前方（后端在交接处按序先发本条、再发 boundary），即「进入普通对话前的最后一条」。
      flushPendingTurn(false);
      while (pipelineStack.length > 1) {
        pipelineStack.pop();
      }
      stack.append(renderPipelineOutcomeMarker(message));
      continue;
    }

    if (message.kind === "normal_chat_boundary") {
      flushPendingTurn(false);
      while (pipelineStack.length > 1) {
        pipelineStack.pop();
      }
      stack.append(renderPipelineBoundaryMarker(message));
      sawNormalBoundary = true;
      continue;
    }

    if (message.kind === "context_compaction_boundary") {
      // 自动压缩常在某个已结束回合的工具循环中途触发（needs_compaction 每次 ReAct 迭代都查），
      // 压缩边界因此落在该回合的助手消息之间。若在此 flushPendingTurn 收组再画分隔线，会把一个
      // 「已处理」组切成上下两个（截图 bug）。改为把分隔线登记进当前 pendingTurn，由 renderCollapsedTurn
      // 折进同一个「已处理」组内压缩发生的位置——整段工具循环仍是一个组。仅普通模式（非流水线嵌套栈）
      // 且已累积中途助手消息时如此；流水线内 / 回合边界（尚无累积消息）/ 顶端孤块仍走下方原逻辑。
      // 判定这条(批)压缩边界是「回合处理过程中」触发、还是「回合收尾之后」触发:向后跳过连续的
      // 压缩边界,看其后第一条“实际”消息。若那是同一回合的助手消息 → 回合中途(含低阈值下一回合内
      // 连发多次的情况),折进该回合「已处理」组内其发生的位置,整段仍是一个组、不在顶层堆叠。若其后
      // 已无实际消息(会话末尾手动 /compact)、或紧跟新回合的用户消息 / 流水线·交接·结局标记,则回合
      // 已收尾,走下方原逻辑在回合下方顶层画出可见分隔线(否则会折进收起的「已处理」组、落在最终回答
      // 之上而不可见 —— 44cd9909 回归)。仅普通模式(非流水线嵌套栈)如此;顶端孤块(pendingTurn 为空)
      // 仍走下方 orphanAtTop 逻辑。
      let compactionLookahead = messageIndex + 1;
      while (
        compactionLookahead < orderedMessages.length &&
        orderedMessages[compactionLookahead].kind === "context_compaction_boundary"
      ) {
        compactionLookahead += 1;
      }
      const compactionNextReal = orderedMessages[compactionLookahead];
      const compactionIsMidTurn =
        !!compactionNextReal &&
        compactionNextReal.role !== "user" &&
        compactionNextReal.kind !== "pipeline_outcome" &&
        compactionNextReal.kind !== "normal_chat_boundary" &&
        compactionNextReal.kind !== "pipeline_step" &&
        compactionNextReal.kind !== "pipeline_candidate" &&
        compactionNextReal.kind !== "pipeline_sub_step";
      if (pipelineStack.length === 1 && pendingTurn && compactionIsMidTurn) {
        (pendingTurn.boundaries || (pendingTurn.boundaries = [])).push({
          afterCount: pendingTurn.agentMessages.length,
          message,
        });
        continue;
      }
      flushPendingTurn(false);
      // 不弹栈：append 到当前最内层 body。普通模式栈深=1 → pipelineStack[0].body 即顶层；
      // 流水线模式 → 当前 step 组 body，分隔条落在压缩实际发生的 step 内。
      const target = pipelineStack[pipelineStack.length - 1].body;
      // 前导孤儿边界：压缩摘要落在转录最顶端时，其之前的历史已被压缩、上方空无一物，画一条
      // 「上文已压缩」分隔线毫无意义（分隔的是虚空），还会让整个会话"以分隔线开场"。跳过这类
      // 顶端孤块（codex 同样不在顶端画分隔条）；会话中途的压缩边界照常内联渲染。
      const orphanAtTop = target === stack && target.childElementCount === 0;
      // 连续压缩边界去重:多次压缩的摘要标记可能在重排后一起下沉到同一个回合间隙(如低阈值下
      // 一回合开头连发多次、reorder 尾部计数把相邻标记堆到答案之后),此分支会给每条各画一条
      // 「上下文已自动压缩」。同一处连着画两条以上纯属视觉噪声(彼此之间无任何真实消息),只保留
      // 一条。连续边界的 compactionNextReal/mid-turn 判定同质,故要么整段折叠、要么整段走到这里;
      // 只需在这里跳过「紧前一条也是压缩边界」的成员——该段的首条已经画过(或首条是顶端孤块被跳过,
      // 则后续同样是空 target 上的孤块,一并跳过,行为一致)。
      const prevOrdered = messageIndex > 0 ? orderedMessages[messageIndex - 1] : null;
      const consecutiveBoundary = !!prevOrdered && prevOrdered.kind === "context_compaction_boundary";
      if (!orphanAtTop && !consecutiveBoundary) {
        target.append(renderCompactionBoundaryMarker(message));
      }
      continue;
    }

    if (
      message.kind === "pipeline_step" ||
      message.kind === "pipeline_candidate" ||
      message.kind === "pipeline_sub_step"
    ) {
      flushPendingTurn(false);
      const depth = pipelineMarkerDepth(message);
      while (pipelineStack.length > 1 && pipelineStack[pipelineStack.length - 1].depth >= depth) {
        pipelineStack.pop();
      }
      const group = renderPipelineMarkerGroup(message, {
        diagrams: overlayDiagramOptimization(state.webDiagrams || [], state),
        candidates: state.webCandidates || [],
        toggleDiagram: (item) => outputController?.toggleDiagramPreview?.(item),
        onSelectCandidate: (item) =>
          handleSelectPipelineCandidate({ candidateName: item.candidateName, candidateIndex: item.candidateIndex }),
        selectedCandidate: resolvePipelineSelectedCandidate(state),
      });
      pipelineStack[pipelineStack.length - 1].body.append(group.details);
      pipelineStack.push({ depth, body: group.body });
      stepGroups.push({
        details: group.details,
        body: group.body,
        status: text(message.pipelineStep?.status || ""),
        diagramGroup: group.diagramGroup || null,
      });
      continue;
    }

    // 流水线步骤里出现的用户消息（如对 confirm_and_select 的选择答复「0」）并不属于任何步骤，
    // 它是步骤之间的一次用户操作。收起当前流水线栈，让它作为独立用户气泡在两个步骤标记之间
    // 于顶层渲染——否则会被折叠进已完成（reload 后收起）的步骤组里而彻底不可见（Issue 2）。
    if (message.role === "user" && pipelineStack.length > 1) {
      while (pipelineStack.length > 1) {
        pipelineStack.pop();
      }
    }

    // 管线分组内部的消息保持逐条内联渲染，不参与折叠；标签由分组头统一标识。
    if (pipelineStack.length > 1) {
      pipelineStack[pipelineStack.length - 1].body.append(
        renderConversationMessage(message, state, { hideLabel: true, openToolUseId: latestToolUseId }),
      );
      continue;
    }

    if (message.role === "user") {
      flushPendingTurn(false);
      stack.append(renderConversationMessage(message, state));
      pendingTurn = { turnId: message.turnId || null, agentMessages: [] };
    } else {
      if (!pendingTurn) {
        pendingTurn = { turnId: message.turnId || null, agentMessages: [] };
      }
      pendingTurn.agentMessages.push(message);
    }
  }
  // 最后一轮：仅当整体仍在进行时保持展开，否则同样折叠。
  flushPendingTurn(Boolean(state.currentTurnActive));
  // 选方案步骤的「查看架构图」按钮组延后到此处挂载：此时该步骤的提示文字及其余内联消息
  // 都已进入 body，按钮 append 到末尾即位于提示下方（贴合规格「置于提示文字下方」）。
  for (const group of stepGroups) {
    if (group.diagramGroup) {
      group.body.append(group.diagramGroup);
    }
  }
  // 流水线事件间隙：回合仍活跃时，给每个进行中的叶子步骤（可能多个并行）在其 body 内各补一枚
  // 流光占位，避免间隙里步骤内一片死寂（工具卡此时已默认收起）。等待输入的步骤 status 为 "input"
  // （非 "working"）故自然排除；步骤内已有实时活动时也不叠加。放在 diagramGroup 挂载之后，使占位
  // 恒在 body 末尾，与心跳静默补建位置一致。同一函数亦由心跳每秒调用（见 pipelineThinkingTick）。
  // 普通模式没有步骤体,占位挂在 stack 底部(syncNormalThinking 会先撤于 lastError/压缩之后再补,
  // 故置于下方 lastError/compaction 之前无碍——其内部已对二者让位)。
  // 记录本帧模式供心跳静默补建复用（心跳在两次 render 间无法自行判断尾部模式）。
  lastRenderPostHandoffNormal = sawNormalBoundary;
  if (state.currentTurnActive === true) {
    syncTurnThinking(stack);
  }
  if (state.lastError?.message) {
    const article = document.createElement("article");
    article.className = "message message-agent message-error";

    const label = document.createElement("p");
    label.className = "message-label";
    label.textContent = "Error";

    const body = document.createElement("div");
    body.className = "message-body markdown-body";
    renderMarkdownInto(body, text(state.lastError.message));

    article.append(label, body);
    stack.append(article);
  }
  if (state.compaction?.status === "running") {
    // 流水线模式:压缩条渲染进触发压缩的进行中叶子步骤体内(syncPipelineThinking 全权负责,含无叶子时
    // 的栈底兜底),不在此处落栈底,避免与步骤内的那枚重复。普通模式无步骤体,仍落 message-stack 底部。
    if (!(isPipelineTranscript(state) && !lastRenderPostHandoffNormal)) {
      stack.append(buildCompactionIndicator(state.compaction));
    }
  } else if (state.compaction?.status === "completed") {
    const notice = buildCompactionNotice(state.compaction);
    if (notice) {
      stack.append(notice);
    }
  }
  // 全量重建后回放用户展开态：把用户手动展开/收起过的卡片/分组恢复成他上次选择的样子，
  // 覆盖各自的程序化默认（Issue 3/5）。
  applyDetailsOpenOverrides(stack);
  scheduleMessageStackOverflowSync(stack);
}

// 排队行内联图标：fill:none + stroke:currentColor（见 styles.css .queued-input-icon）。
// “more” 的三点用带 round linecap 的零长路径渲染成圆点（lucide 写法）。
const QUEUED_ICON_PATHS = {
  "corner-down-right": ["M4 4v6a3 3 0 0 0 3 3h9", "M13 10l3 3-3 3"],
  trash: [
    "M4 6h12",
    "M8 6V4.6A1.6 1.6 0 0 1 9.6 3h0.8A1.6 1.6 0 0 1 12 4.6V6",
    "M6.2 6l.6 8.5A2 2 0 0 0 8.8 16.4h2.4a2 2 0 0 0 2-1.9L13.8 6",
    "M8.6 9v4.4",
    "M11.4 9v4.4",
  ],
  more: ["M5 10h.01", "M10 10h.01", "M15 10h.01"],
};

function makeQueuedIconPath(d) {
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  return path;
}

function makeQueuedIcon(kind, className = "queued-input-icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  for (const d of QUEUED_ICON_PATHS[kind] || []) {
    svg.append(makeQueuedIconPath(d));
  }
  return svg;
}

function renderQueuedInputs(state) {
  const strip = byShell("queued-inputs");
  if (!strip) {
    return;
  }
  const queuedInputs = Array.isArray(state.queuedInputs) ? state.queuedInputs : [];
  strip.replaceChildren();
  strip.hidden = queuedInputs.length === 0;
  if (queuedInputs.length === 0) {
    closeQueuedMoreMenu();
    return;
  }
  const list = document.createElement("div");
  list.className = "queued-input-list";
  queuedInputs.forEach((item, index) => {
    list.append(buildQueuedRow(item, index));
  });
  strip.append(list);
}

function buildQueuedRow(item, index) {
  const rawText = String(item?.text ?? item?.draft ?? "");
  const trimmed = rawText.trim();
  const submitted = Boolean(item?.submitted);
  const row = document.createElement("div");
  row.className = submitted ? "queued-input-row is-submitted" : "queued-input-row";
  row.dataset.index = String(index);

  const lead = document.createElement("span");
  lead.className = "queued-input-lead";
  lead.append(makeQueuedIcon("corner-down-right"));

  const textNode = document.createElement("span");
  textNode.className = "queued-input-text";
  textNode.textContent = trimmed;
  textNode.title = trimmed;

  const actions = document.createElement("div");
  actions.className = "queued-input-actions";

  if (!submitted) {
    const steer = document.createElement("button");
    steer.type = "button";
    steer.className = "queued-input-steer";
    steer.title = t("Submit now");
    steer.append(makeQueuedIcon("corner-down-right"));
    const steerLabel = document.createElement("span");
    steerLabel.className = "queued-input-steer-label";
    steerLabel.textContent = t("Steer");
    steer.append(steerLabel);
    steer.addEventListener("click", (event) => {
      event.stopPropagation();
      void steerQueuedRow(index, rawText);
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "queued-input-remove";
    remove.title = t("Delete");
    remove.setAttribute("aria-label", t("Delete queued message"));
    remove.append(makeQueuedIcon("trash"));
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      void deleteQueuedRow(index, rawText);
    });

    const more = document.createElement("button");
    more.type = "button";
    more.className = "queued-input-more";
    more.title = t("More");
    more.setAttribute("aria-label", t("More actions"));
    more.setAttribute("aria-haspopup", "menu");
    more.append(makeQueuedIcon("more"));
    more.addEventListener("click", (event) => {
      event.stopPropagation();
      openQueuedMoreMenu(index, rawText, more);
    });

    actions.append(steer, remove, more);
  }

  row.append(lead, textNode, actions);
  return row;
}

let appToastTimer = null;

// 应用内轻量提示：替代浏览器原生 alert（后者带域名前缀、样式无法控制、且阻塞交互）。
// 单实例复用，几秒后自动淡出；用于「已自动处理、只需知会一下」的非阻塞提示。
function showAppToast(message, { duration = 4200 } = {}) {
  if (!root || !message) {
    return;
  }
  let toast = byShell("app-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.className = "app-toast";
    toast.dataset.appShell = "app-toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    root.append(toast);
  }
  toast.textContent = message;
  toast.hidden = false;
  // 重启进入动画：先移除可见态并强制回流，再加回。
  toast.classList.remove("is-visible");
  void toast.offsetWidth;
  toast.classList.add("is-visible");
  if (appToastTimer) {
    clearTimeout(appToastTimer);
  }
  appToastTimer = setTimeout(() => {
    toast.classList.remove("is-visible");
    setTimeout(() => {
      toast.hidden = true;
    }, 240);
  }, duration);
}

async function refreshQueuedInputsAfterConflict() {
  // 409：队列已被整体 drain 或被其它操作改动，下标/文本已失效。重新拉取会话快照
  // 重建“排队中”列表，让界面回到与后端一致的状态。
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  const generation = ++sessionLoadGeneration;
  streamHandle?.close();
  streamHandle = null;
  const loaded = await loadSession(sessionId, { forceDraft: false, generation });
  if (loaded) {
    connectCurrentStream(generation);
  }
}

async function steerQueuedRow(index, expectedText) {
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  closeQueuedMoreMenu();
  try {
    await api.steerQueuedInput(sessionId, index, expectedText);
  } catch (error) {
    await refreshQueuedInputsAfterConflict();
  }
}

async function deleteQueuedRow(index, expectedText) {
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  closeQueuedMoreMenu();
  try {
    await api.deleteQueuedInput(sessionId, index, expectedText);
  } catch (error) {
    await refreshQueuedInputsAfterConflict();
  }
}

function editQueuedRow(index, expectedText) {
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  openAppModal({
    title: t("Edit queued message"),
    subtitle: t("Multiple lines supported; ⌘/Ctrl + Enter to save"),
    kind: "input",
    multiline: true,
    initialValue: expectedText,
    confirmLabel: t("Save"),
    onConfirm: async (value) => {
      try {
        await api.editQueuedInput(sessionId, index, value, expectedText);
      } catch (error) {
        if (error?.status === 409) {
          // 409：agent 已消费了一条排队消息（某个回合结束），或队列被其它操作改动，
          // 此时弹窗里记录的下标/原文本已失效，直接重试仍会失败。像删除/引导一样刷新
          // 列表回到与后端一致的状态，关闭弹窗并用中文说明原因。
          await refreshQueuedInputsAfterConflict();
          showAppToast(t("The queued messages changed (one has started processing or was modified). Please edit again."));
          return;
        }
        // 其它错误保持弹窗打开，用中文提示后可重试。
        throw new Error(error?.message || t("Save failed. Please try again."));
      }
    },
  });
}

function ensureQueuedMoreMenu() {
  let menu = byShell("queued-more-menu");
  if (menu) {
    return menu;
  }
  menu = document.createElement("div");
  menu.className = "project-menu-popover queued-more-menu";
  menu.dataset.appShell = "queued-more-menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  menu.addEventListener("click", (event) => event.stopPropagation());
  root?.append(menu);
  return menu;
}

function closeQueuedMoreMenu() {
  const menu = byShell("queued-more-menu");
  if (menu) {
    menu.hidden = true;
  }
}

function openQueuedMoreMenu(index, expectedText, anchor) {
  const menu = ensureQueuedMoreMenu();
  menu.replaceChildren();
  menu.append(
    makeProjectMenuItem("pmi-rename", t("Edit message"), () => {
      closeQueuedMoreMenu();
      editQueuedRow(index, expectedText);
    }),
  );
  menu.hidden = false;
  const anchorRect = anchor.getBoundingClientRect();
  const rootRect = root.getBoundingClientRect();
  const menuWidth = menu.offsetWidth || 160;
  let left = anchorRect.right - rootRect.left - menuWidth;
  left = Math.max(8, Math.min(left, rootRect.width - menuWidth - 8));
  menu.style.left = `${left}px`;
  // 排队条位于底部输入框上方，菜单向上弹出以免溢出视口下沿。
  menu.style.top = `${anchorRect.top - rootRect.top - menu.offsetHeight - 6}px`;
}

function renderBlocking(state) {
  const stack = byShell("blocking-stack");
  if (!stack) {
    return;
  }
  stack.replaceChildren(
    renderBlockingPanels(state, {
      onPermissionAnswer: async (requestId, answer) => {
        await api.answerPermission(requestId, answer);
      },
      onQuestionAnswer: async (requestId, answer) => {
        // 售卖流水线的 ask_user_question 暂停点没有在 question manager 里注册,
        // 它的答案得走标准 pipeline 消息通道(与用户手敲「1」/选项文字等价),
        // 由 _route_pending_question_answer 把序号/label/自由文本喂回暂停的引擎。
        if (state.questions?.[requestId]?.payload?.pipeline) {
          const freeText = text(answer?.free_text).trim();
          const label = text(answer?.selected_label).trim();
          const message = freeText || label;
          if (!message) {
            return;
          }
          const sessionId = state.currentSession?.sessionId;
          if (!sessionId) {
            return;
          }
          await api.postMessage(sessionId, { text: message });
          return;
        }
        await api.answerQuestion(requestId, answer);
      },
    }),
  );
  // 权限面板首次出现时把焦点从输入框移到面板，让上下键 / 回车立即可用，
  // 无需用户先用鼠标点击面板。
  const pendingFocus = stack.querySelector?.('.blocking-panel-permission[data-autofocus="pending"]');
  if (pendingFocus) {
    delete pendingFocus.dataset.autofocus;
    pendingFocus.focus?.({ preventScroll: false });
  }
}

function renderTools(state) {
  const stack = byShell("tool-stack");
  const activityStack = byShell("tool-activity-stack");
  if (!stack && !activityStack) {
    return;
  }
  const fallbackTools = detachedToolState(state);
  const hasToolActivity =
    Object.keys(fallbackTools.tools || {}).length > 0 || Object.keys(fallbackTools.localShell || {}).length > 0;
  if (stack) {
    stack.replaceChildren();
    stack.closest(".workspace-panel")?.classList.toggle("has-tools", false);
  }
  if (activityStack) {
    activityStack.replaceChildren(
      hasToolActivity ? renderToolCards(fallbackTools, { turnActive: !!state.currentTurnActive }) : "",
    );
  }
}

function renderPipeline(state) {
  const workspace = byShell("pipeline-workspace");
  if (!workspace) {
    return;
  }
  workspace.replaceChildren(renderPipelineWorkspace(state, { onSelectCandidate: handleSelectPipelineCandidate }));
}

export function pipelineWorkspaceEntryVisible(candidateState = {}) {
  return text(candidateState.currentSession?.mode) === "pipeline" && !candidateState.newSessionDraft?.active;
}

function renderStatus(state) {
  const session = state.currentSession || {};
  const pendingPermissions = Object.keys(state.permissions || {}).length;
  const pendingQuestions = Object.keys(state.questions || {}).length;
  const pendingTotal = pendingPermissions + pendingQuestions;

  const pipelineWorkspaceOpen = byShell("pipeline-workspace-open");
  if (pipelineWorkspaceOpen) {
    pipelineWorkspaceOpen.hidden = !pipelineWorkspaceEntryVisible(state);
  }

  const draft = state.newSessionDraft?.active ? state.newSessionDraft : null;
  setField("cwd", draft?.cwd || session.cwd || window.location.hostname || "localhost");
  setField("mode", draft?.mode || session.mode || "normal");
  setField("pending", pendingTotal);
  setField("session-id", session.sessionId || "");

  composer?.setTurnActive(Boolean(state.currentTurnActive));
  // 压缩/自动压缩进行中:提交进入排队,等压缩完成后由后端排空(与「回合进行中」一致的等待语义)。
  composer?.setCompacting(state.compaction?.status === "running");
  // 草稿会话下用草稿里暂存的选择，避免每次 render 把用户刚选的权限模式/模型重置掉。
  composer?.setPermissionMode(draft?.permissionMode || session.permissionMode || "default");
  // 按钮显示「本回合真正生效」的思考态：草稿选择优先，否则用后端算好的 thinkingEffective
  // （override 为 null 时即 provider 默认），避免旧会话一律错误显示为“关”。
  // false 是有效值（显式关），用 ?? 而非 || 以免被回退覆盖。
  composer?.setThinkingEnabled(draft?.thinkingEnabled ?? session.thinkingEffective);
  const draftSelection = draft?.providerSelection;
  if (draftSelection) {
    composer?.setActiveProvider(draftSelection);
  } else {
    composer?.setActiveProvider({
      provider: session.provider,
      model: session.model,
      effort: session.effort,
    });
  }
  composer?.setContextUsage(session.contextUsage || session.context_usage || {});
  composer?.setContextUsages(deriveContextUsageWindows(state));
  composer?.setContextFallbackLabel(deriveContextFallbackLabel(state, session));
  composer?.setReadOnly(Boolean(session?.readOnly));

  const readOnlyBanner = byShell("read-only-banner");
  if (readOnlyBanner) {
    readOnlyBanner.hidden = !session?.readOnly;
  }
}

function closeDraftSessionMenus() {
  if (!draftProjectMenuOpen && !draftProjectNewMenuOpen && !draftModeMenuOpen && !draftPipelineSubmenuOpen) {
    return;
  }
  draftProjectMenuOpen = false;
  draftProjectNewMenuOpen = false;
  draftModeMenuOpen = false;
  draftPipelineSubmenuOpen = false;
  renderDraftSessionControls(state);
}

function setDraftSessionPatch(patch = {}) {
  const draft = state.newSessionDraft?.active ? state.newSessionDraft : makeNewSessionDraft();
  state = {
    ...state,
    newSessionDraft: {
      ...draft,
      ...patch,
      active: true,
    },
  };
  render(state);
}

function makeDraftControlContent(iconClass, labelText) {
  const icon = document.createElement("span");
  icon.className = `draft-session-control-icon ${iconClass}`;
  icon.setAttribute("aria-hidden", "true");

  const label = document.createElement("span");
  label.className = "draft-session-control-label";
  label.textContent = labelText;

  const chevron = document.createElement("span");
  chevron.className = "draft-session-control-chevron";
  chevron.setAttribute("aria-hidden", "true");

  return [icon, label, chevron];
}

function makeDraftMenuItem({ iconClass = "", label = "", detail = "", active = false, submenu = false, onClick, onHover }) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = [
    "draft-session-menu-item",
    active ? "is-active" : "",
    submenu ? "has-submenu" : "",
  ]
    .filter(Boolean)
    .join(" ");
  button.setAttribute("role", submenu ? "menuitem" : "menuitemradio");
  if (!submenu) {
    button.setAttribute("aria-checked", active ? "true" : "false");
  }

  const icon = document.createElement("span");
  icon.className = `draft-session-menu-icon ${iconClass}`;
  icon.setAttribute("aria-hidden", "true");

  const copy = document.createElement("span");
  copy.className = "draft-session-menu-copy";
  const labelNode = document.createElement("span");
  labelNode.className = "draft-session-menu-label";
  labelNode.textContent = label;
  copy.append(labelNode);
  if (detail) {
    const detailNode = document.createElement("span");
    detailNode.className = "draft-session-menu-detail";
    detailNode.textContent = detail;
    copy.append(detailNode);
  }

  const check = document.createElement("span");
  check.className = "draft-session-menu-check";
  check.setAttribute("aria-hidden", "true");
  check.textContent = submenu ? "›" : active ? "✓" : "";

  button.append(icon, copy, check);
  button.addEventListener("click", () => onClick?.());
  button.addEventListener("mouseenter", () => onHover?.());
  return button;
}

function renderDraftProjectMenu(draft) {
  const menu = byShell("draft-project-menu");
  if (!menu) {
    return;
  }
  menu.hidden = !draft || !draftProjectMenuOpen;
  menu.replaceChildren();
  if (menu.hidden) {
    return;
  }

  const searchWrap = document.createElement("label");
  searchWrap.className = "draft-session-menu-search-wrap";
  const searchIcon = document.createElement("span");
  searchIcon.className = "draft-session-menu-search-icon";
  searchIcon.setAttribute("aria-hidden", "true");
  const search = document.createElement("input");
  search.className = "draft-session-menu-search";
  search.type = "search";
  search.placeholder = t("Search projects");
  search.value = draftProjectQuery;
  search.addEventListener("input", (event) => {
    draftProjectQuery = text(event.target?.value);
    renderDraftSessionControls(state);
    byShell("draft-project-menu")?.querySelector?.(".draft-session-menu-search")?.focus?.();
  });
  searchWrap.append(searchIcon, search);
  menu.append(searchWrap);

  const normalizedQuery = draftProjectQuery.trim().toLowerCase();
  const groups = groupSessionsByProject(state.sessions || [], state).filter((group) => {
    if (!normalizedQuery) {
      return true;
    }
    return [group.label, group.key].map(text).some((value) => value.toLowerCase().includes(normalizedQuery));
  });
  const customPath = draftProjectQuery.trim();
  const customPathLooksUsable = Boolean(customPath) && (customPath.startsWith("/") || customPath.startsWith("~") || customPath.includes("\\"));
  if (customPathLooksUsable) {
    menu.append(
      makeDraftMenuItem({
        iconClass: "is-project",
        label: t("Use directory"),
        detail: customPath,
        active: customPath === draft.cwd,
        onClick: () => {
          draftProjectMenuOpen = false;
          draftProjectQuery = "";
          setDraftSessionPatch({ cwd: customPath });
        },
      }),
    );
  }
  for (const group of groups.slice(0, 30)) {
    const key = projectKeyFromGroup(group);
    menu.append(
      makeDraftMenuItem({
        iconClass: "is-project",
        label: group.label || basenamePath(key),
        active: key === draft.cwd,
        onClick: () => {
          draftProjectMenuOpen = false;
          draftProjectNewMenuOpen = false;
          draftProjectQuery = "";
          setDraftSessionPatch({ cwd: key });
        },
      }),
    );
  }
  if (groups.length === 0) {
    const empty = document.createElement("div");
    empty.className = "draft-session-menu-empty";
    empty.textContent = t("No matching projects");
    menu.append(empty);
  }

  const divider = document.createElement("div");
  divider.className = "draft-session-menu-divider";
  menu.append(
    divider,
    makeDraftMenuItem({
      iconClass: "is-new-project",
      label: t("New project"),
      submenu: true,
      onClick: () => {
        draftProjectNewMenuOpen = !draftProjectNewMenuOpen;
        renderDraftSessionControls(state);
      },
      onHover: () => {
        if (!draftProjectNewMenuOpen) {
          draftProjectNewMenuOpen = true;
          renderDraftSessionControls(state);
        }
      },
    }),
    makeDraftMenuItem({
      iconClass: "is-no-project",
      label: t("No project"),
      active: !draft.cwd,
      onClick: () => {
        draftProjectMenuOpen = false;
        draftProjectNewMenuOpen = false;
        draftProjectQuery = "";
        setDraftSessionPatch({ cwd: "" });
      },
    }),
  );
}

function renderDraftProjectNewMenu(draft) {
  const menu = byShell("draft-project-new-menu");
  if (!menu) {
    return;
  }
  menu.hidden = !draft || !draftProjectMenuOpen || !draftProjectNewMenuOpen;
  menu.replaceChildren();
  if (menu.hidden) {
    return;
  }
  menu.append(
    makeDraftMenuItem({
      iconClass: "is-new-project",
      label: t("New blank project"),
      onClick: () => {
        draftProjectMenuOpen = false;
        draftProjectNewMenuOpen = false;
        setDraftSessionPatch({ cwd: "" });
      },
    }),
    makeDraftMenuItem({
      iconClass: "is-folder",
      label: t("Use existing folder"),
      onClick: () => {
        draftProjectNewMenuOpen = false;
        draftProjectQuery = "/";
        renderDraftSessionControls(state);
        byShell("draft-project-menu")?.querySelector?.(".draft-session-menu-search")?.focus?.();
      },
    }),
  );
}

function renderDraftModeMenu(draft) {
  const menu = byShell("draft-mode-menu");
  if (!menu) {
    return;
  }
  menu.hidden = !draft || !draftModeMenuOpen;
  menu.replaceChildren();
  if (menu.hidden) {
    return;
  }
  menu.append(
    makeDraftMenuItem({
      iconClass: "is-normal-mode",
      label: t("Normal mode"),
      detail: t("Enter the standard conversational IaC assistant"),
      active: draft.mode !== "pipeline",
      onClick: () => {
        draftModeMenuOpen = false;
        draftPipelineSubmenuOpen = false;
        setDraftSessionPatch({ mode: "normal" });
      },
    }),
    makeDraftMenuItem({
      iconClass: "is-pipeline-mode",
      label: t("Pipeline mode"),
      detail: t("Plan, generate, and validate with the pipeline"),
      active: draft.mode === "pipeline",
      submenu: true,
      onClick: () => {
        draftPipelineSubmenuOpen = !draftPipelineSubmenuOpen;
        renderDraftSessionControls(state);
      },
      onHover: () => {
        if (!draftPipelineSubmenuOpen) {
          draftPipelineSubmenuOpen = true;
          renderDraftSessionControls(state);
        }
      },
    }),
  );
}

function renderDraftPipelineSubmenu(draft) {
  const menu = byShell("draft-pipeline-menu");
  if (!menu) {
    return;
  }
  menu.hidden = !draft || !draftModeMenuOpen || !draftPipelineSubmenuOpen;
  menu.replaceChildren();
  if (menu.hidden) {
    return;
  }
  for (const option of PIPELINE_OPTIONS) {
    menu.append(
      makeDraftMenuItem({
        iconClass: "is-selling-pipeline",
        label: option.label,
        detail: option.detail,
        active: draft.mode === "pipeline" && option.id === draft.pipelineName,
        onClick: () => {
          draftModeMenuOpen = false;
          draftPipelineSubmenuOpen = false;
          setDraftSessionPatch({ mode: "pipeline", pipelineName: option.id });
        },
      }),
    );
  }
}

function renderDraftSessionControls(currentState) {
  const controls = byShell("draft-session-controls");
  if (!controls) {
    return;
  }
  const draft = currentState.newSessionDraft?.active ? currentState.newSessionDraft : null;
  controls.hidden = !draft;
  if (!draft) {
    renderDraftProjectMenu(null);
    renderDraftProjectNewMenu(null);
    renderDraftModeMenu(null);
    renderDraftPipelineSubmenu(null);
    return;
  }

  const projectControl = byShell("draft-project-control");
  if (projectControl) {
    projectControl.replaceChildren(...makeDraftControlContent("is-project", draftProjectLabel(draft)));
    projectControl.setAttribute("aria-expanded", draftProjectMenuOpen ? "true" : "false");
  }

  const modeControl = byShell("draft-mode-control");
  if (modeControl) {
    const modeIconClass = draft.mode === "pipeline" ? "is-selling-pipeline" : "is-normal-mode";
    modeControl.replaceChildren(
      ...makeDraftControlContent(modeIconClass, draft.mode === "pipeline" ? pipelineOptionLabel(draft.pipelineName) : t("Normal mode")),
    );
    modeControl.setAttribute("aria-expanded", draftModeMenuOpen ? "true" : "false");
  }

  renderDraftProjectMenu(draft);
  renderDraftProjectNewMenu(draft);
  renderDraftModeMenu(draft);
  renderDraftPipelineSubmenu(draft);
}

function openThreadMenu() {
  const menu = byShell("thread-menu");
  const toggle = byShell("thread-menu-toggle");
  if (!menu || !toggle) {
    return;
  }
  const pinLabel = byShell("thread-pin-label");
  if (pinLabel) {
    pinLabel.textContent = state.currentSession?.pinned ? t("Unpin conversation") : t("Pin conversation");
  }
  menu.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
}

function closeThreadMenu() {
  const menu = byShell("thread-menu");
  const toggle = byShell("thread-menu-toggle");
  if (!menu || !toggle) {
    return;
  }
  menu.hidden = true;
  toggle.setAttribute("aria-expanded", "false");
}

function startThreadRename() {
  closeThreadMenu();
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  openAppModal({
    title: t("Rename conversation"),
    subtitle: t("Keep it short and recognizable"),
    kind: "input",
    initialValue: resolveThreadTitle(state),
    confirmLabel: t("Save"),
    onConfirm: async (value) => {
      const updatedSession = await api.updateSession(sessionId, { title: value });
      state = replaceUpdatedSessionInState(state, updatedSession);
      render(state);
    },
  });
}

async function toggleCurrentSessionPinned() {
  closeThreadMenu();
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  try {
    const updated = await api.updateSession(sessionId, { pinned: !state.currentSession?.pinned });
    state = replaceUpdatedSessionInState(state, updated);
    await loadSessions();
    render(state);
  } catch (error) {
    window.alert?.(error?.message || t("Operation failed"));
  }
}

async function archiveCurrentSession() {
  closeThreadMenu();
  const session = state.currentSession;
  const sessionId = state.currentSessionId;
  if (!sessionId) {
    return;
  }
  try {
    await api.updateSession(sessionId, { archived: true });
    await loadSessions();
    startNewSessionDraft({ cwd: session?.cwd });
  } catch (error) {
    window.alert?.(error?.message || t("Archive failed"));
  }
}

function renderThreadHeader(state) {
  const session = state.currentSession || {};
  const title = state.newSessionDraft?.active ? t("New chat") : resolveThreadTitle(state);
  const titleButton = byShell("thread-title");
  const toggle = byShell("thread-menu-toggle");
  if (titleButton) {
    titleButton.textContent = title;
    titleButton.title = title;
    titleButton.disabled = !state.currentSessionId || Boolean(state.newSessionDraft?.active);
  }
  if (toggle) {
    toggle.disabled = !state.currentSessionId || Boolean(state.newSessionDraft?.active);
  }
}

// 侧边栏对「非当前会话」是用列表项里的 pendingPermissionCount/pendingQuestionCount 判断是否
// 「等待批准」的，但这些计数只在 loadSessions() 时从后端拉取一次；当前会话通过 SSE 收到的权限/
// 提问事件只写进 state.permissions/state.questions，并不会回写列表项。于是一旦切走，之前正在
// 等待批准的会话就会因为列表项计数仍是旧值（0）而丢掉「等待批准」标记。这里在每次 render 时把
// 当前会话的实时计数回写到列表项，保证切换后标记依然保留。
function persistCurrentSessionActivity(state) {
  const currentId = state.currentSessionId;
  if (!currentId || state.newSessionDraft?.active) {
    return;
  }
  const counts = {
    pendingPermissionCount: Object.keys(state.permissions || {}).length,
    pendingQuestionCount: Object.keys(state.questions || {}).length,
  };
  const patch = (list) => {
    for (const session of list || []) {
      if (displaySessionId(session) === currentId) {
        Object.assign(session, counts);
      }
    }
  };
  patch(state.sessions);
  patch(state.pinnedSessions);
  for (const group of state.projectGroups || []) {
    patch(group.sessions);
  }
}

function render(state) {
  if (!root) {
    return;
  }
  root.dataset.ready = "true";
  persistCurrentSessionActivity(state);
  renderSessions(state);
  renderMessages(state);
  renderQueuedInputs(state);
  renderBlocking(state);
  renderTools(state);
  workspace?.render(state);
  renderPipeline(state);
  renderStatus(state);
  renderInlineSessionStatusPanel(state);
  renderInlineMcpStatusPanel(state);
  renderThreadHeader(state);
  renderDraftSessionControls(state);
  // 流水线事件间隙占位的心跳启停：随每次渲染按 state 决定，启动（回合活跃+流水线）与停机
  // （turn.done/非流水线/切换会话）对称。静默期无 render，心跳一旦启动便自重排续命。
  syncPipelineThinkingHeartbeat(state);
}

let state = emptyState();
let streamHandle = null;
// 手动 /compact 成功后 4826 会重载会话并重连 SSE 以拉回持久化的压缩分隔条。但 context_id 非空的
// 会话(如流水线→normal 交接)重连时会从缓冲区底重放整段事件(见 compute_replay_sequence 的
// is_pipeline 分支),包含那条 compaction.finished(success)——若每次重放都重载,就成了
// 压缩成功→重载→重连→重放成功事件→再重载的死循环。用 {会话 id, 事件序号} 做幂等键:同一会话
// 同一序号只重载一次;重放的旧事件落到下方 reducer 正常渲染完成态。按会话隔离,避免切换会话时
// 新会话的低序号被旧会话的高水位误判为「已处理」而漏掉首次拉边界。
let lastReloadedCompaction = { sessionId: null, sequence: 0 };
// 切换 / 重载会话后强制下一次渲染滚到底（落在最新消息），与「贴底才跟读」的流式逻辑区分开。
let pendingScrollToBottom = false;
// 流式期间把一帧内的多个 SSE 事件合并成一次渲染：真实 LLM 每秒可推很多 token，逐事件全量重建
// 整段正文会卡顿，rAF 合并后每帧至多渲染一次。
let streamRenderScheduled = false;
// 指针悬停在转录区时，逐帧 replaceChildren 会销毁/重建光标下的节点，令 :hover 反复通断（"一闪闪"）
// 并打断点击手势（工具卡/思考块"点不动"）。悬停期间把全量重建合并到一个低频定时器，指针移开即追平。
let messageStackPointerInside = false;
let hoverThrottleTimer = null;
const HOVER_RENDER_THROTTLE_MS = 600;

// 流水线事件间隙占位的低频心跳：render 只被 SSE 事件触发，后端长时间静默（LLM 在想还没吐 delta、
// 工具在跑无进度事件）时不会重渲，占位便永不注入。心跳在回合活跃且处于流水线转录时每秒自重排一次，
// 独立于 SSE，静默期照样补建/续算占位；turn.done / 切换会话 / 非流水线时停机。tick 取 1s：秒数逐秒
// 刷新，文案由 floor(elapsed/3)%4 派生（每 3 秒换词）。pipelineThinkingSince 见 syncPipelineThinking。
let pipelineThinkingTimer = null;
const PIPELINE_THINKING_TICK_MS = 1000;
const pipelineThinkingSince = new Map();
// 普通(非流水线)模式的事件间隙占位:没有步骤体,改在 message-stack 底部挂单枚流光占位,故只需一个
// 「进入静默时刻」的标量(0＝当前不在静默)。语义与 pipelineThinkingSince 一致:活动恢复/回合结束即归零,
// 下段静默重新起算(「距上次可见进度已等待 N 秒」)。同由心跳每秒续算,见 syncNormalThinking。
let normalThinkingSince = 0;
// 最近一次 assistant text/thinking delta 抵达时刻（handleStreamEvent 打点）。流水线段消息的
// .message-agent.is-streaming 会挂到整步结束，唯有靠 delta 时效性区分「此刻在流式」与「标记陈旧、
// 后端已静默」——见 stepBodyHasLiveActivity。>阈值即判为停顿，事件间隙占位得以出现。
let lastStreamDeltaAt = 0;
const PIPELINE_STREAM_SILENCE_MS = 1500;
// 最近一次 render 是否越过「↪ 普通对话」分隔(renderMessages 每帧写)。交接后 session 仍保留
// contextId/taskId → isPipelineTranscript 恒真,但尾部已是普通回合,占位须改走 syncNormalThinking。
let lastRenderPostHandoffNormal = false;

// 按当前转录模式分派事件间隙占位:流水线→逐个 working 叶子步骤体;普通(含交接后普通回合)→message-stack
// 底部单枚。render 快照与心跳静默补建共用本分派。
function syncTurnThinking(stack) {
  if (isPipelineTranscript(state) && !lastRenderPostHandoffNormal) {
    syncPipelineThinking(stack);
  } else {
    syncNormalThinking(stack);
  }
}

// 部署「已用 N 秒」在两帧间隙的每秒续算:据 render 时写入 meta 的 data 基准(基准秒数 + 距帧到达墙钟秒数)
// 原地改文本。下一帧真实进度抵达时 render 会用新基准重写;帧无 data(完成/失败/无时刻)则不在此列、不动。
function syncStackProgressElapsed(stackRoot) {
  const metas = stackRoot.querySelectorAll(".tool-stack-progress-meta[data-stack-received-at]");
  const now = Date.now();
  for (const meta of metas) {
    const base = Number(meta.dataset.stackElapsedBase);
    const receivedAt = Number(meta.dataset.stackReceivedAt);
    if (!Number.isFinite(base) || !Number.isFinite(receivedAt)) {
      continue;
    }
    meta.textContent = t("Elapsed {n}s", { n: base + Math.max(0, Math.floor((now - receivedAt) / 1000)) });
  }
}

function pipelineThinkingTick() {
  pipelineThinkingTimer = null;
  if (!(state.currentTurnActive === true)) {
    // 不再合格：自然停机（不重排）。计时键/标量交给 syncPipelineThinkingHeartbeat 的 else 分支清理。
    return;
  }
  const stack = byShell("message-stack");
  if (stack && !(typeof document !== "undefined" && document.hidden)) {
    syncTurnThinking(stack);
    // 部署进行中的「已用 N 秒」在事件间隙每秒续算(后端约每十几秒才发一帧)。
    syncStackProgressElapsed(stack);
  }
  pipelineThinkingTimer = setTimeout(pipelineThinkingTick, PIPELINE_THINKING_TICK_MS);
}

function stopPipelineThinkingHeartbeat() {
  if (pipelineThinkingTimer !== null) {
    clearTimeout(pipelineThinkingTimer);
    pipelineThinkingTimer = null;
  }
  pipelineThinkingSince.clear();
  normalThinkingSince = 0;
}

// 在 render(state) 末尾调用：启停对称，覆盖 turn.done / 切换会话 / 非活跃三种收束(普通与流水线同门)。
function syncPipelineThinkingHeartbeat(state) {
  const active = state.currentTurnActive === true;
  if (active) {
    if (pipelineThinkingTimer === null) {
      pipelineThinkingTimer = setTimeout(pipelineThinkingTick, PIPELINE_THINKING_TICK_MS);
    }
  } else {
    stopPipelineThinkingHeartbeat();
  }
}

function clearHoverThrottle() {
  if (hoverThrottleTimer !== null) {
    clearTimeout(hoverThrottleTimer);
    hoverThrottleTimer = null;
  }
}

function scheduleStreamRender() {
  if (messageStackPointerInside) {
    if (hoverThrottleTimer !== null) {
      return;
    }
    hoverThrottleTimer = setTimeout(() => {
      hoverThrottleTimer = null;
      render(state);
    }, HOVER_RENDER_THROTTLE_MS);
    return;
  }
  if (streamRenderScheduled) {
    return;
  }
  streamRenderScheduled = true;
  const schedule =
    typeof requestAnimationFrame === "function" ? requestAnimationFrame : (callback) => setTimeout(callback, 16);
  schedule(() => {
    streamRenderScheduled = false;
    render(state);
  });
}
let composer = null;
let workspace = null;
let outputController = null;
let outputRefreshTimer = null;
// 工具进入终态后去抖刷新输出面板:资源栈/模板文件写盘有延迟,合并 400ms 内的多次终态,
// 避免每个 ros_stack/write_file/edit_file 完成都打一次 /outputs。
function scheduleOutputsRefresh() {
  if (outputRefreshTimer) clearTimeout(outputRefreshTimer);
  outputRefreshTimer = setTimeout(() => {
    outputController?.refresh(state.currentSessionId);
  }, 400);
}
let sessionLoadGeneration = 0;
let materializedDraftSession = null;
let draftProjectMenuOpen = false;
let draftProjectNewMenuOpen = false;
let draftModeMenuOpen = false;
let draftPipelineSubmenuOpen = false;
let draftProjectQuery = "";
const expandedProjectKeys = new Set();
const loadingProjectKeys = new Set();
let projectMenuKey = "";
let projectsSectionCollapsed = false;

function activeSessionIdentifiers() {
  return [
    state.currentSessionId,
    state.currentSession?.sessionId,
    state.currentSession?.webSessionId,
  ].filter(Boolean);
}

export function isCurrentSessionEvent(event = {}, activeSessionIds = [], generation = 0, currentGeneration = 0) {
  if (generation !== currentGeneration) {
    return false;
  }
  const ids = new Set(activeSessionIds.filter(Boolean).map(String));
  const eventSessionId = event.sessionId || event.payload?.sessionId || "";
  return !eventSessionId || ids.size === 0 || ids.has(String(eventSessionId));
}

function pipelineEventKind(event = {}) {
  return text(event?.kind || event?.eventType || event?.data?.kind || event?.data?.eventType || "");
}

// 解析当前流水线「已选方案」:实时选择态 → 快照 control → candidate.selected 事件,取先命中者;
// 无有效候选(既无候选名又无候选序号)时返回 null。confirm_and_select 行的绿色对勾与
// pipelineSelectionRequiresWorkspace 共用此判定,保证「已选」口径一致。
function resolvePipelineSelectedCandidate(candidateState = {}) {
  const snapshot = candidateState.pipelineSnapshot || {};
  const selected =
    candidateState.pipelineSelectedCandidate ||
    snapshot.control?.selectedCandidate ||
    (Array.isArray(candidateState.pipelineEvents)
      ? candidateState.pipelineEvents.find((event) => pipelineEventKind(event) === "candidate.selected")
      : null);
  if (
    selected &&
    (text(selected.candidateName) || (selected.candidateIndex !== undefined && selected.candidateIndex !== null))
  ) {
    return selected;
  }
  return null;
}

export function pipelineSelectionRequiresWorkspace(candidateState = {}) {
  if (text(candidateState.currentSession?.mode) !== "pipeline") {
    return false;
  }
  const snapshot = candidateState.pipelineSnapshot || {};
  const snapshotCandidates = Array.isArray(snapshot.display?.candidateDetails)
    ? snapshot.display.candidateDetails
    : [];
  const stepCandidates = (Array.isArray(snapshot.steps) ? snapshot.steps : []).flatMap((step) =>
    Array.isArray(step?.candidates) ? step.candidates : [],
  );
  const liveCandidates = Array.isArray(candidateState.candidateDetails) ? candidateState.candidateDetails : [];
  if (snapshotCandidates.length + stepCandidates.length + liveCandidates.length === 0) {
    return false;
  }

  if (resolvePipelineSelectedCandidate(candidateState)) {
    return false;
  }

  const pendingKind = text(snapshot.pendingInput?.kind || snapshot.control?.waitingInput?.kind);
  const waitingCandidateStep = (Array.isArray(snapshot.steps) ? snapshot.steps : []).some(
    (step) => text(step?.status) === "waiting_input" && Array.isArray(step?.candidates) && step.candidates.length > 0,
  );
  const selectionRequiredEvent = (Array.isArray(candidateState.pipelineEvents) ? candidateState.pipelineEvents : []).some(
    (event) => pipelineEventKind(event) === "candidate.selection.required",
  );
  return pendingKind === "candidate_selection" || waitingCandidateStep || selectionRequiredEvent;
}

function maybeOpenPipelineSelectionWorkspace(candidateState = state) {
  if (pipelineSelectionRequiresWorkspace(candidateState)) {
    openWorkspaceModal("pipeline");
  }
}

// 定期后台刷新用 perProjectLimit(5 条/项目)重建 projectGroups。若用户此前「展开」过某项目
// (expandedProjectKeys 里有其 key),直接用这份精简数据覆盖,会把展开时加载的完整会话列表打回
// 5 条 —— 表现为「展开的会话组无操作过一会自动收起」。这里对已展开的组保留上一份更长的会话列表,
// 并用刷新后的会话对象覆盖重叠项,使可见会话的活动态(转圈/未读)仍随轮询更新。
function preserveExpandedProjectGroups(freshGroups) {
  if (expandedProjectKeys.size === 0) {
    return freshGroups;
  }
  // 关键:expandedProjectKeys 存的是渲染层归一化后的 key(groupSessionsByProject 用
  // cwd || key || projectPath),而后端 projects 载荷只带 cwd、没有 key 字段。这里必须用同一个
  // projectKeyFromGroup 派生 key,否则 has(group.key) 恒为 has(undefined)=false,保留逻辑永不触发。
  const previousByKey = new Map((state.projectGroups || []).map((group) => [projectKeyFromGroup(group), group]));
  return freshGroups.map((group) => {
    const key = projectKeyFromGroup(group);
    if (!expandedProjectKeys.has(key)) {
      return group;
    }
    const previousSessions = previousByKey.get(key)?.sessions || [];
    const freshSessions = group.sessions || [];
    if (previousSessions.length <= freshSessions.length) {
      return group;
    }
    const freshById = new Map(freshSessions.map((session) => [displaySessionId(session), session]));
    return {
      ...group,
      sessions: previousSessions.map((session) => freshById.get(displaySessionId(session)) || session),
    };
  });
}

async function loadSessions() {
  const payload = await api.listSessions({
    limit: 50,
    perProjectLimit: PROJECT_THREAD_PREVIEW_LIMIT,
  });
  const sessions = payload.sessions || [];
  state = {
    ...state,
    sessions,
    pinnedSessions: payload.pinnedSessions || [],
    pinnedProjects: payload.pinnedProjects || [],
    projectGroups: preserveExpandedProjectGroups(payload.projects || []),
  };
  return sessions;
}

async function loadPipelineState(session) {
  const pipelineDisplayReplay = session?.pipeline?.displayReplay || null;
  if (!session?.contextId && !session?.taskId) {
    return pipelineDisplayReplay ? { pipelineDisplayReplay } : {};
  }
  try {
    const pipelineState = await api.getPipelineState({
      contextId: session.contextId || "",
      taskId: session.taskId || "",
    });
    return {
      pipelineSnapshot: pipelineState.snapshot || null,
      pipelineEvents: pipelineState.events || [],
      candidateDetails: pipelineState.snapshot?.display?.candidateDetails || [],
      diagrams: pipelineState.snapshot?.display?.diagrams || [],
      pipelineDisplayReplay,
    };
  } catch (error) {
    if (error?.status === 400 || error?.status === 404) {
      return pipelineDisplayReplay ? { pipelineDisplayReplay } : {};
    }
    return {
      pipelineError: error instanceof Error ? error.message : String(error),
      pipelineDisplayReplay,
    };
  }
}

function pipelineActionMessage(result = {}) {
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

const WORKSPACE_TABS = new Set(["status", "settings", "memory", "skills", "search", "pipeline"]);

function normalizedCommandResult(result = {}) {
  return result && typeof result === "object" ? result : {};
}

export function commandWorkspaceTab(result = {}) {
  const commandResult = normalizedCommandResult(result);
  const action = typeof commandResult.action === "string" ? commandResult.action : "";
  const command = typeof commandResult.command === "string" ? commandResult.command : "";

  // 失败/内容过短的压缩(accepted===false)走 composer 上方的内联「压缩结束」提示,
  // 不能被这条通用 accepted===false 兜底重新拽出废弃的「状态」模态盖住转录,否则内联提示形同虚设。
  if (commandResult.accepted === false && command !== "compact") {
    return "status";
  }
  if (["open_settings", "open_model_selector", "open_effort_selector", "model_updated", "effort_updated"].includes(action)) {
    return "model";
  }
  if (action === "open_panel") {
    const panel = typeof commandResult.panel === "string" ? commandResult.panel : "";
    return WORKSPACE_TABS.has(panel) ? panel : "status";
  }
  if (
    [
      "show_prompt_snapshot",
      "rename_session",
      "close_session_runtime",
      "resume",
    ].includes(action)
  ) {
    return "status";
  }
  // 压缩(手动 /compact 或自动)不再弹废弃的「状态」面板:进度由 composer 上方的
  // 内联压缩指示器 + compaction.started/finished SSE 呈现,命令结果不应打开任何模态。
  if (["help", "prompt", "rename", "clear", "debug"].includes(command)) {
    return "status";
  }
  return "";
}

export function commandOpensWorkspaceModal(result = {}) {
  return Boolean(commandWorkspaceTab(result));
}

function applyCommandResult(result = {}) {
  const commandResult = normalizedCommandResult(result);
  if (commandResult.accepted === true && commandResult.command === "status") {
    showInlineSessionStatus(commandResult.status || {});
    return;
  }
  if (commandResult.accepted === true && commandResult.command === "mcp") {
    showInlineMcpStatus(commandResult.mcp || {});
    return;
  }
  const tab = commandWorkspaceTab(commandResult);
  if (tab) {
    openWorkspaceModal(tab);
  }
  if (tab === "status" || commandResult.accepted === false) {
    workspace?.showStatusResult?.(commandResult);
  }
  if (commandResult.action === "open_session" && commandResult.session) {
    const targetSessionId = displaySessionId(commandResult.session);
    if (targetSessionId && targetSessionId !== state.currentSessionId) {
      void switchSession(targetSessionId);
    }
  }
}

function handleCommandResult(result = {}) {
  applyCommandResult(result);
  render(state);
}

export function createPipelineCandidateSelectionHandler({ selectCandidate, getState, setState, renderState }) {
  return async function handlePipelineCandidateSelection(selection = {}) {
    const requestedSessionId = selection.sessionId || getState().currentSessionId;
    // 乐观更新:点击「选择该方案」的瞬间就把已选态写进 pipelineSelectedCandidate,让对勾立刻
    // 出现、整排方案按钮立刻消失(见 renderPipelineMarkerGroup 的 !selectedCandidate 门控)。
    // 否则要等 selectCandidate() 这个 action POST 返回——而该 POST 会同步跑完整个 deploying 步骤
    // (executor.execute() 内含 ~数分钟部署),导致对勾/按钮抑制迟至「部署结束」才生效。
    // 记录前值以便 POST 失败时回滚;仅在仍是发起会话时应用,避免污染已切走的会话。
    const optimisticApplied = getState().currentSessionId === requestedSessionId;
    const previousSelectedCandidate = optimisticApplied ? getState().pipelineSelectedCandidate : undefined;
    if (optimisticApplied) {
      const optimisticState = {
        ...getState(),
        pipelineSelectedCandidate: {
          candidateName: selection.candidateName,
          candidateIndex: selection.candidateIndex,
        },
      };
      setState(optimisticState);
      renderState(optimisticState);
    }
    try {
      const result = await selectCandidate({
        ...selection,
        sessionId: requestedSessionId,
      });
      if (getState().currentSessionId !== requestedSessionId) {
        return result;
      }
      const nextState = {
        ...getState(),
        pipelineActionResult: result,
        pipelineActionError: "",
        pipelineNotice: pipelineActionMessage(result) || "accepted",
        pipelineSelectedCandidate: {
          candidateName: selection.candidateName,
          candidateIndex: selection.candidateIndex,
        },
      };
      setState(nextState);
      renderState(nextState);
      return result;
    } catch (error) {
      if (getState().currentSessionId !== requestedSessionId) {
        return Promise.reject(error);
      }
      const message = error instanceof Error ? error.message : String(error);
      const nextState = {
        ...getState(),
        pipelineActionError: message,
        pipelineNotice: "",
      };
      // 回滚乐观已选态,避免选择失败后仍错误地显示对勾/隐藏按钮。
      if (optimisticApplied) {
        nextState.pipelineSelectedCandidate = previousSelectedCandidate;
      }
      setState(nextState);
      renderState(nextState);
      return Promise.reject(error);
    }
  };
}

const handleSelectPipelineCandidate = createPipelineCandidateSelectionHandler({
  selectCandidate: api.selectPipelineCandidate,
  getState: () => state,
  setState: (nextState) => {
    state = nextState;
  },
  renderState: render,
});

async function loadSession(sessionId, options = {}) {
  const generation = Number.isInteger(options.generation) ? options.generation : ++sessionLoadGeneration;
  const previousSessionId = state.currentSessionId || null;
  const session = await api.getSession(sessionId);
  if (generation !== sessionLoadGeneration) {
    return false;
  }
  const storedMessages = await api.getMessages(sessionId);
  if (generation !== sessionLoadGeneration) {
    return false;
  }
  const hydratedPipelineState = await loadPipelineState(session);
  if (generation !== sessionLoadGeneration) {
    return false;
  }
  const { messages, tools: storedTools } = buildStoredTranscript(storedMessages);
  state = {
    ...emptyState(),
    sessions: state.sessions || [],
    projectGroups: state.projectGroups || [],
    pinnedSessions: state.pinnedSessions || [],
    pinnedProjects: state.pinnedProjects || [],
    currentSession: session,
    currentSessionId: displaySessionId(session),
    messages,
    tools: storedTools,
    permissions: Object.fromEntries((session.pendingPermissions || []).map((request) => [request.requestId, request])),
    questions: Object.fromEntries((session.pendingQuestions || []).map((request) => [request.requestId, request])),
    // 从会话快照恢复“排队中”列表：resync/切换会话会用 emptyState() 归零，若不在此恢复，
    // 繁忙轮次里权限确认触发的 resync 会让排队凭空消失（排队与权限确认需共存）。
    queuedInputs: (Array.isArray(session.queuedInputs) ? session.queuedInputs : [])
      .map((item) => ({
        text: typeof item?.text === "string" ? item.text : String(item ?? ""),
        draft: typeof item?.draft === "string" ? item.draft : "",
      }))
      .filter((item) => item.text || item.draft),
    // 上面的“排队中”种子是快照在 latestSequence 时的状态;记录该高水位,让 reducer 跳过
    // floor 回放里序号 <= 此值的队列事件,避免种子 + 回放重复计数(见 events.js 的守卫)。
    queuedInputsSeedSequence: Number.isFinite(session.latestSequence) ? session.latestSequence : 0,
    draft: session.draft || "",
    currentTurnActive: Boolean(session.currentTurnActive),
    lastSequence: Number.isFinite(session.replaySequence) ? session.replaySequence : 0,
    ...hydratedPipelineState,
  };
  composer?.setSession(state.currentSessionId);
  composer?.setPermissionMode(session.permissionMode || "default");
  composer?.setThinkingEnabled(session.thinkingEffective);
  composer?.setContextUsage(session.contextUsage || session.context_usage || {});
  composer?.setDraft(state.draft || "", { force: options.forceDraft !== false });
  composer?.setInputHistory(orderedUserInputs(messages));
  workspace?.setSession(state.currentSessionId, state.currentSession);
  // 切换 / 重载会话：本次渲染应落在最新消息处，不受上一个会话的滚动位置影响。
  pendingScrollToBottom = true;
  // 展开态是按 markerId/toolUseId 记的，跨会话可能撞键，切换到别的会话时清空避免串台；
  // 同会话的 resync（如权限确认触发）保留用户展开态，不打断正在查看的过程。
  if (previousSessionId && previousSessionId !== state.currentSessionId) {
    clearDetailsOpenOverrides();
  }
  render(state);
  outputController?.reset();
  outputController?.refresh(sessionId);
  maybeOpenPipelineSelectionWorkspace(state);
  return true;
}

async function handleStreamEvent(event, generation = sessionLoadGeneration) {
  if (!isCurrentSessionEvent(event, activeSessionIdentifiers(), generation, sessionLoadGeneration)) {
    return;
  }
  if (event.type === "session.resync.required") {
    const sessionId = state.currentSessionId;
    const nextGeneration = ++sessionLoadGeneration;
    streamHandle?.close();
    streamHandle = null;
    const loaded = await loadSession(sessionId, { forceDraft: false, generation: nextGeneration });
    if (loaded) {
      connectCurrentStream(nextGeneration);
    }
    return;
  }
  // 手动 /compact 压缩成功后，磁盘落了新的压缩摘要标记，但它只在整会话重载时才被后端序列化为
  // context_compaction_boundary。若不重载，前端在动画结束后一片空白（用户无从判断是否压缩成功）。
  // 复用 resync 的重载路径把持久化的「上文已压缩」分隔条拉回转录。仅对显式 state==="success" 的
  // 手动压缩触发；自动压缩（无 state、且发生在 turn 进行中）不重载，避免打断正在流式的回答。
  if (event.type === "compaction.finished" && event.payload?.state === "success") {
    const sessionId = state.currentSessionId;
    const seq = Number.isFinite(event.sequence) ? event.sequence : 0;
    // 仅对「本会话尚未处理过的、序号更大的」成功事件重载一次。重放的旧事件(seq<=已处理水位)
    // 落到下方 reducer 渲染完成态,不再重载——否则 context_id 会话的缓冲区重放会无限循环。
    const alreadyReloaded =
      lastReloadedCompaction.sessionId === sessionId && seq <= lastReloadedCompaction.sequence;
    if (!alreadyReloaded) {
      lastReloadedCompaction = { sessionId, sequence: seq };
      const nextGeneration = ++sessionLoadGeneration;
      streamHandle?.close();
      streamHandle = null;
      const loaded = await loadSession(sessionId, { forceDraft: false, generation: nextGeneration });
      if (loaded) {
        connectCurrentStream(nextGeneration);
      }
      return;
    }
  }
  state = reduceAndDedupe(state, event);
  // 记录最近一次可见流式进度，供流水线事件间隙占位判定「正在流式 vs 陈旧标记」
  // （见 stepBodyHasLiveActivity）。仅文本/思考 delta 算进度；工具活动另由工具卡自述。
  if (event.type === "assistant.text.delta" || event.type === "assistant.thinking.delta") {
    lastStreamDeltaAt = Date.now();
  }
  if (
    event.type === "candidate.detail" ||
    event.type === "pipeline.snapshot" ||
    (event.type === "pipeline.event" && pipelineEventKind(event.payload) === "candidate.selection.required")
  ) {
    maybeOpenPipelineSelectionWorkspace(state);
  }
  // 本轮进行中的实时上下文用量:后端把 contextUsage 附在 assistant.message.end / turn.done 上
  // (每个模型往返一次)。收到即写回当前会话,下一帧 renderStatus 会驱动 composer 圆环刷新,
  // 使圆环在 turn 进行中就更新,而不是只在会话加载/切换时。
  const liveContextUsage = event.payload?.contextUsage;
  if (liveContextUsage && typeof liveContextUsage === "object" && state.currentSession) {
    state.currentSession = { ...state.currentSession, contextUsage: liveContextUsage };
  }
  if (event.type === "draft.updated") {
    if (event.payload?.restored === true) {
      composer?.restoreDraft?.(event.payload);
    } else {
      composer?.setDraft(event.payload?.draft || "", { force: false });
    }
  }
  // 工具进入终态(ros_stack 建栈、write_file/edit_file 落模板)后,派生的资源栈与模板文件
  // 可能变化;去抖刷新输出面板,让开关角标与列表跟上,而不打断正在流式的正文渲染。
  if (event.type === "tool.finished") {
    scheduleOutputsRefresh();
  }
  // 架构图优化完成后，磁盘缓存已写入优化版；去抖刷新输出面板让 webDiagrams 收敛到优化版
  // （reload 后仍显示优化图，不重算）。
  if (event.type === "diagram.optimized" && event.payload?.status === "done") {
    scheduleOutputsRefresh();
  }
  // 优化开始时也刷新输出面板，让架构图行/预览头的徽标从「待优化」翻到「优化中」。
  if (event.type === "diagram.optimizing") {
    scheduleOutputsRefresh();
  }
  // 栈部署进行中的进度帧到达时也刷新输出面板：后端 outputs_payload 已能从 stack_current_changed
  // 的进行中态派生「创建中」资源栈，但仅在拉取 /outputs 时生效；tool.finished 只在终态触发，
  // 故这里在首个 stack.progress（约一个轮询间隔后）就刷新，让资源栈在创建开始即出现，而非完成后。
  if (event.type === "pipeline.event") {
    const kind = pipelineEventKind(event.payload);
    if (kind === "stack.progress" || kind === "stack.instances.progress") {
      scheduleOutputsRefresh();
    }
  }
  // 合并渲染：高频流式事件下每帧只重建一次正文，避免逐 token 全量重排造成卡顿。
  scheduleStreamRender();
}

function connectCurrentStream(generation = sessionLoadGeneration) {
  if (!state.currentSessionId) {
    return;
  }
  streamHandle?.close();
  streamHandle = api.openEventStream(state.currentSessionId, state.lastSequence || 0, (event) =>
    handleStreamEvent(event, generation),
  );
}

// 后台刷新侧边栏快照:非当前会话的「进行中」转圈与相对时间只来自列表快照,靠这里定期/切换时
// 重新拉取，才能在切走后仍看到正在运行的会话转圈、并让时间反映真实活动。只重绘侧边栏，不动当前
// 会话的正文，避免打断阅读或滚动。
async function refreshSessionsSidebar() {
  try {
    await loadSessions();
    renderSessions(state);
    // 搜索面板对「非当前会话」同样只靠这条轮询刷新:会话从「进行中」变「未读」后,若不重刷
    // 面板,它会一直停在进行中转圈(与侧栏矛盾)。面板打开时用当前输入重刷一遍会话行。
    if (isCommandPaletteOpen()) {
      void refreshPalette(byShell("command-palette-search")?.value || "");
    }
  } catch {
    // 后台刷新失败静默忽略，下一次 tick 再试。
  }
}

// 侧栏对「非当前会话」不建 SSE,其「进行中/等待/未读」只能靠这条列表轮询刷新。
// 空闲时用较慢节奏省开销;一旦列表里出现「非当前会话」处于进行中/等待,切到更快节奏,
// 把「该轮结束 → 转圈残留 / 变未读」的可见延迟从一个慢周期(12s)压到一个快周期(2.5s)——
// 否则驱动本轮的页面与旁观页面会在长达 12s 里各显示转圈/未读,状态互相矛盾。
const SESSIONS_REFRESH_INTERVAL_MS = 12000;
const SESSIONS_REFRESH_ACTIVE_MS = 2500;
let sessionsRefreshTimer = null;

// web 是长驻进程,后端每 6h 重查 PyPI;前端也需在运行中周期轮询 /api/update/status,
// 让运行期间发布的新版能自动弹出横幅,而不必等用户刷新页面。
const UPDATE_CHECK_INTERVAL_MS = 30 * 60 * 1000;
let updateCheckTimer = null;
// 用户本次会话 ✕ 关闭(或「不再提醒」)的版本号:周期轮询命中同一版本时不再重弹;
// 刷新页面即清空(与「✕ 仅本次会话隐藏」语义一致)。
let updateBannerDismissedVersion = null;

// 列表里是否存在「非当前会话」处于进行中/等待:当前会话由 SSE 事件实时驱动、无需靠轮询,
// 故只据此决定后台轮询是否需要提速。
function sidebarHasBackgroundActivity() {
  const lists = [state.sessions, state.pinnedSessions, ...(state.projectGroups || []).map((group) => group.sessions)];
  for (const list of lists) {
    for (const session of list || []) {
      if (displaySessionId(session) === state.currentSessionId) {
        continue;
      }
      if (sessionActivityState(session, state) !== "") {
        return true;
      }
    }
  }
  return false;
}

function scheduleSessionsRefresh() {
  const delay = sidebarHasBackgroundActivity() ? SESSIONS_REFRESH_ACTIVE_MS : SESSIONS_REFRESH_INTERVAL_MS;
  // 自重排的 setTimeout(在上一次刷新 await 完成后再排下一次)天然避免慢接口下的请求叠加。
  sessionsRefreshTimer = setTimeout(runSessionsRefreshTick, delay);
}

async function runSessionsRefreshTick() {
  if (!(typeof document !== "undefined" && document.hidden)) {
    await refreshSessionsSidebar();
  }
  scheduleSessionsRefresh();
}

function startSessionsAutoRefresh() {
  if (sessionsRefreshTimer) {
    return;
  }
  // 后台标签页不轮询;重新可见时立即补刷一次,避免切回原页面还要再等一个周期才追上真实状态。
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        void refreshSessionsSidebar();
      }
    });
  }
  scheduleSessionsRefresh();
}

// 在现有侧边栏列表里按 displaySessionId 找会话摘要，用于乐观切换时立刻显示标题/cwd。
function findSessionSummary(currentState, sessionId) {
  const match = (list) => (list || []).find((session) => displaySessionId(session) === sessionId);
  return (
    match(currentState.sessions) ||
    match(currentState.pinnedSessions) ||
    (currentState.projectGroups || []).map((group) => match(group.sessions)).find(Boolean) ||
    null
  );
}

async function switchSession(sessionId) {
  if (!sessionId) {
    return;
  }
  if (sessionId === state.currentSessionId) {
    return;
  }
  const generation = ++sessionLoadGeneration;
  streamHandle?.close();
  streamHandle = null;
  // 立即同步清空输入框并复位 dirty 标记，让切换后可以马上输入；随后 loadSession 用非强制方式
  // 设置草稿：force 会绕过 dirty 守卫，把用户在加载窗口（getSession/getMessages 等串行请求）里
  // 已经敲入的内容冲掉。改用 forceDraft:false 后，用户此刻的输入受 dirty 守卫保护而保留；若用户
  // 没输入，dirty 为 false，仍会正常载入该会话已保存的草稿。
  composer?.setDraft("", { force: true });
  // 乐观切换：先同步把当前会话切到目标（侧边栏立即高亮、正文清空并显示加载动画），再异步拉正文，
  // 避免在 loadSession 的串行请求（getSession/getMessages/loadPipelineState）返回前界面毫无反应。
  const summary = findSessionSummary(state, sessionId);
  state = {
    ...emptyState(),
    sessions: state.sessions || [],
    projectGroups: state.projectGroups || [],
    pinnedSessions: state.pinnedSessions || [],
    pinnedProjects: state.pinnedProjects || [],
    currentSession: summary || { sessionId },
    currentSessionId: sessionId,
    loadingSession: true,
  };
  composer?.setSession(sessionId);
  // 内容就绪后应落在最新消息处，先置位；loadSession 会在真实内容渲染后消费它。
  pendingScrollToBottom = true;
  render(state);
  try {
    const loaded = await loadSession(sessionId, { forceDraft: false, generation });
    if (loaded) {
      connectCurrentStream(generation);
      // 切走后原来正在运行的会话应继续显示转圈:重新拉取列表快照,让它的运行状态与时间刷新。
      void refreshSessionsSidebar();
    }
    // loaded === false 表示被更晚的切换取代，其 render 已接管，这里不再动状态。
  } catch (error) {
    // 仅当仍是本次切换时清掉加载态并落到错误提示，避免异常时转圈永久卡死。
    if (generation === sessionLoadGeneration) {
      state = { ...state, loadingSession: false, lastError: { message: error?.message || t("Failed to load session. Please try again.") } };
      render(state);
    }
  }
}

function startNewSessionDraft(options = {}) {
  const generation = ++sessionLoadGeneration;
  streamHandle?.close();
  streamHandle = null;
  materializedDraftSession = null;
  closeThreadMenu();
  closeAppModal();
  state = {
    ...emptyState(),
    sessions: state.sessions || [],
    projectGroups: state.projectGroups || [],
    pinnedSessions: state.pinnedSessions || [],
    pinnedProjects: state.pinnedProjects || [],
    newSessionDraft: makeNewSessionDraft(options),
  };
  composer?.setSession("");
  composer?.setContextUsage({});
  composer?.setDraft("", { force: true });
  workspace?.setSession("", null);
  outputController?.reset();
  render(state);
  setMobileSidebarOpen(false);
  return generation;
}

async function createSessionForSubmit() {
  if (!state.newSessionDraft?.active) {
    return state.currentSession || state.currentSessionId || "";
  }
  const generation = ++sessionLoadGeneration;
  streamHandle?.close();
  streamHandle = null;
  const session = await api.createSession(newSessionCreatePayload(state.newSessionDraft));
  if (generation !== sessionLoadGeneration) {
    try {
      await api.deleteSession(displaySessionId(session));
    } catch (_error) {
      // The stale response must never take ownership of the current UI. Cleanup is best-effort.
    }
    return "";
  }
  materializedDraftSession = session;
  state = {
    ...emptyState(),
    sessions: state.sessions || [],
    projectGroups: state.projectGroups || [],
    pinnedSessions: state.pinnedSessions || [],
    pinnedProjects: state.pinnedProjects || [],
    currentSession: session,
    currentSessionId: displaySessionId(session),
    lastSequence: Number.isFinite(session.replaySequence) ? session.replaySequence : 0,
    newSessionDraft: null,
  };
  composer?.setSession(state.currentSessionId, { preserveDraft: true });
  composer?.setPermissionMode(session.permissionMode || "default");
  composer?.setThinkingEnabled(session.thinkingEffective);
  composer?.setContextUsage(session.contextUsage || session.context_usage || {});
  workspace?.setSession(state.currentSessionId, state.currentSession);
  outputController?.reset();
  connectCurrentStream(generation);
  render(state);
  return session;
}

function promoteMaterializedDraftSession(event = {}) {
  if (!state.currentSessionId) {
    return;
  }
  const existing = (state.sessions || []).some((session) => displaySessionId(session) === state.currentSessionId);
  if (existing && !materializedDraftSession) {
    return;
  }
  const submittedTitle = text(event.text).trim();
  const session = {
    ...(materializedDraftSession || {}),
    ...(state.currentSession || {}),
  };
  if (!text(session.title) || session.title === "(empty)") {
    session.title = submittedTitle || t("New chat");
  }
  materializedDraftSession = null;
  state = {
    ...state,
    currentSession: session,
    sessions: existing
      ? (state.sessions || []).map((item) => (displaySessionId(item) === state.currentSessionId ? { ...item, ...session } : item))
      : [session, ...(state.sessions || [])],
    projectGroups: mergeSessionIntoProjectGroups(state.projectGroups || [], session),
  };
  render(state);
}

const RAIL_WIDTH_STORAGE_KEY = "iac-code:rail-width";
const RAIL_DEFAULT_WIDTH = 264;
const RAIL_MIN_WIDTH = 216;
const RAIL_MAX_WIDTH = 420;

function clampRailWidth(width) {
  if (!Number.isFinite(width)) {
    return null;
  }
  return Math.min(RAIL_MAX_WIDTH, Math.max(RAIL_MIN_WIDTH, Math.round(width)));
}

function applyRailWidth(width) {
  const clamped = clampRailWidth(width);
  if (clamped === null) {
    return;
  }
  root?.style?.setProperty("--rail-width", `${clamped}px`);
}

function persistRailWidth(width) {
  const clamped = clampRailWidth(width);
  if (clamped === null) {
    return;
  }
  try {
    window.localStorage?.setItem(RAIL_WIDTH_STORAGE_KEY, String(clamped));
  } catch (error) {
    // Ignore storage access issues (private mode, disabled storage).
  }
}

function restoreRailWidth() {
  try {
    const stored = window.localStorage?.getItem(RAIL_WIDTH_STORAGE_KEY);
    if (stored) {
      applyRailWidth(Number.parseFloat(stored));
    }
  } catch (error) {
    // Ignore storage access issues.
  }
}

function setupSidebarResize() {
  const handle = byShell("sidebar-resize");
  const rail = root?.querySelector(".session-rail");
  if (!handle || !rail) {
    return;
  }
  restoreRailWidth();
  let startX = 0;
  let startWidth = 0;
  let activePointerId = null;

  const onPointerMove = (event) => {
    if (activePointerId !== null && event.pointerId !== activePointerId) {
      return;
    }
    applyRailWidth(startWidth + (event.clientX - startX));
  };

  const stop = (event) => {
    if (activePointerId === null) {
      return;
    }
    if (event && event.pointerId !== undefined && event.pointerId !== activePointerId) {
      return;
    }
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", stop);
    document.removeEventListener("pointercancel", stop);
    document.body?.classList?.remove("is-resizing-sidebar");
    persistRailWidth(rail.getBoundingClientRect().width);
    activePointerId = null;
  };

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== undefined && event.button !== 0) {
      return;
    }
    event.preventDefault();
    activePointerId = event.pointerId ?? 0;
    startX = event.clientX;
    startWidth = rail.getBoundingClientRect().width;
    document.body?.classList?.add("is-resizing-sidebar");
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", stop);
    document.addEventListener("pointercancel", stop);
  });

  handle.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 32 : 16;
    let delta = 0;
    if (event.key === "ArrowLeft") {
      delta = -step;
    } else if (event.key === "ArrowRight") {
      delta = step;
    } else {
      return;
    }
    event.preventDefault();
    const next = clampRailWidth(rail.getBoundingClientRect().width + delta);
    if (next !== null) {
      applyRailWidth(next);
      persistRailWidth(next);
    }
  });

  handle.addEventListener("dblclick", () => {
    applyRailWidth(RAIL_DEFAULT_WIDTH);
    persistRailWidth(RAIL_DEFAULT_WIDTH);
  });
}

// 顶部更新提醒:启动后拉一次 /api/update/status,有新版本则渲染横幅。
// 「立即更新」→ 后台升级 + 轮询状态,done 后复用重启流程(两阶段 /health 轮询)自动刷新。
async function checkForUpdateBanner() {
  let status;
  try {
    status = await api.getUpdateStatus();
  } catch (_error) {
    return; // 检查失败不打扰用户
  }
  if (!status || !status.available) {
    return;
  }
  // 幂等:横幅已在(含正在升级中的横幅)→ 不重复插入,避免叠加或打断进行中的升级轮询。
  if (document.querySelector('[data-app-shell="update-banner"]')) {
    return;
  }
  // 会话级消抹:用户本次已关掉这个版本 → 周期轮询不再重弹。
  if (status.latestVersion && status.latestVersion === updateBannerDismissedVersion) {
    return;
  }

  const banner = document.createElement("div");
  banner.className = "update-banner";
  banner.dataset.appShell = "update-banner";
  banner.dataset.version = status.latestVersion || "";

  const msg = document.createElement("span");
  msg.className = "update-banner-msg";
  msg.textContent = t("New version v{latest} available (current v{current})", { latest: status.latestVersion, current: status.currentVersion });

  const actions = document.createElement("div");
  actions.className = "update-banner-actions";

  const applyBtn = document.createElement("button");
  applyBtn.type = "button";
  applyBtn.className = "update-banner-apply";
  applyBtn.textContent = t("Update now");

  const skipBtn = document.createElement("button");
  skipBtn.type = "button";
  skipBtn.className = "update-banner-skip";
  skipBtn.textContent = t("Don't remind me about this version");

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "update-banner-close";
  closeBtn.setAttribute("aria-label", t("Close"));
  closeBtn.textContent = "✕";

  actions.append(applyBtn, skipBtn, closeBtn);
  banner.append(msg, actions);
  document.body.prepend(banner);

  // ✕:仅本次会话隐藏,不打后端;记录版本,避免周期轮询把同一版本又弹回来;刷新仍会提醒。
  closeBtn.addEventListener("click", () => {
    updateBannerDismissedVersion = status.latestVersion || null;
    banner.remove();
  });

  // 不再提醒此版本:持久 suppress;同时置会话消抹兜底,避免后端状态未及时刷新时轮询重弹。
  skipBtn.addEventListener("click", async () => {
    updateBannerDismissedVersion = status.latestVersion || null;
    try {
      await api.dismissUpdate();
    } catch (_error) {
      // 忽略:移除 banner 优先
    }
    banner.remove();
  });

  // 两阶段健康轮询后自动刷新(与重启流程一致:先确认下线 sawDown 再等恢复)。
  let sawDown = false;
  const pollHealthThenReload = (deadline) => {
    const tick = async () => {
      let ok = false;
      try {
        const res = await fetch("/health", { cache: "no-store" });
        ok = res.ok;
      } catch (_error) {
        ok = false; // 重启期间连接被拒属预期
      }
      if (!ok) {
        sawDown = true;
      } else if (sawDown) {
        window.location.reload();
        return;
      }
      if (Date.now() >= deadline) {
        msg.textContent = t("Update complete. Please refresh manually.");
        removeApplySpinner();
        applyBtn.remove();
        return;
      }
      setTimeout(tick, 500);
    };
    setTimeout(tick, 1000);
  };

  // spinner 提到 banner 作用域,让 pollApplyStatus 的终态分支也能清除它;
  // 单例 + 先清后建,避免重复点「重试」叠加多个 spinner。
  let applySpinner = null;
  const removeApplySpinner = () => {
    if (applySpinner) {
      applySpinner.remove();
      applySpinner = null;
    }
  };

  const pollApplyStatus = (deadline) => {
    const tick = async () => {
      let latest = null;
      try {
        latest = await api.getUpdateStatus();
      } catch (_error) {
        latest = null;
      }
      const applyState = latest?.applyState;
      if (applyState === "failed") {
        msg.textContent = t("Update failed: {error}", { error: latest?.error || t("Unknown error") });
        msg.classList.add("is-error");
        applyBtn.disabled = false;
        applyBtn.textContent = t("Retry");
        removeApplySpinner();
        return;
      }
      if (applyState === "done") {
        msg.textContent = t("Update complete. Restarting…");
        try {
          await api.restartServer();
        } catch (_error) {
          // 202 后进程即将替换,响应可能中断
        }
        pollHealthThenReload(Date.now() + 20000);
        return;
      }
      if (Date.now() >= deadline) {
        msg.textContent = t("The update is taking a while. Please refresh manually later.");
        applyBtn.disabled = false;
        applyBtn.textContent = t("Retry");
        removeApplySpinner();
        return;
      }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 1000);
  };

  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    skipBtn.remove();
    closeBtn.remove();
    msg.textContent = t("Updating…");
    msg.classList.remove("is-error"); // 「重试」时清掉上一次的失败红字
    removeApplySpinner(); // 「重试」时先清掉上一次的 spinner,避免叠加
    applySpinner = document.createElement("div");
    applySpinner.className = "server-restart-spinner update-banner-spinner";
    applySpinner.setAttribute("aria-hidden", "true");
    actions.prepend(applySpinner);
    try {
      await api.applyUpdate();
    } catch (error) {
      msg.textContent = t("Failed to start update: {error}", { error: error instanceof Error ? error.message : String(error) });
      msg.classList.add("is-error");
      applyBtn.disabled = false;
      applyBtn.textContent = t("Retry");
      removeApplySpinner();
      return;
    }
    // pip 升级在后台线程执行,120s 上限覆盖较慢的下载/安装。
    pollApplyStatus(Date.now() + 120000);
  });
}

// 自重排轮询:发现运行期间发布的新版即自动弹出横幅(checkForUpdateBanner 幂等,不会叠加)。
function scheduleUpdateCheck() {
  updateCheckTimer = setTimeout(runUpdateCheckTick, UPDATE_CHECK_INTERVAL_MS);
}

async function runUpdateCheckTick() {
  if (!(typeof document !== "undefined" && document.hidden)) {
    await checkForUpdateBanner();
  }
  scheduleUpdateCheck();
}

function startUpdateAutoCheck() {
  if (updateCheckTimer) {
    return;
  }
  // 后台标签页不轮询;重新可见时立即补查一次,避免切回页面还要再等一个周期才看到新版提醒。
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        void checkForUpdateBanner();
      }
    });
  }
  scheduleUpdateCheck();
}

async function start() {
  applyDomI18n(document);
  if (!root) {
    return;
  }
  composer = createComposerController(
    {
      form: byShell("composer-form"),
      textarea: byShell("composer-input"),
      sendButton: byShell("composer-send"),
      fileInput: byShell("composer-file-input"),
      attachmentChips: byShell("attachment-chips"),
      skillRow: byShell("composer-skill-row"),
      suggestions: byShell("suggestions"),
      errorTarget: byShell("composer-error"),
      permissionControl: byShell("permission-mode-control"),
      permissionMenu: byShell("permission-mode-menu"),
      thinkingToggle: byShell("thinking-toggle"),
      modelControl: byShell("composer-model-control"),
      modelMenu: byShell("composer-model-menu"),
    },
    api,
    {
      onCommandResult: handleCommandResult,
      createSessionForSubmit,
      isDraftSessionActive: () => Boolean(state.newSessionDraft?.active),
      // 流水线会话(含流水线草稿)里从「/」菜单隐藏 /compact 等无法主动执行的命令;
      // 交接为普通对话后 session.mode 翻转为 "normal",不再命中。
      isPipelineMode: () =>
        text(state.currentSession?.mode) === "pipeline" ||
        Boolean(state.newSessionDraft?.active && state.newSessionDraft?.mode === "pipeline"),
      onSubmitAccepted: promoteMaterializedDraftSession,
      onPermissionModeChange: (mode) => {
        if (state.newSessionDraft?.active) {
          setDraftSessionPatch({ permissionMode: mode });
        }
      },
      onThinkingEnabledChange: (enabled) => {
        if (state.newSessionDraft?.active) {
          setDraftSessionPatch({ thinkingEnabled: enabled });
        }
      },
      onProviderSelectionChange: (selection) => {
        if (state.newSessionDraft?.active) {
          setDraftSessionPatch({ providerSelection: selection });
        }
      },
    },
  );
  workspace = createWorkspaceController(
    {
      tabs: byShell("workspace-tabs"),
      content: byShell("workspace-content"),
    },
    api,
    {
      // 归档面板取消归档/删除会话后,重新拉取会话列表并重绘侧栏,使被隐藏的空项目
      // 或恢复的会话立即出现,无需手动刷新页面。
      onSessionsMutated: async () => {
        await loadSessions();
        render(state);
      },
      // 新会话默认面板据此渲染「默认流水线」下拉:流水线模式必须绑定一条具体流水线。
      pipelineOptions: PIPELINE_OPTIONS,
      // 设置面板保存新会话默认后同步前端内存 + 当前草稿,使返回后新建会话立即生效、无需刷新页面。
      onSessionDefaultsSaved: applySessionDefaults,
    },
  );
  outputController = createOutputController({
    getSessionId: () => state.currentSessionId,
    api,
    // 架构图行/预览头的优化三态(待优化/优化中/已完成),按当前会话事件态计算。
    getDiagramState: (item) => diagramOptimizationState(item, state),
    // /outputs 的架构图落到 state.webDiagrams,供 pipeline step4 候选卡内联折叠图消费;
    // pipeline 视图下触发一次重渲染(与 renderPipelineWorkspace 同一渲染入口)。
    onPayload: (payload) => {
      state.webDiagrams = payload.diagrams || [];
      // 权威候选表(confirm_and_select 的 input_required.options)。选择器据此渲染候选行,
      // 不再依赖「架构图能否渲染」——某候选模板解析失败(无图)时仍可选,只是缺「查看架构图」。
      state.webCandidates = payload.candidates || [];
      scheduleStreamRender();
    },
  });
  setupSidebarResize();
  byShell("new-session")?.addEventListener("click", () => startNewSessionDraft());
  byShell("sidebar-drawer-toggle")?.addEventListener("click", toggleMobileSidebar);
  byShell("sidebar-search")?.addEventListener("click", openCommandPalette);
  byShell("sidebar-skills")?.addEventListener("click", () => openWorkspaceModal("skills"));
  byShell("workspace-open-config")?.addEventListener("click", () => openWorkspaceModal("settings"));
  byShell("pipeline-workspace-open")?.addEventListener("click", () => openWorkspaceModal("pipeline"));
  byShell("draft-project-control")?.addEventListener("click", (event) => {
    event.stopPropagation();
    const nextOpen = !draftProjectMenuOpen;
    draftProjectMenuOpen = !draftProjectMenuOpen;
    draftProjectNewMenuOpen = false;
    draftModeMenuOpen = false;
    draftPipelineSubmenuOpen = false;
    renderDraftSessionControls(state);
    if (nextOpen) {
      const menu = byShell("draft-project-menu");
      if (menu) {
        menu.scrollTop = 0;
      }
    }
  });
  byShell("draft-mode-control")?.addEventListener("click", (event) => {
    event.stopPropagation();
    draftModeMenuOpen = !draftModeMenuOpen;
    draftProjectMenuOpen = false;
    draftProjectNewMenuOpen = false;
    draftPipelineSubmenuOpen = false;
    renderDraftSessionControls(state);
  });
  for (const name of ["draft-project-menu", "draft-project-new-menu", "draft-mode-menu", "draft-pipeline-menu"]) {
    byShell(name)?.addEventListener("click", (event) => event.stopPropagation());
  }
  byShell("workspace-modal-close")?.addEventListener("click", closeWorkspaceModal);
  byShell("workspace-modal-backdrop")?.addEventListener("click", closeWorkspaceModal);
  byShell("command-palette-backdrop")?.addEventListener("click", closeCommandPalette);
  byShell("command-palette-search")?.addEventListener("input", (event) => {
    const query = event.target?.value || "";
    if (paletteSearchTimer) {
      clearTimeout(paletteSearchTimer);
    }
    paletteSearchTimer = setTimeout(() => void refreshPalette(query), 150);
  });
  byShell("thread-title")?.addEventListener("click", openThreadMenu);
  byShell("thread-menu-toggle")?.addEventListener("click", (event) => {
    event.stopPropagation();
    if (byShell("thread-menu")?.hidden === false) {
      closeThreadMenu();
      return;
    }
    openThreadMenu();
  });
  byShell("thread-pin")?.addEventListener("click", () => void toggleCurrentSessionPinned());
  byShell("thread-rename")?.addEventListener("click", startThreadRename);
  byShell("thread-archive")?.addEventListener("click", () => void archiveCurrentSession());
  byShell("projects-section-toggle")?.addEventListener("click", toggleProjectsSectionCollapsed);
  byShell("projects-collapse-all")?.addEventListener("click", () => void toggleAllProjectsCollapsed());
  byShell("app-modal-form")?.addEventListener("submit", submitAppModal);
  // 多行编辑:回车换行,⌘/Ctrl + Enter 提交(与 composer 一致)。
  byShell("app-modal-textarea")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void submitAppModal(event);
    }
  });
  byShell("app-modal-cancel")?.addEventListener("click", closeAppModal);
  byShell("app-modal-close")?.addEventListener("click", closeAppModal);
  byShell("app-modal-backdrop")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) {
      closeAppModal();
    }
  });
  document.addEventListener("click", (event) => {
    if (!event.target?.closest?.(".thread-current")) {
      closeThreadMenu();
    }
    if (!event.target?.closest?.(".draft-session-controls")) {
      closeDraftSessionMenus();
    }
    if (!event.target?.closest?.(".project-menu-popover") && !event.target?.closest?.(".project-menu")) {
      closeProjectMenu();
    }
    if (!event.target?.closest?.(".queued-more-menu") && !event.target?.closest?.(".queued-input-more")) {
      closeQueuedMoreMenu();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key.toLowerCase() === "r" && event.metaKey && event.altKey) {
      event.preventDefault();
      startThreadRename();
      return;
    }
    if (event.key.toLowerCase() === "p" && event.metaKey && event.altKey) {
      event.preventDefault();
      void toggleCurrentSessionPinned();
      return;
    }
    if (event.key.toLowerCase() === "a" && event.metaKey && event.shiftKey) {
      event.preventDefault();
      void archiveCurrentSession();
      return;
    }
    if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      openCommandPalette();
      return;
    }
    if (
      (event.metaKey || event.ctrlKey) &&
      !event.altKey &&
      !event.shiftKey &&
      /^[1-9]$/.test(event.key) &&
      byShell("command-palette")?.hidden === false
    ) {
      event.preventDefault();
      activatePaletteResult(paletteResults[Number(event.key) - 1]);
      return;
    }
    if (event.key === "Escape") {
      closeAppModal();
      closeThreadMenu();
      closeDraftSessionMenus();
      closeProjectMenu();
      closeQueuedMoreMenu();
      closeCommandPalette();
      closeWorkspaceModal();
      setMobileSidebarOpen(false);
    }
  });
  await loadSessions();
  // 打开 Web 页面时默认进入新会话界面，而非自动选中最近的已有会话。
  startNewSessionDraft();
  // 定时后台刷新侧边栏,让其它会话的运行转圈与相对时间保持新鲜。
  startSessionsAutoRefresh();
  // 顶部更新提醒(fire-and-forget,检查失败静默);随后周期轮询,让运行中发布的新版自动弹出。
  void checkForUpdateBanner();
  startUpdateAutoCheck();
}

start().catch((error) => {
  const target = byShell("app-error");
  if (target) {
    target.textContent = error instanceof Error ? error.message : String(error);
  }
});
