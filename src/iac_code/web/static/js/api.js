export const WEB_EVENT_TYPES = [
  "session.started",
  "session.updated",
  "session.resync.required",
  "user.message",
  "assistant.message.start",
  "assistant.text.delta",
  "assistant.thinking.delta",
  "assistant.message.tombstone",
  "assistant.message.end",
  "tool.started",
  "tool.input.delta",
  "tool.progress",
  "tool.result",
  "tool.finished",
  "subagent.event",
  "permission.request",
  "permission.resolved",
  "question.request",
  "question.resolved",
  "queued-input.accepted",
  "queued-input.submitted",
  "queued-input.removed",
  "queued-input.updated",
  "draft.updated",
  "interrupt.accepted",
  "command.started",
  "command.finished",
  "compaction.started",
  "compaction.finished",
  "mcp.status.updated",
  "task.notification",
  "resource.observed",
  "plan.updated",
  "debug.stream_event",
  "local.shell.start",
  "local.shell.end",
  "pipeline.event",
  "pipeline.snapshot",
  "pipeline.step.marker",
  "pipeline.step.context",
  "candidate.detail",
  "diagram.render",
  "diagram.optimizing",
  "diagram.optimized",
  "cleanup.status",
  "error",
  "turn.done",
];

const WEB_EVENT_SOURCE_TYPES = [...WEB_EVENT_TYPES, "app.error"];

function sessionUrl(sessionId, suffix = "") {
  return `/api/sessions/${encodeURIComponent(sessionId)}${suffix}`;
}

