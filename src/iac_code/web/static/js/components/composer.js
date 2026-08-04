import { t } from "../i18n.js?v=web-repl-ui-277";

const SUPPORTED_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const ENTER_HELP = t("Enter sends; Shift+Enter inserts a newline.");
const DRAFT_PLACEHOLDER = t("Describe your infrastructure needs");
const FOLLOWUP_PLACEHOLDER = t("Continue adding or adjusting requirements");

function text(value) {
  return value === undefined || value === null ? "" : String(value);
}

function numberValue(...values) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number)) {
      return number;
    }
  }
  return 0;
}

function contextUsageLimit(contextUsage = {}) {
  return numberValue(
    contextUsage.contextWindow,
    contextUsage.context_window,
    contextUsage.maxTokens,
    contextUsage.max_tokens,
    contextUsage.contextLimit,
    contextUsage.context_limit,
  );
}

function contextUsageTotal(contextUsage = {}) {
  return numberValue(contextUsage.totalTokens, contextUsage.total_tokens, contextUsage.usedTokens, contextUsage.used_tokens);
}

export function contextUsagePercent(contextUsage = {}) {
  const limit = contextUsageLimit(contextUsage);
  const total = contextUsageTotal(contextUsage);
  if (limit <= 0 || total < 0) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round((total / limit) * 100)));
}

function contextUsageSuffix(contextUsage = {}) {
  if (contextUsageLimit(contextUsage) <= 0) {
    return "";
  }
  return t("(Used {percent}%)", { percent: contextUsagePercent(contextUsage) });
}

function applyContextUsageIcon(element, contextUsage = {}) {
  if (!element?.style?.setProperty) {
    return;
  }
  const degrees = contextUsagePercent(contextUsage) * 3.6;
  element.style.setProperty("--context-usage-degrees", `${Number(degrees.toFixed(1))}deg`);
}

function makeContextUsageIcon(className, contextUsage = {}) {
  const icon = document.createElement("span");
  icon.className = ["context-usage-icon", className].filter(Boolean).join(" ");
  icon.setAttribute("aria-hidden", "true");
  applyContextUsageIcon(icon, contextUsage);
  return icon;
}

const COMMAND_DISPLAY_TOKEN_BY_NAME = {
  compact: t("Compact"),
  status: t("Status"),
  mcp: "MCP",
};

const COMMAND_DISPLAY_DESCRIPTION_BY_NAME = {
  compact: t("Compact this session's context"),
  status: t("Show current session status"),
  mcp: t("Show MCP server status"),
};

export function suggestionDisplayParts(suggestion = {}, options = {}) {
  const label = text(suggestion.label || suggestion.value || suggestion.name || "");
  const value = text(suggestion.value || suggestion.name || "");
  const match = label.match(/^([\/@$!]?[^\s]+)\s+(.+)$/);
  const commandName = suggestionLayoutKind(suggestion) === "command" ? commandNameFromSuggestion(suggestion) : "";
  const commandDisplayToken = COMMAND_DISPLAY_TOKEN_BY_NAME[commandName] || "";
  const commandDisplayDescription = COMMAND_DISPLAY_DESCRIPTION_BY_NAME[commandName] || "";
  const dynamicSuffix = commandName === "compact" ? contextUsageSuffix(options.contextUsage || {}) : "";
  if (match) {
    return { token: commandDisplayToken || match[1], description: `${commandDisplayDescription || match[2]}${dynamicSuffix}` };
  }
  return {
    token: commandDisplayToken || label || value,
    description: `${commandDisplayDescription || text(suggestion.description || "")}${dynamicSuffix}`,
  };
}

const COMMAND_ICON_CLASS_BY_NAME = {
  auth: "is-command-auth",
  clear: "is-command-clear",
  compact: "is-command-compact",
  debug: "is-command-debug",
  effort: "is-command-effort",
  exit: "is-command-exit",
  help: "is-command-help",
  login: "is-command-auth",
  mcp: "is-command-mcp",
  memory: "is-command-memory",
  "memory-folder": "is-command-memory",
  model: "is-command-model",
  prompt: "is-command-prompt",
  q: "is-command-exit",
  quit: "is-command-exit",
  rename: "is-command-rename",
  resume: "is-command-resume",
  skills: "is-command-skills",
  status: "is-command-status",
};

const HIDDEN_COMPOSER_COMMAND_NAMES = new Set([
  "auth",
  "clear",
  "debug",
  "effort",
  "exit",
  "help",
  "login",
  "memory",
  "memory-folder",
  "model",
  "q",
  "quit",
  "rename",
  "resume",
  "skills",
]);

const SESSION_ONLY_COMMAND_NAMES = new Set(["clear", "compact", "status", "mcp"]);

// 流水线(非普通对话)会话里无法主动执行的命令:如 /compact 压缩上下文只对 normal chat 有意义,
// 从「/」补全菜单里移除。交接为普通对话后 session.mode 翻转为 "normal",届时不再命中此过滤。
const PIPELINE_HIDDEN_COMMAND_NAMES = new Set(["compact"]);

// 补全菜单里回车/Tab 直接执行(而非塞回文本)的斜杠命令。
const IMMEDIATE_COMMAND_NAMES = new Set(["status", "compact", "mcp"]);

function safeSuggestionClassPart(value) {
  return text(value).toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "item";
}

export function commandNameFromSuggestion(suggestion = {}) {
  const raw = text(suggestion.value || suggestion.name || suggestion.label || "").trim();
  const withoutPrefix = raw.replace(/^[\/@$!]+/, "");
  return safeSuggestionClassPart(withoutPrefix.split(/\s+/)[0] || "");
}

export function suggestionIconClasses(suggestion = {}) {
  const kind = suggestionLayoutKind(suggestion);
  const classes = ["suggestion-icon", `is-${kind}`];
  if (kind === "command") {
    const commandName = commandNameFromSuggestion(suggestion);
    classes.push(COMMAND_ICON_CLASS_BY_NAME[commandName] || "is-command-generic");
  }
  return classes.join(" ");
}

export function suggestionLayoutKind(suggestion = {}) {
  const kind = safeSuggestionClassPart(suggestion.kind || "item");
  return kind === "skill" || text(suggestion.origin) ? "skill" : kind;
}

export function skillScopeLabel(suggestion = {}) {
  return (
    {
      bundled: t("System"),
      project: t("Project"),
      user: t("Personal"),
    }[text(suggestion.origin)] || ""
  );
}

function skillDisplayName(suggestion = {}) {
  const parts = suggestionDisplayParts(suggestion);
  const raw = text(parts.token || suggestion.name || suggestion.value || suggestion.label).trim();
  return raw.replace(/^[$\/]+/, "").split(/\s+/)[0] || "skill";
}

function skillCommandValue(suggestion = {}) {
  const raw = text(suggestion.value || suggestion.name || suggestion.label).trim();
  if (raw.startsWith("$")) {
    return raw.split(/\s+/)[0];
  }
  return `$${skillDisplayName(suggestion)}`;
}

export function suggestionMenuSections(rawSuggestions = []) {
  const commandSuggestions = [];
  const skillSuggestions = [];
  const otherSuggestions = [];
  for (const suggestion of Array.isArray(rawSuggestions) ? rawSuggestions : []) {
    const kind = suggestionLayoutKind(suggestion);
    if (kind === "command") {
      commandSuggestions.push(suggestion);
    } else if (kind === "skill") {
      skillSuggestions.push(suggestion);
    } else {
      otherSuggestions.push(suggestion);
    }
  }

  return [
    commandSuggestions.length ? { kind: "command", label: "", suggestions: commandSuggestions } : null,
    skillSuggestions.length ? { kind: "skill", label: t("Skills"), suggestions: skillSuggestions } : null,
    ...otherSuggestions.map((suggestion) => ({
      kind: suggestionLayoutKind(suggestion),
      label: "",
      suggestions: [suggestion],
    })),
  ].filter(Boolean);
}

export function orderedComposerSuggestions(rawSuggestions = []) {
  return suggestionMenuSections(rawSuggestions).flatMap((section) => section.suggestions);
}

export function visibleComposerSuggestions(rawSuggestions = [], options = {}) {
  const draftSessionActive = Boolean(options.draftSessionActive);
  const pipelineMode = Boolean(options.pipelineMode);
  return (Array.isArray(rawSuggestions) ? rawSuggestions : []).filter((suggestion) => {
    if (!suggestion) {
      return false;
    }
    const kind = safeSuggestionClassPart(suggestion.kind || "item");
    if (kind !== "command") {
      return true;
    }
    const commandName = commandNameFromSuggestion(suggestion);
    if (HIDDEN_COMPOSER_COMMAND_NAMES.has(commandName)) {
      return false;
    }
    if (pipelineMode && PIPELINE_HIDDEN_COMMAND_NAMES.has(commandName)) {
      return false;
    }
    return !(draftSessionActive && SESSION_ONLY_COMMAND_NAMES.has(commandName));
  });
}

