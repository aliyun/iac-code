import { t, currentLang } from "../i18n.js?v=web-repl-ui-277";

// 设置导航精简:状态/流水线/搜索不再作为导航项(状态经 render/showResult、流水线经
// 流水线工作区、搜索经侧栏按钮等编程式入口打开),其面板仍在 buildPanels 里注册。
const NAV_GROUPS = [
  {
    title: t("Configuration"),
    tabs: [
      { id: "other", label: t("General") },
      { id: "model", label: t("Models") },
      { id: "cloud", label: t("Cloud credentials") },
      { id: "memory", label: t("Memory") },
      { id: "skills", label: t("Plugins") },
      // 「开发」分页:仅在开发者模式开启时出现(devOnly)。承载失败工具标红开关与重启入口。
      { id: "developer", label: t("Developer"), devOnly: true },
    ],
  },
  {
    title: t("History"),
    tabs: [{ id: "archived", label: t("Archived conversations") }],
  },
];

const CLOUD_VENDORS = [{ id: "aliyun", label: t("Alibaba Cloud") }];

const CREDENTIAL_MODE_ORDER = ["AK", "StsToken", "RamRoleArn", "OAuth"];

const CLOUD_MODE_LABELS = {
  AK: t("AccessKey"),
  StsToken: t("STS Token"),
  RamRoleArn: t("RAM role"),
  OAuth: t("OAuth browser login"),
};

const CLOUD_SOURCE_LABELS = {
  env: t("Environment variables"),
  config: t("Saved locally"),
  cli: t("aliyun CLI config"),
};

// 阿里云 ECS 公共云地域(手填不在列表内的地域也可,详见区域字段的组合框)。
const ALIYUN_REGIONS = [
  { id: "cn-qingdao", label: t("North China 1 (Qingdao)") },
  { id: "cn-beijing", label: t("North China 2 (Beijing)") },
  { id: "cn-zhangjiakou", label: t("North China 3 (Zhangjiakou)") },
  { id: "cn-huhehaote", label: t("North China 5 (Hohhot)") },
  { id: "cn-wulanchabu", label: t("North China 6 (Ulanqab)") },
  { id: "cn-hangzhou", label: t("East China 1 (Hangzhou)") },
  { id: "cn-shanghai", label: t("East China 2 (Shanghai)") },
  { id: "cn-nanjing", label: t("East China 5 (Nanjing - Local Region)") },
  { id: "cn-fuzhou", label: t("East China 6 (Fuzhou - Local Region)") },
  { id: "cn-shenzhen", label: t("South China 1 (Shenzhen)") },
  { id: "cn-heyuan", label: t("South China 2 (Heyuan)") },
  { id: "cn-guangzhou", label: t("South China 3 (Guangzhou)") },
  { id: "cn-wuhan-lr", label: t("Central China 1 (Wuhan - Local Region)") },
  { id: "cn-chengdu", label: t("Southwest China 1 (Chengdu)") },
  { id: "cn-hongkong", label: t("Hong Kong, China") },
  { id: "ap-southeast-1", label: t("Singapore") },
  { id: "ap-southeast-2", label: t("Australia (Sydney)") },
  { id: "ap-southeast-3", label: t("Malaysia (Kuala Lumpur)") },
  { id: "ap-southeast-5", label: t("Indonesia (Jakarta)") },
  { id: "ap-southeast-6", label: t("Philippines (Manila)") },
  { id: "ap-southeast-7", label: t("Thailand (Bangkok)") },
  { id: "ap-northeast-1", label: t("Japan (Tokyo)") },
  { id: "ap-northeast-2", label: t("South Korea (Seoul)") },
  { id: "ap-south-1", label: t("India (Mumbai)") },
  { id: "us-west-1", label: t("US (Silicon Valley)") },
  { id: "us-east-1", label: t("US (Virginia)") },
  { id: "eu-central-1", label: t("Germany (Frankfurt)") },
  { id: "eu-west-1", label: t("UK (London)") },
  { id: "me-east-1", label: t("UAE (Dubai)") },
  { id: "me-central-1", label: t("Saudi Arabia (Riyadh)") },
];

function text(value) {
  return value === undefined || value === null ? "" : String(value);
}

function pretty(value) {
  if (value === undefined || value === null || value === "") {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return text(value);
  }
}

function displaySessionId(session) {
  return session?.webSessionId || session?.sessionId || "";
}

// 复制文本到剪贴板:优先用异步 Clipboard API,不可用(非安全上下文/旧浏览器)时回退到
// 临时 textarea + execCommand。返回是否成功,供调用方给出反馈。
async function copyTextToClipboard(value) {
  const textValue = text(value);
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

function makeElement(tagName, { className = "", textContent = "", attributes = {}, dataset = {} } = {}) {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (textContent !== "") {
    node.textContent = textContent;
  }
  for (const [name, value] of Object.entries(attributes)) {
    if (value !== undefined && value !== null) {
      node.setAttribute(name, text(value));
    }
  }
  for (const [name, value] of Object.entries(dataset)) {
    if (value !== undefined && value !== null) {
      node.dataset[name] = text(value);
    }
  }
  return node;
}

function appendChildren(parent, children) {
  parent.append(...children.filter(Boolean));
  return parent;
}

function makeButton(label, action, className = "workspace-action") {
  const button = makeElement("button", {
    className,
    textContent: label,
    attributes: { type: "button" },
    dataset: { workspaceAction: action },
  });
  return button;
}

function makeField(labelText, control, description = "", hint = "") {
  if (!description && !hint) {
    const label = makeElement("label", { className: "workspace-field" });
    const labelSpan = makeElement("span", { textContent: labelText });
    label.append(labelSpan, control);
    return label;
  }
  const label = makeElement("label", { className: "workspace-field has-desc" });
  const textWrap = makeElement("div", { className: "workspace-field-text" });
  const titleSpan = makeElement("span", { className: "workspace-field-title", textContent: labelText });
  textWrap.append(titleSpan);
  if (description) {
    textWrap.append(makeElement("span", { className: "workspace-field-desc", textContent: description }));
  }
  if (hint) {
    textWrap.append(makeElement("span", { className: "workspace-field-hint", textContent: hint }));
  }
  label.append(textWrap, control);
  return label;
}

function makeTextInput(marker, placeholder = "") {
  const input = makeElement("input", {
    className: "workspace-input",
    attributes: { type: "text", placeholder },
    dataset: { workspaceAction: marker },
  });
  return input;
}

function makeNumberInput(marker, placeholder = "") {
  return makeElement("input", {
    className: "workspace-input",
    attributes: { type: "number", inputmode: "numeric", min: "1", step: "1", placeholder },
    dataset: { workspaceAction: marker },
  });
}

function makePasswordInput(marker, placeholder = "") {
  return makeElement("input", {
    className: "workspace-input",
    attributes: { type: "password", autocomplete: "new-password", placeholder },
    dataset: { workspaceAction: marker },
  });
}

// 把密码输入框包成带「眼睛」查看/隐藏按钮的控件。返回 { wrap, toggle, conceal }。
function withPasswordToggle(input) {
  const wrap = makeElement("div", { className: "workspace-password" });
  const toggle = makeElement("button", {
    className: "workspace-password-toggle",
    attributes: { type: "button", "aria-label": t("Show secret"), title: t("Show secret") },
  });
  const conceal = () => {
    input.type = "password";
    toggle.classList.toggle("is-revealed", false);
    toggle.setAttribute("aria-label", t("Show secret"));
    toggle.setAttribute("title", t("Show secret"));
  };
  toggle.addEventListener("click", () => {
    if (input.type === "password") {
      input.type = "text";
      toggle.classList.toggle("is-revealed", true);
      toggle.setAttribute("aria-label", t("Hide secret"));
      toggle.setAttribute("title", t("Hide secret"));
    } else {
      conceal();
    }
  });
  wrap.append(input, toggle);
  return { wrap, toggle, conceal };
}

function makeSelect(marker) {
  return makeElement("select", {
    className: "workspace-select",
    dataset: { workspaceAction: marker },
  });
}

// 可手动输入的组合框:<input list> + <datalist>。既能选建议项也能自由输入,
// 用于 OpenRouter/本地模型/兼容模式等模型、推理强度列表可能为空的服务商。
// 返回 { input, list };datalist 需一并挂到 DOM(其自身不占布局)。
function makeCombobox(marker, placeholder = "") {
  const listId = `${marker}-list`;
  const input = makeElement("input", {
    className: "workspace-input workspace-combobox",
    attributes: { type: "text", placeholder, list: listId, autocomplete: "off", spellcheck: "false" },
    dataset: { workspaceAction: marker },
  });
  const list = makeElement("datalist", { attributes: { id: listId } });
  return { input, list };
}

function fillDatalist(list, values) {
  if (!list) {
    return;
  }
  list.replaceChildren(
    ...values.map((value) => makeElement("option", { attributes: { value: value.value }, textContent: value.label || "" })),
  );
}

// 自由输入 + 常显下拉:input 可手填任意值,点右侧 chevron 展开「全部候选」列表;
// 输入内容不过滤列表(方便切换区域),点候选即回填。返回与 makeChoiceField 兼容的
// 控制器({ wrap, input, setOptions, onChange, value };list 为 null——菜单内嵌在 wrap 中,
// 无需像原生 datalist 那样单独挂到 DOM)。
function makeFreeDropdown(marker, placeholder = "") {
  const input = makeElement("input", {
    className: "workspace-input workspace-combobox",
    attributes: { type: "text", placeholder, autocomplete: "off", spellcheck: "false" },
    dataset: { workspaceAction: marker },
  });
  const toggle = makeElement("button", {
    className: "workspace-choice-toggle",
    attributes: { type: "button", "aria-label": t("Expand list"), tabindex: "-1" },
  });
  const menu = makeElement("div", { className: "workspace-choice-menu", attributes: { role: "listbox" } });
  menu.hidden = true;
  const wrap = appendChildren(
    makeElement("div", { className: "workspace-choice workspace-choice-free" }),
    [input, toggle, menu],
  );

  const closeMenu = () => {
    menu.hidden = true;
    toggle.classList.toggle("is-open", false);
  };
  const openMenu = () => {
    menu.hidden = false;
    toggle.classList.toggle("is-open", true);
  };

  toggle.addEventListener("click", (event) => {
    event.preventDefault();
    if (menu.hidden) openMenu();
    else closeMenu();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  });
  // 点击控件外部时关闭(仅浏览器环境;无头测试不触发交互,故 guard 掉缺失的 document)。
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("click", (event) => {
      if (!menu.hidden && !wrap.contains(event.target)) closeMenu();
    });
  }

  const controller = {
    wrap,
    list: null,
    input,
    setOptions(items) {
      menu.replaceChildren(
        ...items.map((item) => {
          const option = makeElement("button", {
            className: "workspace-choice-option",
            attributes: { type: "button", role: "option" },
            textContent: item.label || item.value,
          });
          // 用 mousedown 抢在 input blur 之前回填,避免焦点竞态吞掉点击。
          option.addEventListener("mousedown", (event) => {
            event.preventDefault();
            input.value = item.value;
            closeMenu();
            input.dispatchEvent(new Event("change", { bubbles: true }));
          });
          return option;
        }),
      );
    },
    onChange(handler) {
      input.addEventListener("change", handler);
    },
  };
  Object.defineProperty(controller, "value", {
    get: () => input.value || "",
    set: (next) => {
      input.value = next || "";
    },
  });
  return controller;
}

// 模型/推理强度字段:枚举型服务商用原生 <select>(可下拉浏览全部选项,后端强校验);
// 候选为空的服务商(OpenRouter/本地/兼容模式等)退回可自由输入的组合框。二者共用
// 同一字段槽,按是否有候选项切换显隐,取值恒取当前可见控件。setOptions 传入候选项即
// 切换形态;当前已保存值若不在候选中(历史/自定义值)会补进下拉,避免丢失。
// forceFree=true 时用自定义常显下拉(makeFreeDropdown):input 可手填任意值,右侧
// chevron 展开「全部候选」——输入内容不过滤列表(原生 <datalist> 会按输入过滤,导致
// 换区域时看不到其它选项)。用于「区域」等既要给候选、又必须允许手填任意值的字段。
// 此时该 marker 在 DOM 中唯一对应 input,取值恒取 input.value。
function makeChoiceField(marker, placeholder = "", { forceFree = false } = {}) {
  if (forceFree) {
    return makeFreeDropdown(marker, placeholder);
  }
  const { input, list } = makeCombobox(marker, placeholder);
  const select = makeSelect(marker);
  const wrap = appendChildren(
    makeElement("div", { className: "workspace-choice" }),
    forceFree ? [input] : [select, input],
  );
  let free = forceFree;
  const controller = {
    wrap,
    list,
    select,
    input,
    setOptions(items, { current = "" } = {}) {
      fillDatalist(list, items);
      free = forceFree || items.length === 0;
      if (!free) {
        const options = [makeElement("option", { attributes: { value: "" }, textContent: placeholder })];
        if (current && !items.some((item) => item.value === current)) {
          options.push(makeElement("option", { attributes: { value: current }, textContent: current }));
        }
        for (const item of items) {
          options.push(
            makeElement("option", { attributes: { value: item.value }, textContent: item.label || item.value }),
          );
        }
        select.replaceChildren(...options);
      }
      select.hidden = free;
      input.hidden = !free;
    },
    onChange(handler) {
      select.addEventListener("change", handler);
      input.addEventListener("change", handler);
    },
  };
  Object.defineProperty(controller, "value", {
    get: () => (free ? input.value : select.value) || "",
    set: (next) => {
      const v = next || "";
      select.value = v;
      input.value = v;
    },
  });
  return controller;
}

function makeTextarea(marker, placeholder = "") {
  const textarea = makeElement("textarea", {
    className: "workspace-textarea",
    attributes: { rows: "8", placeholder },
    dataset: { workspaceAction: marker },
  });
  return textarea;
}

function setOutput(target, payload) {
  if (target) {
    target.textContent = pretty(payload);
  }
}

function setMessage(target, message) {
  if (target) {
    target.textContent = text(message);
  }
}

function selectedValue(select) {
  return select?.value || "";
}

function clearSelect(select, placeholder) {
  if (!select) {
    return;
  }
  const empty = makeElement("option", { textContent: placeholder, attributes: { value: "" } });
  select.replaceChildren(empty);
}

function appendOption(select, value, label) {
  const option = makeElement("option", { textContent: label || value, attributes: { value } });
  select.append(option);
}

function providerItems(payload) {
  return Array.isArray(payload?.providers) ? payload.providers : [];
}

function modelItems(provider) {
  return Array.isArray(provider?.models) ? provider.models : [];
}

function effortItems(model) {
  return Array.isArray(model?.efforts) ? model.efforts : [];
}

function renderSummaryList(target, entries) {
  if (!target) {
    return;
  }
  const rows = entries.map(([label, value, actionNode]) => {
    const row = makeElement("div");
    const term = makeElement("dt", { textContent: label });
    const description = makeElement("dd", { textContent: value === "" || value === undefined || value === null ? "-" : value });
    if (actionNode) {
      // 值旁的行内操作(如复制按钮);dd 变为行内 flex 容纳文本 + 操作。
      description.classList.add("workspace-status-value-with-action");
      description.append(actionNode);
    }
    row.append(term, description);
    return row;
  });
  target.replaceChildren(...rows);
}

function aliyunCloudSummary(payload) {
  return {
    configured: Boolean(payload?.configured),
    mode: payload?.mode || null,
    region: payload?.region || null,
    expiration: payload?.expiration ?? null,
    oauthSiteType: payload?.oauthSiteType || null,
    oauthAccessTokenExpire: payload?.oauthAccessTokenExpire ?? null,
    oauthRefreshTokenExpire: payload?.oauthRefreshTokenExpire ?? null,
    stsExpiration: payload?.stsExpiration ?? null,
  };
}

// 把秒级 Unix 时间戳格式化为本地时区可读时间;无效/空值返回 ""。
function formatLocalEpoch(epochSeconds) {
  const seconds = Number(epochSeconds);
  if (!seconds || !Number.isFinite(seconds)) {
    return "";
  }
  try {
    return new Date(seconds * 1000).toLocaleString();
  } catch (_error) {
    return "";
  }
}

function isSessionCurrent(context, requestedSessionId) {
  return requestedSessionId && context.sessionId() === requestedSessionId;
}

function createStatusPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel",
    attributes: { "data-workspace-panel": "status" },
  });
  const heading = makeElement("h3", { textContent: t("Status") });
  const board = makeElement("div", { className: "workspace-status-board" });
  const boardTitle = makeElement("strong", { className: "workspace-status-title", textContent: t("Session ready") });
  const boardMeta = makeElement("span", { className: "workspace-status-meta", textContent: t("normal / idle") });
  const boardBadge = makeElement("span", { className: "workspace-status-badge", textContent: "idle" });
  board.append(boardTitle, boardMeta, boardBadge);
  const summary = makeElement("dl", { className: "workspace-list workspace-status-summary" });
  const actions = makeElement("div", { className: "workspace-action-row" });
  const resultDetails = makeElement("details", { className: "workspace-status-details" });
  const resultSummary = makeElement("summary", { textContent: t("Response detail") });
  const result = makeElement("pre", { className: "workspace-result", textContent: t("Choose an action.") });
  resultDetails.append(resultSummary, result);
  let requestToken = 0;

  // 会话 ID 复制按钮:常驻节点(每次 render 复用同一实例,复位定时器不受重建影响),
  // 由 render() 更新它要复制的目标 ID。
  let statusSessionId = "";
  let copyResetTimer = null;
  const copyIdButton = makeElement("button", {
    className: "workspace-status-copy",
    attributes: { type: "button", "aria-label": t("Copy session ID"), title: t("Copy session ID") },
  });
  copyIdButton.append(makeElement("span", { className: "workspace-status-copy-icon", attributes: { "aria-hidden": "true" } }));
  copyIdButton.addEventListener("click", async () => {
    if (!statusSessionId) {
      return;
    }
    const ok = await copyTextToClipboard(statusSessionId);
    copyIdButton.dataset.copied = ok ? "yes" : "no";
    copyIdButton.setAttribute("aria-label", ok ? t("Session ID copied") : t("Copy failed"));
    copyIdButton.title = ok ? t("Session ID copied") : t("Copy failed");
    if (copyResetTimer !== null) {
      clearTimeout(copyResetTimer);
    }
    copyResetTimer = setTimeout(() => {
      delete copyIdButton.dataset.copied;
      copyIdButton.setAttribute("aria-label", t("Copy session ID"));
      copyIdButton.title = t("Copy session ID");
      copyResetTimer = null;
    }, 1600);
  });

  const run = async (label, action) => {
    const requestedSessionId = context.sessionId();
    if (!requestedSessionId) {
      setOutput(result, { error: t("No active session.") });
      return;
    }
    const token = ++requestToken;
    setMessage(result, `${label}...`);
    try {
      const payload = await action(requestedSessionId);
      if (!isSessionCurrent(context, requestedSessionId) || token !== requestToken) {
        return;
      }
      setOutput(result, payload);
    } catch (error) {
      if (!isSessionCurrent(context, requestedSessionId) || token !== requestToken) {
        return;
      }
      setOutput(result, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  const statusButton = makeButton(t("Session Status"), "workspace-status-load");
  statusButton.addEventListener("click", () => run(t("Loading status"), api.getSessionStatus));
  const debugButton = makeButton(t("Debug"), "workspace-debug-load");
  debugButton.addEventListener("click", () => run(t("Loading debug"), api.getSessionDebug));
  const promptButton = makeButton(t("Prompt"), "workspace-prompt-load");
  promptButton.addEventListener("click", () => run(t("Loading prompt"), api.getSessionPrompt));
  const compactButton = makeButton(t("Compact"), "workspace-compact-run", "workspace-action workspace-action-primary");
  compactButton.addEventListener("click", () => run(t("Compacting"), api.compactSession));

  actions.append(statusButton, debugButton, promptButton, compactButton);
  panel.append(heading, board, summary, actions, resultDetails);

  return {
    panel,
    render(state) {
      const session = state?.currentSession || context.session();
      const status = session?.status || "idle";
      const mode = session?.mode || "normal";
      const sessionId = displaySessionId(session) || context.sessionId();
      boardTitle.textContent = status === "running" ? t("Session working") : t("Session ready");
      boardMeta.textContent = `${mode} / ${sessionId || t("no session")}`;
      boardBadge.textContent = status;
      boardBadge.dataset.status = status;
      statusSessionId = sessionId || "";
      copyIdButton.disabled = !statusSessionId;
      renderSummaryList(summary, [
        [t("Session"), sessionId, copyIdButton],
        [t("Mode"), mode],
        [t("Status"), status],
        [t("Debug"), session?.debugEnabled === true ? t("enabled") : t("off")],
      ]);
    },
    showResult(payload) {
      setOutput(result, payload);
      resultSummary.textContent = t("Response detail updated");
    },
    reset() {
      requestToken += 1;
    },
  };
}

function createModelPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-model-panel",
    attributes: { "data-workspace-panel": "model" },
  });
  const heading = makeElement("h3", { textContent: t("Models") });

  const groupNav = makeElement("nav", { className: "workspace-model-groups", attributes: { "aria-label": t("Provider groups") } });
  const providerNav = makeElement("nav", { className: "workspace-provider-nav", attributes: { "aria-label": t("Model providers") } });
  const form = makeElement("div", { className: "workspace-provider-form" });
  const layout = appendChildren(makeElement("div", { className: "workspace-model-layout" }), [groupNav, providerNav, form]);

  const modelChoice = makeChoiceField("workspace-model-model", t("Enter or select a model"));
  const effortChoice = makeChoiceField("workspace-model-effort", t("Enter or select reasoning effort"));
  const apiBaseInput = makeTextInput("workspace-model-api-base", "https://example.test/v1");
  const apiKeyInput = makePasswordInput("workspace-model-api-key", t("Enter API key"));
  const apiKeyField = withPasswordToggle(apiKeyInput);
  // 高级设置(默认折叠):最大输出 tokens(全模型生效)、思考预算(仅支持独立预算的模型可见)。
  const maxTokensInput = makeNumberInput("workspace-model-max-tokens", t("Leave blank to use model default"));
  const thinkingBudgetInput = makeNumberInput("workspace-model-thinking-budget", t("Leave blank to use model default"));
  const maxTokensField = makeField(t("Max output tokens"), maxTokensInput, t("Max output tokens per reply; leave blank to use model default"));
  const thinkingBudgetField = makeField(t("Thinking budget"), thinkingBudgetInput, t("Token budget allocated to the thinking process; leave blank to use model default"));
  const advancedToggle = makeElement("button", {
    className: "workspace-advanced-toggle",
    attributes: { type: "button", "aria-expanded": "false" },
  });
  advancedToggle.append(
    makeElement("span", { className: "workspace-advanced-chevron", attributes: { "aria-hidden": "true" } }),
    makeElement("span", { className: "workspace-advanced-title", textContent: t("Advanced settings") }),
  );
  const advancedBody = appendChildren(makeElement("div", { className: "workspace-advanced-body" }), [
    maxTokensField,
    thinkingBudgetField,
  ]);
  advancedBody.hidden = true;
  const advancedSection = appendChildren(makeElement("div", { className: "workspace-advanced" }), [
    advancedToggle,
    advancedBody,
  ]);
  advancedToggle.addEventListener("click", () => {
    const expanded = advancedToggle.getAttribute("aria-expanded") === "true";
    advancedToggle.setAttribute("aria-expanded", String(!expanded));
    advancedBody.hidden = expanded;
  });
  const actions = makeElement("div", { className: "workspace-provider-form-footer" });
  const saveButton = makeButton(t("Save configuration"), "workspace-model-save", "workspace-action workspace-action-primary");
  const activateButton = makeButton(t("Set as current model"), "workspace-model-activate");
  const clearButton = makeButton(t("Clear configuration"), "workspace-model-clear", "workspace-action workspace-action-danger");
  const result = makeElement("pre", { className: "workspace-result", textContent: t("Select a provider on the left to view and edit its configuration.") });

  const formTitle = makeElement("h4", { className: "workspace-settings-group-title", textContent: "" });
  // 状态徽章:顶部只读摘要中唯一非重复的信息(当前模型/已配置/未配置),收进标题旁。
  // 模型/推理强度/API 密钥不再单列摘要——它们与下方可编辑字段逐条重复。
  const statusBadge = makeElement("span", { className: "workspace-provider-status-badge" });
  statusBadge.hidden = true;
  const titleRow = appendChildren(makeElement("div", { className: "workspace-provider-title-row" }), [formTitle, statusBadge]);
  const formDesc = makeElement("p", { className: "workspace-settings-group-desc", textContent: "" });
  const head = appendChildren(makeElement("div", { className: "workspace-settings-group-head" }), [titleRow, formDesc]);
  const partnerNote = makeElement("p", { className: "workspace-provider-partner-note", textContent: "" });
  partnerNote.hidden = true;
  const fields = appendChildren(makeElement("section", { className: "workspace-settings-group workspace-settings-provider" }), [
    makeField(t("Models"), modelChoice.wrap, t("The model used for generation under this provider")),
    makeField(t("Reasoning effort"), effortChoice.wrap, t("How deeply the model thinks; higher is slower but more detailed")),
    makeField(t("API base URL"), apiBaseInput, t("Custom API Base URL; leave blank to use default")),
    makeField(t("API key"), apiKeyField.wrap, t("The key required to call this provider; stored locally only")),
    advancedSection,
    result,
  ]);
  // 按钮作为卡片 footer(直接挂在卡片上),用 margin-top:auto 贴住卡片底部。
  // 清空配置置于最左(danger),保存/设为当前靠右。
  actions.append(clearButton, saveButton, activateButton);
  appendChildren(form, [head, partnerNote, fields, actions]);
  // datalist 自身不占布局,挂在卡片下以持久存在(组合框手动输入的建议来源)。
  form.append(modelChoice.list, effortChoice.list);
  panel.append(heading, layout);

  let providersPayload = null;
  let selectedGroup = "";
  let selectedKey = "";
  let requestToken = 0;

  const providerByKey = (key) => providerItems(providersPayload).find((item) => item.key === key) || null;
  const selectedModelItem = () =>
    modelItems(providerByKey(selectedKey)).find((item) => item.id === modelChoice.value) || null;

  const fillEfforts = (activeEffort = "") => {
    // 有已知推理强度规格→下拉全量;无规格(手动模型/兼容模式)→退回自由输入组合框。
    effortChoice.setOptions(
      effortItems(selectedModelItem()).map((effort) => ({ value: effort })),
      { current: activeEffort },
    );
    effortChoice.value = activeEffort || "";
  };

  // 高级设置回填 + 能力门控:最大输出 tokens 恒显示;思考预算仅在模型 supportsThinkingBudget 时显示。
  const fillAdvanced = () => {
    const model = selectedModelItem();
    // 两个旋钮按模型存储(providers.<key>.models.<id>),故按选中模型回填,而非 provider 级。
    maxTokensInput.value = model?.savedMaxCompletionTokens ?? "";
    thinkingBudgetInput.value = model?.savedThinkingBudget ?? "";
    const defaultMax = model?.defaultMaxCompletionTokens;
    maxTokensInput.placeholder = defaultMax ? t("Leave blank to use model default ({value})", { value: defaultMax }) : t("Leave blank to use model default");
    const supportsBudget = Boolean(model?.supportsThinkingBudget);
    thinkingBudgetField.hidden = !supportsBudget;
    const defaultBudget = model?.defaultThinkingBudget;
    thinkingBudgetInput.placeholder =
      supportsBudget && defaultBudget ? t("Leave blank to use model default ({value})", { value: defaultBudget }) : t("Leave blank to use model default");
  };

  // 服务商按 group 分组;空 group 归入「其他」。分组顺序沿用后端返回的 provider 顺序
  // (即 registry 的 PROVIDER_GROUPS 顺序),第 1 栏据此渲染。
  const GROUP_FALLBACK = t("Other");
  const orderedGroups = () => {
    const groups = new Map();
    for (const provider of providerItems(providersPayload)) {
      const label = provider.group || GROUP_FALLBACK;
      if (!groups.has(label)) {
        groups.set(label, { label, providers: [] });
      }
      groups.get(label).providers.push(provider);
    }
    return [...groups.values()];
  };
  const groupLabelOf = (key) => {
    const provider = providerByKey(key);
    return provider ? provider.group || GROUP_FALLBACK : "";
  };
  const providersInSelectedGroup = () =>
    orderedGroups().find((group) => group.label === selectedGroup)?.providers || [];

  // 第 1 栏:分组列表。含当前模型的组标「当前」。
  const renderGroups = () => {
    const activeKey = providersPayload?.active?.provider || "";
    const nodes = orderedGroups().map((group) => {
      const button = makeElement("button", {
        className: "workspace-model-group-item",
        attributes: { type: "button" },
        dataset: { groupLabel: group.label },
      });
      const label = makeElement("span", { className: "workspace-model-group-label", textContent: group.label });
      button.append(label);
      const hasCurrent = group.providers.some((provider) => provider.key === activeKey || provider.current === true);
      if (hasCurrent) {
        button.append(makeElement("span", { className: "workspace-provider-nav-current", textContent: t("Current") }));
      }
      button.classList.toggle("is-selected", group.label === selectedGroup);
      button.addEventListener("click", () => selectGroup(group.label));
      return button;
    });
    groupNav.replaceChildren(...nodes);
  };

  // 第 2 栏:仅当前选中组内的 provider(短列表,选中行右缘融入第 3 栏配置卡片)。
  const renderProviders = () => {
    const activeKey = providersPayload?.active?.provider || "";
    const nodes = providersInSelectedGroup().map((provider) => {
      const button = makeElement("button", {
        className: "workspace-provider-nav-item",
        textContent: "",
        attributes: { type: "button" },
        dataset: { providerKey: provider.key },
      });
      const label = makeElement("span", { className: "workspace-provider-nav-label", textContent: provider.name || provider.key });
      const status = makeElement("span", {
        className: provider.usable ? "workspace-provider-nav-status is-usable" : "workspace-provider-nav-status",
        attributes: { "aria-hidden": "true", title: provider.usable ? t("Available") : t("Unavailable") },
      });
      button.append(label, status);
      const isCurrent = provider.key === activeKey || provider.current === true;
      if (isCurrent) {
        button.classList.add("is-active");
        button.append(makeElement("span", { className: "workspace-provider-nav-current", textContent: t("Current") }));
      }
      button.classList.toggle("is-selected", provider.key === selectedKey);
      button.addEventListener("click", () => selectProvider(provider.key));
      return button;
    });
    providerNav.replaceChildren(...nodes);
    // 首个 provider 选中时,其顶部凹角(::before)会与卡片圆角左上角冲突,浮出一块。
    // 此时让卡片左上角变方、隐藏顶部凹角,使选中项顶边与卡片顶边齐平相接。
    const providers = providersInSelectedGroup();
    const firstSelected = providers.length > 0 && providers[0].key === selectedKey;
    layout.classList.toggle("is-first-provider-selected", firstSelected);
  };

  const renderForm = () => {
    const provider = providerByKey(selectedKey);
    // 徽章仅在可编辑服务商分支显示;空/只读来源分支保持隐藏。
    statusBadge.hidden = true;
    if (!provider) {
      formTitle.textContent = "";
      formDesc.textContent = "";
      partnerNote.hidden = true;
      fields.hidden = false;
      saveButton.hidden = false;
      activateButton.hidden = false;
      clearButton.hidden = true;
      return;
    }
    if (provider.kind === "partner" || provider.readOnly === true) {
      formTitle.textContent = provider.name || provider.key;
      formDesc.textContent = provider.note || t("Managed by external login");
      const detail = provider.providerLabel
        ? t("This source is managed by an external application login and cannot be edited here. Current provider: {provider}", { provider: provider.providerLabel })
        : t("This source is managed by an external application login and cannot be edited here.");
      partnerNote.textContent = detail;
      partnerNote.hidden = false;
      fields.hidden = true;
      // 只读来源不能保存配置,但可「设为当前模型」(切换到该第三方来源)。
      saveButton.hidden = true;
      activateButton.hidden = false;
      activateButton.disabled = provider.current === true;
      clearButton.hidden = true;
      return;
    }
    partnerNote.hidden = true;
    fields.hidden = false;
    saveButton.hidden = false;
    activateButton.hidden = false;
    clearButton.hidden = false;
    formTitle.textContent = provider.name || provider.key;
    formDesc.textContent = provider.displayName && provider.displayName !== provider.name ? provider.displayName : t("Configure this provider's model and credentials.");
    const savedModel = provider.savedModel || provider.defaultModel || "";
    modelChoice.setOptions(
      modelItems(provider).map((model) => ({
        value: model.id,
        label: model.name && model.name !== model.id ? model.name : "",
      })),
      { current: savedModel },
    );
    modelChoice.value = savedModel;
    fillEfforts(provider.savedEffort || "");
    fillAdvanced();
    apiBaseInput.value = provider.savedApiBase || "";
    apiKeyInput.value = provider.savedApiKey || "";
    apiKeyField.conceal();
    apiBaseInput.placeholder = provider.apiBase || "https://example.test/v1";
    const isActive = providersPayload?.active?.provider === provider.key;
    activateButton.disabled = !provider.savedModel;
    // 清空:当前模型不可清空(须先切换);无任何已存配置时也无需清空。
    const hasSavedConfig = Boolean(
      provider.savedModel ||
        provider.hasApiKey ||
        provider.savedApiBase ||
        provider.savedEffort ||
        provider.savedMaxCompletionTokens ||
        provider.savedThinkingBudget,
    );
    clearButton.disabled = isActive || !hasSavedConfig;
    clearButton.title = isActive ? t("The current model cannot be cleared; switch to another model first") : "";
    // 状态徽章:当前模型(绿)/ 已配置(中性)/ 未配置(淡)。data-state 供 CSS 上色。
    const state = isActive ? "active" : provider.savedModel ? "saved" : "unset";
    statusBadge.textContent = isActive ? t("Current model") : provider.savedModel ? t("Configured") : t("Not configured");
    statusBadge.dataset.state = state;
    statusBadge.hidden = false;
  };

  const renderAll = () => {
    renderGroups();
    renderProviders();
    renderForm();
  };

  const selectProvider = (key) => {
    selectedKey = key;
    selectedGroup = groupLabelOf(key) || selectedGroup;
    renderAll();
  };

  // 点组:选中该组,并自动选中组内的当前模型;没有当前模型则选第一个,配置栏永不空。
  const selectGroup = (label) => {
    selectedGroup = label;
    const providers = providersInSelectedGroup();
    const activeKey = providersPayload?.active?.provider || "";
    const preferred = providers.find((provider) => provider.key === activeKey || provider.current === true) || providers[0];
    selectedKey = preferred ? preferred.key : "";
    renderAll();
  };

  const applyPayload = (payload, { keepSelection = true } = {}) => {
    providersPayload = payload || {};
    const providers = providerItems(providersPayload);
    if (!keepSelection || !providerByKey(selectedKey)) {
      // 打开落在当前模型;无当前模型则取第一个 provider。
      const activeKey = providersPayload?.active?.provider || "";
      const initial = providers.find((provider) => provider.key === activeKey) || providers[0] || null;
      selectedKey = initial ? initial.key : "";
    }
    selectedGroup = groupLabelOf(selectedKey) || orderedGroups()[0]?.label || "";
    renderAll();
  };

  const loadProviders = async () => {
    const token = ++requestToken;
    setMessage(result, t("Loading model configuration…"));
    try {
      const payload = await api.getProviders();
      if (token !== requestToken) return;
      applyPayload(payload, { keepSelection: false });
      setMessage(result, t("Select a provider on the left to view and edit its configuration."));
    } catch (error) {
      if (token !== requestToken) return;
      setOutput(result, { error: error instanceof Error ? error.message : String(error) });
    }
  };

  modelChoice.onChange(() => {
    fillEfforts("");
    fillAdvanced();
  });

  saveButton.addEventListener("click", async () => {
    if (!selectedKey) return;
    const token = ++requestToken;
    // 空 → null(清除回落默认);非法/非正数也当 null;正整数照原样提交。
    const parsePositiveInt = (raw) => {
      const text = String(raw ?? "").trim();
      if (text === "") return null;
      const value = Number.parseInt(text, 10);
      return Number.isNaN(value) || value <= 0 ? null : value;
    };
    const payloadToSave = {
      provider: selectedKey,
      model: modelChoice.value,
      effort: effortChoice.value,
      apiBase: apiBaseInput.value.trim(),
      apiKey: apiKeyInput.value,
      // 最大输出 tokens 对所有模型生效,始终提交(int 或 null)。
      maxCompletionTokens: parsePositiveInt(maxTokensInput.value),
    };
    // 思考预算仅在字段可见(模型支持独立预算)时提交,避免给不支持的模型写入。
    if (!thinkingBudgetField.hidden) {
      payloadToSave.thinkingBudget = parsePositiveInt(thinkingBudgetInput.value);
    }
    setMessage(result, t("Saving configuration…"));
    try {
      const payload = await api.saveProviderConfig(payloadToSave);
      if (token !== requestToken) return;
      applyPayload(payload);
      setMessage(result, t("Configuration saved."));
    } catch (error) {
      if (token !== requestToken) return;
      setOutput(result, { error: error instanceof Error ? error.message : String(error) });
    }
  });

  activateButton.addEventListener("click", async () => {
    if (!selectedKey) return;
    const token = ++requestToken;
    setMessage(result, t("Setting as current model…"));
    try {
      await api.setActiveProvider(selectedKey);
      if (token !== requestToken) return;
      // 重新拉取整表:第三方来源的 current 标记来自后端 llm_source,仅更新 active
      // 无法刷新它;整表刷新可让普通/第三方两种「当前」标记都正确。
      const payload = await api.getProviders();
      if (token !== requestToken) return;
      applyPayload(payload);
      setMessage(result, t("Set as current model."));
    } catch (error) {
      if (token !== requestToken) return;
      setOutput(result, { error: error instanceof Error ? error.message : String(error) });
    }
  });

  clearButton.addEventListener("click", async () => {
    if (!selectedKey) return;
    const token = ++requestToken;
    setMessage(result, t("Clearing configuration…"));
    try {
      const payload = await api.clearProviderConfig(selectedKey);
      if (token !== requestToken) return;
      applyPayload(payload);
      setMessage(result, t("Configuration cleared."));
    } catch (error) {
      if (token !== requestToken) return;
      setOutput(result, { error: error instanceof Error ? error.message : String(error) });
    }
  });

  return {
    panel,
    activate() {
      if (!providersPayload) {
        loadProviders();
      }
    },
    reset() {
      requestToken += 1;
      providersPayload = null;
      selectedGroup = "";
      selectedKey = "";
    },
  };
}

function createCloudPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-cloud-panel",
    attributes: { "data-workspace-panel": "cloud" },
  });
  const heading = makeElement("h3", { textContent: t("Cloud credentials") });

  // 左:云厂商导航
  const vendorNav = makeElement("nav", {
    className: "workspace-provider-nav workspace-cloud-vendors",
    attributes: { "aria-label": t("Cloud provider") },
  });

  // 右:表单
  const form = makeElement("div", { className: "workspace-provider-form" });
  const layout = makeElement("div", { className: "workspace-cloud-layout" });
  layout.append(vendorNav, form);

  // 地域:强制组合框——给出完整 ECS 地域候选,同时允许手填未列出的地域。
  const regionChoice = makeChoiceField("workspace-cloud-region", t("Select or enter a region, e.g. cn-hangzhou"), { forceFree: true });
  const regionFields = makeElement("div", { className: "workspace-cloud-region-fields" });

  // 认证方式
  const cloudModeSelect = makeSelect("workspace-cloud-mode");
  for (const mode of CREDENTIAL_MODE_ORDER) {
    appendOption(cloudModeSelect, mode, CLOUD_MODE_LABELS[mode] || mode);
  }
  // 「当前已保存哪种认证方式」提示,回填时更新文本。
  const cloudModeHint = makeElement("span", { className: "workspace-field-hint", dataset: { workspaceAction: "workspace-cloud-mode-hint" }, textContent: "" });
  const modeFields = makeElement("div", { className: "workspace-cloud-mode-fields" });

  // 持久输入(AK/Sts/Ram)
  const cloudAccessKeyIdInput = makeTextInput("workspace-cloud-access-key-id", "AccessKeyId");
  const cloudAccessKeySecretInput = makePasswordInput("workspace-cloud-access-key-secret", "AccessKeySecret");
  const cloudAccessKeySecretField = withPasswordToggle(cloudAccessKeySecretInput);
  const cloudStsTokenInput = makePasswordInput("workspace-cloud-sts-token", t("STS token"));
  const cloudStsTokenField = withPasswordToggle(cloudStsTokenInput);
  const cloudRamRoleArnInput = makeTextInput("workspace-cloud-ram-role-arn", "acs:ram::123:role/name");
  const cloudRamSessionNameInput = makeTextInput("workspace-cloud-ram-session-name", t("Session name"));

  // OAuth:站点 + 浏览器登录(无 token 输入)
  const cloudOauthSiteSelect = makeSelect("workspace-cloud-oauth-site");
  for (const [value, label] of [["CN", t("China (aliyun.com)")], ["INTL", t("International (alibabacloud.com)")]]) {
    appendOption(cloudOauthSiteSelect, value, label);
  }
  const oauthLoginButton = makeButton(t("Log in with browser"), "workspace-cloud-oauth-login", "workspace-action workspace-action-primary");
  const oauthCancelButton = makeButton(t("Cancel"), "workspace-cloud-oauth-cancel");
  oauthCancelButton.hidden = true;
  const oauthStatus = makeElement("p", { className: "workspace-cloud-oauth-status", textContent: "" });

  const saveCloudButton = makeButton(t("Save cloud credentials"), "workspace-cloud-save", "workspace-action workspace-action-primary");
  const actions = makeElement("div", { className: "workspace-provider-form-footer" });
  actions.append(saveCloudButton);

  // result 仅承载加载/保存/出错的状态提示,成功后隐藏——不再往这里堆一坨凭证摘要 JSON。
  const result = makeElement("pre", { className: "workspace-result" });
  result.hidden = true;
  const showCloudStatus = (message) => {
    result.hidden = false;
    setMessage(result, message);
  };
  const showCloudError = (error) => {
    result.hidden = false;
    setOutput(result, { error: error instanceof Error ? error.message : String(error) });
  };
  const hideCloudResult = () => {
    result.hidden = true;
    result.textContent = "";
  };

  let requestToken = 0;
  let loaded = false;
  let lastDetected = null;
  let lastOauth = { access: null, refresh: null, sts: null };
  // 已保存凭证快照(来自后端回传的原始值):供输入框预填,以及「切换认证方式」时
  // 按目标模式重置/回填。mode 为空表示尚未保存任何云凭证。
  let savedCloud = null;

  const sourceLabel = (source) => CLOUD_SOURCE_LABELS[source] || source || "";

  const hintFor = (field, { secret = false } = {}) => {
    const d = lastDetected;
    if (!d || !d.source) return "";
    // 配置文件来源的凭证已按模式预填进输入框(可用眼睛查看),再显示 hint 会重复;
    // 仅对 env/cli 等「未落盘」的环境凭证保留提示。
    if (d.source === "config") return "";
    // 按鉴权模式隔离:配置文件里 access_key_id/secret/sts_token 字段被各模式复用,
    // 例如 OAuth 运行时会派生出 STS.* 的 AccessKeyId。若不按当前模式过滤,
    // 这些派生值会串味到 AK/StsToken 表单里,造成不同鉴权类型混在一起。
    const currentMode = selectedValue(cloudModeSelect) || "AK";
    if (d.mode && d.mode !== currentMode) return "";
    const shown = secret ? (d[field] ? t("Configured") : "") : (d[field] || "");
    if (!shown) return "";
    return t("Read from {source} · {shown}", { source: sourceLabel(d.source), shown });
  };

  const renderRegionField = () => {
    const d = lastDetected;
    const hint = d && d.source && d.region ? t("Read from {source} · {shown}", { source: sourceLabel(d.source), shown: d.region }) : "";
    regionFields.replaceChildren(
      makeField(t("Region"), regionChoice.wrap, t("Region for deployment and resource queries"), hint)
    );
  };

  const renderModeFields = () => {
    const mode = selectedValue(cloudModeSelect) || "AK";
    const rows = [];
    if (mode === "AK" || mode === "StsToken" || mode === "RamRoleArn") {
      rows.push(makeField(t("AccessKeyId"), cloudAccessKeyIdInput, t("Alibaba Cloud access key ID"), hintFor("accessKeyId")));
      rows.push(makeField(t("AccessKeySecret"), cloudAccessKeySecretField.wrap, t("Access key secret; stored locally only"), hintFor("hasAccessKeySecret", { secret: true })));
    }
    if (mode === "StsToken") {
      rows.push(makeField(t("StsToken"), cloudStsTokenField.wrap, t("Temporary security token"), hintFor("hasStsToken", { secret: true })));
    }
    if (mode === "RamRoleArn") {
      rows.push(makeField(t("RamRoleArn"), cloudRamRoleArnInput, t("The RAM role ARN to assume"), hintFor("ramRoleArn")));
      rows.push(makeField(t("RamSessionName"), cloudRamSessionNameInput, t("Role session name (optional)"), hintFor("ramSessionName")));
    }
    if (mode === "OAuth") {
      rows.push(makeField(t("Login site"), cloudOauthSiteSelect, t("Select the site your Alibaba Cloud account belongs to")));
      const oauthRow = makeElement("div", { className: "workspace-field workspace-cloud-oauth-row" });
      oauthRow.append(oauthLoginButton, oauthCancelButton, oauthStatus);
      rows.push(oauthRow);
      // 登录成功后展示令牌过期时间(本地时区);未登录时不显示。
      const accessText = formatLocalEpoch(lastOauth.access);
      const refreshText = formatLocalEpoch(lastOauth.refresh);
      if (accessText) {
        rows.push(
          makeField(
            t("Access token expiry"),
            makeElement("span", { className: "workspace-field-value", textContent: accessText }),
            t("Current access token expiry time (local time zone)")
          )
        );
        // 已登录即展示刷新令牌一行;阿里云 OAuth 响应不返回 refresh_expires_in,
        // 此时磁盘上过期时间为 0,故给出诚实兜底文案而非隐藏整行。
        rows.push(
          makeField(
            t("Refresh token expiry"),
            makeElement("span", {
              className: "workspace-field-value",
              textContent: refreshText || t("Unknown (Alibaba Cloud did not provide an expiry)"),
            }),
            t("Refresh token expiry time (local time zone); re-login required after expiry")
          )
        );
        // OAuth 登录会派生出 STS 临时凭证(实际调用云 API 用),其到期时间独立于两个令牌。
        // 尚未派生(如刚登录未换取)时磁盘上为 0,给出诚实兜底文案而非隐藏整行。
        const stsText = formatLocalEpoch(lastOauth.sts);
        rows.push(
          makeField(
            t("STS expiry"),
            makeElement("span", {
              className: "workspace-field-value",
              textContent: stsText || t("Unknown (STS credentials not yet obtained)"),
            }),
            t("Expiry time of the STS temporary credentials derived from OAuth (local time zone)")
          )
        );
      }
    }
    // OAuth 登录成功即自动持久化,无需「加载/保存」——隐藏底部按钮避免误解。
    actions.hidden = mode === "OAuth";
    modeFields.replaceChildren(...rows);
  };

  const renderVendors = (configured) => {
    const nodes = CLOUD_VENDORS.map((vendor, index) => {
      const button = makeElement("button", {
        className: "workspace-provider-nav-item",
        attributes: { type: "button" },
        dataset: { cloudVendor: vendor.id },
      });
      button.append(makeElement("span", { className: "workspace-provider-nav-label", textContent: vendor.label }));
      button.append(makeElement("span", {
        className: configured ? "workspace-provider-nav-status is-usable" : "workspace-provider-nav-status",
        attributes: { "aria-hidden": "true", title: configured ? t("Configured") : t("Not configured") },
      }));
      if (index === 0) {
        button.classList.toggle("is-selected", true);
        button.classList.toggle("is-active", true);
        button.append(makeElement("span", { className: "workspace-provider-nav-current", textContent: t("Current") }));
      }
      return button;
    });
    vendorNav.replaceChildren(...nodes);
  };

  // 按目标认证方式回填/重置凭证输入框:
  // - 目标模式 == 已保存模式:回填已保存的原始值(像模型 API Key 一样,可用眼睛查看);
  // - 否则:清空全部凭证输入(切换认证方式时不让上一模式的 AccessKey 等残留)。
  // 密钥输入回填后始终先隐藏,避免明文默认可见。
  const applyCloudInputs = (mode) => {
    const match = Boolean(savedCloud && savedCloud.mode && savedCloud.mode === mode);
    cloudAccessKeyIdInput.value = match ? savedCloud.accessKeyId || "" : "";
    cloudAccessKeySecretInput.value = match ? savedCloud.accessKeySecret || "" : "";
    cloudStsTokenInput.value = match ? savedCloud.stsToken || "" : "";
    cloudRamRoleArnInput.value = match ? savedCloud.ramRoleArn || "" : "";
    cloudRamSessionNameInput.value = match ? savedCloud.ramSessionName || "" : "";
    cloudAccessKeySecretField.conceal();
    cloudStsTokenField.conceal();
  };

  // 「当前已保存哪种认证方式」提示,展示在认证方式选择框旁。
  const renderModeHint = () => {
    const saved = savedCloud && savedCloud.mode;
    cloudModeHint.textContent = saved
      ? t("Currently saved: {mode}", { mode: CLOUD_MODE_LABELS[saved] || saved })
      : t("No cloud credentials saved yet");
  };

  const cloudPayloadFromForm = () => {
    const mode = selectedValue(cloudModeSelect);
    const usesAccessKey = mode === "AK" || mode === "StsToken" || mode === "RamRoleArn";
    return {
      mode,
      region: regionChoice.value.trim(),
      accessKeyId: usesAccessKey ? cloudAccessKeyIdInput.value.trim() : "",
      accessKeySecret: usesAccessKey ? cloudAccessKeySecretInput.value : "",
      stsToken: mode === "StsToken" ? cloudStsTokenInput.value : "",
      // StsExpiration 由后端自动派生,前端不再让用户填写;保留键以维持 payload 形状。
      stsExpiration: "",
      ramRoleArn: mode === "RamRoleArn" ? cloudRamRoleArnInput.value.trim() : "",
      ramSessionName: mode === "RamRoleArn" ? cloudRamSessionNameInput.value.trim() : "",
      oauthSiteType: mode === "OAuth" ? selectedValue(cloudOauthSiteSelect) : "",
      oauthAccessToken: "",
      oauthRefreshToken: "",
      oauthAccessTokenExpire: "",
      oauthRefreshTokenExpire: "",
    };
  };

  const fillCloudForm = (payload) => {
    const summaryPayload = aliyunCloudSummary(payload);
    lastDetected = (payload && typeof payload === "object" && payload.detected) || null;
    lastOauth = {
      access: summaryPayload.oauthAccessTokenExpire,
      refresh: summaryPayload.oauthRefreshTokenExpire,
      sts: summaryPayload.stsExpiration,
    };
    // 后端回传的已保存原始凭证值(仅来自本地 .cloud-credentials.yml,Web 仅监听 127.0.0.1),
    // 存快照供输入框预填与切换认证方式时重置/回填——注意不写入下方 result 摘要,避免明文外泄。
    savedCloud = {
      mode: summaryPayload.mode || "",
      accessKeyId: (payload && payload.accessKeyId) || "",
      accessKeySecret: (payload && payload.accessKeySecret) || "",
      stsToken: (payload && payload.stsToken) || "",
      ramRoleArn: (payload && payload.ramRoleArn) || "",
      ramSessionName: (payload && payload.ramSessionName) || "",
    };
    cloudModeSelect.value = summaryPayload.mode || "AK";
    // 回填已保存的 OAuth 登录站点(CN/INTL),否则站点选择框永远停在默认值。
    if (summaryPayload.oauthSiteType) cloudOauthSiteSelect.value = summaryPayload.oauthSiteType;
    regionChoice.setOptions(
      ALIYUN_REGIONS.map((r) => ({ value: r.id, label: `${r.label} · ${r.id}` })),
      { current: summaryPayload.region || "" }
    );
    regionChoice.value = summaryPayload.region || "";
    renderVendors(Boolean(summaryPayload.configured));
    renderRegionField();
    applyCloudInputs(selectedValue(cloudModeSelect) || "AK");
    renderModeHint();
    renderModeFields();
    hideCloudResult();
  };

  const loadCloud = async () => {
    const token = ++requestToken;
    showCloudStatus(t("Loading cloud credentials…"));
    try {
      const payload = await api.getAliyunCloud();
      if (token !== requestToken) return;
      loaded = true;
      fillCloudForm(payload);
    } catch (error) {
      if (token !== requestToken) return;
      showCloudError(error);
    }
  };

  // OAuth 等待中的 UI 复位:恢复登录按钮、隐藏取消按钮。
  const resetOauthUi = () => {
    oauthLoginButton.disabled = false;
    oauthCancelButton.hidden = true;
  };
  let oauthAbortController = null;

  cloudModeSelect.addEventListener("change", () => {
    // 若正在等待 OAuth(取消按钮可见),切换认证方式即视为放弃本次登录。
    if (!oauthCancelButton.hidden) {
      oauthAbortController?.abort();
      oauthAbortController = null;
      requestToken++;
      resetOauthUi();
    }
    // 切换认证方式时重置输入:回到已保存模式则回填,切到其它模式则清空,
    // 避免上一模式的 AccessKey 等内容残留串味。
    applyCloudInputs(selectedValue(cloudModeSelect) || "AK");
    renderModeFields();
  });

  saveCloudButton.addEventListener("click", async () => {
    const token = ++requestToken;
    const payloadToSave = cloudPayloadFromForm();
    showCloudStatus(t("Saving cloud credentials…"));
    try {
      const payload = await api.saveAliyunCloud(payloadToSave);
      if (token !== requestToken) return;
      fillCloudForm(payload);
    } catch (error) {
      if (token !== requestToken) return;
      showCloudError(error);
    }
  });

  oauthLoginButton.addEventListener("click", async () => {
    const token = ++requestToken;
    const abortController = new AbortController();
    oauthAbortController = abortController;
    const site = selectedValue(cloudOauthSiteSelect) || "CN";
    oauthLoginButton.disabled = true;
    oauthCancelButton.hidden = false;
    oauthStatus.textContent = t("Opening the browser; please complete login on the Alibaba Cloud page… (if the browser did not open or was closed, click Cancel)");
    try {
      const payload = await api.oauthLoginAliyun({
        site,
        region: regionChoice.value.trim(),
        signal: abortController.signal,
      });
      if (token !== requestToken) return;
      fillCloudForm(payload);
      oauthStatus.textContent = t("Login succeeded.");
    } catch (error) {
      if (token !== requestToken) return;
      oauthStatus.textContent = error?.name === "AbortError"
        ? t("OAuth login cancelled.")
        : t("Login failed: {error}", { error: error instanceof Error ? error.message : String(error) });
    } finally {
      // 仅在本次请求仍是最新时复位;被取消/切换认证方式作废的请求已各自复位。
      if (oauthAbortController === abortController) oauthAbortController = null;
      if (token === requestToken) resetOauthUi();
    }
  });

  oauthCancelButton.addEventListener("click", () => {
    // 作废进行中的登录请求,并关闭 token 模式的手工回填弹窗。
    oauthAbortController?.abort();
    oauthAbortController = null;
    requestToken++;
    resetOauthUi();
    oauthStatus.textContent = t("Login canceled. You can start again or switch to another authentication method.");
  });

  // 组装:分区标题/说明置于卡片外层(Codex 风),卡片内只保留行。
  const regionHead = makeElement("div", { className: "workspace-settings-group-head" });
  regionHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Region") }),
    makeElement("p", { className: "workspace-settings-group-desc", textContent: t("Select the Alibaba Cloud region for deployment and resource queries.") })
  );
  const regionCard = makeElement("section", { className: "workspace-settings-group workspace-cloud-region-card" });
  regionCard.append(regionFields);

  const credHead = makeElement("div", { className: "workspace-settings-group-head" });
  credHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Credentials") }),
    makeElement("p", { className: "workspace-settings-group-desc", textContent: t("Select the authentication method for accessing Alibaba Cloud; stored locally only.") })
  );
  const credCard = makeElement("section", { className: "workspace-settings-group workspace-settings-provider" });
  // 认证方式一行:说明子行下追加「当前已保存哪种认证方式」提示(由 renderModeHint 更新)。
  const cloudModeField = makeField(t("Authentication method"), cloudModeSelect, t("Different methods require different parameters; only relevant fields are shown"));
  cloudModeField.querySelector(".workspace-field-text")?.append(cloudModeHint);
  credCard.append(
    cloudModeField,
    modeFields,
    result
  );

  // 区域下拉菜单内嵌于 regionChoice.wrap(自定义常显下拉),无需像原生 datalist 单独挂载。
  form.append(regionHead, regionCard, credHead, credCard, actions);
  panel.append(heading, layout);

  renderVendors(false);
  renderRegionField();
  renderModeFields();

  return {
    panel,
    activate() {
      if (!loaded) loadCloud();
    },
    reset() {
      loaded = false;
    },
  };
}

// 记忆库条目类型 → 中文标签(配色由 .workspace-memory-type-<type> 提供)。
const MEMORY_TYPE_LABELS = { user: t("User"), feedback: t("Feedback"), project: t("Project"), reference: t("Reference") };
// 记忆库条目作用域 → 中文标签。带「记忆」后缀以示"存储位置"、与类型徽标里的「项目」区分开。
const MEMORY_SCOPE_LABELS = { global: t("Global memory"), project: t("Project memory") };

function createMemoryPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-memory-panel",
    attributes: { "data-workspace-panel": "memory" },
  });
  const heading = makeElement("h3", { textContent: t("Memory") });

  // ── 状态 ──────────────────────────────────────────────
  let loaded = false;
  let memoryRequestToken = 0;
  let legacyRequestToken = 0;
  let projectPath = "";
  let userPath = "";
  let emptyMessage = t("No memory entries yet");
  let searchTimer = null;
  // 项目记忆按项目切换:projects 为可选项目列表,selectedCwd 为当前所选项目路径。
  let projects = [];
  let selectedCwd = "";
  // 记忆库删除的二次确认:面板级记录当前处于「确认删除?」态的按钮,点击别处即复位。
  let armedDelete = null;

  const baseName = (value) => {
    const parts = text(value).split("/");
    return parts[parts.length - 1].split("\\").pop() || "";
  };
  const stamp = (target, message, isError = false) => {
    if (!target) return;
    target.textContent = text(message);
    target.classList.toggle("is-error", Boolean(isError));
  };

  // ── 常驻记忆(项目 / 用户)────────────────────────────
  const projectArea = makeTextarea("workspace-memory-project", t("Write project-level instructions to inject into every conversation turn; Markdown supported"));
  const userArea = makeTextarea("workspace-memory-user", t("Write personal preferences to inject across all projects; Markdown supported"));
  const projectStatus = makeElement("span", { className: "workspace-memory-status" });
  const userStatus = makeElement("span", { className: "workspace-memory-status" });
  const saveProjectButton = makeButton(t("Save"), "workspace-memory-save-project", "workspace-action workspace-action-primary");
  const saveUserButton = makeButton(t("Save"), "workspace-memory-save-user", "workspace-action workspace-action-primary");

  // 项目选择器:AGENTS.md 按项目区分,可在此切换目标项目。作为记忆面板顶部的全局选择器,
  // 提到「记忆」标题与「常驻记忆」之间,统辖下方项目级记忆的展示。
  const projectSelect = makeElement("select", {
    className: "workspace-select workspace-memory-project-picker",
    dataset: { workspaceAction: "workspace-memory-project-select" },
  });
  projectSelect.setAttribute("aria-label", t("Select project"));
  const projectPickerRow = makeElement("div", { className: "workspace-memory-project-row" });
  projectPickerRow.append(
    makeElement("span", { className: "workspace-memory-project-label", textContent: t("Selected project") }),
    projectSelect,
  );

  const makeNote = (titleText, badgeText, area, status, saveButton, headExtra = null) => {
    const note = makeElement("div", { className: "workspace-memory-note" });
    const head = makeElement("div", { className: "workspace-memory-note-head" });
    head.append(
      makeElement("strong", { className: "workspace-memory-note-title", textContent: titleText }),
      makeElement("span", { className: "workspace-memory-note-badge", textContent: badgeText }),
    );
    if (headExtra) {
      head.append(headExtra);
    }
    const foot = makeElement("div", { className: "workspace-memory-note-foot" });
    foot.append(status, saveButton);
    note.append(head, area, foot);
    return note;
  };

  const notesHead = makeElement("div", { className: "workspace-settings-group-head" });
  notesHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Persistent memory") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Long-term instructions injected to the Agent every conversation turn, split into two layers: Selected project and All projects."),
    }),
  );
  const notesCard = makeElement("section", { className: "workspace-settings-group workspace-memory-notes" });
  notesCard.append(
    makeNote(t("Project memory"), t("Selected project"), projectArea, projectStatus, saveProjectButton),
    makeNote(t("User memory"), t("All projects"), userArea, userStatus, saveUserButton),
  );

  // ── 自动记忆开关 ──────────────────────────────────────
  const autoToggle = makeElement("input", {
    attributes: { type: "checkbox" },
    dataset: { workspaceAction: "workspace-memory-auto" },
  });
  const autoSwitch = makeElement("label", { className: "workspace-switch" });
  autoSwitch.append(
    autoToggle,
    makeElement("span", { className: "workspace-switch-track", attributes: { "aria-hidden": "true" } }),
  );
  const autoStatus = makeElement("span", { className: "workspace-memory-status workspace-memory-auto-status" });
  const autoHead = makeElement("div", { className: "workspace-settings-group-head" });
  autoHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Automatic memory") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Let the Agent automatically record noteworthy facts while working."),
    }),
  );
  const autoField = makeField(
    t("Auto-record"),
    autoSwitch,
    t("When enabled, the Agent writes noteworthy facts into the memory library below at appropriate times."),
  );
  autoField.querySelector(".workspace-field-text")?.append(autoStatus);
  const autoCard = makeElement("section", {
    className: "workspace-settings-group workspace-settings-provider workspace-memory-auto-card",
  });
  autoCard.append(autoField);

  // ── 记忆库(结构化条目)────────────────────────────────
  const legacyQueryInput = makeTextInput("workspace-memory-legacy-query", t("Search memory…"));
  legacyQueryInput.className = "workspace-input workspace-memory-search-input";
  const legacySearchButton = makeButton("", "workspace-memory-legacy-search", "workspace-memory-search-button");
  legacySearchButton.setAttribute("aria-label", t("Search memory"));
  legacySearchButton.setAttribute("title", t("Search memory"));
  const searchBox = makeElement("div", { className: "workspace-memory-search" });
  searchBox.append(legacyQueryInput, legacySearchButton);

  const libraryHeadText = makeElement("div", { className: "workspace-memory-library-heading" });
  libraryHeadText.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Memory library") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Structured memory automatically recorded by the Agent; searchable and deletable."),
    }),
  );
  const libraryHead = makeElement("div", { className: "workspace-settings-group-head workspace-memory-library-head" });
  libraryHead.append(libraryHeadText, searchBox);

  const legacy = makeElement("div", {
    className: "workspace-memory-list",
    dataset: { workspaceAction: "workspace-memory-legacy-results" },
  });
  const libraryCard = makeElement("section", { className: "workspace-settings-group workspace-memory-library" });
  libraryCard.append(legacy);

  const renderLegacyItems = (items) => {
    // 列表重建后旧按钮作废,清掉可能残留的「确认删除?」态。
    armedDelete = null;
    const legacyItems = Array.isArray(items) ? items : [];
    if (!legacyItems.length) {
      legacy.replaceChildren(makeElement("div", { className: "workspace-memory-empty", textContent: emptyMessage }));
      return;
    }
    legacy.replaceChildren(
      ...legacyItems.map((item) => {
        const row = makeElement("div", { className: "workspace-memory-item" });
        const main = makeElement("div", { className: "workspace-memory-item-main" });
        const titleRow = makeElement("div", { className: "workspace-memory-item-title-row" });
        titleRow.append(
          makeElement("span", {
            className: "workspace-memory-item-title",
            textContent: item.name || item.memoryId || t("Memory"),
          }),
        );
        const type = text(item.type || "");
        if (type) {
          titleRow.append(
            makeElement("span", {
              className: `workspace-memory-type workspace-memory-type-${type}`,
              textContent: MEMORY_TYPE_LABELS[type] || type,
            }),
          );
        }
        const scope = text(item.scope || "global") || "global";
        titleRow.append(
          makeElement("span", {
            className: `workspace-memory-scope workspace-memory-scope-${scope}`,
            textContent: MEMORY_SCOPE_LABELS[scope] || scope,
          }),
        );
        main.append(
          titleRow,
          makeElement("p", {
            className: "workspace-memory-item-summary",
            textContent: item.summary || item.description || "",
          }),
        );
        const deleteButton = makeButton("", "workspace-memory-legacy-delete", "workspace-memory-delete");
        deleteButton.setAttribute("aria-label", t("Delete this memory"));
        deleteButton.setAttribute("title", t("Delete this memory"));
        deleteButton.dataset.legacyMemoryId = text(item.memoryId || item.name || "");
        deleteButton.dataset.legacyScope = scope;
        // 复位本按钮的「确认删除?」态。
        const disarm = () => {
          deleteButton.classList.toggle("is-confirming", false);
          deleteButton.textContent = "";
        };
        // 二次确认:首次点击进入确认态并记到面板级 armedDelete(点击别处会复位),再次点同一按钮才删除。
        deleteButton.addEventListener("click", async () => {
          if (armedDelete?.button !== deleteButton) {
            if (armedDelete) {
              armedDelete.disarm();
            }
            deleteButton.classList.toggle("is-confirming", true);
            deleteButton.textContent = t("Confirm delete?");
            armedDelete = { button: deleteButton, disarm };
            return;
          }
          armedDelete = null;
          const token = ++legacyRequestToken;
          const memoryId = deleteButton.dataset.legacyMemoryId || "";
          const memoryScope = deleteButton.dataset.legacyScope || "global";
          deleteButton.disabled = true;
          try {
            await api.deleteLegacyMemory(memoryId, memoryScope === "project" ? selectedCwd : "", memoryScope);
            if (token !== legacyRequestToken) {
              return;
            }
            await loadLegacyMemory(token);
          } catch (error) {
            if (token !== legacyRequestToken) {
              return;
            }
            deleteButton.disabled = false;
            disarm();
            emptyMessage = t("Delete failed: {error}", { error: error instanceof Error ? error.message : String(error) });
            renderLegacyItems([]);
          }
        });
        row.append(main, deleteButton);
        return row;
      }),
    );
  };

  const loadLegacyMemory = async (existingToken = null) => {
    const token = existingToken ?? ++legacyRequestToken;
    const query = legacyQueryInput.value.trim();
    try {
      const payload = await api.searchLegacyMemory(query, selectedCwd);
      if (token !== legacyRequestToken) {
        return;
      }
      const memories = Array.isArray(payload?.memories) ? payload.memories : [];
      emptyMessage = query ? t("No matching memory found") : t("No memory entries yet");
      renderLegacyItems(memories);
    } catch (error) {
      if (token !== legacyRequestToken) {
        return;
      }
      emptyMessage = t("Load failed: {error}", { error: error instanceof Error ? error.message : String(error) });
      renderLegacyItems([]);
    }
  };

  // 选中的项目路径:优先当前会话所在项目,其次后端标记的 current(启动目录)。
  const defaultCwd = () => {
    const sessionCwd = text(context.session()?.cwd || "");
    if (sessionCwd && projects.some((item) => item.cwd === sessionCwd)) {
      return sessionCwd;
    }
    const current = projects.find((item) => item.current);
    return current ? current.cwd : text(projects[0]?.cwd || "");
  };

  const populateProjectSelect = () => {
    projectSelect.replaceChildren(
      ...projects.map((item) =>
        makeElement("option", {
          textContent: item.current ? t("{label} (current)", { label: item.label }) : item.label,
          attributes: { value: item.cwd },
        }),
      ),
    );
    projectSelect.disabled = projects.length === 0;
    if (selectedCwd) {
      projectSelect.value = selectedCwd;
    }
  };

  const loadProjects = async () => {
    try {
      const payload = await api.listMemoryProjects();
      projects = Array.isArray(payload?.projects) ? payload.projects : [];
    } catch (_error) {
      projects = [];
    }
    if (!projects.some((item) => item.cwd === selectedCwd)) {
      selectedCwd = defaultCwd();
    }
    populateProjectSelect();
  };

  // 刷新项目记忆(切换项目时用);记忆库随项目重取见 projectSelect change,用户/自动为全局。
  const loadProjectNote = async (token) => {
    stamp(projectStatus, t("Loading…"));
    const payload = await api.getMemory(selectedCwd ? { cwd: selectedCwd } : {});
    if (token !== memoryRequestToken) {
      return payload;
    }
    projectArea.value = payload?.project?.content || "";
    projectPath = text(payload?.project?.path || "");
    stamp(projectStatus, projectPath ? t("File: {path}", { path: projectPath }) : "");
    return payload;
  };

  const loadMemory = async () => {
    const token = ++memoryRequestToken;
    await loadProjects();
    if (token !== memoryRequestToken) {
      return;
    }
    try {
      const payload = await loadProjectNote(token);
      if (token !== memoryRequestToken) {
        return;
      }
      userArea.value = payload?.user?.content || "";
      autoToggle.checked = payload?.autoMemoryEnabled !== false;
      userPath = text(payload?.user?.path || "");
      stamp(userStatus, userPath ? t("File: {path}", { path: userPath }) : "");
      const legacyItems = Array.isArray(payload?.legacy) ? payload.legacy : [];
      emptyMessage = t("No memory entries yet");
      renderLegacyItems(legacyItems);
      loaded = true;
    } catch (error) {
      if (token !== memoryRequestToken) {
        return;
      }
      const message = t("Load failed: {error}", { error: error instanceof Error ? error.message : String(error) });
      stamp(projectStatus, message, true);
      stamp(userStatus, message, true);
    }
  };

  const saveNote = async (status, savedPath, save) => {
    const token = ++memoryRequestToken;
    stamp(status, t("Saving…"));
    try {
      await save();
      if (token !== memoryRequestToken) {
        return;
      }
      stamp(status, savedPath() ? t("Saved · {name}", { name: baseName(savedPath()) }) : t("Saved"));
    } catch (error) {
      if (token !== memoryRequestToken) {
        return;
      }
      stamp(status, t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  };

  projectSelect.addEventListener("change", async () => {
    selectedCwd = projectSelect.value;
    // 记忆库按作用域展示「所选项目 + 全局」,切项目需随之刷新。
    loadLegacyMemory();
    const token = ++memoryRequestToken;
    try {
      await loadProjectNote(token);
    } catch (error) {
      if (token !== memoryRequestToken) {
        return;
      }
      stamp(projectStatus, t("Load failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  });

  saveProjectButton.addEventListener("click", () =>
    saveNote(projectStatus, () => projectPath, () =>
      api.saveProjectMemory({ cwd: selectedCwd, content: projectArea.value }),
    ),
  );
  saveUserButton.addEventListener("click", () =>
    saveNote(userStatus, () => userPath, () => api.saveUserMemory({ content: userArea.value })),
  );
  autoToggle.addEventListener("change", async () => {
    const token = ++memoryRequestToken;
    stamp(autoStatus, t("Saving…"));
    try {
      await api.saveAutoMemory(autoToggle.checked);
      if (token !== memoryRequestToken) {
        return;
      }
      stamp(autoStatus, autoToggle.checked ? t("Automatic memory enabled") : t("Automatic memory disabled"));
    } catch (error) {
      if (token !== memoryRequestToken) {
        return;
      }
      stamp(autoStatus, t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  });

  legacySearchButton.addEventListener("click", () => {
    loadLegacyMemory();
  });
  legacyQueryInput.addEventListener("input", () => {
    if (searchTimer) {
      clearTimeout(searchTimer);
    }
    searchTimer = setTimeout(() => {
      loadLegacyMemory();
    }, 220);
  });
  legacyQueryInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault?.();
      loadLegacyMemory();
    }
  });

  // 点击删除按钮以外的任何地方,复位「确认删除?」态(仅浏览器环境;无头测试不触发交互)。
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("click", (event) => {
      if (armedDelete && !armedDelete.button.contains?.(event.target)) {
        armedDelete.disarm();
        armedDelete = null;
      }
    });
  }

  panel.append(heading, projectPickerRow, notesHead, notesCard, autoHead, autoCard, libraryHead, libraryCard);

  return {
    panel,
    activate() {
      if (!loaded) {
        loadMemory();
      }
    },
    reset() {
      memoryRequestToken += 1;
      legacyRequestToken += 1;
      loaded = false;
      projects = [];
      selectedCwd = "";
      if (armedDelete) {
        armedDelete.disarm();
        armedDelete = null;
      }
      projectPath = "";
      userPath = "";
      emptyMessage = t("No memory entries yet");
      projectArea.value = "";
      userArea.value = "";
      autoToggle.checked = false;
      legacyQueryInput.value = "";
      projectSelect.replaceChildren();
      stamp(projectStatus, "");
      stamp(userStatus, "");
      stamp(autoStatus, "");
      legacy.replaceChildren();
    },
  };
}

// 技能来源 → 中文徽标。
const SKILL_SOURCE_LABELS = { bundled: t("Built-in"), project: t("Project"), user: t("User") };

// 参照终端 skills_picker 的 token 估算(约 4 字符/token),压成紧凑徽标文案。
function formatSkillTokens(contentLength) {
  const tokens = Math.max(1, Math.ceil((Number(contentLength) || 0) / 4));
  if (tokens >= 1000) {
    return `~${(tokens / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  }
  return `~${tokens}`;
}

// 「插件」容器面板:技能与 MCP 曾是两个平级导航项,现合并到「插件」下,通过横向标签页
// (技能 / MCP)切换。容器拥有唯一的「插件」标题与横向子标签栏;复用原有的
// createSkillsPanel / createMcpPanel 作为两个子面板,仅剥掉它们各自的 <h3>(与子标签
// 文案、容器标题重复)。子面板内部状态、控制器接口(activate/reset)保持不变。
const PLUGINS_SUBTABS = [
  { id: "skills", label: t("Skills") },
  { id: "mcp", label: "MCP" },
];

function createPluginsPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-plugins-panel",
    attributes: { "data-workspace-panel": "skills" },
  });
  const heading = makeElement("h3", { textContent: t("Plugins") });

  const subtabs = makeElement("div", {
    className: "workspace-plugins-subtabs",
    attributes: { role: "tablist" },
  });

  const children = {
    skills: createSkillsPanel(api, context),
    mcp: createMcpPanel(api, context),
  };
  // 剥掉子面板自带的 <h3>(技能面板为「插件」、MCP 面板为「MCP」),避免与容器标题及
  // 子标签重复;并移除子面板的 data-workspace-panel 标记,让容器成为唯一的面板锚点
  // (否则 [data-workspace-panel="skills"] 之类的规则会在嵌套层重复命中)。
  for (const child of Object.values(children)) {
    child.panel.querySelector(":scope > h3")?.remove?.();
    child.panel.removeAttribute?.("data-workspace-panel");
  }

  const buttons = new Map();
  for (const tab of PLUGINS_SUBTABS) {
    const button = makeElement("button", {
      className: "workspace-plugins-subtab",
      attributes: { type: "button", role: "tab" },
      dataset: { pluginsSubtab: tab.id },
      textContent: tab.label,
    });
    buttons.set(tab.id, button);
    subtabs.append(button);
  }

  let activeSub = "skills";

  function showSub(id) {
    activeSub = children[id] ? id : "skills";
    for (const [tabId, button] of buttons.entries()) {
      const isActive = tabId === activeSub;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    }
    for (const [tabId, child] of Object.entries(children)) {
      child.panel.hidden = tabId !== activeSub;
    }
  }

  for (const [id, button] of buttons.entries()) {
    button.addEventListener("click", () => {
      showSub(id);
      children[activeSub].activate?.();
    });
  }

  showSub("skills");
  panel.append(heading, subtabs, children.skills.panel, children.mcp.panel);

  return {
    panel,
    activate() {
      children[activeSub].activate?.();
    },
    reset() {
      children.skills.reset?.();
      children.mcp.reset?.();
      showSub("skills");
    },
  };
}

function createSkillsPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-plugins-panel",
    attributes: { "data-workspace-panel": "skills" },
  });
  const heading = makeElement("h3", { textContent: t("Plugins") });

  // ── 技能(未来其它插件可在此面板追加同级区块)──────────────
  const skillsHead = makeElement("div", { className: "workspace-settings-group-head" });
  skillsHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Skills") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Each skill can be invoked by the Agent or used as a / command. Built-in skills are always on and cannot be disabled."),
    }),
  );

  // 项目选择器:项目级技能按所选项目发现(内置/用户技能与项目无关,始终常驻)。
  // 与记忆面板共用同一「已知项目」枚举。
  const projectSelect = makeElement("select", {
    className: "workspace-select workspace-skills-project-picker",
    dataset: { workspaceAction: "workspace-skills-project-select" },
  });
  projectSelect.setAttribute("aria-label", t("Select project"));
  const projectPickerRow = makeElement("div", { className: "workspace-skills-project-row" });
  projectPickerRow.append(
    makeElement("span", { className: "workspace-skills-project-label", textContent: t("Selected project") }),
    projectSelect,
  );

  const searchInput = makeTextInput("workspace-skills-search", t("Search skills…"));
  searchInput.className = "workspace-input workspace-skills-search-input";
  const list = makeElement("div", {
    className: "workspace-skills-list",
    dataset: { workspaceAction: "workspace-skills-list" },
  });
  const status = makeElement("p", { className: "workspace-skills-status" });
  status.hidden = true;
  const card = makeElement("section", { className: "workspace-settings-group workspace-skills-card" });
  card.append(searchInput, list, status);

  let loaded = false;
  let requestToken = 0;
  let allSkills = [];
  let projects = [];
  let selectedCwd = "";

  // 技能不依赖活动会话:无会话时后端回落到默认项目 cwd(含全部内置技能)。
  // 竞态判定为「请求发起时的会话仍是当前会话」,空会话("")亦视为一致——
  // 不能用 isSessionCurrent(空 id 会被判为失效而丢弃载荷)。
  const isSkillsRequestStale = (requestedSessionId, token) =>
    context.sessionId() !== requestedSessionId || token !== requestToken;

  const showStatus = (message, isError = false) => {
    status.hidden = false;
    status.textContent = text(message);
    status.classList.toggle("is-error", Boolean(isError));
  };
  const clearStatus = () => {
    status.hidden = true;
    status.textContent = "";
    status.classList.toggle("is-error", false);
  };

  const makeBadge = (label, extraClass = "") =>
    makeElement("span", {
      className: `workspace-skill-badge${extraClass ? ` ${extraClass}` : ""}`,
      textContent: label,
    });

  const makeSkillRow = (skill) => {
    const locked = Boolean(skill.locked);
    const enabled = skill.enabled !== false;
    const row = makeElement("div", { className: "workspace-skill-row" });
    if (!enabled) row.classList.toggle("is-disabled", true);

    const head = makeElement("div", { className: "workspace-skill-row-head" });
    const main = makeElement("div", { className: "workspace-skill-main" });
    main.append(makeElement("strong", { className: "workspace-skill-name", textContent: skill.name || t("Skills") }));

    const badges = makeElement("div", { className: "workspace-skill-badges" });
    const source = text(skill.source);
    badges.append(
      makeBadge(SKILL_SOURCE_LABELS[source] || source || t("Unknown"), `workspace-skill-source-${source || "unknown"}`),
    );
    if (locked) badges.append(makeBadge(t("Locked"), "workspace-skill-badge-locked"));
    if (skill.commandAvailable) badges.append(makeBadge(t("/command")));
    if (skill.modelInvocable) badges.append(makeBadge(t("Models")));
    badges.append(makeBadge(formatSkillTokens(skill.contentLength), "workspace-skill-badge-token"));
    main.append(badges);

    const toggle = makeElement("label", { className: "workspace-switch" });
    const checkbox = makeElement("input", { attributes: { type: "checkbox" } });
    checkbox.checked = enabled;
    checkbox.disabled = locked;
    checkbox.dataset.skillName = text(skill.name);
    if (locked) {
      toggle.classList.toggle("is-locked", true);
      toggle.setAttribute("title", t("Built-in skills cannot be disabled"));
    }
    toggle.append(checkbox, makeElement("span", { className: "workspace-switch-track", attributes: { "aria-hidden": "true" } }));
    checkbox.addEventListener("change", () => saveToggle(skill, checkbox));

    head.append(main, toggle);
    row.append(head);
    if (skill.description) {
      row.append(makeElement("p", { className: "workspace-skill-desc", textContent: skill.description }));
    }
    return row;
  };

  const renderList = () => {
    const query = searchInput.value.trim().toLowerCase();
    const matches = query
      ? allSkills.filter((skill) =>
          `${skill.name || ""} ${skill.description || ""}`.toLowerCase().includes(query),
        )
      : allSkills;
    if (!allSkills.length) {
      list.replaceChildren(makeElement("div", { className: "workspace-skills-empty", textContent: t("No skills available yet.") }));
      return;
    }
    if (!matches.length) {
      list.replaceChildren(makeElement("div", { className: "workspace-skills-empty", textContent: t("No matching skills found.") }));
      return;
    }
    list.replaceChildren(...matches.map(makeSkillRow));
  };

  const disabledNames = () =>
    allSkills.filter((skill) => skill.enabled === false && !skill.locked).map((skill) => skill.name).filter(Boolean);

  // 切换开关即时保存:更新本地状态 → 全量提交 disabled 列表;失败回滚。
  const saveToggle = async (skill, checkbox) => {
    const requestedSessionId = context.sessionId();
    const previous = skill.enabled;
    skill.enabled = checkbox.checked;
    const token = ++requestToken;
    showStatus(t("Saving…"));
    try {
      const payload = await api.saveDisabledSkills({
        sessionId: requestedSessionId,
        disabled: disabledNames(),
        cwd: selectedCwd,
      });
      if (isSkillsRequestStale(requestedSessionId, token)) {
        return;
      }
      allSkills = Array.isArray(payload.skills) ? payload.skills : allSkills;
      renderList();
      showStatus(t("Updated."));
      // 成功提示是瞬时反馈,短暂展示后自动收起(无新请求接管时);否则会一直残留。
      const settledToken = token;
      setTimeout(() => {
        if (settledToken === requestToken) {
          clearStatus();
        }
      }, 1600);
    } catch (error) {
      if (isSkillsRequestStale(requestedSessionId, token)) {
        return;
      }
      skill.enabled = previous;
      renderList();
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  };

  // 选中的项目路径:优先当前会话所在项目,其次后端标记的 current(启动目录)。
  const defaultCwd = () => {
    const sessionCwd = text(context.session()?.cwd || "");
    if (sessionCwd && projects.some((item) => item.cwd === sessionCwd)) {
      return sessionCwd;
    }
    const current = projects.find((item) => item.current);
    return current ? current.cwd : text(projects[0]?.cwd || "");
  };

  const populateProjectSelect = () => {
    projectSelect.replaceChildren(
      ...projects.map((item) =>
        makeElement("option", {
          textContent: item.current ? t("{label} (current)", { label: item.label }) : item.label,
          attributes: { value: item.cwd },
        }),
      ),
    );
    projectSelect.disabled = projects.length === 0;
    if (selectedCwd) {
      projectSelect.value = selectedCwd;
    }
  };

  const loadProjects = async () => {
    try {
      const payload = await api.listProjects();
      projects = Array.isArray(payload?.projects) ? payload.projects : [];
    } catch (_error) {
      projects = [];
    }
    if (!projects.some((item) => item.cwd === selectedCwd)) {
      selectedCwd = defaultCwd();
    }
    populateProjectSelect();
  };

  // 拉取所选项目的技能并渲染;返回是否成功(供 loaded 标记用)。
  const fetchAndRender = async (requestedSessionId, token) => {
    const payload = await api.getSkills(requestedSessionId, selectedCwd);
    if (isSkillsRequestStale(requestedSessionId, token)) {
      return false;
    }
    allSkills = Array.isArray(payload.skills) ? payload.skills : [];
    renderList();
    clearStatus();
    return true;
  };

  const loadSkills = async () => {
    const requestedSessionId = context.sessionId();
    const token = ++requestToken;
    showStatus(t("Loading skills…"));
    try {
      await loadProjects();
      if (isSkillsRequestStale(requestedSessionId, token)) {
        return;
      }
      if (await fetchAndRender(requestedSessionId, token)) {
        loaded = true;
      }
    } catch (error) {
      if (isSkillsRequestStale(requestedSessionId, token)) {
        return;
      }
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  };

  // 切换项目:仅重取该项目技能(项目列表无需重拉)。
  projectSelect.addEventListener("change", async () => {
    selectedCwd = projectSelect.value;
    const requestedSessionId = context.sessionId();
    const token = ++requestToken;
    showStatus(t("Loading skills…"));
    try {
      await fetchAndRender(requestedSessionId, token);
    } catch (error) {
      if (isSkillsRequestStale(requestedSessionId, token)) {
        return;
      }
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  });

  searchInput.addEventListener("input", renderList);
  // 项目选择器为面板级控件(未来其它插件区块共用),置于「插件」标题正下方。
  panel.append(heading, projectPickerRow, skillsHead, card);

  return {
    panel,
    activate() {
      if (!loaded) {
        loadSkills();
      }
    },
    reset() {
      requestToken += 1;
      loaded = false;
      allSkills = [];
      projects = [];
      selectedCwd = "";
      searchInput.value = "";
      projectSelect.replaceChildren();
      list.replaceChildren();
      clearStatus();
    },
  };
}

// ── MCP 服务器管理面板 ────────────────────────────────────────────────
// 与 REPL 的 /mcp 命令(交互式 MCPManagerDialog + iac-code mcp CLI)功能对齐:
// 列出各作用域(user/local/project)持久化的 MCP 服务器,查看连接/认证/审批状态,
// 启停、审批/拒绝项目级服务器、连接检查、查看 tools/resources/prompts、OAuth 认证/
// 重新认证/清除认证,以及新增(表单或 JSON)、编辑、删除。后端复用 mcp 服务层。
const MCP_SCOPE_LABELS = { user: t("User"), local: t("Local"), project: t("Project") };
const MCP_TRANSPORT_LABELS = { stdio: "stdio", http: "http", sse: "sse", ws: "ws" };
const MCP_CONNECTION_LABELS = {
  connected: t("Connected"),
  failed: t("Connection failed"),
  needs_auth: t("Needs authentication"),
  "needs-auth": t("Needs authentication"),
  pending: t("Pending connection"),
  disabled: t("Disabled"),
  // 离线列表(未点「检查连接」)下所有服务器连接状态均为 skipped,须给出中文而非回退英文。
  skipped: t("Not checked"),
  "pending-approval": t("Pending approval"),
  "missing-env": t("Missing environment variables"),
  "invalid-config": t("Invalid configuration"),
};
const MCP_AUTH_LABELS = {
  authenticated: t("Authenticated"),
  configured: t("Configured"),
  "needs-auth": t("Needs authentication"),
  needs_auth: t("Needs authentication"),
  "not-configured": t("Not configured"),
  not_configured: t("Not configured"),
  error: t("Authentication error"),
};
const MCP_TRANSPORTS = [
  { value: "stdio", label: t("stdio (local command)") },
  { value: "http", label: t("http (remote)") },
  { value: "sse", label: t("sse (remote)") },
  { value: "ws", label: "ws(WebSocket)" },
];
const MCP_SCOPES = [
  { value: "user", label: t("User (~/.iac-code/settings.yml)") },
  { value: "local", label: t("Local (.iac-code/settings.local.yml)") },
  { value: "project", label: t("Project (.mcp.json)") },
];

function mcpConnectionClass(state) {
  const key = text(state);
  if (key === "connected") return "is-connected";
  if (key === "failed" || key === "invalid-config") return "is-failed";
  if (key === "needs_auth" || key === "needs-auth" || key === "missing-env") return "is-warn";
  if (key === "disabled") return "is-disabled";
  if (key === "pending-approval") return "is-pending";
  return "is-neutral";
}

// KEY=VALUE\n… 文本 ↔ 映射对象;用于 env / headers 编辑。
function parseKeyValueLines(value) {
  const result = {};
  for (const line of text(value).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    const colon = trimmed.indexOf(":");
    let sep = eq;
    if (eq === -1 || (colon !== -1 && colon < eq)) sep = colon;
    if (sep === -1) {
      result[trimmed] = "";
      continue;
    }
    const key = trimmed.slice(0, sep).trim();
    if (key) result[key] = trimmed.slice(sep + 1).trim();
  }
  return result;
}

function stringifyKeyValues(mapping) {
  if (!mapping || typeof mapping !== "object") return "";
  return Object.entries(mapping)
    .map(([key, value]) => `${key}=${value === null || value === undefined ? "" : value}`)
    .join("\n");
}

function createMcpPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-mcp-panel",
    attributes: { "data-workspace-panel": "mcp" },
  });
  const heading = makeElement("h3", { textContent: "MCP" });

  const groupHead = makeElement("div", { className: "workspace-settings-group-head" });
  groupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("MCP servers") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Manage MCP servers: add/edit/delete, enable/disable, approve project-level configs, connection checks, view capabilities, and OAuth authentication."),
    }),
  );

  // 项目选择器(项目级服务器随所选项目变化;用户级始终可见)。
  const projectSelect = makeElement("select", {
    className: "workspace-select workspace-mcp-project-picker",
    dataset: { workspaceAction: "workspace-mcp-project-select" },
  });
  projectSelect.setAttribute("aria-label", t("Select project"));
  const projectPickerRow = makeElement("div", { className: "workspace-mcp-project-row" });
  projectPickerRow.append(
    makeElement("span", { className: "workspace-mcp-project-label", textContent: t("Selected project") }),
    projectSelect,
  );

  const toolbar = makeElement("div", { className: "workspace-mcp-toolbar" });
  const addButton = makeButton(t("Add server"), "workspace-mcp-add", "workspace-action workspace-action-primary");
  const refreshButton = makeButton(t("Refresh"), "workspace-mcp-refresh");
  const checkAllButton = makeButton(t("Check all connections"), "workspace-mcp-check-all");
  toolbar.append(addButton, refreshButton, checkAllButton);

  const status = makeElement("p", { className: "workspace-mcp-status" });
  status.hidden = true;
  const warningsBox = makeElement("div", { className: "workspace-mcp-warnings" });
  warningsBox.hidden = true;
  const addFormMount = makeElement("div", { className: "workspace-mcp-add-mount" });
  addFormMount.hidden = true;
  const list = makeElement("div", { className: "workspace-mcp-list" });

  const card = makeElement("section", { className: "workspace-settings-group workspace-mcp-card" });
  card.append(toolbar, warningsBox, addFormMount, status, list);

  let loaded = false;
  let requestToken = 0;
  let servers = [];
  let projects = [];
  let selectedCwd = "";
  const expanded = new Map(); // name -> { caps, edit } 展开状态

  const isStale = (requestedSessionId, token) =>
    context.sessionId() !== requestedSessionId || token !== requestToken;

  const showStatus = (message, isError = false) => {
    status.hidden = false;
    status.textContent = text(message);
    status.classList.toggle("is-error", Boolean(isError));
  };
  const clearStatus = () => {
    status.hidden = true;
    status.textContent = "";
    status.classList.toggle("is-error", false);
  };
  const flashStatus = (message) => {
    showStatus(message);
    const token = ++requestToken;
    setTimeout(() => {
      if (token === requestToken) clearStatus();
    }, 1600);
  };

  const scopeParams = () => ({ sessionId: context.sessionId(), cwd: selectedCwd });

  const serverKey = (server) => `${server.name}@${server.scope}@${text(server.source_path)}`;

  const makeBadge = (label, extraClass = "") =>
    makeElement("span", {
      className: `workspace-mcp-badge${extraClass ? ` ${extraClass}` : ""}`,
      textContent: label,
    });

  // ── 新增/编辑表单 ────────────────────────────────────────────────
  function buildServerForm({ mode, server = null }) {
    const form = makeElement("form", { className: "workspace-mcp-form", attributes: { autocomplete: "off" } });
    const title = makeElement("h5", {
      className: "workspace-mcp-form-title",
      textContent: mode === "edit" ? t("Edit server: {name}", { name: server?.name || "" }) : t("Add MCP server"),
    });

    const editable = mode === "edit" && server?.editable_config ? server.editable_config : {};
    const initialTransport = text(editable.type || (editable.command ? "stdio" : server?.transport) || "stdio");

    const nameInput = makeTextInput("workspace-mcp-name", t("Server name"));
    nameInput.value = mode === "edit" ? text(server?.name) : "";
    if (mode === "edit") nameInput.disabled = true;

    const scopeSelect = makeSelect("workspace-mcp-scope");
    scopeSelect.replaceChildren(
      ...MCP_SCOPES.map((item) => makeElement("option", { textContent: item.label, attributes: { value: item.value } })),
    );
    scopeSelect.value = mode === "edit" ? text(server?.scope) : "user";
    if (mode === "edit") scopeSelect.disabled = true;

    const transportSelect = makeSelect("workspace-mcp-transport");
    transportSelect.replaceChildren(
      ...MCP_TRANSPORTS.map((item) =>
        makeElement("option", { textContent: item.label, attributes: { value: item.value } }),
      ),
    );
    transportSelect.value = initialTransport || "stdio";

    // stdio 字段
    const commandInput = makeTextInput("workspace-mcp-command", t("e.g. npx"));
    commandInput.value = text(editable.command);
    const argsInput = makeTextarea("workspace-mcp-args", t("One argument per line"));
    argsInput.rows = 3;
    argsInput.value = Array.isArray(editable.args) ? editable.args.join("\n") : "";
    const envInput = makeTextarea("workspace-mcp-env", t("KEY=${VAR}\none per line"));
    envInput.rows = 3;
    envInput.value = stringifyKeyValues(editable.env);

    // 远程字段
    const urlInput = makeTextInput("workspace-mcp-url", "https://…");
    urlInput.value = text(editable.url);
    const headersInput = makeTextarea("workspace-mcp-headers", t("Name: ${VAR}\none per line"));
    headersInput.rows = 3;
    headersInput.value = stringifyKeyValues(editable.headers);

    // OAuth 字段(可选)
    const oauth = editable.oauth && typeof editable.oauth === "object" ? editable.oauth : {};
    const oauthClientId = makeTextInput("workspace-mcp-oauth-client-id", "OAuth Client ID");
    oauthClientId.value = text(oauth.clientId || oauth.client_id);
    const oauthClientSecretEnv = makeTextInput("workspace-mcp-oauth-secret-env", t("Client Secret environment variable name"));
    oauthClientSecretEnv.value = text(oauth.clientSecretEnv || oauth.client_secret_env);
    const oauthCallbackPort = makeTextInput("workspace-mcp-oauth-port", t("Callback port (optional)"));
    oauthCallbackPort.value = text(oauth.callbackPort || oauth.callback_port);
    const oauthMetadataUrl = makeTextInput("workspace-mcp-oauth-meta", t("Authorization server metadata URL (optional)"));
    oauthMetadataUrl.value = text(oauth.authServerMetadataUrl || oauth.auth_server_metadata_url);

    const stdioFields = makeElement("div", { className: "workspace-mcp-fields workspace-mcp-fields-stdio" });
    stdioFields.append(
      makeField(t("Command"), commandInput, t("The executable command for the stdio server.")),
      makeField(t("Arguments"), argsInput, t("One command argument per line.")),
      makeField(t("Environment variables"), envInput, t("KEY=VALUE; reference secrets with ${VAR}, no plaintext.")),
    );
    const remoteFields = makeElement("div", { className: "workspace-mcp-fields workspace-mcp-fields-remote" });
    remoteFields.append(
      makeField(t("URL"), urlInput, t("Remote server address for http/sse/ws.")),
      makeField(t("Request headers"), headersInput, t("Name: Value; reference secrets with ${VAR} (ws does not support request headers).")),
    );
    const oauthFields = makeElement("details", { className: "workspace-mcp-oauth-fields" });
    oauthFields.append(
      makeElement("summary", { textContent: t("OAuth (optional)") }),
      makeField(t("Client ID"), oauthClientId),
      makeField(t("Client Secret environment variable"), oauthClientSecretEnv, t("Name of the environment variable storing the secret (do not enter the plaintext secret).")),
      makeField(t("Callback port"), oauthCallbackPort),
      makeField(t("Metadata URL"), oauthMetadataUrl),
    );

    const jsonToggle = makeElement("label", { className: "workspace-mcp-json-toggle" });
    const jsonCheckbox = makeElement("input", { attributes: { type: "checkbox" } });
    jsonToggle.append(jsonCheckbox, makeElement("span", { textContent: t("Edit configuration directly as JSON") }));
    const jsonInput = makeTextarea("workspace-mcp-json", '{\n  "command": "npx",\n  "args": ["-y", "server"]\n}');
    jsonInput.rows = 8;
    jsonInput.value = mode === "edit" ? pretty(editable) : "";
    const jsonWrap = makeElement("div", { className: "workspace-mcp-json-wrap" });
    jsonWrap.hidden = true;
    jsonWrap.append(makeField(t("Configuration JSON"), jsonInput, t("MCP server configuration object (with command/args/env or type/url/headers/oauth).")));

    const formStatus = makeElement("p", { className: "workspace-mcp-form-status" });
    formStatus.hidden = true;
    const actions = makeElement("div", { className: "workspace-mcp-form-actions" });
    const submitButton = makeButton(mode === "edit" ? t("Save") : t("Create"), "workspace-mcp-form-submit", "workspace-action workspace-action-primary");
    submitButton.type = "submit";
    const cancelButton = makeButton(t("Cancel"), "workspace-mcp-form-cancel");
    actions.append(submitButton, cancelButton);

    const syncTransport = () => {
      const t = transportSelect.value;
      stdioFields.hidden = t !== "stdio";
      remoteFields.hidden = t === "stdio";
      // ws 不支持 headers/oauth
      const isWs = t === "ws";
      headersInput.closest(".workspace-field").hidden = isWs;
      oauthFields.hidden = isWs;
    };
    const syncJson = () => {
      const useJson = jsonCheckbox.checked;
      jsonWrap.hidden = !useJson;
      transportSelect.closest(".workspace-field").hidden = useJson;
      stdioFields.hidden = useJson || transportSelect.value !== "stdio";
      remoteFields.hidden = useJson || transportSelect.value === "stdio";
      oauthFields.hidden = useJson || transportSelect.value === "ws";
    };
    transportSelect.addEventListener("change", syncTransport);
    jsonCheckbox.addEventListener("change", syncJson);

    const showFormStatus = (message, isError = false) => {
      formStatus.hidden = false;
      formStatus.textContent = text(message);
      formStatus.classList.toggle("is-error", Boolean(isError));
    };

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const name = nameInput.value.trim();
      if (!name) {
        showFormStatus(t("Please enter a server name."), true);
        return;
      }
      const scope = scopeSelect.value;
      const params = scopeParams();
      submitButton.disabled = true;
      showFormStatus(t("Saving…"));
      try {
        let payloadArgs;
        if (jsonCheckbox.checked) {
          let config;
          try {
            config = JSON.parse(jsonInput.value);
          } catch (parseError) {
            showFormStatus(t("JSON parse failed: {error}", { error: parseError.message }), true);
            submitButton.disabled = false;
            return;
          }
          payloadArgs = { ...params, name, scope, config };
        } else {
          const transport = transportSelect.value;
          const fields = { transport };
          if (transport === "stdio") {
            fields.command = commandInput.value.trim();
            fields.args = argsInput.value.split("\n").map((line) => line.trim()).filter(Boolean);
            fields.env = parseKeyValueLines(envInput.value);
          } else {
            fields.url = urlInput.value.trim();
            if (transport !== "ws") fields.headers = parseKeyValueLines(headersInput.value);
          }
          if (transport !== "ws") {
            const oauthPayload = {};
            if (oauthClientId.value.trim()) oauthPayload.clientId = oauthClientId.value.trim();
            if (oauthClientSecretEnv.value.trim()) oauthPayload.clientSecretEnv = oauthClientSecretEnv.value.trim();
            if (oauthCallbackPort.value.trim()) oauthPayload.callbackPort = oauthCallbackPort.value.trim();
            if (oauthMetadataUrl.value.trim()) oauthPayload.authServerMetadataUrl = oauthMetadataUrl.value.trim();
            if (Object.keys(oauthPayload).length) fields.oauth = oauthPayload;
          }
          payloadArgs = { ...params, name, scope, fields };
        }
        if (mode === "edit") {
          await api.updateMcpServer(payloadArgs);
        } else {
          await api.addMcpServer(payloadArgs);
        }
        addFormMount.hidden = true;
        addFormMount.replaceChildren();
        await reload();
        flashStatus(mode === "edit" ? t("Saved.") : t("Created."));
        void autoCheckServer(name, scope);
      } catch (error) {
        showFormStatus(error instanceof Error ? error.message : String(error), true);
      } finally {
        submitButton.disabled = false;
      }
    });
    cancelButton.addEventListener("click", () => {
      addFormMount.hidden = true;
      addFormMount.replaceChildren();
    });

    form.append(
      title,
      makeField(t("Name"), nameInput),
      makeField(t("Scope"), scopeSelect),
      jsonToggle,
      makeField(t("Transport type"), transportSelect),
      stdioFields,
      remoteFields,
      oauthFields,
      jsonWrap,
      formStatus,
      actions,
    );
    syncTransport();
    syncJson();
    return form;
  }

  function openAddForm() {
    addFormMount.replaceChildren(buildServerForm({ mode: "add" }));
    addFormMount.hidden = false;
    addFormMount.scrollIntoView?.({ block: "nearest" });
  }
  function openEditForm(server) {
    addFormMount.replaceChildren(buildServerForm({ mode: "edit", server }));
    addFormMount.hidden = false;
    addFormMount.scrollIntoView?.({ block: "nearest" });
  }

  // ── 能力(tools/resources/prompts)展开区 ─────────────────────────
  async function loadCapabilities(server, container) {
    container.replaceChildren(makeElement("p", { className: "workspace-mcp-caps-loading", textContent: t("Connecting and fetching capabilities…") }));
    try {
      const payload = await api.getMcpCapabilities({
        ...scopeParams(),
        name: server.name,
        scope: server.scope,
        sourcePath: text(server.source_path),
      });
      const sections = [];
      const capErrors = payload.capability_errors || {};
      const renderItems = (label, capKey, items, renderItem) => {
        const section = makeElement("div", { className: "workspace-mcp-caps-section" });
        section.append(makeElement("h6", { textContent: `${label}(${items.length})` }));
        const capError = capKey ? capErrors[capKey] : "";
        if (!items.length) {
          // 该能力被服务器拒绝(如 prompts/list Method not found)时,按项说明而非顶部红条,
          // 避免让一个仍可正常连接、工具可用的服务器看起来整体失败。
          if (capError) {
            section.append(
              makeElement("p", { className: "workspace-mcp-caps-note", textContent: t("Not supported: {error}", { error: text(capError) }) }),
            );
          } else {
            section.append(makeElement("p", { className: "workspace-mcp-caps-empty", textContent: t("None") }));
          }
        } else {
          const ul = makeElement("ul", { className: "workspace-mcp-caps-items" });
          for (const item of items) ul.append(renderItem(item));
          section.append(ul);
        }
        return section;
      };
      sections.push(
        renderItems(t("Tools"), "tools", payload.tools || [], (tool) => {
          const li = makeElement("li", { className: "workspace-mcp-caps-item" });
          li.append(makeElement("strong", { textContent: text(tool.name) }));
          if (tool.description) li.append(makeElement("p", { textContent: text(tool.description) }));
          return li;
        }),
      );
      sections.push(
        renderItems(t("Resources"), "resources", payload.resources || [], (resource) => {
          const li = makeElement("li", { className: "workspace-mcp-caps-item" });
          li.append(makeElement("strong", { textContent: text(resource.name || resource.uri) }));
          li.append(makeElement("code", { className: "workspace-mcp-caps-uri", textContent: text(resource.uri) }));
          if (resource.description) li.append(makeElement("p", { textContent: text(resource.description) }));
          return li;
        }),
      );
      sections.push(
        renderItems(t("Prompts"), "prompts", payload.prompts || [], (prompt) => {
          const li = makeElement("li", { className: "workspace-mcp-caps-item" });
          li.append(makeElement("strong", { textContent: text(prompt.name) }));
          if (prompt.description) li.append(makeElement("p", { textContent: text(prompt.description) }));
          return li;
        }),
      );
      // 仅当连接本身失败(而非单个能力不支持)时,才在顶部显示整体错误。
      if (payload.latest_failure && payload.connection_state !== "connected") {
        sections.unshift(
          makeElement("p", { className: "workspace-mcp-caps-error", textContent: text(payload.latest_failure) }),
        );
      }
      container.replaceChildren(...sections);
    } catch (error) {
      container.replaceChildren(
        makeElement("p", {
          className: "workspace-mcp-caps-error",
          textContent: error instanceof Error ? error.message : String(error),
        }),
      );
    }
  }

  // ── OAuth 认证流程区 ──────────────────────────────────────────────
  async function runAuthFlow(server, container, { reauthenticate }) {
    container.replaceChildren(makeElement("p", { textContent: t("Starting authentication flow…") }));
    let flow;
    try {
      flow = await api.startMcpAuth({
        ...scopeParams(),
        name: server.name,
        scope: server.scope,
        sourcePath: text(server.source_path),
        reauthenticate,
      });
    } catch (error) {
      container.replaceChildren(
        makeElement("p", {
          className: "workspace-mcp-caps-error",
          textContent: error instanceof Error ? error.message : String(error),
        }),
      );
      return;
    }
    const wrap = makeElement("div", { className: "workspace-mcp-auth-flow" });
    const info = makeElement("p", { textContent: t("Open the following address in your browser to complete authorization; the system detects the callback automatically once done.") });
    const link = makeElement("a", {
      className: "workspace-mcp-auth-link",
      textContent: text(flow.authorization_url),
      attributes: { href: text(flow.authorization_url), target: "_blank", rel: "noopener noreferrer" },
    });
    const manualRow = makeElement("div", { className: "workspace-mcp-auth-manual" });
    const manualInput = makeTextInput("workspace-mcp-auth-callback", t("If automatic detection fails, paste the callback URL"));
    const manualButton = makeButton(t("Submit callback URL"), "workspace-mcp-auth-complete");
    manualRow.append(manualInput, manualButton);
    const cancelButton = makeButton(t("Cancel authentication"), "workspace-mcp-auth-cancel");
    const authStatus = makeElement("p", { className: "workspace-mcp-auth-status" });
    wrap.append(info, link, manualRow, cancelButton, authStatus);
    container.replaceChildren(wrap);

    let settled = false;
    const finish = async (message, isError) => {
      if (settled) return;
      settled = true;
      authStatus.textContent = text(message);
      authStatus.classList.toggle("is-error", Boolean(isError));
      if (!isError) {
        await reload();
      }
    };
    manualButton.addEventListener("click", async () => {
      const url = manualInput.value.trim();
      if (!url) return;
      manualButton.disabled = true;
      try {
        await api.completeMcpAuth(flow.flow_id, url);
        await finish(t("Authentication succeeded."), false);
      } catch (error) {
        manualButton.disabled = false;
        authStatus.textContent = error instanceof Error ? error.message : String(error);
        authStatus.classList.toggle("is-error", true);
      }
    });
    cancelButton.addEventListener("click", async () => {
      if (settled) return;
      settled = true;
      try {
        await api.cancelMcpAuth(flow.flow_id);
      } catch (_error) {
        /* 已尽力取消 */
      }
      authStatus.textContent = t("Authentication canceled.");
    });
    // 后台阻塞等待回调(loopback 服务器捕获授权码)。
    api
      .waitMcpAuth(flow.flow_id)
      .then(() => finish(t("Authentication succeeded."), false))
      .catch((error) => {
        if (!settled) {
          authStatus.textContent = error instanceof Error ? error.message : String(error);
          authStatus.classList.toggle("is-error", true);
        }
      });
  }

  // ── 单个服务器操作 ────────────────────────────────────────────────
  async function runAction(fn, successMessage) {
    showStatus(t("Processing…"));
    try {
      await fn();
      await reload();
      if (successMessage) flashStatus(successMessage);
      else clearStatus();
    } catch (error) {
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  }

  function makeServerCard(server) {
    const key = serverKey(server);
    const state = expanded.get(key) || {};
    const row = makeElement("div", { className: "workspace-mcp-server" });
    if (server.disabled) row.classList.toggle("is-disabled", true);

    const head = makeElement("div", { className: "workspace-mcp-server-head" });
    const main = makeElement("div", { className: "workspace-mcp-server-main" });
    main.append(makeElement("strong", { className: "workspace-mcp-server-name", textContent: text(server.name) }));
    const badges = makeElement("div", { className: "workspace-mcp-badges" });
    badges.append(makeBadge(MCP_SCOPE_LABELS[server.scope] || server.scope, `workspace-mcp-scope-${server.scope}`));
    badges.append(makeBadge(MCP_TRANSPORT_LABELS[server.transport] || server.transport, "workspace-mcp-transport"));
    // 连接状态 pending-approval 与下方专用「待审批」徽标信息重复,择一显示,避免双徽标。
    if (server.connection_state !== "pending-approval") {
      badges.append(
        makeBadge(
          MCP_CONNECTION_LABELS[server.connection_state] || server.connection_state,
          `workspace-mcp-state ${mcpConnectionClass(server.connection_state)}`,
        ),
      );
    }
    if (server.approval_state === "pending-approval") {
      badges.append(makeBadge(t("Pending approval"), "workspace-mcp-state is-pending"));
    }
    main.append(badges);

    // 启停开关(pending-approval 时不提供启用,仅可审批/拒绝/禁用/删除)。
    const toggle = makeElement("label", { className: "workspace-switch" });
    const checkbox = makeElement("input", { attributes: { type: "checkbox" } });
    checkbox.checked = !server.disabled;
    toggle.append(checkbox, makeElement("span", { className: "workspace-switch-track", attributes: { "aria-hidden": "true" } }));
    checkbox.addEventListener("change", () => {
      runAction(
        () =>
          api.setMcpEnabled({
            ...scopeParams(),
            name: server.name,
            scope: server.scope,
            disabled: !checkbox.checked,
            sourcePath: text(server.source_path),
          }),
        checkbox.checked ? t("Enabled.") : t("Disabled."),
      );
    });
    head.append(main, toggle);
    row.append(head);

    // 元信息行
    const meta = makeElement("div", { className: "workspace-mcp-server-meta" });
    const endpoint = server.transport === "stdio" ? text(server.command) : text(server.url);
    if (endpoint) meta.append(makeElement("code", { className: "workspace-mcp-endpoint", textContent: endpoint }));
    meta.append(
      makeElement("span", {
        className: "workspace-mcp-counts",
        textContent: t("Tools {tools} · Resources {resources} · Prompts {prompts}", { tools: text(server.tools ?? "-"), resources: text(server.resources ?? "-"), prompts: text(server.prompts ?? "-") }),
      }),
    );
    const authIsRemote = server.transport !== "stdio";
    const authUnconfigured =
      !server.auth_state || server.auth_state === "not-configured" || server.auth_state === "not_configured";
    if (!authUnconfigured) {
      meta.append(makeElement("span", { className: "workspace-mcp-auth", textContent: t("Auth: {state}", { state: MCP_AUTH_LABELS[server.auth_state] || server.auth_state }) }));
    } else if (authIsRemote) {
      // 远程服务器即便离线未认证也应展示状态:支持认证但尚未进行(含动态注册,配置无 oauth 段)。
      meta.append(makeElement("span", { className: "workspace-mcp-auth", textContent: t("Auth: not performed") }));
    }
    row.append(meta);
    if (server.latest_failure) {
      row.append(makeElement("p", { className: "workspace-mcp-failure", textContent: text(server.latest_failure) }));
    }
    if (server.source_path) {
      row.append(makeElement("p", { className: "workspace-mcp-source", textContent: text(server.source_path) }));
    }

    // 操作栏
    const actions = makeElement("div", { className: "workspace-mcp-server-actions" });
    const capsContainer = makeElement("div", { className: "workspace-mcp-caps" });
    capsContainer.hidden = !state.caps;
    const authContainer = makeElement("div", { className: "workspace-mcp-auth-region" });
    authContainer.hidden = true;

    if (server.approval_state === "pending-approval") {
      const approve = makeButton(t("Approve"), "workspace-mcp-approve", "workspace-action workspace-action-primary");
      approve.addEventListener("click", () =>
        runAction(() => api.setMcpApproval({ ...scopeParams(), name: server.name, decision: "approve" }), t("Approved.")),
      );
      const reject = makeButton(t("Reject"), "workspace-mcp-reject");
      reject.addEventListener("click", () =>
        runAction(() => api.setMcpApproval({ ...scopeParams(), name: server.name, decision: "reject" }), t("Rejected.")),
      );
      actions.append(approve, reject);
    }

    if (!server.disabled && server.approval_state !== "pending-approval") {
      const checkButton = makeButton(t("Check connection"), "workspace-mcp-check");
      checkButton.addEventListener("click", async () => {
        checkButton.disabled = true;
        showStatus(t("Checking {name}…", { name: server.name }));
        try {
          const payload = await api.checkMcpServers({
            ...scopeParams(),
            name: server.name,
            scope: server.scope,
            sourcePath: text(server.source_path),
          });
          mergeCheckResults(payload.servers || []);
          renderList();
          clearStatus();
        } catch (error) {
          showStatus(error instanceof Error ? error.message : String(error), true);
        } finally {
          checkButton.disabled = false;
        }
      });
      actions.append(checkButton);

      const capsButton = makeButton(state.caps ? t("Hide capabilities") : t("View capabilities"), "workspace-mcp-caps-toggle");
      capsButton.addEventListener("click", () => {
        // 展开/收起不会重建卡片,故须从 expanded map 现读当前状态,
        // 不能用建卡时捕获的 state 闭包(否则收起会算成再次展开并重新拉能力)。
        const current = expanded.get(key) || {};
        const next = !current.caps;
        expanded.set(key, { ...current, caps: next });
        capsContainer.hidden = !next;
        capsButton.textContent = next ? t("Hide capabilities") : t("View capabilities");
        if (next) loadCapabilities(server, capsContainer);
      });
      actions.append(capsButton);
    }

    // 远程传输(http/sse/ws)都可能需要 OAuth 认证,含动态客户端注册:配置里没有 oauth 段、
    // 离线也没有 configured/stored client_id。因此只要是远程即提供「认证/清除认证」操作,
    // 与 /mcp 状态面板判定一致;本地 stdio 命令不涉及身份验证,不显示。
    const remote = server.transport !== "stdio";
    const hasOauth = remote;
    if (hasOauth) {
      const authed = server.auth_state === "authenticated";
      const authButton = makeButton(authed ? t("Re-authenticate") : t("Authenticate"), "workspace-mcp-auth-start");
      authButton.addEventListener("click", () => {
        authContainer.hidden = false;
        runAuthFlow(server, authContainer, { reauthenticate: authed });
      });
      actions.append(authButton);
      const resetButton = makeButton(t("Clear authentication"), "workspace-mcp-reset-auth");
      resetButton.addEventListener("click", () =>
        runAction(
          () =>
            api.resetMcpAuth({
              ...scopeParams(),
              name: server.name,
              scope: server.scope,
              sourcePath: text(server.source_path),
            }),
          t("Authentication state cleared."),
        ),
      );
      actions.append(resetButton);
    }

    const editButton = makeButton(t("Edit"), "workspace-mcp-edit");
    editButton.addEventListener("click", () => openEditForm(server));
    actions.append(editButton);

    const removeButton = makeButton(t("Delete"), "workspace-mcp-remove", "workspace-action workspace-action-danger");
    removeButton.addEventListener("click", () => {
      if (typeof window !== "undefined" && window.confirm && !window.confirm(t("Delete MCP server {name}?", { name: server.name }))) {
        return;
      }
      runAction(
        () =>
          api.removeMcpServer({
            ...scopeParams(),
            name: server.name,
            scope: server.scope,
            sourcePath: text(server.source_path),
          }),
        t("Deleted."),
      );
    });
    actions.append(removeButton);

    row.append(actions, authContainer, capsContainer);
    if (state.caps) loadCapabilities(server, capsContainer);
    return row;
  }

  function mergeCheckResults(checked) {
    const byKey = new Map(checked.map((item) => [serverKey(item), item]));
    servers = servers.map((server) => {
      const match = byKey.get(serverKey(server));
      return match ? { ...server, ...match } : server;
    });
  }

  function renderWarnings(warnings) {
    if (!warnings || !warnings.length) {
      warningsBox.hidden = true;
      warningsBox.replaceChildren();
      return;
    }
    warningsBox.hidden = false;
    warningsBox.replaceChildren(
      ...warnings.map((warning) =>
        makeElement("p", {
          className: "workspace-mcp-warning",
          textContent: `${warning.server_name ? `${warning.server_name}:` : ""}${text(warning.message)}`,
        }),
      ),
    );
  }

  function renderList() {
    if (!servers.length) {
      list.replaceChildren(
        makeElement("div", { className: "workspace-mcp-empty", textContent: t("No MCP servers configured yet. Click Add server to add one.") }),
      );
      return;
    }
    list.replaceChildren(...servers.map(makeServerCard));
  }

  const defaultCwd = () => {
    const sessionCwd = text(context.session()?.cwd || "");
    if (sessionCwd && projects.some((item) => item.cwd === sessionCwd)) return sessionCwd;
    const current = projects.find((item) => item.current);
    return current ? current.cwd : text(projects[0]?.cwd || "");
  };

  const populateProjectSelect = () => {
    projectSelect.replaceChildren(
      ...projects.map((item) =>
        makeElement("option", {
          textContent: item.current ? t("{label} (current)", { label: item.label }) : item.label,
          attributes: { value: item.cwd },
        }),
      ),
    );
    projectSelect.disabled = projects.length === 0;
    if (selectedCwd) projectSelect.value = selectedCwd;
  };

  const loadProjects = async () => {
    try {
      const payload = await api.listProjects();
      projects = Array.isArray(payload?.projects) ? payload.projects : [];
    } catch (_error) {
      projects = [];
    }
    if (!projects.some((item) => item.cwd === selectedCwd)) selectedCwd = defaultCwd();
    populateProjectSelect();
  };

  const fetchAndRender = async (requestedSessionId, token) => {
    const payload = await api.getMcpServers(scopeParams());
    if (isStale(requestedSessionId, token)) return false;
    servers = Array.isArray(payload.servers) ? payload.servers : [];
    renderWarnings(payload.warnings);
    renderList();
    clearStatus();
    return true;
  };

  async function reload() {
    const requestedSessionId = context.sessionId();
    const token = ++requestToken;
    try {
      await fetchAndRender(requestedSessionId, token);
    } catch (error) {
      if (isStale(requestedSessionId, token)) return;
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  }

  // 创建/编辑后自动在后台探测该服务器,让卡片尽快显示真实连接状态与能力,
  // 无需用户再手动点「检查连接」并等待网络往返。失败保持静默(手动检查仍可用)。
  async function autoCheckServer(serverName, serverScope) {
    const requestedSessionId = context.sessionId();
    const token = requestToken;
    try {
      const payload = await api.checkMcpServers({ ...scopeParams(), name: serverName, scope: serverScope });
      if (isStale(requestedSessionId, token)) return;
      mergeCheckResults(payload.servers || []);
      renderList();
    } catch (_error) {
      // 静默:创建本身已成功,连接探测失败不应打断用户。
    }
  }

  const loadAll = async () => {
    const requestedSessionId = context.sessionId();
    const token = ++requestToken;
    showStatus(t("Loading MCP servers…"));
    try {
      await loadProjects();
      if (isStale(requestedSessionId, token)) return;
      if (await fetchAndRender(requestedSessionId, token)) loaded = true;
    } catch (error) {
      if (isStale(requestedSessionId, token)) return;
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  };

  addButton.addEventListener("click", openAddForm);
  refreshButton.addEventListener("click", () => {
    expanded.clear();
    loadAll();
  });
  checkAllButton.addEventListener("click", async () => {
    checkAllButton.disabled = true;
    showStatus(t("Checking all connections…"));
    try {
      const payload = await api.checkMcpServers(scopeParams());
      mergeCheckResults(payload.servers || []);
      renderList();
      clearStatus();
    } catch (error) {
      showStatus(error instanceof Error ? error.message : String(error), true);
    } finally {
      checkAllButton.disabled = false;
    }
  });
  projectSelect.addEventListener("change", async () => {
    selectedCwd = projectSelect.value;
    expanded.clear();
    const requestedSessionId = context.sessionId();
    const token = ++requestToken;
    showStatus(t("Loading MCP servers…"));
    try {
      await fetchAndRender(requestedSessionId, token);
    } catch (error) {
      if (isStale(requestedSessionId, token)) return;
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  });

  panel.append(heading, projectPickerRow, groupHead, card);

  return {
    panel,
    activate() {
      if (!loaded) loadAll();
    },
    reset() {
      requestToken += 1;
      loaded = false;
      servers = [];
      projects = [];
      selectedCwd = "";
      expanded.clear();
      addFormMount.hidden = true;
      addFormMount.replaceChildren();
      list.replaceChildren();
      warningsBox.hidden = true;
      warningsBox.replaceChildren();
      clearStatus();
    },
  };
}

// 已归档对话面板:复刻 Codex「已归档对话」管理界面。
//   类型筛选:全部聊天(本应用会话皆为普通聊天,无「本地/云端」区分)。
//   排序方式:更新时间 / 创建时间 / 按字母顺序。
//   项目筛选:所有项目 / 各项目(本应用无「聊天/已安排任务」桶)。
const ARCHIVED_TYPE_LABELS = { all: t("All chats") };
const ARCHIVED_SORT_LABELS = { updated: t("Updated"), created: t("Created"), alpha: t("Alphabetical") };

// 项目路径 → 短标签,与侧边栏「项目/会话列表」完全同一规则(app.js 的
// projectDisplayLabels 为规范实现):默认取最后一段目录名,仅当多个项目末段
// 重名时才逐级向上追加父目录直到唯一。归档界面直接复用此规则,避免展示全路径。
function archivedPathParts(value) {
  const normalized = text(value)
    .replace(/\\/g, "/")
    .replace(/\/+$/u, "");
  return normalized.split("/").filter(Boolean);
}

function archivedProjectSuffixLabel(key, depth) {
  const parts = archivedPathParts(key);
  if (parts.length === 0) {
    return "Local project";
  }
  return parts.slice(-Math.min(depth, parts.length)).join("/");
}

function archivedProjectDisplayLabels(keys = []) {
  const normalizedKeys = [...new Set(keys.map(text).filter(Boolean))];
  return Object.fromEntries(
    normalizedKeys.map((key) => {
      const parts = archivedPathParts(key);
      let depth = 1;
      let label = archivedProjectSuffixLabel(key, depth);
      while (
        depth < Math.max(1, parts.length) &&
        normalizedKeys.filter((candidate) => archivedProjectSuffixLabel(candidate, depth) === label).length > 1
      ) {
        depth += 1;
        label = archivedProjectSuffixLabel(key, depth);
      }
      return [key, label];
    }),
  );
}

// 把 ISO 时间戳格式化为「YYYY年M月D日,HH:MM」(精确到分钟);无法解析则返回空串。
function formatArchivedDate(value) {
  const raw = text(value);
  if (!raw) {
    return "";
  }
  const time = new Date(raw).getTime();
  if (!Number.isFinite(time)) {
    return "";
  }
  const date = new Date(time);
  const pad = (n) => String(n).padStart(2, "0");
  return t("{year}-{month}-{day} {hh}:{mm}", { year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(), hh: pad(date.getHours()), mm: pad(date.getMinutes()) });
}

function archivedSessionTitle(session) {
  const title = text(session.title);
  if (title && title !== "(empty)") {
    return title;
  }
  return text(session.sessionId) || t("Conversation");
}

function archivedSessionId(session) {
  return text(session.webSessionId || session.sessionId);
}

function createArchivedPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-archived-panel",
    attributes: { "data-workspace-panel": "archived" },
  });

  // ── 顶栏:标题 + 「全部删除」──────────────────────────────
  const header = makeElement("div", { className: "workspace-archived-header" });
  const heading = makeElement("h3", { className: "workspace-archived-title", textContent: t("Archived conversations") });
  const deleteAllButton = makeElement("button", {
    className: "workspace-archived-delete-all",
    textContent: t("Delete all"),
    attributes: { type: "button" },
    dataset: { workspaceAction: "workspace-archived-delete-all" },
  });
  header.append(heading, deleteAllButton);

  // ── 筛选栏:搜索 + 类型/排序下拉 + 项目下拉 ───────────────
  const filters = makeElement("div", { className: "workspace-archived-filters" });
  const searchInput = makeTextInput("workspace-archived-search", t("Search archived chats"));
  searchInput.className = "workspace-input workspace-archived-search";
  // 搜索框内嵌放大镜图标(Codex 同款,位于左侧),用包裹层 ::before 承载。
  const searchWrap = makeElement("div", { className: "workspace-archived-search-wrap" });
  searchWrap.append(searchInput);

  const typeDropdown = makeArchivedTypeSortDropdown();
  const projectDropdown = makeArchivedProjectDropdown();
  filters.append(searchWrap, typeDropdown.wrap, projectDropdown.wrap);

  const groupsContainer = makeElement("div", {
    className: "workspace-archived-groups",
    dataset: { workspaceAction: "workspace-archived-groups" },
  });
  const status = makeElement("p", { className: "workspace-archived-status" });
  status.hidden = true;

  panel.append(header, filters, groupsContainer, status);

  let requestToken = 0;
  let allProjects = [];
  // 当前处于「确认删除」态的按钮(面板级单例,点击别处复位)。
  let armedDelete = null;
  // 当前展开的项目「…」菜单({ menu, close });点击别处关闭。
  let openGroupMenu = null;

  const disarmDelete = () => {
    if (armedDelete) {
      armedDelete.disarm();
      armedDelete = null;
    }
  };
  const closeGroupMenu = () => {
    if (openGroupMenu) {
      openGroupMenu.close();
      openGroupMenu = null;
    }
  };

  const showStatus = (message, isError = false) => {
    status.hidden = false;
    status.textContent = text(message);
    status.classList.toggle("is-error", Boolean(isError));
  };
  const clearStatus = () => {
    status.hidden = true;
    status.textContent = "";
    status.classList.toggle("is-error", false);
  };

  // 两段式确认:首次点击进入「确认…」态,再次点击执行 onConfirm。
  // 进入确认态时先复位其它已武装按钮;点击别处由面板级监听复位。
  const armOrConfirm = (button, confirmLabel, onConfirm) => {
    if (armedDelete && armedDelete.button === button) {
      disarmDelete();
      onConfirm();
      return;
    }
    disarmDelete();
    const original = button.textContent;
    button.classList.toggle("is-confirming", true);
    button.textContent = confirmLabel;
    armedDelete = {
      button,
      disarm() {
        button.classList.toggle("is-confirming", false);
        button.textContent = original;
      },
    };
  };

  const currentSort = () => typeDropdown.sort();
  const currentProject = () => projectDropdown.value();

  // 按当前筛选/排序/搜索计算要展示的项目分组。
  const visibleGroups = () => {
    const query = searchInput.value.trim().toLowerCase();
    const sort = currentSort();
    const projectFilter = currentProject();

    const groups = allProjects
      .filter((group) => {
        if (projectFilter.startsWith("cwd:")) {
          return group.cwd === projectFilter.slice(4);
        }
        return true;
      })
      .map((group) => {
        const sessions = (group.sessions || []).filter((session) => {
          if (!query) {
            return true;
          }
          return archivedSessionTitle(session).toLowerCase().includes(query);
        });
        return { ...group, sessions };
      })
      .filter((group) => group.sessions.length > 0);

    for (const group of groups) {
      group.sessions = sortArchivedSessions(group.sessions, sort);
    }
    return groups;
  };

  const reload = async () => {
    const token = ++requestToken;
    showStatus(t("Loading archived conversations…"));
    try {
      const payload = await api.listArchivedSessions();
      if (token !== requestToken) {
        return;
      }
      allProjects = Array.isArray(payload?.projects) ? payload.projects : [];
      // 分组头与项目下拉都改用侧边栏同款短标签(重名才逐级消歧),不再展示全路径。
      const displayLabels = archivedProjectDisplayLabels(allProjects.map((group) => group.cwd));
      for (const group of allProjects) {
        group.label = displayLabels[text(group.cwd)] || archivedProjectSuffixLabel(group.cwd, 1);
      }
      projectDropdown.setProjects(allProjects);
      render();
      clearStatus();
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  };

  const runAction = async (label, thunk) => {
    const token = ++requestToken;
    showStatus(label);
    try {
      await thunk();
      if (token !== requestToken) {
        return;
      }
      await reload();
      // 取消归档/删除会改变主侧栏的活动会话与项目分组,通知外层刷新侧栏。
      context.onSessionsMutated?.();
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      showStatus(error instanceof Error ? error.message : String(error), true);
    }
  };

  const makeSessionRow = (session) => {
    const row = makeElement("div", { className: "workspace-archived-item" });
    const info = makeElement("div", { className: "workspace-archived-item-info" });
    info.append(
      makeElement("span", { className: "workspace-archived-item-title", textContent: archivedSessionTitle(session) }),
      makeElement("span", { className: "workspace-archived-item-date", textContent: formatArchivedDate(session.updatedAt || session.createdAt) }),
    );

    const actions = makeElement("div", { className: "workspace-archived-item-actions" });
    const trash = makeElement("button", {
      className: "workspace-archived-item-trash",
      attributes: { type: "button", "aria-label": t("Delete conversation"), title: t("Delete conversation") },
    });
    trash.addEventListener("click", (event) => {
      event.stopPropagation();
      armOrConfirm(trash, t("Confirm delete?"), () => {
        void runAction(t("Deleting…"), () => api.deleteSession(archivedSessionId(session)));
      });
    });
    const unarchive = makeElement("button", {
      className: "workspace-archived-item-unarchive",
      textContent: t("Unarchive"),
      attributes: { type: "button" },
    });
    unarchive.addEventListener("click", (event) => {
      event.stopPropagation();
      void runAction(t("Unarchiving…"), () => api.updateSession(archivedSessionId(session), { archived: false }));
    });
    actions.append(trash, unarchive);
    row.append(info, actions);
    return row;
  };

  const makeGroupSection = (group) => {
    const section = makeElement("div", { className: "workspace-archived-group" });
    const groupHeader = makeElement("div", { className: "workspace-archived-group-head" });
    const label = makeElement("span", { className: "workspace-archived-group-label" });
    label.append(
      makeElement("span", { className: "workspace-archived-group-icon", attributes: { "aria-hidden": "true" } }),
      makeElement("span", { className: "workspace-archived-group-name", textContent: text(group.label) || text(group.cwd) }),
      makeElement("small", { className: "workspace-archived-group-count", textContent: t("{count} chats", { count: group.sessions.length }) }),
    );

    // 「…」菜单:唯一项「删除项目中的全部内容」(红)。
    const menuWrap = makeElement("div", { className: "workspace-archived-group-menu" });
    const menuButton = makeElement("button", {
      className: "workspace-archived-group-menu-button",
      attributes: { type: "button", "aria-label": t("Project actions"), title: t("Project actions") },
    });
    const menu = makeElement("div", { className: "workspace-archived-group-menu-popover", attributes: { role: "menu" } });
    menu.hidden = true;
    const deleteProjectItem = makeElement("button", {
      className: "workspace-archived-group-menu-item is-danger",
      textContent: t("Delete all content in project"),
      attributes: { type: "button", role: "menuitem" },
    });
    deleteProjectItem.addEventListener("click", (event) => {
      event.stopPropagation();
      // 首次点击只在展开的浮层内把该项 arm 成「确认删除?」(不再立刻关浮层,否则确认态被
      // 藏进已隐藏的菜单里,看起来像「点了没反应」);再次点同一项才真正删除,此时收起菜单。
      armOrConfirm(deleteProjectItem, t("Confirm delete?"), () => {
        closeGroupMenu();
        void runAction(t("Deleting project content…"), () => api.deleteArchivedSessions(group.cwd));
      });
    });
    menu.append(deleteProjectItem);
    menuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      if (openGroupMenu && openGroupMenu.menu === menu) {
        closeGroupMenu();
        return;
      }
      closeGroupMenu();
      menu.hidden = false;
      openGroupMenu = { menu, close: () => { menu.hidden = true; } };
    });
    menuWrap.append(menuButton, menu);

    groupHeader.append(label, menuWrap);
    const list = makeElement("div", { className: "workspace-archived-group-list" });
    for (const session of group.sessions) {
      list.append(makeSessionRow(session));
    }
    section.append(groupHeader, list);
    return section;
  };

  function render() {
    disarmDelete();
    closeGroupMenu();
    const groups = visibleGroups();
    const hasAny = allProjects.some((group) => (group.sessions || []).length > 0);
    deleteAllButton.disabled = !hasAny;
    if (!groups.length) {
      const message = hasAny ? t("No matching archived conversations.") : t("No archived conversations yet.");
      groupsContainer.replaceChildren(
        makeElement("div", { className: "workspace-archived-empty", textContent: message }),
      );
      return;
    }
    groupsContainer.replaceChildren(...groups.map(makeGroupSection));
  }

  deleteAllButton.addEventListener("click", (event) => {
    event.stopPropagation();
    armOrConfirm(deleteAllButton, t("Confirm delete all?"), () => {
      void runAction(t("Deleting all…"), () => api.deleteArchivedSessions());
    });
  });
  searchInput.addEventListener("input", render);
  typeDropdown.onChange(render);
  projectDropdown.onChange(render);

  // 点击面板外部:复位确认态与展开的项目菜单(仅浏览器环境)。
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("click", (event) => {
      if (armedDelete && !armedDelete.button.contains?.(event.target)) {
        disarmDelete();
      }
      if (openGroupMenu && !openGroupMenu.menu.parentNode?.contains?.(event.target)) {
        closeGroupMenu();
      }
    });
  }

  return {
    panel,
    activate() {
      // 每次切到该标签都重新拉取,保证刚归档的会话立即出现(无需刷新页面)。
      void reload();
    },
    reset() {
      requestToken += 1;
      allProjects = [];
      disarmDelete();
      closeGroupMenu();
      searchInput.value = "";
      typeDropdown.reset();
      projectDropdown.reset();
      groupsContainer.replaceChildren();
      clearStatus();
    },
  };
}

// 按更新时间(默认)/创建时间倒序、或按标题字母升序排列会话。
function sortArchivedSessions(sessions, sort) {
  const rows = [...sessions];
  if (sort === "alpha") {
    rows.sort((left, right) =>
      archivedSessionTitle(left).localeCompare(archivedSessionTitle(right), "zh-Hans-CN"),
    );
    return rows;
  }
  const key = sort === "created" ? "createdAt" : "updatedAt";
  rows.sort((left, right) => String(text(right[key])).localeCompare(String(text(left[key]))));
  return rows;
}

// 类型 + 排序合并下拉:一个触发按钮展开带「类型」「排序方式」两组单选项的浮层。
function makeArchivedTypeSortDropdown() {
  let typeValue = "all";
  let sortValue = "updated";
  const changeHandlers = [];

  const trigger = makeElement("button", {
    // --filter 修饰类给 trigger 前置漏斗筛选图标(Codex 同款)。
    className: "workspace-archived-dropdown-trigger workspace-archived-dropdown-trigger--filter",
    attributes: { type: "button" },
  });
  const triggerLabel = makeElement("span", { className: "workspace-archived-dropdown-label" });
  const caret = makeElement("span", { className: "workspace-archived-dropdown-caret", attributes: { "aria-hidden": "true" } });
  trigger.append(triggerLabel, caret);

  const menu = makeElement("div", { className: "workspace-archived-dropdown-menu", attributes: { role: "menu" } });
  menu.hidden = true;
  const wrap = makeElement("div", { className: "workspace-archived-dropdown" });
  wrap.append(trigger, menu);

  const emitChange = () => {
    for (const handler of changeHandlers) {
      handler();
    }
  };
  const closeMenu = () => {
    menu.hidden = true;
    trigger.classList.toggle("is-open", false);
  };
  const syncTriggerLabel = () => {
    triggerLabel.textContent = ARCHIVED_TYPE_LABELS[typeValue] || ARCHIVED_TYPE_LABELS.all;
  };

  const renderMenu = () => {
    menu.replaceChildren();
    const typeSection = makeElement("div", { className: "workspace-archived-dropdown-section" });
    typeSection.append(makeElement("p", { className: "workspace-archived-dropdown-section-title", textContent: t("Type") }));
    for (const [value, label] of Object.entries(ARCHIVED_TYPE_LABELS)) {
      typeSection.append(
        makeArchivedDropdownOption(label, typeValue === value, () => {
          typeValue = value;
          syncTriggerLabel();
          renderMenu();
          closeMenu();
          emitChange();
        }),
      );
    }
    const sortSection = makeElement("div", { className: "workspace-archived-dropdown-section" });
    sortSection.append(makeElement("p", { className: "workspace-archived-dropdown-section-title", textContent: t("Sort by") }));
    for (const [value, label] of Object.entries(ARCHIVED_SORT_LABELS)) {
      sortSection.append(
        makeArchivedDropdownOption(label, sortValue === value, () => {
          sortValue = value;
          renderMenu();
          closeMenu();
          emitChange();
        }),
      );
    }
    menu.append(typeSection, sortSection);
  };

  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    if (menu.hidden) {
      renderMenu();
      menu.hidden = false;
      trigger.classList.toggle("is-open", true);
    } else {
      closeMenu();
    }
  });
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("click", (event) => {
      if (!menu.hidden && !wrap.contains(event.target)) {
        closeMenu();
      }
    });
  }
  syncTriggerLabel();

  return {
    wrap,
    type: () => typeValue,
    sort: () => sortValue,
    onChange(handler) {
      changeHandlers.push(handler);
    },
    reset() {
      typeValue = "all";
      sortValue = "updated";
      syncTriggerLabel();
      closeMenu();
    },
  };
}

// 项目下拉:所有项目 / 各项目(文件夹图标)。
function makeArchivedProjectDropdown() {
  let value = "";
  let projects = [];
  const changeHandlers = [];

  const trigger = makeElement("button", {
    // --project 修饰类给 trigger 前置文件夹图标(Codex 同款,与分组头一致)。
    className: "workspace-archived-dropdown-trigger workspace-archived-dropdown-trigger--project",
    attributes: { type: "button" },
  });
  const triggerLabel = makeElement("span", { className: "workspace-archived-dropdown-label" });
  const caret = makeElement("span", { className: "workspace-archived-dropdown-caret", attributes: { "aria-hidden": "true" } });
  trigger.append(triggerLabel, caret);

  const menu = makeElement("div", { className: "workspace-archived-dropdown-menu", attributes: { role: "menu" } });
  menu.hidden = true;
  const wrap = makeElement("div", { className: "workspace-archived-dropdown" });
  wrap.append(trigger, menu);

  const emitChange = () => {
    for (const handler of changeHandlers) {
      handler();
    }
  };
  const closeMenu = () => {
    menu.hidden = true;
    trigger.classList.toggle("is-open", false);
  };
  const labelFor = (val) => {
    if (val.startsWith("cwd:")) {
      const cwd = val.slice(4);
      const project = projects.find((item) => item.cwd === cwd);
      return project ? text(project.label) || cwd : cwd;
    }
    return t("All projects");
  };
  const syncTriggerLabel = () => {
    triggerLabel.textContent = labelFor(value);
  };
  const select = (val) => {
    value = val;
    syncTriggerLabel();
    renderMenu();
    closeMenu();
    emitChange();
  };

  const renderMenu = () => {
    menu.replaceChildren();
    menu.append(makeArchivedDropdownOption(t("All projects"), value === "", () => select("")));
    for (const project of projects) {
      const val = `cwd:${project.cwd}`;
      menu.append(
        makeArchivedDropdownOption(text(project.label) || text(project.cwd), value === val, () => select(val), {
          icon: "workspace-archived-dropdown-folder",
        }),
      );
    }
  };

  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    if (menu.hidden) {
      renderMenu();
      menu.hidden = false;
      trigger.classList.toggle("is-open", true);
    } else {
      closeMenu();
    }
  });
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("click", (event) => {
      if (!menu.hidden && !wrap.contains(event.target)) {
        closeMenu();
      }
    });
  }
  syncTriggerLabel();

  return {
    wrap,
    value: () => value,
    setProjects(next) {
      projects = Array.isArray(next) ? next : [];
      // 所选具体项目已消失时回落到「所有项目」。
      if (value.startsWith("cwd:") && !projects.some((item) => `cwd:${item.cwd}` === value)) {
        value = "";
      }
      syncTriggerLabel();
    },
    onChange(handler) {
      changeHandlers.push(handler);
    },
    reset() {
      value = "";
      projects = [];
      syncTriggerLabel();
      closeMenu();
    },
  };
}

function makeArchivedDropdownOption(label, active, onSelect, { icon = "" } = {}) {
  const option = makeElement("button", {
    className: `workspace-archived-dropdown-option${active ? " is-active" : ""}`,
    attributes: { type: "button", role: "menuitemradio", "aria-checked": active ? "true" : "false" },
  });
  if (icon) {
    option.append(makeElement("span", { className: `workspace-archived-dropdown-option-icon ${icon}`, attributes: { "aria-hidden": "true" } }));
  }
  option.append(makeElement("span", { className: "workspace-archived-dropdown-option-label", textContent: label }));
  option.append(makeElement("span", { className: "workspace-archived-dropdown-option-check", attributes: { "aria-hidden": "true" } }));
  option.addEventListener("click", (event) => {
    event.stopPropagation();
    onSelect();
  });
  return option;
}

// 「外来会话」开关:构造一个与自动记忆一致的 workspace-switch(label > input[checkbox]
// + track span),返回 { control, input } 供 makeField 消费。
function makeForeignSwitch(marker) {
  const input = makeElement("input", {
    attributes: { type: "checkbox" },
    dataset: { workspaceAction: marker },
  });
  const control = makeElement("label", { className: "workspace-switch" });
  control.append(
    input,
    makeElement("span", { className: "workspace-switch-track", attributes: { "aria-hidden": "true" } }),
  );
  return { control, input };
}

// 新会话默认权限下拉的取值/文案,与 composer 的 PERMISSION_MODE_OPTIONS 对齐。后端强校验取值。
// 默认模式下拉不用常量:选项由 context.pipelineOptions 动态派生(普通模式 + 每条流水线)。
const SESSION_DEFAULT_PERMISSION_OPTIONS = [
  { value: "default", label: t("Approval requested") },
  { value: "accept_edits", label: t("Approve for me") },
  { value: "bypass_permissions", label: t("Full access") },
  { value: "dont_ask", label: t("Don't ask") },
];

// 「其他」面板:控制非 web 入口产生的外来会话是否在侧栏出现。开关切换后即写回后端,
// 成功再通知 app.js 刷新侧栏,让被隐藏/显露的会话随之增减。
function createOtherPanel(api, context) {
  // preview: 该主题的 [bg, panel-raised, unread] 三色字面量,用于卡片预览色带。
  // 不能用 var(--codex-*) + 嵌套 data-theme:主题块作用域为 :root(=<html>),
  // 嵌套元素不会重新解析,故预览色带用字面量色。
  const THEME_OPTIONS = [
    { slug: "graphite", name: t("Graphite"), preview: ["#1a1a1a", "#2b2b2b", "#4c8dff"] },
    { slug: "midnight", name: t("Midnight blue"), preview: ["#12161f", "#1e2534", "#5b9cff"] },
    { slug: "evergreen", name: t("Evergreen"), preview: ["#141a17", "#1f2a24", "#58b98a"] },
    { slug: "sepia", name: t("Sepia"), preview: ["#1c1815", "#2a231d", "#d8aa70"] },
    { slug: "ivory", name: t("Ivory"), preview: ["#f6f6f3", "#ffffff", "#2f6fed"] },
  ];
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-other-panel",
    attributes: { "data-workspace-panel": "other" },
  });
  const heading = makeElement("h3", { textContent: t("General") });

  const groupHead = makeElement("div", { className: "workspace-settings-group-head" });
  groupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Foreign session visibility") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Sessions created outside the web entry point (CLI / A2A / ACP, etc.) are hidden by default; reveal them here as needed."),
    }),
  );

  const pipelineToggle = makeForeignSwitch("workspace-foreign-pipeline");
  const normalToggle = makeForeignSwitch("workspace-foreign-normal");
  const reviewStepToggle = makeForeignSwitch("workspace-pipeline-review-step");
  const devModeToggle = makeForeignSwitch("workspace-developer-mode");
  const status = makeElement("span", { className: "workspace-memory-status workspace-foreign-status" });
  // 「已保存」是瞬时反馈:安排一个自动淡出定时器,避免它永久驻留在面板里。
  // 任何新的 stamp / clearStatus 都先取消挂起的定时器,防止旧定时器抹掉更新后的状态。
  let clearTimer = null;
  const cancelClear = () => {
    if (clearTimer !== null) {
      clearTimeout(clearTimer);
      clearTimer = null;
    }
  };
  const stamp = (message, isError = false) => {
    cancelClear();
    status.textContent = text(message);
    status.classList.toggle("is-error", Boolean(isError));
  };
  // 成功提示("已保存")停留片刻后自动清除;token 保证仅当其后没有更新的请求时才清空。
  const stampSaved = (token) => {
    stamp(t("Saved"));
    clearTimer = setTimeout(() => {
      clearTimer = null;
      if (token === requestToken) {
        status.textContent = "";
        status.classList.remove("is-error");
      }
    }, 2200);
  };
  const clearStatus = () => {
    cancelClear();
    status.textContent = "";
    status.classList.remove("is-error");
  };

  const card = makeElement("section", {
    className: "workspace-settings-group workspace-settings-provider",
  });
  card.append(
    makeField(
      t("Show foreign pipeline sessions (read-only)"),
      pipelineToggle.control,
      t("When enabled, pipeline sessions created outside the web entry point appear in the list for read-only viewing."),
    ),
    makeField(
      t("Show foreign normal sessions (resumable)"),
      normalToggle.control,
      t("When enabled, normal sessions created outside the web entry point appear in the list and can be resumed and taken over in the web."),
    ),
  );

  // 开发者模式:打开后露出「开发」分页(承载失败工具标红开关与重启入口)。默认关闭。
  const devModeGroupHead = makeElement("div", { className: "workspace-settings-group-head" });
  devModeGroupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Developer mode") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("When enabled, a Developer settings tab appears with additional tools."),
    }),
  );
  const devModeCard = makeElement("section", {
    className: "workspace-settings-group workspace-settings-provider",
  });
  devModeCard.append(
    makeField(
      t("Enable developer mode"),
      devModeToggle.control,
      t("Reveals the Developer settings tab. Turning it off hides that tab again."),
    ),
  );

  // 售卖流水线:控制可选步骤。审查步骤(enable_reviewing)默认关闭;仅影响新发起的流水线运行,
  // 进行中的流水线保持发起时的配置(后端从 sidecar 读取冻结的特性开关)。
  const reviewStepGroupHead = makeElement("div", { className: "workspace-settings-group-head" });
  reviewStepGroupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Sales pipeline") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Control optional steps of the sales pipeline; affects only newly started pipeline runs. Running pipelines keep their original configuration."),
    }),
  );
  const reviewStepCard = makeElement("section", {
    className: "workspace-settings-group workspace-settings-provider",
  });
  reviewStepCard.append(
    makeField(
      t("Enable review step"),
      reviewStepToggle.control,
      t("When enabled, the sales pipeline appends a review round after generating the template (requires the infraguard tool); the backend skips it automatically when the tool is not installed."),
    ),
  );

  // 前置依赖提示:仅当检测到 infraguard 缺失且 web 端可安装时显露。默认隐藏,
  // 用 [hidden] 属性控制(配套 CSS 的 .review-step-prereq-notice[hidden]{display:none}
  // 覆盖 display,避免「hidden 属性遇 display 类被无视」的陷阱)。
  const prereqNotice = makeElement("div", {
    className: "review-step-prereq-notice",
    attributes: { hidden: "hidden", "data-workspace-action": "review-step-prereq" },
  });
  const prereqText = makeElement("p", {
    className: "review-step-prereq-text",
    textContent: t("The review step depends on the infraguard tool, which was not detected. Install it here with one click."),
  });
  const prereqInstallButton = makeButton(t("Install infraguard"), "review-step-install", "workspace-action");
  const prereqProgress = makeElement("div", {
    className: "review-step-prereq-progress",
    attributes: { hidden: "hidden" },
  });
  const prereqPhaseLabel = makeElement("span", { className: "review-step-prereq-phase" });
  const prereqBar = makeElement("div", { className: "prereq-progress-bar" });
  const prereqFill = makeElement("div", { className: "prereq-progress-fill" });
  prereqBar.append(prereqFill);
  prereqProgress.append(prereqPhaseLabel, prereqBar);
  prereqNotice.append(prereqText, prereqInstallButton, prereqProgress);
  reviewStepCard.append(prereqNotice);

  // 新会话默认:控制「新建会话」草稿的初始权限模式与会话模式,免去每次手动重选。
  // 仅影响新建草稿;已有会话保留各自存储的选择,重开时不受此处影响。
  const sessionDefaultsGroupHead = makeElement("div", { className: "workspace-settings-group-head" });
  sessionDefaultsGroupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("New session defaults") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Set the default permission and mode for new sessions; applies only to new sessions. Existing sessions keep their own choices."),
    }),
  );
  const permissionSelect = makeSelect("session-default-permission");
  for (const option of SESSION_DEFAULT_PERMISSION_OPTIONS) {
    permissionSelect.append(
      makeElement("option", { attributes: { value: option.value }, textContent: option.label }),
    );
  }
  // 「默认模式」复刻 composer 的二级弹出选择器(普通模式 / 流水线模式 → 子菜单选具体流水线),
  // 而非扁平下拉——与会话里的选择器保持一致。复用 composer 的 draft-session-* 类名统一外观,
  // 但为面板场景在 styles.css 用 .workspace-mode-picker 覆盖为向下展开。选项由 context.pipelineOptions 驱动。
  const pipelineOptions = context.pipelineOptions || [{ id: "selling", label: t("Sales pipeline") }];
  const fallbackPipelineName = pipelineOptions[0]?.id || "selling";
  // 当前选择:普通模式恒带一个合法 pipelineName(取首条),便于后端存储、切回流水线时有默认。
  let modeSelection = { mode: "normal", pipelineName: fallbackPipelineName };
  let modeMenuOpen = false;
  let pipelineSubmenuOpen = false;

  const pipelineLabelFor = (id) => {
    const match = pipelineOptions.find((option) => option.id === id);
    return match ? match.label || match.id : id || fallbackPipelineName;
  };
  // 校正存储值:流水线名不在选项内则回落首条,mode 非 pipeline 则归普通。
  const normalizeModeSelection = (mode, pipelineName) => {
    if (mode === "pipeline") {
      const wanted = String(pipelineName || "").trim();
      const match = pipelineOptions.find((option) => option.id === wanted) || pipelineOptions[0];
      if (match) {
        return { mode: "pipeline", pipelineName: match.id };
      }
    }
    return { mode: "normal", pipelineName: fallbackPipelineName };
  };

  const modePicker = makeElement("div", { className: "draft-session-picker draft-mode-picker workspace-mode-picker" });
  const modeTrigger = makeElement("button", {
    className: "draft-session-control",
    attributes: { type: "button", "aria-haspopup": "menu", "aria-expanded": "false" },
    dataset: { workspaceAction: "session-default-mode" },
  });
  const modeMenu = makeElement("div", { className: "draft-session-menu draft-mode-menu", attributes: { role: "menu" } });
  const pipelineSubmenu = makeElement("div", {
    className: "draft-session-menu draft-session-submenu draft-pipeline-submenu",
    attributes: { role: "menu" },
  });
  modePicker.append(modeTrigger, modeMenu, pipelineSubmenu);

  // composer makeDraftMenuItem 的本地等价物:图标 + 文案 + 勾选/子菜单箭头,类名一致以复用样式。
  const makeModeMenuItem = ({ iconClass, label, detail = "", active = false, submenu = false, onClick, onHover }) => {
    const button = makeElement("button", {
      className: ["draft-session-menu-item", active ? "is-active" : "", submenu ? "has-submenu" : ""]
        .filter(Boolean)
        .join(" "),
      attributes: { type: "button", role: submenu ? "menuitem" : "menuitemradio" },
    });
    if (!submenu) {
      button.setAttribute("aria-checked", active ? "true" : "false");
    }
    const icon = makeElement("span", {
      className: `draft-session-menu-icon ${iconClass}`,
      attributes: { "aria-hidden": "true" },
    });
    const copy = makeElement("span", { className: "draft-session-menu-copy" });
    copy.append(makeElement("span", { className: "draft-session-menu-label", textContent: label }));
    if (detail) {
      copy.append(makeElement("span", { className: "draft-session-menu-detail", textContent: detail }));
    }
    const check = makeElement("span", {
      className: "draft-session-menu-check",
      textContent: submenu ? "›" : active ? "✓" : "",
      attributes: { "aria-hidden": "true" },
    });
    button.append(icon, copy, check);
    button.addEventListener("click", onClick);
    if (onHover) {
      button.addEventListener("mouseenter", onHover);
    }
    return button;
  };

  const renderModeTrigger = () => {
    const isPipeline = modeSelection.mode === "pipeline";
    const iconClass = isPipeline ? "is-selling-pipeline" : "is-normal-mode";
    const labelText = isPipeline ? pipelineLabelFor(modeSelection.pipelineName) : t("Normal mode");
    modeTrigger.replaceChildren(
      makeElement("span", {
        className: `draft-session-control-icon ${iconClass}`,
        attributes: { "aria-hidden": "true" },
      }),
      makeElement("span", { className: "draft-session-control-label", textContent: labelText }),
      makeElement("span", { className: "draft-session-control-chevron", attributes: { "aria-hidden": "true" } }),
    );
    modeTrigger.setAttribute("aria-expanded", modeMenuOpen ? "true" : "false");
  };

  // 面板在 .workspace-content(overflow:auto)内滚动,任何 absolute 菜单向下展开一旦超出
  // 滚动盒下缘就会被裁(card 的 overflow:visible 只能救「菜单仍在盒内」的情形)。故打开时
  // 用 position:fixed + 由触发器 rect 实算坐标,把菜单钉到视口、脱离所有 overflow 祖先。
  // 祖先(modal/dialog/content/tab-panel)均无 transform/filter/backdrop-filter/contain,
  // fixed 相对视口定位成立。无头测试桩无 getBoundingClientRect/style/window,自然跳过,
  // 退回 CSS 的向下 absolute + card overflow:visible 作为兜底。
  const MODE_MENU_GAP_PX = 6;
  const positionModeMenus = () => {
    if (typeof window === "undefined" || typeof modeTrigger.getBoundingClientRect !== "function") {
      return;
    }
    const viewportWidth = window.innerWidth || 0;
    const triggerRect = modeTrigger.getBoundingClientRect();
    if (modeMenu.style) {
      modeMenu.style.position = "fixed";
      modeMenu.style.top = `${Math.round(triggerRect.bottom + MODE_MENU_GAP_PX)}px`;
      modeMenu.style.bottom = "auto";
      modeMenu.style.left = "auto";
      // 右对齐触发器右缘,宽菜单不溢出视口右侧。
      modeMenu.style.right = `${Math.round(Math.max(8, viewportWidth - triggerRect.right))}px`;
    }
    if (!pipelineSubmenu.hidden && typeof modeMenu.getBoundingClientRect === "function" && pipelineSubmenu.style) {
      const menuRect = modeMenu.getBoundingClientRect();
      pipelineSubmenu.style.position = "fixed";
      pipelineSubmenu.style.top = `${Math.round(menuRect.top)}px`;
      pipelineSubmenu.style.bottom = "auto";
      pipelineSubmenu.style.left = "auto";
      // 子菜单开在一级菜单左侧,避免右溢出。
      pipelineSubmenu.style.right = `${Math.round(Math.max(8, viewportWidth - menuRect.left + MODE_MENU_GAP_PX))}px`;
    }
  };

  const renderModeMenus = () => {
    modeMenu.hidden = !modeMenuOpen;
    modeMenu.replaceChildren();
    pipelineSubmenu.hidden = !modeMenuOpen || !pipelineSubmenuOpen;
    pipelineSubmenu.replaceChildren();
    if (modeMenuOpen) {
      modeMenu.append(
        makeModeMenuItem({
          iconClass: "is-normal-mode",
          label: t("Normal mode"),
          detail: t("Enter the standard conversational IaC assistant"),
          active: modeSelection.mode !== "pipeline",
          onClick: () => {
            modeMenuOpen = false;
            pipelineSubmenuOpen = false;
            modeSelection = { mode: "normal", pipelineName: fallbackPipelineName };
            renderModeSelector();
            persistSessionDefaults();
          },
        }),
        makeModeMenuItem({
          iconClass: "is-pipeline-mode",
          label: t("Pipeline mode"),
          detail: t("Plan, generate, and validate with the pipeline"),
          active: modeSelection.mode === "pipeline",
          submenu: true,
          onClick: () => {
            pipelineSubmenuOpen = !pipelineSubmenuOpen;
            renderModeMenus();
          },
          onHover: () => {
            if (!pipelineSubmenuOpen) {
              pipelineSubmenuOpen = true;
              renderModeMenus();
            }
          },
        }),
      );
    }
    if (!pipelineSubmenu.hidden) {
      for (const option of pipelineOptions) {
        pipelineSubmenu.append(
          makeModeMenuItem({
            iconClass: "is-selling-pipeline",
            label: option.label || option.id,
            detail: option.detail,
            active: modeSelection.mode === "pipeline" && option.id === modeSelection.pipelineName,
            onClick: () => {
              modeMenuOpen = false;
              pipelineSubmenuOpen = false;
              modeSelection = { mode: "pipeline", pipelineName: option.id };
              renderModeSelector();
              persistSessionDefaults();
            },
          }),
        );
      }
    }
    if (modeMenuOpen) {
      positionModeMenus();
    }
  };

  function renderModeSelector() {
    renderModeTrigger();
    renderModeMenus();
  }

  modeTrigger.addEventListener("click", (event) => {
    event.stopPropagation?.();
    modeMenuOpen = !modeMenuOpen;
    if (!modeMenuOpen) {
      pipelineSubmenuOpen = false;
    }
    renderModeSelector();
  });
  // 点击选择器之外收起菜单。createOtherPanel 在控制器构造时即执行,Node 测试桩的 document 仅有
  // createElement,故守卫 addEventListener 存在再挂,避免构造期抛错。
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener("click", (event) => {
      if (!modeMenuOpen) {
        return;
      }
      if (typeof modePicker.contains === "function" && modePicker.contains(event.target)) {
        return;
      }
      modeMenuOpen = false;
      pipelineSubmenuOpen = false;
      renderModeSelector();
    });
  }
  renderModeSelector();

  const modeField = makeElement("div", { className: "workspace-field has-desc" });
  const modeFieldText = makeElement("div", { className: "workspace-field-text" });
  modeFieldText.append(
    makeElement("span", { className: "workspace-field-title", textContent: t("Default mode") }),
    makeElement("span", {
      className: "workspace-field-desc",
      textContent: t("The normal mode or specific pipeline preset for new sessions, consistent with the session selector."),
    }),
  );
  modeField.append(modeFieldText, modePicker);

  const sessionDefaultsCard = makeElement("section", {
    className: "workspace-settings-group workspace-settings-provider workspace-session-defaults-card",
  });
  sessionDefaultsCard.append(makeField(t("Default permission"), permissionSelect, t("The permission mode preset for new sessions.")), modeField);

  const themeGroupHead = makeElement("div", { className: "workspace-settings-group-head" });
  themeGroupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Color scheme") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Switch the color scheme of the entire interface; consistent across all devices after saving."),
    }),
  );
  const themeGrid = makeElement("div", { className: "workspace-theme-grid" });
  const themeSwatches = new Map();
  const setActiveTheme = (slug) => {
    document.documentElement.dataset.theme = slug;
    for (const [key, el] of themeSwatches) {
      el.classList.toggle("is-active", key === slug);
    }
  };
  for (const option of THEME_OPTIONS) {
    const swatch = makeElement("button", {
      className: "workspace-theme-swatch",
      attributes: { type: "button" },
      dataset: { theme: option.slug },
    });
    const strip = makeElement("div", { className: "workspace-theme-strip" });
    for (const color of option.preview) {
      strip.append(makeElement("span", { attributes: { style: `background: ${color};` } }));
    }
    swatch.append(
      makeElement("span", { className: "workspace-theme-name", textContent: option.name }),
      strip,
    );
    swatch.addEventListener("click", () => selectTheme(option.slug));
    themeSwatches.set(option.slug, swatch);
    themeGrid.append(swatch);
  }

  const languageSelect = makeSelect("workspace-language-select");
  const languageField = makeField(
    t("Language"),
    languageSelect,
    t("Choose the interface language; the page reloads after saving."),
  );
  languageSelect.addEventListener("change", async () => {
    await api.saveUiLanguage(languageSelect.value);
    location.reload();
  });

  let requestToken = 0;

  async function persist() {
    const token = ++requestToken;
    stamp(t("Saving…"));
    try {
      await api.saveForeignSessionsVisibility({
        showPipeline: pipelineToggle.input.checked,
        showNormal: normalToggle.input.checked,
      });
      if (token !== requestToken) {
        return;
      }
      stampSaved(token);
      context.onSessionsMutated?.();
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      stamp(t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  }

  async function persistReviewStep() {
    const token = ++requestToken;
    stamp(t("Saving…"));
    try {
      await api.saveSellingReviewStep(reviewStepToggle.input.checked);
      if (token !== requestToken) {
        return;
      }
      stampSaved(token);
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      stamp(t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  }

  // 后端只回传结构化的 phase(不含译文);中文标签一律在前端映射,后端保持零翻译。
  const PREREQ_PHASE_LABELS = {
    download: t("Downloading…"),
    install: t("Installing…"),
    version_check: t("Verifying version…"),
    post_install: t("Finishing post-install configuration…"),
    path_hint: t("Configuring path…"),
  };
  const prereqPhaseLabelFor = (phase) => PREREQ_PHASE_LABELS[phase] || t("Processing…");

  const setPrereqInstalling = (installing) => {
    prereqInstallButton.disabled = installing;
    prereqProgress.hidden = !installing;
    if (installing) {
      prereqPhaseLabel.textContent = t("Preparing…");
      prereqFill.style.width = "0%";
      prereqFill.classList.remove("is-indeterminate");
    }
  };

  const applyPrereqProgress = (event) => {
    prereqPhaseLabel.textContent = prereqPhaseLabelFor(event.phase);
    const total = Number(event.total_bytes) || 0;
    const done = Number(event.downloaded_bytes) || 0;
    if (event.phase === "download" && total > 0) {
      const percent = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
      prereqFill.classList.remove("is-indeterminate");
      prereqFill.style.width = `${percent}%`;
    } else {
      // 无字节进度的阶段(安装/校验/后处理):用不定进度条表示忙碌。
      // 清掉内联 width,让 CSS 的 .is-indeterminate 接管条宽与滑动动画。
      prereqFill.classList.add("is-indeterminate");
      prereqFill.style.width = "";
    }
  };

  // 依据检测结果渲染三态,让面板始终「体现出」当前依赖状态:
  //   已安装        -> 正面提示「已安装」,无按钮无进度;
  //   缺失且可安装  -> 提示 + 安装按钮;
  //   缺失不可安装  -> 仅提示(需手动安装)。
  function renderReviewPrereq(detection) {
    if (!detection) {
      prereqNotice.hidden = true;
      return;
    }
    prereqNotice.hidden = false;
    prereqProgress.hidden = true;
    const installed = detection.satisfied === true;
    const installable = detection.installable === true;
    prereqNotice.classList.toggle("is-installed", installed);
    prereqNotice.classList.toggle("is-missing", !installed);
    prereqInstallButton.hidden = installed || !installable;
    if (installed) {
      prereqText.textContent = t("infraguard is installed; the review step is available.");
    } else if (installable) {
      prereqText.textContent = t("The review step depends on the infraguard tool, which was not detected. Install it here with one click.");
    } else {
      prereqText.textContent = t("The infraguard tool was not detected and cannot be installed automatically in the current environment; please install it manually first.");
    }
  }

  // 探测前置依赖并渲染状态;探测失败不阻塞面板其它状态,静默隐藏提示。
  async function refreshReviewPrereq() {
    try {
      renderReviewPrereq(await api.getReviewStepPrerequisite());
    } catch (error) {
      prereqNotice.hidden = true;
    }
  }

  async function installReviewPrereq() {
    setPrereqInstalling(true);
    let result = null;
    try {
      await api.installReviewStepPrerequisite((event) => {
        if (event.phase === "result") {
          result = event;
          return;
        }
        applyPrereqProgress(event);
      });
    } catch (error) {
      setPrereqInstalling(false);
      stamp(t("Install failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
      return;
    }
    setPrereqInstalling(false);
    if (result && result.status === "ok" && result.satisfied) {
      // 安装成功后原地切到「已安装」态,而不是隐藏整块 —— 让用户看到结果。
      renderReviewPrereq({ satisfied: true, installable: false });
      const token = ++requestToken;
      stampSaved(token);
    } else {
      const message = result && result.message ? result.message : t("Unknown error");
      stamp(t("Install failed: {error}", { error: message }), true);
    }
  }

  async function persistSessionDefaults() {
    const token = ++requestToken;
    stamp(t("Saving…"));
    const payload = {
      permissionMode: permissionSelect.value,
      mode: modeSelection.mode,
      pipelineName: modeSelection.pipelineName,
    };
    try {
      await api.saveSessionDefaults(payload);
      if (token !== requestToken) {
        return;
      }
      // 保存成功即回写 app 内存默认 + 当前草稿,免去「改了默认要刷新页面才生效」。
      context.onSessionDefaultsSaved?.(payload);
      stampSaved(token);
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      stamp(t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  }

  async function selectTheme(slug) {
    setActiveTheme(slug);
    const token = ++requestToken;
    stamp(t("Saving…"));
    try {
      await api.saveAppearance(slug);
      if (token !== requestToken) {
        return;
      }
      stampSaved(token);
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      stamp(t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  }

  pipelineToggle.input.addEventListener("change", persist);
  normalToggle.input.addEventListener("change", persist);
  reviewStepToggle.input.addEventListener("change", persistReviewStep);
  prereqInstallButton.addEventListener("click", installReviewPrereq);
  permissionSelect.addEventListener("change", persistSessionDefaults);

  // 开发者模式开关:保存(并入共享缓存,不覆盖 highlightFailedTools)后即时增删「开发」分页。
  async function persistDeveloperMode() {
    const token = ++requestToken;
    stamp(t("Saving…"));
    try {
      const saved = await context.saveDeveloperState?.({ mode: devModeToggle.input.checked });
      if (token !== requestToken) {
        return;
      }
      context.onDeveloperModeChanged?.(saved ? saved.mode : devModeToggle.input.checked);
      stampSaved(token);
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      stamp(t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  }
  devModeToggle.input.addEventListener("change", persistDeveloperMode);

  // 章节顺序:新会话默认 → 配色方案(含界面语言)→ 售卖流水线 → 外来会话可见性 → 开发者模式。
  // 界面语言(languageField)紧随配色方案,既保持外观类设置成组,也让所有分区标题的相邻关系
  // (h3→head、settings-group→head、field→head)仍被现有章节间距选择器覆盖,无需改 CSS。
  panel.append(
    heading,
    sessionDefaultsGroupHead,
    sessionDefaultsCard,
    themeGroupHead,
    themeGrid,
    languageField,
    reviewStepGroupHead,
    reviewStepCard,
    groupHead,
    card,
    devModeGroupHead,
    devModeCard,
    status,
  );

  return {
    panel,
    async activate() {
      const token = ++requestToken;
      try {
        const value = await api.getForeignSessionsVisibility();
        if (token !== requestToken) {
          return;
        }
        pipelineToggle.input.checked = Boolean(value?.showPipeline);
        normalToggle.input.checked = Boolean(value?.showNormal);
      } catch (error) {
        if (token !== requestToken) {
          return;
        }
        stamp(t("Load failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
      }
      try {
        const reviewStep = await api.getSellingReviewStep();
        if (token === requestToken) {
          reviewStepToggle.input.checked = Boolean(reviewStep?.enabled);
        }
      } catch (error) {
        /* 审查步骤开关加载失败保持默认关闭,不覆盖其它已加载状态 */
      }
      // 前置依赖探测独立于开关状态(不受 token 竞态影响,内部自行处理失败)。
      await refreshReviewPrereq();
      try {
        const appearance = await api.getAppearance();
        if (token === requestToken) {
          setActiveTheme(appearance?.theme || "graphite");
        }
      } catch (error) {
        /* 主题加载失败保持首屏注入值,不覆盖 */
      }
      try {
        const uiLang = await api.getUiLanguage();
        if (token === requestToken) {
          languageSelect.replaceChildren();
          for (const { code, name } of uiLang.availableLanguages ?? []) {
            const option = makeElement("option");
            option.value = code;
            option.textContent = name;
            languageSelect.append(option);
          }
          languageSelect.value = uiLang.uiLanguage ?? currentLang();
        }
      } catch (error) {
        /* 界面语言加载失败保持首屏注入值,不覆盖 */
      }
      try {
        const defaults = await api.getSessionDefaults();
        if (token === requestToken && defaults) {
          if (typeof defaults.permissionMode === "string") {
            permissionSelect.value = defaults.permissionMode;
          }
          // {mode, pipelineName} 校正后驱动二级选择器;不存在的流水线回落普通/首条(见 normalizeModeSelection)。
          modeSelection = normalizeModeSelection(defaults.mode, defaults.pipelineName);
          renderModeSelector();
        }
      } catch (error) {
        if (token === requestToken) {
          stamp(t("Load failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
        }
      }
      try {
        const developer = await api.getDeveloperSettings();
        if (token === requestToken) {
          // 并入共享缓存,保留 highlightFailedTools,仅据此回显开发者模式开关。
          const state = context.cacheDeveloperState
            ? context.cacheDeveloperState(developer)
            : { mode: Boolean(developer?.mode) };
          devModeToggle.input.checked = Boolean(state.mode);
        }
      } catch (error) {
        /* 开发者模式加载失败保持默认关闭,不覆盖其它已加载状态 */
      }
    },
    reset() {
      requestToken += 1;
      pipelineToggle.input.checked = false;
      normalToggle.input.checked = false;
      devModeToggle.input.checked = false;
      reviewStepToggle.input.checked = false;
      prereqNotice.hidden = true;
      prereqNotice.classList.remove("is-installed", "is-missing");
      prereqInstallButton.hidden = false;
      setPrereqInstalling(false);
      permissionSelect.value = "default";
      modeSelection = { mode: "normal", pipelineName: fallbackPipelineName };
      modeMenuOpen = false;
      pipelineSubmenuOpen = false;
      renderModeSelector();
      clearStatus();
    },
  };
}

// 「开发」面板:仅开发者模式开启时出现。功能1=失败工具标红开关(改 body 类即时生效);
// 功能2=从「常规」面板移来的重启入口。
function createDeveloperPanel(api, context) {
  const panel = makeElement("section", {
    className: "workspace-tab-panel workspace-developer-panel",
    attributes: { "data-workspace-panel": "developer" },
  });
  const heading = makeElement("h3", { textContent: t("Developer") });

  let requestToken = 0;
  const status = makeElement("span", { className: "workspace-memory-status workspace-developer-status" });
  let clearTimer = null;
  const cancelClear = () => {
    if (clearTimer !== null) {
      clearTimeout(clearTimer);
      clearTimer = null;
    }
  };
  const stamp = (message, isError = false) => {
    cancelClear();
    status.textContent = text(message);
    status.classList.toggle("is-error", Boolean(isError));
  };
  const stampSaved = (token) => {
    stamp(t("Saved"));
    clearTimer = setTimeout(() => {
      clearTimer = null;
      if (token === requestToken) {
        status.textContent = "";
        status.classList.remove("is-error");
      }
    }, 2200);
  };
  const clearStatus = () => {
    cancelClear();
    status.textContent = "";
    status.classList.remove("is-error");
  };

  // 失败工具标红:切换 body.dev-highlight-tool-errors,让整段转录里失败工具卡的标红规则
  // 即时生效/失效(样式在 styles.css 门控于该类,无需重渲染工具卡)。
  const applyHighlightClass = (enabled) => {
    if (typeof document !== "undefined" && document.body) {
      document.body.classList.toggle("dev-highlight-tool-errors", Boolean(enabled));
    }
  };

  const highlightToggle = makeForeignSwitch("workspace-highlight-failed-tools");
  const highlightGroupHead = makeElement("div", { className: "workspace-settings-group-head" });
  highlightGroupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Failed tool calls") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Control how failed tool calls are shown in the transcript."),
    }),
  );
  const highlightCard = makeElement("section", {
    className: "workspace-settings-group workspace-settings-provider",
  });
  highlightCard.append(
    makeField(
      t("Highlight failed tool calls in red"),
      highlightToggle.control,
      t("When enabled, failed tool calls are painted red. When disabled, they look like any other tool call."),
    ),
  );

  async function persistHighlight() {
    const token = ++requestToken;
    const enabled = highlightToggle.input.checked;
    applyHighlightClass(enabled); // 即时反馈,不等待网络。
    stamp(t("Saving…"));
    try {
      await context.saveDeveloperState?.({ highlightFailedTools: enabled });
      if (token !== requestToken) {
        return;
      }
      stampSaved(token);
    } catch (error) {
      if (token !== requestToken) {
        return;
      }
      stamp(t("Save failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
    }
  }
  highlightToggle.input.addEventListener("change", persistHighlight);

  // 重启服务:通用重启入口。点击 → 全屏遮罩确认 → POST → 轮询 /health → 恢复后自动刷新。
  const restartGroupHead = makeElement("div", { className: "workspace-settings-group-head" });
  restartGroupHead.append(
    makeElement("h4", { className: "workspace-settings-group-title", textContent: t("Restart service") }),
    makeElement("p", {
      className: "workspace-settings-group-desc",
      textContent: t("Restart the local web service process (interrupts ongoing conversations); the page refreshes automatically once complete."),
    }),
  );
  const restartCard = makeElement("section", { className: "workspace-settings-group workspace-settings-provider" });
  const restartButton = makeButton(t("Restart service"), "server-restart", "workspace-action is-danger");
  restartCard.append(restartButton);

  const runRestartFlow = () => {
    const overlay = makeElement("div", {
      className: "server-restart-overlay",
      attributes: { role: "alertdialog", "aria-modal": "true" },
    });
    const box = makeElement("div", { className: "server-restart-box" });
    const msg = makeElement("p", { className: "server-restart-msg", textContent: t("Restart the current service?") });
    const actions = makeElement("div", { className: "server-restart-actions" });
    const cancelBtn = makeButton(t("Cancel"), "server-restart-cancel", "workspace-action");
    const confirmBtn = makeButton(t("Confirm restart"), "server-restart-confirm", "workspace-action is-danger");
    actions.append(cancelBtn, confirmBtn);
    box.append(msg, actions);
    overlay.append(box);
    document.body.append(overlay);

    cancelBtn.addEventListener("click", () => overlay.remove());

    // 两阶段:先确认旧进程已下线(sawDown),再等新进程恢复才刷新。否则 800ms 首轮
    // 可能命中「execv 尚未生效、旧进程仍返回 200」,导致重启前误刷新。
    let sawDown = false;
    const pollHealth = async (deadline) => {
      let ok = false;
      try {
        const res = await fetch("/health", { cache: "no-store" });
        ok = res.ok;
      } catch (_error) {
        ok = false; // 进程重启期间连接被拒属预期。
      }
      if (!ok) {
        sawDown = true;
      } else if (sawDown) {
        window.location.reload();
        return;
      }
      if (Date.now() >= deadline) {
        msg.textContent = t("The restart is taking a while. Please refresh manually.");
        const reloadBtn = makeButton(t("Retry refresh"), "server-restart-reload", "workspace-action");
        reloadBtn.addEventListener("click", () => window.location.reload());
        actions.replaceChildren(reloadBtn);
        return;
      }
      setTimeout(() => pollHealth(deadline), 500);
    };

    confirmBtn.addEventListener("click", async () => {
      msg.textContent = t("Restarting; the page refreshes automatically once the service recovers…");
      const spinner = makeElement("div", { className: "server-restart-spinner", attributes: { "aria-hidden": "true" } });
      actions.replaceChildren(spinner);
      try {
        await api.restartServer();
      } catch (_error) {
        // 202 之后进程即将替换,响应可能在读取前中断;无论如何进入轮询恢复。
      }
      // 首轮延迟 > 后端 execv 的 0.4s,避免过早命中旧进程;20s 超时留足重新初始化余量。
      setTimeout(() => pollHealth(Date.now() + 20000), 1000);
    });
  };
  restartButton.addEventListener("click", runRestartFlow);

  panel.append(heading, highlightGroupHead, highlightCard, restartGroupHead, restartCard, status);

  return {
    panel,
    async activate() {
      const token = ++requestToken;
      try {
        const developer = await api.getDeveloperSettings();
        if (token !== requestToken) {
          return;
        }
        const state = context.cacheDeveloperState
          ? context.cacheDeveloperState(developer)
          : { highlightFailedTools: Boolean(developer?.highlightFailedTools) };
        highlightToggle.input.checked = Boolean(state.highlightFailedTools);
        applyHighlightClass(state.highlightFailedTools);
      } catch (error) {
        if (token !== requestToken) {
          return;
        }
        stamp(t("Load failed: {error}", { error: error instanceof Error ? error.message : String(error) }), true);
      }
    },
    reset() {
      requestToken += 1;
      clearStatus();
    },
  };
}

function createPipelinePanel() {
  const panel = makeElement("section", {
    className: "workspace-tab-panel",
    attributes: { "data-workspace-panel": "pipeline" },
  });
  const heading = makeElement("h3", { textContent: t("Pipeline") });
  const mount = makeElement("div", {
    className: "pipeline-region",
    attributes: { "data-app-shell": "pipeline-workspace" },
  });
  panel.append(heading, mount);
  return { panel };
}

export function createWorkspaceController({ tabs, content }, api, options = {}) {
  let activeTab = "other";
  let currentSessionId = "";
  let currentSession = null;
  const panelControllers = new Map();
  // 开发者模式:developerMode 决定「开发」分页是否出现在导航;developerState 是 {mode,
  // highlightFailedTools} 的单一缓存,让「常规」面板的开发者开关与「开发」面板的标红开关
  // 各自改一个字段而不覆盖另一个(保存前先并入缓存)。
  let developerMode = false;
  const developerState = { mode: false, highlightFailedTools: false };

  const context = {
    sessionId: () => currentSessionId,
    session: () => currentSession,
    // 归档面板里取消归档/删除会话后,主侧栏需要重新拉取会话列表(否则被隐藏的空项目
    // 不会随取消归档而重新出现)。通过该回调通知 app.js 刷新侧栏。
    onSessionsMutated: () => options.onSessionsMutated?.(),
    // 新会话默认面板的「默认流水线」下拉数据源;缺省回落到单条售卖流水线,保证列表非空。
    pipelineOptions:
      Array.isArray(options.pipelineOptions) && options.pipelineOptions.length
        ? options.pipelineOptions
        : [{ id: "selling", label: t("Sales pipeline") }],
    // 新会话默认保存成功后回写 app 内存 + 当前草稿(缺此透传则 persistSessionDefaults 的通知静默丢弃,
    // 表现为「改了默认要刷新页面才生效」)。
    onSessionDefaultsSaved: (payload) => options.onSessionDefaultsSaved?.(payload),
  };

  // 开发者状态的读改写入口,供两个面板共享,保证互不覆盖对方字段。
  context.getDeveloperState = () => ({ ...developerState });
  context.cacheDeveloperState = (value) => {
    developerState.mode = Boolean(value?.mode);
    developerState.highlightFailedTools = Boolean(value?.highlightFailedTools);
    return context.getDeveloperState();
  };
  context.saveDeveloperState = async (partial) => {
    const next = { ...developerState, ...partial };
    const saved = await api.saveDeveloperSettings(next);
    return context.cacheDeveloperState(saved);
  };
  // 用户在「常规」面板切换开发者模式后调用:即时增删「开发」分页。
  context.onDeveloperModeChanged = (mode) => applyDeveloperMode(Boolean(mode));

  function setActiveTab(tabId) {
    // 以「已注册的面板」而非导航列表校验:状态/流水线/搜索虽已移出导航,仍可编程式打开;
    // 未知标签(如 openWorkspaceModal("settings"))回落到「常规」——进入配置默认选中常规。
    activeTab = panelControllers.has(tabId) ? tabId : "other";
    for (const button of tabs?.querySelectorAll("[data-workspace-tab]") || []) {
      const isActive = button.dataset.workspaceTab === activeTab;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    }
    for (const [id, controller] of panelControllers.entries()) {
      controller.panel.hidden = id !== activeTab;
    }
    panelControllers.get(activeTab)?.activate?.();
  }

  // 每次调用都从 NAV_GROUPS 全量重建(替换 index.html 里的静态按钮),据 developerMode
  // 过滤 devOnly 分页——这样开关开发者模式即可增删「开发」分页,无需刷新页面。
  function buildTabs() {
    if (!tabs) {
      return;
    }
    const nodes = [];
    for (const group of NAV_GROUPS) {
      const groupTabs = group.tabs.filter((tab) => !tab.devOnly || developerMode);
      if (!groupTabs.length) {
        continue;
      }
      nodes.push(makeElement("p", { className: "workspace-tab-group-title", textContent: group.title }));
      for (const tab of groupTabs) {
        const button = makeElement("button", {
          attributes: { type: "button", role: "tab" },
          dataset: { workspaceTab: tab.id },
        });
        const icon = makeElement("span", {
          className: `workspace-tab-icon workspace-tab-icon-${tab.id}`,
          attributes: { "aria-hidden": "true" },
        });
        const label = makeElement("span", { textContent: tab.label });
        button.append(icon, label);
        nodes.push(button);
      }
    }
    tabs.replaceChildren(...nodes);
    // 重建产生的是全新按钮,旧监听随旧节点丢弃;顺带按 activeTab 复位高亮态。
    for (const button of tabs.querySelectorAll("[data-workspace-tab]")) {
      button.addEventListener("click", () => setActiveTab(button.dataset.workspaceTab || "other"));
      const isActive = button.dataset.workspaceTab === activeTab;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    }
  }

  // 增删「开发」分页;关闭时若正停在该分页,回落到「常规」。
  function applyDeveloperMode(enabled) {
    const next = Boolean(enabled);
    if (developerMode === next) {
      return;
    }
    developerMode = next;
    buildTabs();
    if (!developerMode && activeTab === "developer") {
      setActiveTab("other");
    }
  }

  // 构造时读取一次开发者模式:开启则露出「开发」分页(默认关闭 → 首次构建不含该分页)。
  async function refreshDeveloperMode() {
    try {
      const value = await api.getDeveloperSettings();
      context.cacheDeveloperState(value);
      applyDeveloperMode(Boolean(value?.mode));
    } catch (_error) {
      /* 读取失败 → 开发者模式保持关闭,「开发」分页不出现 */
    }
  }

  function buildPanels() {
    if (!content) {
      return;
    }
    panelControllers.set("status", createStatusPanel(api, context));
    panelControllers.set("model", createModelPanel(api, context));
    panelControllers.set("cloud", createCloudPanel(api, context));
    panelControllers.set("memory", createMemoryPanel(api, context));
    panelControllers.set("skills", createPluginsPanel(api, context));
    panelControllers.set("archived", createArchivedPanel(api, context));
    panelControllers.set("other", createOtherPanel(api, context));
    panelControllers.set("developer", createDeveloperPanel(api, context));
    panelControllers.set("pipeline", createPipelinePanel());
    content.replaceChildren(...[...panelControllers.values()].map((controller) => controller.panel));
    setActiveTab(activeTab);
  }

  buildTabs();
  buildPanels();
  void refreshDeveloperMode();

  return {
    setSession(sessionId, session = null) {
      const nextSessionId = sessionId || displaySessionId(session);
      if (nextSessionId !== currentSessionId) {
        for (const controller of panelControllers.values()) {
          controller.reset?.();
        }
      }
      currentSessionId = nextSessionId;
      currentSession = session;
      panelControllers.get(activeTab)?.activate?.();
    },
    render(state) {
      if (state?.currentSession || state?.currentSessionId) {
        currentSession = state.currentSession || currentSession;
        currentSessionId = state.currentSessionId || displaySessionId(currentSession);
      }
      panelControllers.get("status")?.render?.(state);
    },
    showStatusResult(payload) {
      panelControllers.get("status")?.showResult?.(payload);
    },
    setActiveTab,
  };
}