async function jsonFetch(url, options = {}) {
  const headers = {
    Accept: "application/json",
    ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
    ...(options.headers || {}),
  };
  const response = await fetch(url, { cache: "no-store", ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  let payload = "";
  try {
    payload = contentType.includes("application/json") ? await response.json() : await response.text();
  } catch (error) {
    payload = {
      error: {
        message: `Request failed with ${response.status}`,
        parseError: error instanceof Error ? error.message : String(error),
      },
    };
  }
  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && payload.error && payload.error.message
        ? payload.error.message
        : `Request failed with ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export function createSession(payload = {}) {
  return jsonFetch("/api/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listSessions({ limit = 50, cwd = "", projectLimit, perProjectLimit } = {}) {
  const url = new URL("/api/sessions", window.location.origin);
  if (Number.isFinite(limit)) {
    url.searchParams.set("limit", String(limit));
  }
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  }
  if (Number.isFinite(projectLimit)) {
    url.searchParams.set("projectLimit", String(projectLimit));
  }
  if (Number.isFinite(perProjectLimit)) {
    url.searchParams.set("perProjectLimit", String(perProjectLimit));
  }
  return jsonFetch(`${url.pathname}${url.search}`);
}

export function updateProject(cwd, payload = {}) {
  return jsonFetch("/api/projects", {
    method: "PATCH",
    body: JSON.stringify({ cwd, ...payload }),
  });
}

export function revealProject(cwd) {
  return jsonFetch("/api/projects/reveal", {
    method: "POST",
    body: JSON.stringify({ cwd }),
  });
}

export function archiveProjectSessions(cwd) {
  return jsonFetch("/api/projects/archive-sessions", {
    method: "POST",
    body: JSON.stringify({ cwd }),
  });
}

export function getSession(sessionId) {
  return jsonFetch(sessionUrl(sessionId));
}

export function getMessages(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/messages"));
}

export function getOutputs(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/outputs"));
}

export function getOutputFile(sessionId, path) {
  const url = new URL(sessionUrl(sessionId, "/outputs/file"), window.location.origin);
  url.searchParams.set("path", path);
  return jsonFetch(url.toString());
}

export function updateSession(sessionId, payload = {}) {
  return jsonFetch(sessionUrl(sessionId), {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function getSessionStatus(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/status"));
}

export function getStatus(sessionId) {
  return getSessionStatus(sessionId);
}

export function getSessionDebug(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/debug"));
}

export function getSessionPrompt(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/prompt"));
}

export function compactSession(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/compact"), {
    method: "POST",
  });
}

export function restartServer() {
  return jsonFetch("/api/server/restart", { method: "POST" });
}

// 更新能力:检查缓存中的可用更新 / 启动后台升级 / 忽略此版本(持久 suppress)。
export function getUpdateStatus() {
  return jsonFetch("/api/update/status");
}

export function applyUpdate() {
  return jsonFetch("/api/update/apply", { method: "POST" });
}

export function dismissUpdate() {
  return jsonFetch("/api/update/dismiss", { method: "POST" });
}

export function getProviders() {
  return jsonFetch("/api/providers");
}

export function saveProviderConfig({
  provider = "",
  model = "",
  effort = "",
  apiBase = "",
  apiKey = "",
  thinkingBudget,
  maxCompletionTokens,
} = {}) {
  const payload = {
    provider,
    model,
  };
  if (effort) {
    payload.effort = effort;
  }
  if (apiBase) {
    payload.apiBase = apiBase;
  }
  if (apiKey) {
    payload.apiKey = apiKey;
  }
  // 数值上限须可清除:显式传入(int 或 null)才带上;undefined 表示保持现状。
  if (thinkingBudget !== undefined) {
    payload.thinkingBudget = thinkingBudget;
  }
  if (maxCompletionTokens !== undefined) {
    payload.maxCompletionTokens = maxCompletionTokens;
  }
  return jsonFetch("/api/providers/config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function setActiveProvider(provider = "") {
  return jsonFetch("/api/providers/active", {
    method: "PUT",
    body: JSON.stringify({ provider }),
  });
}

export function clearProviderConfig(provider = "") {
  return jsonFetch("/api/providers/config", {
    method: "DELETE",
    body: JSON.stringify({ provider }),
  });
}

export function getAliyunCloud() {
  return jsonFetch("/api/cloud/aliyun");
}

export function saveAliyunCloud({
  mode = "",
  region = "",
  accessKeyId = "",
  accessKeySecret = "",
  stsToken = "",
  stsExpiration = "",
  ramRoleArn = "",
  ramSessionName = "",
  oauthSiteType = "",
  oauthAccessToken = "",
  oauthRefreshToken = "",
  oauthAccessTokenExpire = "",
  oauthRefreshTokenExpire = "",
} = {}) {
  const payload = {
    mode,
    region,
  };
  const optionalFields = {
    accessKeyId,
    accessKeySecret,
    stsToken,
    ramRoleArn,
    ramSessionName,
    oauthSiteType,
    oauthAccessToken,
    oauthRefreshToken,
  };
  for (const [key, value] of Object.entries(optionalFields)) {
    if (value) {
      payload[key] = value;
    }
  }
  if (stsExpiration !== "") {
    payload.stsExpiration = Number(stsExpiration);
  }
  if (oauthAccessTokenExpire !== "") {
    payload.oauthAccessTokenExpire = Number(oauthAccessTokenExpire);
  }
  if (oauthRefreshTokenExpire !== "") {
    payload.oauthRefreshTokenExpire = Number(oauthRefreshTokenExpire);
  }
  return jsonFetch("/api/cloud/aliyun", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function oauthLoginAliyun({ site = "", region = "" } = {}) {
  const payload = { site };
  if (region) {
    payload.region = region;
  }
  return jsonFetch("/api/cloud/aliyun/oauth-login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getMemory({ sessionId = "", cwd = "" } = {}) {
  const url = new URL("/api/memory", window.location.origin);
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  } else if (sessionId) {
    url.searchParams.set("sessionId", sessionId);
  }
  return jsonFetch(url.toString());
}

export function listMemoryProjects() {
  return jsonFetch("/api/memory/projects");
}

// 通用「已知项目」枚举,与记忆面板共用同一后端(插件面板项目选择器亦用之)。
export function listProjects() {
  return jsonFetch("/api/memory/projects");
}

export function saveProjectMemory({ sessionId = "", cwd = "", content = "" } = {}) {
  return jsonFetch("/api/memory/project", {
    method: "PUT",
    body: JSON.stringify({ sessionId, cwd, content }),
  });
}

export function saveUserMemory({ sessionId = "", content = "" } = {}) {
  return jsonFetch("/api/memory/user", {
    method: "PUT",
    body: JSON.stringify({ sessionId, content }),
  });
}

export function saveAutoMemory(enabled) {
  return jsonFetch("/api/memory/auto", {
    method: "PUT",
    body: JSON.stringify({ enabled: Boolean(enabled) }),
  });
}

// 外来会话可见性:控制非 web 入口产生的 pipeline/普通会话是否在侧栏列表出现。
export function getForeignSessionsVisibility() {
  return jsonFetch("/api/settings/foreign-sessions");
}

export function saveForeignSessionsVisibility({ showPipeline = false, showNormal = false } = {}) {
  return jsonFetch("/api/settings/foreign-sessions", {
    method: "PUT",
    body: JSON.stringify({
      showPipeline: Boolean(showPipeline),
      showNormal: Boolean(showNormal),
    }),
  });
}

// 售卖流水线审查步骤:读取/保存是否开启 review step(enable_reviewing 特性开关)。
export function getSellingReviewStep() {
  return jsonFetch("/api/settings/pipeline-review-step");
}

export function saveSellingReviewStep(enabled) {
  return jsonFetch("/api/settings/pipeline-review-step", {
    method: "PUT",
    body: JSON.stringify({ enabled: Boolean(enabled) }),
  });
}

// 审查步骤前置依赖(infraguard):只读探测是否就绪、web 端能否安装。
export function getReviewStepPrerequisite() {
  return jsonFetch("/api/settings/pipeline-review-step/prerequisite");
}

// 触发 infraguard 安装,逐行读取 NDJSON 进度事件并回调 onEvent。
export async function installReviewStepPrerequisite(onEvent) {
  const response = await fetch("/api/settings/pipeline-review-step/install", {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/x-ndjson" },
  });
  if (!response.ok) {
    let message = `Request failed with ${response.status}`;
    try {
      const payload = await response.json();
      if (payload && payload.error && payload.error.message) {
        message = payload.error.message;
      }
    } catch (error) {
      // ignore parse errors; fall back to status message
    }
    const err = new Error(message);
    err.status = response.status;
    throw err;
  }
  if (!response.body) {
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (chunk) => {
    buffer += chunk;
    let index = buffer.indexOf("\n");
    while (index !== -1) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (line) {
        try {
          onEvent(JSON.parse(line));
        } catch (error) {
          // skip malformed lines
        }
      }
      index = buffer.indexOf("\n");
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    flush(decoder.decode(value, { stream: true }));
  }
  flush(decoder.decode());
  const tail = buffer.trim();
  if (tail) {
    try {
      onEvent(JSON.parse(tail));
    } catch (error) {
      // skip malformed trailing line
    }
  }
}

// 配色方案:读取/保存当前 UI 主题(服务端 settings.yml,跨设备一致)。
export function getAppearance() {
  return jsonFetch("/api/settings/appearance");
}

export function saveAppearance(theme) {
  return jsonFetch("/api/settings/appearance", {
    method: "PUT",
    body: JSON.stringify({ theme }),
  });
}

export function getUiLanguage() {
  return jsonFetch("/api/settings/ui-language");
}

export function saveUiLanguage(language) {
  return jsonFetch("/api/settings/ui-language", {
    method: "PUT",
    body: JSON.stringify({ language }),
  });
}

// 新会话默认:读取/保存新建会话草稿的初始权限模式与会话模式(服务端 settings.yml)。
export function getSessionDefaults() {
  return jsonFetch("/api/settings/session-defaults");
}

export function saveSessionDefaults({ permissionMode, mode, pipelineName } = {}) {
  return jsonFetch("/api/settings/session-defaults", {
    method: "PUT",
    body: JSON.stringify({ permissionMode, mode, pipelineName }),
  });
}

export function searchLegacyMemory(query = "", cwd = "") {
  const url = new URL("/api/memory/legacy", window.location.origin);
  url.searchParams.set("q", query);
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  }
  return jsonFetch(url.toString());
}

export function deleteLegacyMemory(memoryId, cwd = "", scope = "") {
  const url = new URL("/api/memory/legacy/" + encodeURIComponent(memoryId), window.location.origin);
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  }
  if (scope) {
    url.searchParams.set("scope", scope);
  }
  return jsonFetch(url.toString(), { method: "DELETE" });
}

// 跨全部项目模糊搜索会话,供 spotlight 命令面板消费。空 query 返回最近若干条。
export function searchSessions(query = "", { limit = 50, archived = false } = {}) {
  const url = new URL("/api/sessions/search", window.location.origin);
  url.searchParams.set("q", query);
  url.searchParams.set("limit", String(limit));
  if (archived) {
    url.searchParams.set("archived", "true");
  }
  return jsonFetch(url.toString());
}

// 已归档对话:按项目分组返回所有 archived 会话(不受活动列表的每项目上限约束)。
export function listArchivedSessions() {
  return jsonFetch("/api/sessions/archived");
}

// 永久删除单个会话(存储 + 内存),用于已归档视图的垃圾桶按钮。
export function deleteSession(sessionId) {
  return jsonFetch(sessionUrl(sessionId), { method: "DELETE" });
}

// 删除全部已归档会话;传 cwd 则仅删该项目内的已归档会话。
export function deleteArchivedSessions(cwd = "") {
  const url = new URL("/api/sessions/archived", window.location.origin);
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  }
  return jsonFetch(`${url.pathname}${url.search}`, { method: "DELETE" });
}

export function getSkills(sessionId = "", cwd = "") {
  const url = new URL("/api/skills", window.location.origin);
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  } else if (sessionId) {
    url.searchParams.set("sessionId", sessionId);
  }
  return jsonFetch(url.toString());
}

export function saveDisabledSkills({ sessionId = "", disabled = [], cwd = "" } = {}) {
  return jsonFetch("/api/skills/disabled", {
    method: "PUT",
    body: JSON.stringify({ sessionId, disabled, cwd }),
  });
}

function mcpScopeParams(url, { sessionId = "", cwd = "" } = {}) {
  if (cwd) {
    url.searchParams.set("cwd", cwd);
  } else if (sessionId) {
    url.searchParams.set("sessionId", sessionId);
  }
  return url;
}

export function getMcpServers({ sessionId = "", cwd = "" } = {}) {
  const url = mcpScopeParams(new URL("/api/mcp/servers", window.location.origin), { sessionId, cwd });
  return jsonFetch(url.toString());
}

export function checkMcpServers({ sessionId = "", cwd = "", name = "", scope = "", sourcePath = "" } = {}) {
  const url = mcpScopeParams(new URL("/api/mcp/check", window.location.origin), { sessionId, cwd });
  if (name) url.searchParams.set("name", name);
  if (scope) url.searchParams.set("scope", scope);
  if (sourcePath) url.searchParams.set("sourcePath", sourcePath);
  return jsonFetch(url.toString());
}

export function getMcpCapabilities({ sessionId = "", cwd = "", name = "", scope = "", sourcePath = "" } = {}) {
  const url = mcpScopeParams(new URL("/api/mcp/capabilities", window.location.origin), { sessionId, cwd });
  url.searchParams.set("name", name);
  if (scope) url.searchParams.set("scope", scope);
  if (sourcePath) url.searchParams.set("sourcePath", sourcePath);
  return jsonFetch(url.toString());
}

export function addMcpServer({ sessionId = "", cwd = "", name, scope = "", fields = null, config = null } = {}) {
  const body = { sessionId, cwd, name };
  if (scope) body.scope = scope;
  if (config !== null) body.config = config;
  else body.fields = fields || {};
  return jsonFetch("/api/mcp/servers", { method: "POST", body: JSON.stringify(body) });
}

export function updateMcpServer({ sessionId = "", cwd = "", name, scope, fields = null, config = null } = {}) {
  const body = { sessionId, cwd, name, scope };
  if (config !== null) body.config = config;
  else body.fields = fields || {};
  return jsonFetch(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) });
}

export function removeMcpServer({ sessionId = "", cwd = "", name, scope = "", sourcePath = "" } = {}) {
  const url = mcpScopeParams(
    new URL(`/api/mcp/servers/${encodeURIComponent(name)}`, window.location.origin),
    { sessionId, cwd },
  );
  if (scope) url.searchParams.set("scope", scope);
  if (sourcePath) url.searchParams.set("sourcePath", sourcePath);
  return jsonFetch(`${url.pathname}${url.search}`, { method: "DELETE" });
}

export function setMcpEnabled({ sessionId = "", cwd = "", name, scope, disabled, sourcePath = "" } = {}) {
  return jsonFetch(`/api/mcp/servers/${encodeURIComponent(name)}/enabled`, {
    method: "PUT",
    body: JSON.stringify({ sessionId, cwd, scope, disabled, sourcePath }),
  });
}

export function setMcpApproval({ sessionId = "", cwd = "", name, decision } = {}) {
  return jsonFetch(`/api/mcp/servers/${encodeURIComponent(name)}/approval`, {
    method: "POST",
    body: JSON.stringify({ sessionId, cwd, decision }),
  });
}

export function resetMcpAuth({ sessionId = "", cwd = "", name, scope = "", sourcePath = "" } = {}) {
  return jsonFetch(`/api/mcp/servers/${encodeURIComponent(name)}/reset-auth`, {
    method: "POST",
    body: JSON.stringify({ sessionId, cwd, scope, sourcePath }),
  });
}

export function startMcpAuth({ sessionId = "", cwd = "", name, scope = "", sourcePath = "", reauthenticate = false } = {}) {
  return jsonFetch(`/api/mcp/servers/${encodeURIComponent(name)}/auth`, {
    method: "POST",
    body: JSON.stringify({ sessionId, cwd, scope, sourcePath, reauthenticate }),
  });
}

export function waitMcpAuth(flowId) {
  return jsonFetch(`/api/mcp/auth/${encodeURIComponent(flowId)}/wait`, { method: "POST" });
}

export function completeMcpAuth(flowId, callbackUrl) {
  return jsonFetch(`/api/mcp/auth/${encodeURIComponent(flowId)}/complete`, {
    method: "POST",
    body: JSON.stringify({ callbackUrl }),
  });
}

export function cancelMcpAuth(flowId) {
  return jsonFetch(`/api/mcp/auth/${encodeURIComponent(flowId)}/cancel`, { method: "POST" });
}

export function getTranscriptTurn(turnId, { sessionId = "" } = {}) {
  const url = new URL("/api/transcript/" + encodeURIComponent(turnId), window.location.origin);
  if (sessionId) {
    url.searchParams.set("sessionId", sessionId);
  }
  return jsonFetch(url.toString());
}

export function getPipelineState({ contextId = "", taskId = "", afterSequence = 0 } = {}) {
  const url = new URL("/api/pipeline/state", window.location.origin);
  if (contextId) {
    url.searchParams.set("contextId", contextId);
  }
  if (taskId) {
    url.searchParams.set("taskId", taskId);
  }
  if (afterSequence > 0) {
    url.searchParams.set("afterSequence", String(afterSequence));
  }
  return jsonFetch(url.toString());
}

export function selectPipelineCandidate({
  sessionId = "",
  candidateName = "",
  candidateIndex = null,
  parameterOverrides = {},
} = {}) {
  return jsonFetch("/api/pipeline/candidates/select", {
    method: "POST",
    body: JSON.stringify({
      sessionId,
      candidateName,
      candidateIndex,
      parameterOverrides,
    }),
  });
}

export function getCommands() {
  return jsonFetch("/api/commands");
}

export function getSuggestions({ sessionId = "", kind = "command", query = "" } = {}) {
  const url = new URL("/api/suggestions", window.location.origin);
  url.searchParams.set("kind", kind);
  url.searchParams.set("q", query);
  if (sessionId) {
    url.searchParams.set("sessionId", sessionId);
  }
  return jsonFetch(url.toString());
}

function base64FromBytes(bytes) {
  let binary = "";
  const chunkSize = 8192;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.slice(index, index + chunkSize));
  }
  return btoa(binary);
}

export async function uploadImage(sessionId, file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  return jsonFetch(sessionUrl(sessionId, "/images"), {
    method: "POST",
    body: JSON.stringify({
      name: file.name || "image",
      mediaType: file.type || "application/octet-stream",
      data: base64FromBytes(bytes),
    }),
  });
}

export function postMessage(sessionId, { text = "", imageIds = [], fileRefs = [] } = {}) {
  return jsonFetch(sessionUrl(sessionId, "/messages"), {
    method: "POST",
    body: JSON.stringify({ text, imageIds, fileRefs }),
  });
}

export function postQueuedInput(sessionId, text = "") {
  return jsonFetch(sessionUrl(sessionId, "/queued-inputs"), {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export function deleteQueuedInput(sessionId, index, expectedText) {
  return jsonFetch(sessionUrl(sessionId, `/queued-inputs/${index}`), {
    method: "DELETE",
    body: JSON.stringify({ expectedText }),
  });
}

export function editQueuedInput(sessionId, index, text, expectedText) {
  return jsonFetch(sessionUrl(sessionId, `/queued-inputs/${index}`), {
    method: "PATCH",
    body: JSON.stringify({ text, expectedText }),
  });
}

export function steerQueuedInput(sessionId, index, expectedText) {
  return jsonFetch(sessionUrl(sessionId, `/queued-inputs/${index}/steer`), {
    method: "POST",
    body: JSON.stringify({ expectedText }),
  });
}

export function postInterrupt(sessionId, { message = "", imageIds = [], fileRefs = [] } = {}) {
  return jsonFetch(sessionUrl(sessionId, "/interrupt"), {
    method: "POST",
    body: JSON.stringify({ message, imageIds, fileRefs }),
  });
}

export function postCommand(sessionId, command = "") {
  return jsonFetch(sessionUrl(sessionId, "/commands"), {
    method: "POST",
    body: JSON.stringify({ command }),
  });
}

export function savePermissionMode(sessionId, mode) {
  return jsonFetch(sessionUrl(sessionId, "/permission-mode"), {
    method: "PUT",
    body: JSON.stringify({ mode }),
  });
}

// 会话级 thinking 开关：enabled 为 true/false（null 表示清除覆盖、回落 provider 默认）。
export function saveThinkingEnabled(sessionId, enabled) {
  return jsonFetch(sessionUrl(sessionId, "/thinking-enabled"), {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

export function saveSessionModel(sessionId, { provider = "", model = "", effort = "" } = {}) {
  const payload = { provider, model };
  if (effort) {
    payload.effort = effort;
  }
  return jsonFetch(sessionUrl(sessionId, "/model"), {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

// 清掉会话级 provider/model 覆盖，让会话回落到全局(含合作方源)。
export function clearSessionModel(sessionId) {
  return jsonFetch(sessionUrl(sessionId, "/model"), {
    method: "DELETE",
  });
}

export function answerPermission(requestId, answer) {
  return jsonFetch(`/api/permissions/${encodeURIComponent(requestId)}/answer`, {
    method: "POST",
    body: JSON.stringify(answer || {}),
  });
}

export function answerQuestion(requestId, answer) {
  return jsonFetch(`/api/questions/${encodeURIComponent(requestId)}/answer`, {
    method: "POST",
    body: JSON.stringify(answer || {}),
  });
}

export function openEventStream(sessionId, afterSequence = 0, onEvent = () => {}) {
  const url = new URL(sessionUrl(sessionId, "/events"), window.location.origin);
  if (afterSequence > 0) {
    url.searchParams.set("afterSequence", String(afterSequence));
  }

  const source = new EventSource(url.toString());
  const handlers = new Map();

  const dispatchEvent = (event, { synthetic = false } = {}) => {
    Promise.resolve(onEvent(event, source)).catch((error) => {
      if (synthetic) {
        return;
      }
      dispatchEvent(
        {
          type: "error",
          sequence: 0,
          sessionId,
          payload: {
            message: error instanceof Error ? error.message : String(error),
            sourceEventType: event.type,
          },
        },
        { synthetic: true },
      );
    });
  };

  for (const eventType of WEB_EVENT_SOURCE_TYPES) {
    const handler = (messageEvent) => {
      if (!messageEvent.data) {
        return;
      }
      try {
        dispatchEvent(JSON.parse(messageEvent.data));
      } catch (error) {
        dispatchEvent(
          {
            type: "error",
            sequence: 0,
            sessionId,
            payload: {
              message: error instanceof Error ? error.message : String(error),
              sourceEventType: eventType,
            },
          },
          { synthetic: true },
        );
      }
    };
    source.addEventListener(eventType, handler);
    handlers.set(eventType, handler);
  }

  source.onopen = () => {
    dispatchEvent(
      {
        type: "stream.connected",
        sequence: 0,
        sessionId,
        payload: {},
      },
      { synthetic: true },
    );
  };

  source.onerror = () => {
    dispatchEvent(
      {
        type: "stream.disconnected",
        sequence: 0,
        sessionId,
        payload: { message: "Event stream disconnected" },
      },
      { synthetic: true },
    );
  };

  return {
    source,
    close() {
      for (const [eventType, handler] of handlers.entries()) {
        source.removeEventListener(eventType, handler);
      }
      source.close();
    },
  };
}