function makeAttachment(file) {
  const isImage = text(file?.type).startsWith("image/");
  return {
    id: `attachment-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    name: text(file?.name || "attachment"),
    mediaType: text(file?.type),
    isImage,
    file,
    status: "pending",
    imageId: "",
    fileRef: "",
    previewUrl: "",
    localPreviewUrl: "",
  };
}

function objectUrlApi() {
  const urlApi = globalThis.URL;
  if (!urlApi || typeof urlApi.createObjectURL !== "function") {
    return null;
  }
  return urlApi;
}

function assignLocalPreviewUrl(attachment) {
  if (!attachment?.isImage || !attachment.file || attachment.previewUrl) {
    return;
  }
  const urlApi = objectUrlApi();
  if (!urlApi) {
    return;
  }
  try {
    const previewUrl = text(urlApi.createObjectURL(attachment.file));
    attachment.previewUrl = previewUrl;
    attachment.localPreviewUrl = previewUrl;
  } catch {
    attachment.previewUrl = "";
    attachment.localPreviewUrl = "";
  }
}

function revokeAttachmentPreview(attachment) {
  const localPreviewUrl = text(attachment?.localPreviewUrl);
  if (!localPreviewUrl) {
    return;
  }
  const urlApi = objectUrlApi();
  if (urlApi && typeof urlApi.revokeObjectURL === "function") {
    try {
      urlApi.revokeObjectURL(localPreviewUrl);
    } catch {
      // Best effort cleanup only.
    }
  }
  if (attachment.previewUrl === localPreviewUrl) {
    attachment.previewUrl = "";
  }
  attachment.localPreviewUrl = "";
}

function makeFileReferenceAttachment(fileRef) {
  return {
    id: `file-ref-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    name: fileRef,
    mediaType: "text/plain",
    isImage: false,
    file: null,
    status: "ready",
    imageId: "",
    fileRef,
  };
}

function makeImageReferenceAttachment(imageId, sessionId, tokenMode = false) {
  return {
    id: `image-ref-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    name: imageId,
    mediaType: "image/*",
    isImage: true,
    file: null,
    status: "ready",
    imageId,
    fileRef: "",
    previewUrl: sessionId && !tokenMode
      ? `/api/images/${encodeURIComponent(imageId)}?sessionId=${encodeURIComponent(sessionId)}`
      : "",
    localPreviewUrl: "",
  };
}

function isRecoverableError(error) {
  return !error || !Number.isInteger(error.status) || error.status < 500;
}

export function isMidTurnCommandLike(value) {
  return text(value).trimStart().startsWith("/") || text(value).trimStart().startsWith("$") || text(value).trimStart().startsWith("!");
}

// 直接键入并提交(绕过「/」补全菜单)的命令也须与菜单同门:菜单会依运行时状态隐藏两类命令——
// 流水线模式下的 PIPELINE_HIDDEN_COMMAND_NAMES,以及新会话草稿阶段的 SESSION_ONLY_COMMAND_NAMES
// (/compact 等只对已存在的普通会话有意义)。命中时返回 { command, reason },否则返回 null,供
// submit() 在建会话、下发命令前拦截并提示,与 visibleComposerSuggestions 的过滤保持一致。
export function blockedComposerCommandName(value, options = {}) {
  if (!isMidTurnCommandLike(value)) {
    return null;
  }
  const commandName = commandNameFromSuggestion({ value });
  if (!commandName) {
    return null;
  }
  if (options.pipelineMode && PIPELINE_HIDDEN_COMMAND_NAMES.has(commandName)) {
    return { command: commandName, reason: "pipeline" };
  }
  if (options.draftSessionActive && SESSION_ONLY_COMMAND_NAMES.has(commandName)) {
    return { command: commandName, reason: "draft" };
  }
  return null;
}

export function shouldAcceptSuggestionOnEnter(value, suggestion) {
  if (!suggestion) {
    return false;
  }
  const suggestionValue = text(suggestion.value).trim();
  if (!suggestionValue) {
    return false;
  }
  const draft = text(value).trim();
  const isCommandSuggestion =
    suggestion.kind === "command" ||
    suggestion.kind === "skill" ||
    suggestionValue.startsWith("/") ||
    suggestionValue.startsWith("$") ||
    suggestionValue.startsWith("!");
  return !(isCommandSuggestion && draft === suggestionValue);
}

function providerItems(payload) {
  return Array.isArray(payload?.providers) ? payload.providers : [];
}

function hasSelectableModel(provider) {
  return Array.isArray(provider?.models) && provider.models.length > 0;
}

// 合作方源(第三方登录托管):key 形如 "partner:qwenpaw",kind==="partner",无枚举模型。
function isPartnerProvider(provider) {
  return provider?.kind === "partner" || text(provider?.key).startsWith("partner:");
}

// 后端把当前生效的合作方源标记为 current(见 settings._partner_payloads);
// 合作方源作为全局 llm_source 生效时,payload.active.provider 为空,只能靠此标记识别。
function currentPartnerProvider(payload) {
  return providerItems(payload).find((provider) => isPartnerProvider(provider) && provider?.current) || null;
}

// 会话切换列表 = 亮绿点(usable)且有可选模型的 provider ∪ 已保存配置 ∪ 当前 active ∪ 合作方源。
// 与设置里的绿点(usable)对齐:凡是绿点且能选到模型的都可在会话里切换(如仅填了
// key、未保存配置的 provider)。合作方源无枚举模型,选中即全局锁定、无需选 model,
// 因此也纳入切换列表(点击直接激活)。兼容模式等其他无模型 provider 仍按 configured/active 判定,
// 避免出现点了没反应的按钮。
function switchableProviderItems(payload, activeProviderKey = "") {
  const items = providerItems(payload);
  const switchable = items.filter(
    (provider) =>
      provider?.key === activeProviderKey ||
      provider?.configured ||
      isPartnerProvider(provider) ||
      (provider?.usable && hasSelectableModel(provider)),
  );
  return switchable.length > 0 ? switchable : items.filter((provider) => provider?.key === activeProviderKey);
}

function modelItems(provider) {
  return Array.isArray(provider?.models) ? provider.models : [];
}

function effortItems(model) {
  return Array.isArray(model?.efforts) ? model.efforts : [];
}

function activeProviderSummary(active = {}) {
  return {
    provider: text(active.provider),
    model: text(active.model),
    effort: text(active.effort),
    apiBase: text(active.apiBase),
    hasApiKey: Boolean(active.hasApiKey),
  };
}

function findProvider(payload, providerKey) {
  return providerItems(payload).find((provider) => provider.key === providerKey) || null;
}

function findModel(provider, modelId) {
  return modelItems(provider).find((model) => model.id === modelId) || null;
}

function firstModel(provider) {
  const models = modelItems(provider);
  return models.find((model) => model.default) || models.find((model) => model.id === provider?.defaultModel) || models[0] || null;
}

function modelLabel(model, fallback = "") {
  return text(model?.name || model?.id || fallback);
}

function providerLabel(provider, fallback = "") {
  return text(provider?.displayName || provider?.name || provider?.key || fallback);
}

function effortLabel(effort) {
  return (
    {
      // DashScope glm-5.2 / Gemini-3 等模型的最低两档;缺映射时会裸露小写英文。
      none: t("None"),
      minimal: t("Minimal"),
      low: t("Low"),
      medium: t("Medium"),
      high: t("High"),
      xhigh: t("Very high"),
      max: t("Max"),
      auto: t("Auto"),
    }[text(effort)] || text(effort)
  );
}

const PERMISSION_MODE_OPTIONS = [
  {
    id: "default",
    label: t("Ask for approval"),
    menuLabel: t("Ask for approval"),
    description: t("Ask before writes, external files, and restricted tool calls"),
    icon: "hand",
  },
  {
    id: "accept_edits",
    label: t("Approve for me"),
    menuLabel: t("Approve for me"),
    description: t("Automatically accept common file edits; still ask for other risky actions"),
    icon: "terminal-shield",
  },
  {
    id: "bypass_permissions",
    label: t("Full access"),
    menuLabel: t("Full access permissions"),
    description: t("Skip permission checks; only ask for safety protections"),
    icon: "alert-shield",
  },
  {
    id: "dont_ask",
    label: t("Don't ask"),
    menuLabel: t("Don't ask"),
    description: t("Automatically deny actions that need approval"),
    icon: "shield-slash",
  },
];

function permissionOption(mode) {
  return PERMISSION_MODE_OPTIONS.find((option) => option.id === mode) || PERMISSION_MODE_OPTIONS[0];
}

function permissionModeClass(mode) {
  return `is-${permissionOption(mode).id.replace(/_/g, "-")}`;
}

function makeSvgPath(d) {
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  return path;
}

function makePermissionIcon(kind, className = "permission-mode-icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  const paths = {
    hand: [
      "M6.4 9.3V4.8a1.25 1.25 0 0 1 2.5 0v4",
      "M8.9 8V3.9a1.25 1.25 0 0 1 2.5 0V8",
      "M11.4 8.2V5.1a1.2 1.2 0 0 1 2.4 0v4.4",
      "M13.8 9.5V7.2a1.15 1.15 0 0 1 2.3 0v3.4c0 3.4-2.1 5.7-5.3 5.7h-.7c-1.7 0-2.7-.6-3.7-1.8l-2.2-2.6a1.18 1.18 0 0 1 1.8-1.5l1.1 1.2",
    ],
    "terminal-shield": [
      "M10 3.1l5.2 2.1v4.2c0 3.2-2.1 5.8-5.2 6.8-3.1-1-5.2-3.6-5.2-6.8V5.2L10 3.1z",
      "M7.2 8.5 8.8 10l-1.6 1.5",
      "M10.4 11.5h2.4",
    ],
    "alert-shield": [
      "M10 3.1l5.2 2.1v4.2c0 3.2-2.1 5.8-5.2 6.8-3.1-1-5.2-3.6-5.2-6.8V5.2L10 3.1z",
      "M10 7.4v3.6",
      "M10 13h.01",
    ],
    "shield-slash": [
      "M10 2.7l5.2 1.9v4.2c0 3.4-2.1 6.4-5.2 7.6-3.1-1.2-5.2-4.2-5.2-7.6V4.6L10 2.7z",
      "M5 15L15 5",
    ],
  }[kind] || ["M10 2.7l5.2 1.9v4.2c0 3.4-2.1 6.4-5.2 7.6-3.1-1.2-5.2-4.2-5.2-7.6V4.6L10 2.7z"];
  svg.append(...paths.map(makeSvgPath));
  return svg;
}

function effectiveEffort(model, preferred = "") {
  const efforts = effortItems(model);
  if (preferred && efforts.includes(preferred)) {
    return preferred;
  }
  if (model?.defaultEffort && efforts.includes(model.defaultEffort)) {
    return model.defaultEffort;
  }
  return efforts[0] || "";
}

export function createComposerController(elements = {}, api = {}, options = {}) {
  const form = elements.form;
  const textarea = elements.textarea;
  const sendButton = elements.sendButton;
  const fileInput = elements.fileInput;
  const chips = elements.attachmentChips;
  const skillRow = elements.skillRow;
  const suggestionsList = elements.suggestions;
  const errorTarget = elements.errorTarget;
  const permissionControl = elements.permissionControl;
  const permissionMenu = elements.permissionMenu;
  const thinkingToggle = elements.thinkingToggle;
  const modelControl = elements.modelControl;
  const modelMenu = elements.modelMenu;

  let sessionId = "";
  let sessionRevision = 0;
  let restoreRevision = 0;
  let turnActive = false;
  // 压缩/自动压缩进行中:与 turnActive 一样让提交进入排队(而非新起 turn),
  // 但不把发送按钮变成「停止」——压缩期间没有可中断的 turn。压缩完成后由后端排空队列。
  let compacting = false;
  let readOnly = false;
  let attachments = [];
  let selectedSkill = null;
  let suggestions = [];
  let visibleSuggestions = [];
  let activeSuggestionIndex = -1;
  let suggestionRequestVersion = 0;
  let localDraftDirty = false;
  // 输入历史(方向键召回):本对话优先,新会话回退全局。
  // historyEntries 为进入导航时选定的来源(oldest→newest);historyPos=null 表示未导航;
  // historyStash 存进入导航前的缓冲,↓ 越过最新时用于还原。
  const INPUT_HISTORY_KEY = "iac-code:input-history:global";
  const INPUT_HISTORY_LIMIT = 200;
  let conversationHistory = [];
  let globalHistory = loadGlobalHistory();
  let historyEntries = [];
  let historyPos = null;
  let historyStash = "";
  let providersPayload = null;
  let activeProvider = activeProviderSummary();
  // 当前会话选定的 provider/模型（会话级）。为空时回退到全局 active。
  let sessionSelection = null;
  let permissionMode = "default";
  // 会话级 thinking 开关的本地镜像；true=开。null/false 均视作关。
  let thinkingEnabled = false;
  // 草稿会话(尚无 override)时按钮跟随所选模型的思考默认;provider/模型切换或列表异步加载后需重算。
  let thinkingFollowsDefault = false;
  let permissionMenuOpen = false;
  let modelMenuOpen = false;
  let activeSubmenu = "";
  let contextUsage = {};
  let contextUsageWindows = [];
  // 无活跃步骤窗口时回退单主环的标签。空串=普通会话(t("Normal chat"))；流水线会话由 app.js
  // 算出「当前等待步骤名 / 流水线名」传入,避免流水线会话在选择门/步骤间隙误显示「普通会话」。
  let contextFallbackLabel = "";

  // 排队消息为纯文本:回合运行中尝试携带附件时给出提示,回合结束或移除附件后需自动清除,避免残留。
  const QUEUED_ATTACHMENT_ERROR_CODE = "queued_attachment_not_supported";
  const QUEUED_ATTACHMENT_ERROR_TEXT = t("Attachments can be sent after the current turn finishes.");

  function setError(error, code = "") {
    if (!errorTarget) {
      return;
    }
    errorTarget.textContent = text(error);
    if (errorTarget.dataset) {
      errorTarget.dataset.code = code;
    }
  }

  function clearError() {
    setError("");
  }

  // 仅清除排队附件提示,不影响其它错误(如命令失败)。
  function clearQueuedAttachmentError() {
    if (errorTarget?.dataset.code === QUEUED_ATTACHMENT_ERROR_CODE) {
      clearError();
    }
  }

  function hasSubmittableContent() {
    return Boolean(text(textarea?.value).trim()) || attachments.length > 0 || Boolean(selectedSkill);
  }

  function canSubmitToSession() {
    if (readOnly) {
      return false;
    }
    return Boolean(sessionId || options.createSessionForSubmit);
  }

  function syncSendButtonState() {
    if (!sendButton) {
      return;
    }
    // 运行中：发送按钮变身为"停止"按钮，需保持可点击以便中断当前轮次。
    if (turnActive) {
      sendButton.disabled = false;
      return;
    }
    if (readOnly) {
      sendButton.disabled = true;
      return;
    }
    sendButton.disabled = !canSubmitToSession() || !hasSubmittableContent();
  }

  function syncPlaceholder() {
    if (!textarea) {
      return;
    }
    if (readOnly) {
      textarea.placeholder = t("This session was created outside the web entry point and is read-only.");
      return;
    }
    textarea.placeholder = sessionId ? FOLLOWUP_PLACEHOLDER : DRAFT_PLACEHOLDER;
  }

  function syncComposerAttachmentState() {
    form?.classList?.toggle("has-attachments", attachments.length > 0);
    form?.classList?.toggle("has-skill", Boolean(selectedSkill));
  }

  function renderSkillChip() {
    syncComposerAttachmentState();
    syncSendButtonState();
    if (!skillRow) {
      return;
    }
    skillRow.replaceChildren();
    skillRow.hidden = !selectedSkill;
    if (!selectedSkill) {
      return;
    }
    const chip = document.createElement("span");
    chip.className = "composer-skill-chip";
    const icon = document.createElement("span");
    icon.className = "suggestion-icon is-skill composer-skill-chip-icon";
    icon.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "composer-skill-chip-label";
    label.textContent = selectedSkill.name;
    chip.append(icon, label);
    skillRow.append(chip);
  }

  function clearSelectedSkill() {
    selectedSkill = null;
    renderSkillChip();
  }

  // 这些斜杠命令在补全菜单中回车/Tab 即直接执行(经 postCommand),
  // 而不是把「/status」「/compact」文本塞回输入框再让用户二次回车。
  function isImmediateCommandSuggestion(suggestion = {}) {
    return suggestionLayoutKind(suggestion) === "command" && IMMEDIATE_COMMAND_NAMES.has(commandNameFromSuggestion(suggestion));
  }

  function renderAttachmentChips() {
    syncComposerAttachmentState();
    syncSendButtonState();
    if (!chips) {
      return;
    }
    chips.replaceChildren();
    for (const attachment of attachments) {
      const statusClass = attachment.status ? ` is-${attachment.status}` : "";
      const prefix = attachment.fileRef ? "@ " : "";
      const label =
        attachment.status === "ready" ? `${prefix}${attachment.name}` : `${attachment.name} · ${attachment.status}`;
      const removeAttachment = () => {
        revokeAttachmentPreview(attachment);
        attachments = attachments.filter((item) => item.id !== attachment.id);
        renderAttachmentChips();
        // 移除附件后,排队附件提示不再适用,清除以免残留。
        clearQueuedAttachmentError();
      };
      if (attachment.isImage && attachment.previewUrl) {
        // 图片附件:整块不再是删除按钮。点缩略图预览大图,只有右上角 × 才删除。
        const chip = document.createElement("div");
        chip.className = `attachment-chip attachment-chip-image${statusClass}`;
        chip.title = label;
        const preview = document.createElement("button");
        preview.type = "button";
        preview.className = "attachment-chip-preview-btn";
        preview.setAttribute("aria-label", t("Preview image"));
        const image = document.createElement("img");
        image.className = "attachment-chip-preview";
        image.src = attachment.previewUrl;
        image.alt = attachment.name;
        image.draggable = false;
        preview.append(image);
        preview.addEventListener("click", () => {
          options.onPreviewImage?.({ src: attachment.previewUrl, alt: attachment.name });
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "attachment-chip-remove";
        remove.setAttribute("aria-label", t("Remove {name}", { name: label }));
        remove.textContent = "×";
        remove.addEventListener("click", removeAttachment);
        chip.append(preview, remove);
        chips.append(chip);
      } else {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "attachment-chip";
        chip.title = label;
        chip.setAttribute("aria-label", t("Remove {name}", { name: label }));
        chip.textContent = label;
        chip.addEventListener("click", removeAttachment);
        chips.append(chip);
      }
    }
  }

  function selectedProvider() {
    return findProvider(providersPayload, activeProvider.provider);
  }

  function selectedModel() {
    return findModel(selectedProvider(), activeProvider.model);
  }

  function selectedEffort() {
    return effectiveEffort(selectedModel(), activeProvider.effort);
  }

  // 后端 resolve_thinking_active 算好的「该模型无 override 时是否思考」;未知模型 → 关。
  function selectedModelThinkingDefault() {
    return selectedModel()?.thinkingDefault === true;
  }

  function closePermissionMenu() {
    if (!permissionMenuOpen) {
      return;
    }
    permissionMenuOpen = false;
    renderPermissionControls();
  }

  function elementContains(element, target) {
    if (!element || !target) {
      return false;
    }
    return element === target || Boolean(element.contains?.(target));
  }

  function isPermissionMenuEvent(event) {
    const target = event?.target;
    if (!target) {
      return false;
    }
    return elementContains(permissionControl, target) || elementContains(permissionMenu, target);
  }

  function renderPermissionControl() {
    if (!permissionControl) {
      return;
    }
    const option = permissionOption(permissionMode);
    permissionControl.className = `permission-mode-control ${permissionModeClass(option.id)}`;
    permissionControl.disabled = !api.savePermissionMode;
    permissionControl.setAttribute("aria-expanded", permissionMenuOpen ? "true" : "false");
    permissionControl.replaceChildren();
    permissionControl.append(makePermissionIcon(option.icon, "permission-mode-icon permission-mode-control-icon"));
    const label = document.createElement("span");
    label.className = "permission-mode-control-label";
    label.textContent = option.label;
    const chevron = document.createElement("span");
    chevron.className = "permission-mode-control-chevron";
    chevron.setAttribute("aria-hidden", "true");
    permissionControl.append(label, chevron);
  }

  function makePermissionMenuHeader() {
    const header = document.createElement("div");
    header.className = "permission-mode-menu-header";
    const title = document.createElement("span");
    title.className = "permission-mode-menu-title";
    title.textContent = t("How should IaC Code actions be approved?");
    header.append(title);
    return header;
  }

  function makePermissionMenuItem(option) {
    const active = option.id === permissionMode;
    const button = document.createElement("button");
    button.type = "button";
    button.className = active ? "permission-mode-menu-item is-active" : "permission-mode-menu-item";
    button.setAttribute("data-permission-mode", option.id);
    button.setAttribute("role", "menuitemradio");
    button.setAttribute("aria-checked", active ? "true" : "false");
    button.append(makePermissionIcon(option.icon));

    const copy = document.createElement("span");
    copy.className = "permission-mode-menu-copy";
    const label = document.createElement("span");
    label.className = "permission-mode-menu-label";
    label.textContent = option.menuLabel;
    const description = document.createElement("span");
    description.className = "permission-mode-menu-description";
    description.textContent = option.description;
    copy.append(label, description);

    const check = document.createElement("span");
    check.className = "permission-mode-menu-check";
    check.setAttribute("aria-hidden", "true");
    check.textContent = active ? "✓" : "";
    button.append(copy, check);
    button.addEventListener("click", () => savePermissionMode(option.id));
    return button;
  }

  function renderPermissionMenu() {
    if (!permissionMenu) {
      return;
    }
    permissionMenu.hidden = !permissionMenuOpen;
    permissionMenu.replaceChildren();
    if (!permissionMenuOpen) {
      return;
    }
    permissionMenu.append(makePermissionMenuHeader());
    for (const option of PERMISSION_MODE_OPTIONS) {
      permissionMenu.append(makePermissionMenuItem(option));
    }
  }

  function renderPermissionControls() {
    renderPermissionControl();
    renderPermissionMenu();
  }

  async function savePermissionMode(nextMode) {
    const normalizedMode = permissionOption(nextMode).id;
    if (!api.savePermissionMode || !sessionId) {
      permissionMode = normalizedMode;
      // 草稿会话（尚未创建）：把选择回传给上层，随会话创建一起持久化，避免提交后回退。
      options.onPermissionModeChange?.(normalizedMode);
      closePermissionMenu();
      renderPermissionControls();
      return;
    }
    try {
      const saved = await api.savePermissionMode(sessionId, normalizedMode);
      permissionMode = permissionOption(saved?.permissionMode || normalizedMode).id;
      permissionMenuOpen = false;
      clearError();
      renderPermissionControls();
    } catch (error) {
      setError(error.message || t("Could not save permission mode"));
    }
  }

  function renderThinkingToggle() {
    if (!thinkingToggle) {
      return;
    }
    const on = thinkingEnabled === true;
    thinkingToggle.className = on ? "thinking-toggle is-on" : "thinking-toggle";
    thinkingToggle.setAttribute("aria-pressed", on ? "true" : "false");
    thinkingToggle.disabled = !api.saveThinkingEnabled && !options.onThinkingEnabledChange;
  }

  async function saveThinkingEnabled(next) {
    const normalized = next === true;
    if (!api.saveThinkingEnabled || !sessionId) {
      // 用户显式切换即成为草稿的 override,不再跟随模型默认(否则 provider 重算会覆盖用户选择)。
      thinkingFollowsDefault = false;
      thinkingEnabled = normalized;
      // 草稿会话（尚未创建）：把选择回传给上层，随会话创建一起持久化，避免提交后回退。
      options.onThinkingEnabledChange?.(normalized);
      renderThinkingToggle();
      return;
    }
    try {
      const saved = await api.saveThinkingEnabled(sessionId, normalized);
      // 镜像本回合真正生效的思考态；显式保存后 effective 即等于 override。
      thinkingEnabled = saved?.thinkingEffective === true;
      clearError();
      renderThinkingToggle();
    } catch (error) {
      setError(error.message || t("Could not save thinking toggle"));
    }
  }

  function setComposerMenuButtonState(button, active) {
    button.className = active ? "composer-model-menu-item is-active" : "composer-model-menu-item";
    button.setAttribute("aria-checked", active ? "true" : "false");
  }

  function makeComposerMenuButton({ setting, label, detail = "", active = false, disabled = false, onClick }) {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-composer-setting", setting);
    button.disabled = disabled;
    button.setAttribute("role", "menuitemradio");
    setComposerMenuButtonState(button, active);

    const labelNode = document.createElement("span");
    labelNode.className = "composer-model-menu-label";
    labelNode.textContent = label;
    button.append(labelNode);
    if (detail) {
      const detailNode = document.createElement("span");
      detailNode.className = "composer-model-menu-detail";
      detailNode.textContent = detail;
      button.append(detailNode);
    }
    button.addEventListener("click", () => onClick?.());
    return button;
  }

  function makeComposerDisabledMenuItem(label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "composer-model-menu-item is-disabled";
    button.disabled = true;
    button.setAttribute("role", "menuitem");

    const labelNode = document.createElement("span");
    labelNode.className = "composer-model-menu-label";
    labelNode.textContent = label;
    button.append(labelNode);
    return button;
  }

  function makeComposerMenuHeading(title) {
    const heading = document.createElement("div");
    heading.className = "composer-model-menu-heading";
    heading.textContent = title;
    return heading;
  }

  function makeComposerMenuDivider() {
    const divider = document.createElement("div");
    divider.className = "composer-model-menu-divider";
    return divider;
  }

  function makeComposerSubmenuTrigger({ kind, label, active = false }) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = active
      ? "composer-model-menu-item composer-model-submenu-trigger is-active"
      : "composer-model-menu-item composer-model-submenu-trigger";
    button.setAttribute("data-composer-submenu-trigger", kind);
    button.setAttribute("role", "menuitem");

    const labelNode = document.createElement("span");
    labelNode.className = "composer-model-menu-label";
    labelNode.textContent = label;
    const chevron = document.createElement("span");
    chevron.className = "composer-model-menu-chevron";
    chevron.setAttribute("aria-hidden", "true");
    button.append(labelNode, chevron);
    button.addEventListener("click", () => {
      activeSubmenu = activeSubmenu === kind ? "" : kind;
      renderProviderControls();
    });
    button.addEventListener("mouseenter", () => {
      activeSubmenu = kind;
      renderProviderControls();
    });
    return button;
  }

  function makeComposerSubmenu(kind, buttons = []) {
    const submenu = document.createElement("section");
    submenu.className = "composer-model-submenu";
    submenu.setAttribute("data-composer-submenu", kind);
    submenu.hidden = activeSubmenu !== kind;
    submenu.append(makeComposerMenuHeading(kind === "model" ? t("Model") : t("Provider")), ...buttons);
    return submenu;
  }

  function closeModelMenu() {
    if (!modelMenuOpen && !activeSubmenu) {
      return;
    }
    modelMenuOpen = false;
    activeSubmenu = "";
    renderProviderControls();
  }

  function isModelMenuEvent(event) {
    const target = event?.target;
    if (!target) {
      return false;
    }
    return elementContains(modelControl, target) || elementContains(modelMenu, target);
  }

  function isSuggestionEvent(event) {
    const target = event?.target;
    if (!target) {
      return false;
    }
    return elementContains(textarea, target) || elementContains(suggestionsList, target);
  }

  function renderModelControl() {
    if (!modelControl) {
      return;
    }
    const provider = selectedProvider();
    // 合作方源无模型/推理强度,显示其名称即可(如 QwenPaw)。
    const model = isPartnerProvider(provider) ? null : selectedModel() || findModel(provider, activeProvider.model);
    const modelText = isPartnerProvider(provider) ? providerLabel(provider) : modelLabel(model, activeProvider.model);
    const effortText = isPartnerProvider(provider) ? "" : effortLabel(selectedEffort());
    const controlText = modelText ? [modelText, effortText].filter(Boolean).join(" ") : t("Configure model");
    const label = document.createElement("span");
    label.className = "composer-model-control-label";
    label.textContent = controlText;
    // 悬浮提示追加百分比进度（问题 #5）。仅在有有效上限（分母>0）时拼接，避免显示「· 0%」误导。
    const withPercent = (label, usage) => {
      const percent = contextUsagePercent(usage);
      const suffix = contextUsageLimit(usage) > 0 ? ` · ${percent}%` : "";
      return `${label}${suffix}`;
    };
    const usageIcons = contextUsageWindows.length
      ? contextUsageWindows.map((win) => {
          const icon = makeContextUsageIcon("composer-model-usage-icon", win.contextUsage);
          const base = win.candidateName ? `${win.candidateName} · ${win.title}` : win.title;
          const tooltip = withPercent(base, win.contextUsage);
          if (tooltip) {
            icon.title = tooltip;
          }
          return icon;
        })
      : [makeContextUsageIcon("composer-model-usage-icon", contextUsage)];
    if (!contextUsageWindows.length) {
      usageIcons[0].title = withPercent(contextFallbackLabel || t("Normal chat"), contextUsage);
    }
    modelControl.replaceChildren(...usageIcons, label);
    modelControl.setAttribute("aria-label", controlText);
    modelControl.disabled = !api.getProviders;
    modelControl.setAttribute("aria-expanded", modelMenuOpen ? "true" : "false");
  }

  function renderModelMenu() {
    if (!modelMenu) {
      return;
    }
    modelMenu.hidden = !modelMenuOpen;
    modelMenu.replaceChildren();
    if (!modelMenuOpen) {
      return;
    }
    if (!providersPayload) {
      const empty = document.createElement("div");
      empty.className = "composer-model-menu-empty";
      empty.textContent = api.getProviders ? t("Loading model configuration...") : t("Model configuration unavailable");
      modelMenu.append(empty);
      return;
    }

    const provider = selectedProvider();
    const currentModel = selectedModel();
    const effortButtons = effortItems(currentModel).map((effort) =>
      makeComposerMenuButton({
        setting: `effort:${effort}`,
        label: effortLabel(effort),
        active: effort === selectedEffort(),
        onClick: () => saveComposerProviderSelection({ effort }),
      }),
    );

    const modelButtons = modelItems(provider).map((model) =>
      makeComposerMenuButton({
        setting: `model:${model.id}`,
        label: modelLabel(model),
        active: model.id === activeProvider.model,
        onClick: () => saveComposerProviderSelection({ model: model.id }),
      }),
    );

    const providerButtons = switchableProviderItems(providersPayload, activeProvider.provider).map((item) =>
      makeComposerMenuButton({
        setting: `provider:${item.key}`,
        label: providerLabel(item),
        active: item.key === activeProvider.provider,
        onClick: () => saveComposerProviderSelection({ provider: item.key }),
      }),
    );

    // 合作方源已锁定:无模型/推理可选,只展示 provider 切换列表,避免空的模型/推理项。
    if (isPartnerProvider(provider)) {
      modelMenu.append(
        makeComposerSubmenuTrigger({
          kind: "provider",
          label: providerLabel(provider, activeProvider.provider) || "Provider",
          active: activeSubmenu === "provider",
        }),
      );
      if (activeSubmenu === "provider") {
        modelMenu.append(makeComposerSubmenu("provider", providerButtons));
      }
      return;
    }

    modelMenu.append(makeComposerMenuHeading(t("Reasoning")));
    if (effortButtons.length > 0) {
      modelMenu.append(...effortButtons);
    } else {
      modelMenu.append(makeComposerDisabledMenuItem(t("Not supported")));
    }
    modelMenu.append(
      makeComposerMenuDivider(),
      makeComposerSubmenuTrigger({
        kind: "model",
        label: modelLabel(currentModel, activeProvider.model) || t("Model"),
        active: activeSubmenu === "model",
      }),
      makeComposerSubmenuTrigger({
        kind: "provider",
        label: providerLabel(provider, activeProvider.provider) || "Provider",
        active: activeSubmenu === "provider",
      }),
    );
    if (activeSubmenu === "model") {
      modelMenu.append(makeComposerSubmenu("model", modelButtons));
    }
    if (activeSubmenu === "provider") {
      modelMenu.append(makeComposerSubmenu("provider", providerButtons));
    }
  }

  function renderProviderControls() {
    renderModelControl();
    renderModelMenu();
  }

  // 依据会话级选择（若有）叠加到 providersPayload 上算出当前显示的 provider/模型；
  // 否则回退到全局 active。credentials/apiBase 仍取该 provider 的全局配置。
  function resolveActiveProvider() {
    if (sessionSelection && text(sessionSelection.provider) && text(sessionSelection.model)) {
      const providerItem = providersPayload ? findProvider(providersPayload, sessionSelection.provider) : null;
      activeProvider = activeProviderSummary({
        provider: sessionSelection.provider,
        model: sessionSelection.model,
        effort: sessionSelection.effort,
        apiBase: providerItem?.apiBase || activeProvider.apiBase,
        hasApiKey: providerItem ? providerItem.hasApiKey : activeProvider.hasApiKey,
      });
    } else {
      // 无会话级覆盖:优先显示当前生效的合作方源(其 active.provider 为空,靠 current 标记识别),
      // 否则回退到全局 active。
      const partner = providersPayload ? currentPartnerProvider(providersPayload) : null;
      if (partner) {
        activeProvider = activeProviderSummary({ provider: partner.key, hasApiKey: true });
      } else if (providersPayload?.active) {
        activeProvider = activeProviderSummary(providersPayload.active);
      }
    }
    // 草稿态按钮跟随所选模型的思考默认:provider 列表异步就绪或用户切换 provider 后重算并刷新。
    if (thinkingFollowsDefault) {
      thinkingEnabled = selectedModelThinkingDefault();
      renderThinkingToggle();
    }
  }

  async function loadProviderControls() {
    if (!api.getProviders || !modelControl) {
      renderProviderControls();
      return;
    }
    try {
      providersPayload = await api.getProviders();
      resolveActiveProvider();
      renderProviderControls();
    } catch (error) {
      modelControl.textContent = t("Configure model");
      if (!isRecoverableError(error)) {
        setError(error.message || t("Could not load model settings"));
      }
    }
  }

  // 合作方源:选中即全局锁定(写 llm_source),不选 model;同时清掉本会话的会话级覆盖,
  // 好让全局合作方源对当前会话立即生效(会话级 provider 不能存合作方 key,不在 registry)。
  async function activatePartnerSelection(provider) {
    if (!api.setActiveProvider) {
      return;
    }
    try {
      await api.setActiveProvider(provider.key);
      if (sessionId && api.clearSessionModel) {
        await api.clearSessionModel(sessionId);
      } else {
        // 草稿会话(尚未创建):清掉暂存的会话级选择,建会话时改为继承全局(合作方源)。
        options.onProviderSelectionChange?.(null);
      }
      sessionSelection = null;
      activeSubmenu = "";
      clearError();
      await loadProviderControls();
    } catch (error) {
      setError(error.message || t("Could not switch provider"));
    }
  }

  async function saveComposerProviderSelection(next = {}) {
    if (!providersPayload) {
      return;
    }
    const providerKey = text(next.provider || activeProvider.provider);
    const provider = findProvider(providersPayload, providerKey);
    if (!provider) {
      return;
    }
    if (isPartnerProvider(provider)) {
      await activatePartnerSelection(provider);
      return;
    }
    const model = text(next.model) ? findModel(provider, next.model) : findModel(provider, activeProvider.model) || firstModel(provider);
    // 兼容模式/本地等 provider 在 registry 中无枚举模型（payload.models 为空），
    // findModel/firstModel 取不到 model 对象；回退到设置里已保存或注册表默认的模型 id，
    // 否则这些 provider 在会话切换里点了没反应（永远选不中）。configured 依赖 usable，
    // 而 usable 要求 savedModel 或 defaultModel 存在，故能出现在切换列表的一定能取到 id。
    const modelId = model ? model.id : text(next.model) || text(provider.savedModel) || text(provider.defaultModel);
    if (!modelId) {
      return;
    }
    const effort = text(next.effort || effectiveEffort(model, activeProvider.effort) || provider.savedEffort);
    // provider/模型是会话级设置：只更新当前会话，不再写全局 activeProvider。
    const selection = { provider: provider.key, model: modelId, effort };
    try {
      if (sessionId && api.saveSessionModel) {
        await api.saveSessionModel(sessionId, selection);
      } else {
        // 草稿会话（尚未创建）：回传给上层，随会话创建一起持久化。
        options.onProviderSelectionChange?.(selection);
      }
      sessionSelection = selection;
      resolveActiveProvider();
      activeSubmenu = "";
      clearError();
      renderProviderControls();
    } catch (error) {
      setError(error.message || t("Could not save model settings"));
    }
  }

  function positionSuggestions() {
    if (!suggestionsList || !textarea) {
      return;
    }
    const computed = window.getComputedStyle?.(textarea);
    const lineHeight = Number.parseFloat(computed?.lineHeight || "") || 20;
    const paddingTop = Number.parseFloat(computed?.paddingTop || "") || 12;
    const paddingLeft = Number.parseFloat(computed?.paddingLeft || "") || 12;
    const value = text(textarea.value);
    const caret = textarea.selectionStart ?? value.length;
    const lineIndex = value.slice(0, caret).split("\n").length - 1;
    const rawTop = paddingTop + lineHeight * (lineIndex + 1) - (textarea.scrollTop || 0);
    const maxTop = Math.max(paddingTop + lineHeight, (textarea.clientHeight || 0) - lineHeight);
    const top = Math.max(paddingTop + lineHeight, Math.min(rawTop, maxTop));
    suggestionsList.style.setProperty("--suggestions-top", `${top}px`);
    suggestionsList.style.setProperty("--suggestions-left", `${paddingLeft}px`);
    suggestionsList.style.setProperty("--suggestions-right", `${paddingLeft}px`);
  }

  function renderSuggestions() {
    if (!suggestionsList) {
      return;
    }
    suggestionsList.replaceChildren();
    const sections = suggestionMenuSections(suggestions.filter(Boolean));
    visibleSuggestions = sections.flatMap((section) => section.suggestions);
    const kindClass = safeSuggestionClassPart(visibleSuggestions[0]?.kind || "item");
    suggestionsList.className = `suggestions is-${kindClass}-suggestions`;
    for (const section of sections) {
      if (section.label) {
        const sectionLabel = document.createElement("div");
        sectionLabel.className = "suggestion-section-label";
        sectionLabel.textContent = section.label;
        suggestionsList.append(sectionLabel);
      }
      for (const suggestion of section.suggestions) {
        const index = visibleSuggestions.indexOf(suggestion);
        const kind = suggestionLayoutKind(suggestion);
        const itemKindClass = kind === "skill" ? "is-skill-suggestion" : `is-${kind}-suggestion`;
        const item = document.createElement("button");
        item.type = "button";
        item.className = `suggestion-item ${itemKindClass}`;
        const parts = suggestionDisplayParts(suggestion, { contextUsage });
        const icon = document.createElement("span");
        icon.className = suggestionIconClasses(suggestion);
        icon.setAttribute("aria-hidden", "true");
        if (commandNameFromSuggestion(suggestion) === "compact") {
          applyContextUsageIcon(icon, contextUsage);
        }
        const copy = document.createElement("span");
        copy.className = "suggestion-copy";
        const token = document.createElement("span");
        token.className = "suggestion-token";
        token.textContent = parts.token;
        const description = document.createElement("span");
        description.className = "suggestion-description";
        description.textContent = parts.description;
        const scope = document.createElement("span");
        scope.className = "suggestion-scope";
        scope.textContent = skillScopeLabel(suggestion);
        copy.append(token, description);
        item.append(icon, copy, scope);
        const activateItem = () => {
          activeSuggestionIndex = index;
          syncSuggestionActiveState();
        };
        item.addEventListener("mouseenter", activateItem);
        item.addEventListener("pointerenter", activateItem);
        item.addEventListener("mousemove", activateItem);
        item.addEventListener("focus", activateItem);
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          void acceptSuggestion(index);
        });
        suggestionsList.append(item);
      }
    }
    suggestionsList.hidden = visibleSuggestions.length === 0;
    positionSuggestions();
    syncSuggestionActiveState();
  }

  function suggestionItems() {
    if (!suggestionsList) {
      return [];
    }
    if (typeof suggestionsList.querySelectorAll === "function") {
      return [...suggestionsList.querySelectorAll(".suggestion-item")];
    }
    return [...(suggestionsList.children || [])].filter((item) => text(item.className).includes("suggestion-item"));
  }

  function syncSuggestionActiveState() {
    for (const [index, item] of suggestionItems().entries()) {
      const classes = new Set(text(item.className).split(/\s+/).filter(Boolean));
      if (index === activeSuggestionIndex) {
        classes.add("is-active");
      } else {
        classes.delete("is-active");
      }
      item.className = [...classes].join(" ");
    }
    suggestionsList?.querySelector?.(".suggestion-item.is-active")?.scrollIntoView?.({ block: "nearest" });
  }

  function clearSuggestions() {
    suggestionRequestVersion += 1;
    suggestions = [];
    visibleSuggestions = [];
    activeSuggestionIndex = -1;
    renderSuggestions();
  }

  function currentPrefix() {
    const value = text(textarea?.value);
    const beforeCursor = value.slice(0, textarea?.selectionStart ?? value.length);
    return beforeCursor.split(/\s/).pop() || "";
  }

  function suggestionKind(prefix) {
    const trigger = prefix[0];
    if (trigger === "!") {
      const value = text(textarea?.value);
      const beforeCursor = value.slice(0, textarea?.selectionStart ?? value.length);
      const currentLine = beforeCursor.split("\n").pop() || "";
      if (!currentLine.trimStart().startsWith("!")) {
        return "";
      }
    }
    return {
      "/": "command",
      "$": "skill",
      "@": "file",
      "!": "shell",
    }[trigger] || "";
  }

  async function refreshSuggestions() {
    const prefix = currentPrefix();
    const kind = suggestionKind(prefix);
    if (!kind) {
      clearSuggestions();
      return;
    }
    const requestVersion = ++suggestionRequestVersion;
    const requestedSessionId = sessionId;
    try {
      const payload = await api.getSuggestions?.({
        sessionId: requestedSessionId,
        kind,
        query: prefix.slice(1),
      });
      if (
        requestVersion !== suggestionRequestVersion ||
        sessionId !== requestedSessionId ||
        currentPrefix() !== prefix ||
        suggestionKind(currentPrefix()) !== kind
      ) {
        return;
      }
      suggestions = visibleComposerSuggestions(payload?.suggestions || [], {
        draftSessionActive: Boolean(options.isDraftSessionActive?.()) || !sessionId,
        pipelineMode: Boolean(options.isPipelineMode?.()),
      });
      activeSuggestionIndex = suggestions.length > 0 ? 0 : -1;
      renderSuggestions();
    } catch (error) {
      if (requestVersion === suggestionRequestVersion && !isRecoverableError(error)) {
        setError(error.message || t("Could not load suggestions"));
      }
    }
  }

  async function acceptImmediateCommandSuggestion(suggestion, beforeCursor, afterCursor, prefix) {
    const commandText = text(suggestion.value).trim();
    if (!commandText || !sessionId) {
      return false;
    }
    textarea.value = `${beforeCursor.slice(0, beforeCursor.length - prefix.length)}${afterCursor}`.trimStart();
    localDraftDirty = Boolean(text(textarea.value).trim());
    syncSendButtonState();
    textarea.focus();
    clearSuggestions();
    try {
      const result = await api.postCommand?.(sessionId, commandText);
      options.onCommandResult?.(result);
      options.onSubmitAccepted?.({ sessionId, result, kind: "command", text: commandText });
    } catch (error) {
      if (!isRecoverableError(error)) {
        setError(error.message || t("Command failed"));
      } else {
        setError(error.message || t("The command was not accepted"));
      }
    }
    return true;
  }

  async function acceptSuggestion(index = activeSuggestionIndex) {
    const suggestion = visibleSuggestions[index];
    if (!suggestion || !textarea) {
      return;
    }
    const value = text(textarea.value);
    const caret = textarea.selectionStart ?? value.length;
    const beforeCursor = value.slice(0, caret);
    const afterCursor = value.slice(caret);
    const prefix = currentPrefix();
    if (suggestion.kind === "file" || text(suggestion.value).startsWith("@")) {
      const fileRef = text(suggestion.value).replace(/^@/, "");
      if (fileRef) {
        attachments = [...attachments, makeFileReferenceAttachment(fileRef)];
        renderAttachmentChips();
      }
      textarea.value = `${beforeCursor.slice(0, beforeCursor.length - prefix.length)}${afterCursor}`.trimStart();
      localDraftDirty = true;
      textarea.focus();
      clearSuggestions();
      return;
    }
    if (suggestionLayoutKind(suggestion) === "skill") {
      selectedSkill = {
        name: skillDisplayName(suggestion),
        command: skillCommandValue(suggestion),
      };
      textarea.value = `${beforeCursor.slice(0, beforeCursor.length - prefix.length)}${afterCursor}`.trimStart();
      localDraftDirty = true;
      renderSkillChip();
      syncSendButtonState();
      textarea.focus();
      clearSuggestions();
      return;
    }
    if (isImmediateCommandSuggestion(suggestion)) {
      await acceptImmediateCommandSuggestion(suggestion, beforeCursor, afterCursor, prefix);
      return;
    }
    textarea.value = `${beforeCursor.slice(0, beforeCursor.length - prefix.length)}${suggestion.value} ${afterCursor}`;
    localDraftDirty = true;
    syncSendButtonState();
    textarea.focus();
    clearSuggestions();
  }

  function moveSuggestion(delta) {
    if (visibleSuggestions.length === 0) {
      return;
    }
    activeSuggestionIndex = (activeSuggestionIndex + delta + visibleSuggestions.length) % visibleSuggestions.length;
    renderSuggestions();
  }

  async function addFiles(files) {
    for (const file of [...(files || [])]) {
      const attachment = makeAttachment(file);
      if (!attachment.isImage) {
        setError(t("Use @ suggestions for workspace file references."), "unsupported_file_picker");
        continue;
      }
      assignLocalPreviewUrl(attachment);
      attachments = [...attachments, attachment];
      renderAttachmentChips();
      if (!SUPPORTED_IMAGE_TYPES.has(attachment.mediaType)) {
        attachment.status = "unsupported";
        setError(t("Unsupported image type."), "unsupported_image");
        renderAttachmentChips();
        continue;
      }
      if (!sessionId) {
        attachment.status = "pending";
        renderAttachmentChips();
        continue;
      }
      try {
        attachment.status = "uploading";
        renderAttachmentChips();
        const uploaded = await api.uploadImage?.(sessionId, file);
        const uploadedPreviewUrl = text(uploaded?.previewUrl);
        if (uploadedPreviewUrl) {
          revokeAttachmentPreview(attachment);
          attachment.previewUrl = uploadedPreviewUrl;
        }
        attachment.imageId = text(uploaded?.imageId);
        attachment.status = "ready";
        clearError();
        renderAttachmentChips();
      } catch (error) {
        attachment.status = "failed";
        setError(error.message || t("Image upload failed"), "unsupported_image");
        renderAttachmentChips();
      }
    }
  }

  async function ensureSessionForSubmit() {
    if (sessionId) {
      return sessionId;
    }
    if (!options.createSessionForSubmit) {
      return "";
    }
    const created = await options.createSessionForSubmit();
    const nextSessionId = text(created?.webSessionId || created?.sessionId || created);
    if (nextSessionId) {
      sessionId = nextSessionId;
      syncSendButtonState();
    }
    return sessionId;
  }

  function validateAttachmentsBeforeSessionCreation() {
    const unsupportedImage = attachments.find(
      (attachment) => attachment.isImage && !attachment.imageId && !SUPPORTED_IMAGE_TYPES.has(attachment.mediaType),
    );
    if (unsupportedImage) {
      unsupportedImage.status = "unsupported";
      renderAttachmentChips();
      const error = new Error(t("Unsupported image type."));
      error.code = "unsupported_image";
      throw error;
    }
    const pendingImage = attachments.find((attachment) => attachment.isImage && !attachment.imageId);
    if (pendingImage && !api.uploadImage) {
      const error = new Error(t("This image has not been uploaded yet."));
      error.code = "unsupported_image";
      throw error;
    }
  }

  async function uploadPendingAttachmentsForSession(activeSessionId) {
    for (const attachment of attachments) {
      if (!attachment.isImage || attachment.imageId) {
        continue;
      }
      if (!SUPPORTED_IMAGE_TYPES.has(attachment.mediaType)) {
        attachment.status = "unsupported";
        renderAttachmentChips();
        const error = new Error(t("Unsupported image type."));
        error.code = "unsupported_image";
        throw error;
      }
      if (!api.uploadImage) {
        const error = new Error(t("This image has not been uploaded yet."));
        error.code = "unsupported_image";
        throw error;
      }
      try {
        attachment.status = "uploading";
        renderAttachmentChips();
        const uploaded = await api.uploadImage(activeSessionId, attachment.file);
        const uploadedPreviewUrl = text(uploaded?.previewUrl);
        if (uploadedPreviewUrl) {
          revokeAttachmentPreview(attachment);
          attachment.previewUrl = uploadedPreviewUrl;
        }
        attachment.imageId = text(uploaded?.imageId);
        attachment.status = "ready";
        clearError();
        renderAttachmentChips();
      } catch (error) {
        attachment.status = "failed";
        renderAttachmentChips();
        throw error;
      }
    }
  }

  function attachmentPayload() {
    const unsupportedImage = attachments.find((attachment) => attachment.isImage && !attachment.imageId);
    if (unsupportedImage) {
      const error = new Error(t("This image has not been uploaded yet."));
      error.code = "unsupported_image";
      throw error;
    }
    return {
      imageIds: attachments.filter((attachment) => attachment.imageId).map((attachment) => attachment.imageId),
      fileRefs: attachments.filter((attachment) => attachment.fileRef).map((attachment) => attachment.fileRef),
    };
  }

  function submittedDraftText(draft) {
    const plainDraft = text(draft);
    if (!selectedSkill) {
      return plainDraft;
    }
    return [selectedSkill.command, plainDraft.trim() ? plainDraft : ""].filter(Boolean).join("\n");
  }

  function resetSubmittedDraft(submittedDraft = "", submittedRestoreRevision = restoreRevision) {
    if (submittedRestoreRevision !== restoreRevision) {
      return;
    }
    if (textarea) {
      if (!submittedDraft || textarea.value === submittedDraft) {
        textarea.value = "";
      }
    }
    if (!textarea || !textarea.value) {
      localDraftDirty = false;
    }
    clearSelectedSkill();
    for (const attachment of attachments) {
      revokeAttachmentPreview(attachment);
    }
    attachments = [];
    renderAttachmentChips();
    clearSuggestions();
    clearError();
    resetHistoryNav();
  }

  async function submit() {
    if (!textarea) {
      return;
    }
    const draft = textarea.value;
    const submittedDraft = submittedDraftText(draft);
    const submittedSessionRevision = sessionRevision;
    const submittedRestoreRevision = restoreRevision;
    if (!text(submittedDraft).trim() && attachments.length === 0) {
      return;
    }
    // 直接键入 /compact 等命令并提交也要被拦截(仅「/」菜单过滤不够):按当前运行时状态,
    // 与补全菜单一致地拦下流水线模式、新会话草稿阶段无法主动执行的命令——提示不可用、
    // 不创建会话、不下发命令(draftSessionActive 判据与菜单一致:显式草稿或尚无 sessionId)。
    const blockedCommand = blockedComposerCommandName(submittedDraft, {
      pipelineMode: Boolean(options.isPipelineMode?.()),
      draftSessionActive: Boolean(options.isDraftSessionActive?.()) || !sessionId,
    });
    if (blockedCommand) {
      const slashCommand = "/" + blockedCommand.command;
      setError(
        blockedCommand.reason === "pipeline"
          ? t("{command} is not available in pipeline mode", { command: slashCommand })
          : t("{command} is only available in an active conversation", { command: slashCommand }),
      );
      return;
    }
    let activeSessionId;
    let payload;
    try {
      validateAttachmentsBeforeSessionCreation();
      activeSessionId = await ensureSessionForSubmit();
      if (!activeSessionId) {
        return;
      }
      await uploadPendingAttachmentsForSession(activeSessionId);
      if (sessionRevision !== submittedSessionRevision || sessionId !== activeSessionId) {
        return;
      }
      payload = attachmentPayload();
    } catch (error) {
      if (sessionRevision === submittedSessionRevision) {
        setError(error.message, error.code || "unsupported_image");
      }
      return;
    }
    if (!text(submittedDraft).trim() && payload.imageIds.length === 0 && payload.fileRefs.length === 0) {
      return;
    }

    try {
      // 压缩进行中同样按「排队」处理:输入不会新起 turn,等压缩完成后由后端逐条排空。
      if (turnActive || compacting) {
        if (payload.imageIds.length > 0 || payload.fileRefs.length > 0) {
          setError(QUEUED_ATTACHMENT_ERROR_TEXT, QUEUED_ATTACHMENT_ERROR_CODE);
          return;
        }
        await api.postQueuedInput?.(activeSessionId, submittedDraft);
        rememberInput(draft);
        if (!isMidTurnCommandLike(submittedDraft) && sessionRevision === submittedSessionRevision) {
          resetSubmittedDraft(draft, submittedRestoreRevision);
        }
        return;
      }
      if (isMidTurnCommandLike(submittedDraft)) {
        const result = await api.postCommand?.(activeSessionId, submittedDraft);
        options.onCommandResult?.(result);
        options.onSubmitAccepted?.({ sessionId: activeSessionId, result, kind: "command", text: submittedDraft });
        rememberInput(draft);
        if (sessionRevision === submittedSessionRevision) {
          resetSubmittedDraft(draft, submittedRestoreRevision);
        }
        return;
      }
      const result = await api.postMessage?.(activeSessionId, {
        text: submittedDraft,
        imageIds: payload.imageIds,
        fileRefs: payload.fileRefs,
      });
      options.onSubmitAccepted?.({ sessionId: activeSessionId, result, kind: "message", text: submittedDraft });
      rememberInput(draft);
      if (sessionRevision === submittedSessionRevision) {
        resetSubmittedDraft(draft, submittedRestoreRevision);
      }
    } catch (error) {
      if (sessionRevision === submittedSessionRevision) {
        if (!isRecoverableError(error)) {
          setError(error.message || t("Send failed"));
        } else {
          setError(error.message || t("The message was not accepted"));
        }
      }
    }
  }

  async function stopCurrentTurn() {
    if (!sessionId) {
      return;
    }
    try {
      await api.postInterrupt?.(sessionId, { message: "" });
    } catch (error) {
      if (!isRecoverableError(error)) {
        setError(error.message || t("Stop failed"));
      }
    }
  }

  function loadGlobalHistory() {
    try {
      const raw = window.localStorage?.getItem(INPUT_HISTORY_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed)
        ? parsed.filter((item) => typeof item === "string" && item.trim() !== "")
        : [];
    } catch {
      return [];
    }
  }

  function saveGlobalHistory() {
    try {
      window.localStorage?.setItem(INPUT_HISTORY_KEY, JSON.stringify(globalHistory));
    } catch {
      // localStorage 不可用(隐私模式/配额已满):历史仅存内存,不影响本次导航。
    }
  }

  // 追加到 oldest→newest 列表:折叠与末尾连续重复;limit>0 时截断头部。返回同一数组。
  function pushHistory(list, value, limit) {
    if (list[list.length - 1] === value) {
      return list;
    }
    list.push(value);
    if (limit && list.length > limit) {
      list.splice(0, list.length - limit);
    }
    return list;
  }

  function rememberInput(raw) {
    const value = text(raw);
    if (value.trim() === "") {
      return;
    }
    pushHistory(conversationHistory, value, 0);
    pushHistory(globalHistory, value, INPUT_HISTORY_LIMIT);
    saveGlobalHistory();
  }

  function resetHistoryNav() {
    historyEntries = [];
    historyPos = null;
    historyStash = "";
  }

  // 程序化写入召回值:置文末光标、清联想、同步 dirty/发送按钮。程序化赋值不触发 input 事件。
  function applyHistoryValue(value) {
    if (!textarea) {
      return;
    }
    textarea.value = value;
    const end = textarea.value.length;
    try {
      textarea.setSelectionRange(end, end);
    } catch {
      // 某些无头 DOM 桩不实现 setSelectionRange;忽略。
    }
    clearSuggestions();
    localDraftDirty = Boolean(textarea.value);
    syncSendButtonState();
  }

  // 返回 true=已消费(调用方 preventDefault);false=放行默认多行光标移动。
  function handleHistoryNavigation(key) {
    if (!textarea) {
      return false;
    }
    if (historyPos === null) {
      if (key !== "ArrowUp") {
        return false; // 未导航时只有 ↑ 能进入;↓ 放行默认
      }
      if (textarea.value.trim() !== "") {
        return false; // 输入框非空:放行默认(多行光标移动)
      }
      const source = conversationHistory.length ? conversationHistory : globalHistory;
      if (source.length === 0) {
        return false;
      }
      historyEntries = source;
      historyStash = textarea.value;
      historyPos = historyEntries.length - 1;
      applyHistoryValue(historyEntries[historyPos]);
      return true;
    }
    // 导航中:若内容已被编辑(不再等于召回项)→ 退出导航,放行默认光标移动。
    if (textarea.value !== historyEntries[historyPos]) {
      resetHistoryNav();
      return false;
    }
    if (key === "ArrowUp") {
      if (historyPos > 0) {
        historyPos -= 1;
        applyHistoryValue(historyEntries[historyPos]);
      }
      return true; // 已在最早:原地停住,仍消费按键
    }
    // ArrowDown
    if (historyPos < historyEntries.length - 1) {
      historyPos += 1;
      applyHistoryValue(historyEntries[historyPos]);
      return true;
    }
    // 越过最新:还原进入前缓冲并退出导航。
    const stash = historyStash;
    resetHistoryNav();
    applyHistoryValue(stash);
    return true;
  }

  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });

  textarea?.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (visibleSuggestions.length > 0) {
        event.preventDefault();
        moveSuggestion(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      // 无联想菜单:尝试历史导航;未消费则放行默认多行光标移动(修复方向键被吞、光标无法换行)。
      if (handleHistoryNavigation(event.key)) {
        event.preventDefault();
      }
      return;
    }
    if (event.key === "Tab") {
      if (visibleSuggestions.length > 0) {
        event.preventDefault();
        void acceptSuggestion();
      }
      return;
    }
    if (event.key === "Escape") {
      clearSuggestions();
      return;
    }
    if ((event.key === "Backspace" || event.key === "Delete") && selectedSkill && !text(textarea.value)) {
      event.preventDefault();
      clearSelectedSkill();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (shouldAcceptSuggestionOnEnter(textarea.value, visibleSuggestions[activeSuggestionIndex])) {
        void acceptSuggestion();
        return;
      }
      submit();
    }
  });

  textarea?.addEventListener("input", refreshSuggestions);
  textarea?.addEventListener("input", () => {
    localDraftDirty = true;
    syncSendButtonState();
  });
  textarea?.addEventListener("scroll", positionSuggestions);
  textarea?.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])].filter((file) => text(file?.type).startsWith("image/"));
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    addFiles(files);
  });
  form?.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types?.includes("Files")) {
      event.preventDefault();
    }
  });
  form?.addEventListener("drop", (event) => {
    const files = [...(event.dataTransfer?.files || [])].filter((file) => text(file?.type).startsWith("image/"));
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    addFiles(files);
  });
  fileInput?.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });
  // 发送按钮在运行中充当"停止"：点击时中断当前轮次，而不是触发表单提交。
  sendButton?.addEventListener("click", (event) => {
    if (turnActive) {
      event.preventDefault();
      void stopCurrentTurn();
    }
  });
  permissionControl?.addEventListener("click", (event) => {
    event.stopPropagation?.();
    permissionMenuOpen = !permissionMenuOpen;
    if (permissionMenuOpen) {
      closeModelMenu();
    }
    renderPermissionControls();
  });
  permissionMenu?.addEventListener("click", (event) => {
    event.stopPropagation?.();
  });
  thinkingToggle?.addEventListener("click", (event) => {
    event.stopPropagation?.();
    void saveThinkingEnabled(!(thinkingEnabled === true));
  });
  modelControl?.addEventListener("click", (event) => {
    event.stopPropagation?.();
    modelMenuOpen = !modelMenuOpen;
    activeSubmenu = "";
    if (modelMenuOpen) {
      closePermissionMenu();
    }
    renderProviderControls();
    if (modelMenuOpen && !providersPayload) {
      loadProviderControls();
    }
  });
  modelMenu?.addEventListener("click", (event) => {
    event.stopPropagation?.();
  });
  const canListenToDocumentClicks =
    typeof document !== "undefined" && typeof document.addEventListener === "function";
  if (canListenToDocumentClicks && (modelControl || modelMenu)) {
    document.addEventListener("click", (event) => {
      if (modelMenuOpen && !isModelMenuEvent(event)) {
        closeModelMenu();
      }
    });
  }
  if (canListenToDocumentClicks && (permissionControl || permissionMenu)) {
    document.addEventListener("click", (event) => {
      if (permissionMenuOpen && !isPermissionMenuEvent(event)) {
        closePermissionMenu();
      }
    });
  }
  if (canListenToDocumentClicks && (textarea || suggestionsList)) {
    document.addEventListener("click", (event) => {
      if (suggestionsList && !suggestionsList.hidden && !isSuggestionEvent(event)) {
        clearSuggestions();
      }
    });
  }

  if (sendButton) {
    sendButton.title = ENTER_HELP;
  }
  syncPlaceholder();
  syncSendButtonState();
  renderPermissionControls();
  renderThinkingToggle();
  loadProviderControls();

  return {
    setSession(nextSessionId, sessionOptions = {}) {
      const next = text(nextSessionId);
      const preserveDraft = sessionOptions.preserveDraft === true;
      if (next !== sessionId && !preserveDraft) {
        sessionRevision += 1;
        if (textarea) {
          textarea.value = "";
        }
        localDraftDirty = false;
        clearSelectedSkill();
        for (const attachment of attachments) {
          revokeAttachmentPreview(attachment);
        }
        attachments = [];
        renderAttachmentChips();
        clearSuggestions();
        clearError();
      }
      sessionId = next;
      conversationHistory = [];
      resetHistoryNav();
      syncPlaceholder();
      syncSendButtonState();
    },
    setReadOnly(value) {
      readOnly = Boolean(value);
      if (textarea) {
        textarea.disabled = readOnly;
      }
      syncPlaceholder();
      syncSendButtonState();
    },
    setPermissionMode(nextMode) {
      permissionMode = permissionOption(text(nextMode)).id;
      renderPermissionControls();
    },
    setThinkingEnabled(value) {
      if (value === true || value === false) {
        // 明确态(真实会话的 thinkingEffective / 草稿里用户已切过):直接采用,不再跟随默认。
        thinkingFollowsDefault = false;
        thinkingEnabled = value;
      } else {
        // 未设置(新会话草稿):跟随所选模型的思考默认,provider 解析/切换后由 resolveActiveProvider 重算。
        thinkingFollowsDefault = true;
        thinkingEnabled = selectedModelThinkingDefault();
      }
      renderThinkingToggle();
    },
    setActiveProvider(next = {}) {
      const provider = text(next.provider);
      const model = text(next.model);
      sessionSelection = provider && model ? { provider, model, effort: text(next.effort) } : null;
      resolveActiveProvider();
      renderProviderControls();
    },
    // 设置里保存/激活 provider 后重新拉取整表,让会话切换列表立即反映新变绿的 provider,
    // 无需刷新整页(providersPayload 原本只在为空时才重取)。
    refreshProviders() {
      loadProviderControls();
    },
    setContextUsage(nextContextUsage) {
      contextUsage = nextContextUsage && typeof nextContextUsage === "object" ? nextContextUsage : {};
      renderProviderControls();
      if (suggestionsList && !suggestionsList.hidden && suggestions.length > 0) {
        renderSuggestions();
      }
    },
    setContextUsages(windows) {
      contextUsageWindows = Array.isArray(windows)
        ? windows.filter((win) => win && typeof win === "object" && win.contextUsage)
        : [];
      renderProviderControls();
    },
    setContextFallbackLabel(label) {
      contextFallbackLabel = typeof label === "string" ? label : "";
      renderProviderControls();
    },
    setTurnActive(active) {
      turnActive = Boolean(active);
      // 回合结束后,之前"附件需等回合结束"的提示已失效,清除以免残留。
      if (!turnActive) {
        clearQueuedAttachmentError();
      }
      if (sendButton) {
        // 运行中：切换为"停止"外观（方块图标），并更新可访问性标签与提示。
        sendButton.classList.toggle("is-stopping", turnActive);
        sendButton.setAttribute("aria-label", turnActive ? t("Stop") : t("Send"));
        sendButton.title = turnActive ? t("Stop the current reply") : ENTER_HELP;
      }
      syncSendButtonState();
    },
    setCompacting(active) {
      // 压缩态只影响「提交→排队」判定,不改发送按钮外观(压缩期间无 turn 可停)。
      compacting = Boolean(active);
      syncSendButtonState();
    },
    setDraft(draft, options = {}) {
      if (!options.force && localDraftDirty) {
        return;
      }
      if (textarea && textarea.value !== text(draft)) {
        textarea.value = text(draft);
      }
      localDraftDirty = false;
      resetHistoryNav();
      if (options.force && !text(draft)) {
        clearSelectedSkill();
      }
      syncSendButtonState();
    },
    restoreDraft(payload = {}) {
      restoreRevision += 1;
      for (const attachment of attachments) {
        revokeAttachmentPreview(attachment);
      }
      const imageIds = Array.isArray(payload.imageIds) ? payload.imageIds.map(text).filter(Boolean) : [];
      const fileRefs = Array.isArray(payload.fileRefs) ? payload.fileRefs.map(text).filter(Boolean) : [];
      attachments = [
        ...imageIds.map((imageId) => makeImageReferenceAttachment(imageId, sessionId, Boolean(api.isTokenMode?.()))),
        ...fileRefs.map(makeFileReferenceAttachment),
      ];
      clearSelectedSkill();
      if (textarea) {
        textarea.value = text(payload.draft);
      }
      localDraftDirty = Boolean(text(payload.draft).trim()) || attachments.length > 0;
      resetHistoryNav();
      renderAttachmentChips();
      if (api.isTokenMode?.() && api.getImageObjectUrl) {
        for (const attachment of attachments.filter((item) => item.imageId && !item.previewUrl)) {
          void api.getImageObjectUrl(attachment.imageId, sessionId).then((url) => {
            if (!attachments.includes(attachment)) return;
            attachment.previewUrl = text(url);
            renderAttachmentChips();
          }).catch(() => {});
        }
      }
      clearSuggestions();
      clearError();
      syncSendButtonState();
    },
    setInputHistory(items) {
      conversationHistory = (Array.isArray(items) ? items : [])
        .map((item) => text(item))
        .filter((item) => item.trim() !== "");
      resetHistoryNav();
    },
    submit,
    addFiles,
  };
}
