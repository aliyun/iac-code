import { t } from "../i18n.js?v=web-repl-ui-277";
// 输出面板:悬浮抽屉汇总资源栈与模板文件;自持 DOM,不进 render 扇出。
import { renderMermaid, renderMermaidViews, renderDiagramPrice } from "../mermaid_render.js?v=arch-diagram-v5";

const STATUS_LABELS = { true: t("Success"), false: t("In progress / failed") };

// 行首类型图标(静态 SVG 常量,随 currentColor 着色):资源栈=堆叠层、模板=文档、架构图=流程节点。
const ROW_ICONS = {
  stack:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="M4 12l8 4.5L20 12"/></svg>',
  file:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  diagram:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="3" width="8" height="5" rx="1"/><rect x="3" y="16" width="8" height="5" rx="1"/><rect x="13" y="16" width="8" height="5" rx="1"/><path d="M12 8v3M12 11H7v5M12 11h5v5"/></svg>',
};

function byShell(name) {
  return document.querySelector(`[data-app-shell="${name}"]`);
}

// 图标内容为受控静态常量(无用户数据),innerHTML 安全。
function rowIcon(kind) {
  const span = document.createElement("span");
  span.className = "output-row-icon";
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = ROW_ICONS[kind] || "";
  return span;
}

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// 轻量正则高亮(无第三方依赖):先整体转义,再按 token 包裹。
export function highlightTemplate(text, format) {
  const escaped = escapeHtml(text);
  if (format === "json") {
    return escaped
      .replace(/&quot;(?:\\&quot;|(?!&quot;).)*&quot;(?=\s*:)/g, (m) => `<span class="tok-key">${m}</span>`)
      .replace(/(:\s*)(&quot;(?:\\&quot;|(?!&quot;).)*&quot;)/g, (_m, p, s) => `${p}<span class="tok-string">${s}</span>`)
      .replace(/(?<!&#)\b(-?\d+(?:\.\d+)?)\b/g, '<span class="tok-number">$1</span>')
      .replace(/\b(true|false|null)\b/g, '<span class="tok-boolean">$1</span>');
  }
  // yaml / terraform:注释、键、字符串、数字、布尔
  return escaped
    .replace(/(^|\n)(\s*#.*)/g, (_m, nl, c) => `${nl}<span class="tok-comment">${c}</span>`)
    .replace(/(^|\n)(\s*)([A-Za-z0-9_.-]+)(\s*:)/g, (_m, nl, indent, key, colon) => `${nl}${indent}<span class="tok-key">${key}</span>${colon}`)
    .replace(/(&quot;(?:\\&quot;|(?!&quot;).)*&quot;|&#39;(?:(?!&#39;).)*&#39;)/g, (m) => `<span class="tok-string">${m}</span>`)
    .replace(/\b(true|false|null|yes|no)\b/g, '<span class="tok-boolean">$1</span>');
}

// 架构图优化态徽标:pending→「待优化」(静态点)、optimizing→「优化中」(转圈);
// done/none 返回 null(不挂徽标)。样式复用 styles.css 的 .diagram-pending / .diagram-optimizing。
export function diagramStateBadge(stateStr) {
  if (stateStr === "optimizing") {
    const badge = document.createElement("span");
    badge.className = "diagram-optimizing";
    badge.textContent = t("Optimizing");
    return badge;
  }
  if (stateStr === "pending") {
    const badge = document.createElement("span");
    badge.className = "diagram-pending";
    badge.textContent = t("Pending optimization");
    return badge;
  }
  return null;
}

export function createOutputController({ getSessionId, api, onPayload = () => {}, getDiagramState = () => "none" }) {
  const toggle = byShell("output-toggle");
  const countBadge = byShell("output-count");
  const panel = byShell("output-panel");
  const body = byShell("output-panel-body");
  const closeBtn = byShell("output-panel-close");

  const preview = byShell("output-file-preview");

  let latestPayload = { stacks: [], files: [], diagrams: [], candidates: [] };
  let isOpen = false;
  let autoOpenedOnce = false;
  let activePreviewPath = null;

  function closePreview() {
    activePreviewPath = null;
    if (preview) preview.hidden = true;
  }

  async function openPreview(path) {
    const id = getSessionId?.();
    if (!id || !preview) return;
    activePreviewPath = path;
    preview.hidden = false;
    preview.replaceChildren(previewHead(path), loadingBody());
    let data;
    try {
      data = await api.getOutputFile(id, path);
    } catch (err) {
      preview.replaceChildren(previewHead(path), fallbackBody(t("File no longer exists")));
      return;
    }
    if (activePreviewPath !== path) return; // 期间被切走
    const pre = document.createElement("pre");
    pre.className = "output-preview-code tok-root";
    pre.innerHTML = highlightTemplate(data.content, data.format);
    const bodyWrap = document.createElement("div");
    bodyWrap.className = "output-preview-body";
    bodyWrap.append(pre);
    preview.replaceChildren(previewHead(data.path), bodyWrap);
  }

  // 架构图预览:mermaidSource 已在 payload,无需回 API;复用 openPreview 的面板打开
  // 与 head 渲染,仅把 body 换成 mermaid 容器。渲染前后加同款会话失效防护。
  async function openDiagramPreview(item) {
    const id = getSessionId?.();
    if (!id || !preview) return;
    const title = item.candidateName || item.sourceRelPath;
    const st = getDiagramState(item);
    // 用唯一 diagramId 做陈旧性键(重名候选的 title 可能相同,无法区分)。
    const key = item.diagramId || title;
    activePreviewPath = key;
    preview.hidden = false;
    preview.replaceChildren(previewHead(title, st), loadingBody());
    // 先在游离容器里渲染(renderMermaid 会往其中注入 .mermaid-diagram),
    // 渲染完成后再校验会话/选中态未被切走,才写入面板 —— 与 openPreview 同款防护。
    const container = document.createElement("div");
    container.className = "output-preview-body";
    if (Array.isArray(item.views) && item.views.length > 1) {
      await renderMermaidViews(container, item.views);
    } else {
      await renderMermaid(container, item.mermaidSource);
    }
    container.append(renderDiagramPrice(item));
    if (activePreviewPath !== key || getSessionId?.() !== id) return; // 期间被切走
    preview.replaceChildren(previewHead(title, st), container);
  }

  // 切换架构图预览:同一图已在预览中(面板可见且键相同)则关闭,否则打开/切换到该图。
  // 返回切换后的开启态(true=现为打开)。键与 openDiagramPreview 同源(diagramId 优先,
  // 兜底 candidateName/sourceRelPath),保证重名候选也能精确辨别。
  function toggleDiagramPreview(item) {
    const key = item.diagramId || item.candidateName || item.sourceRelPath;
    if (activePreviewPath === key && preview && !preview.hidden) {
      closePreview();
      return false;
    }
    openDiagramPreview(item);
    return true;
  }

  function previewHead(path, stateStr) {
    const head = document.createElement("header");
    head.className = "output-preview-head";
    // label + 优化态徽标编成左组,close 在右;否则 space-between 会把徽标甩到中间。
    const left = document.createElement("span");
    left.className = "output-preview-head-left";
    const label = document.createElement("span");
    label.className = "output-preview-path";
    label.textContent = path;
    left.append(label);
    // 架构图优化态徽标紧跟标题(待优化/优化中);已优化或非候选图不挂。
    const stateBadge = diagramStateBadge(stateStr);
    if (stateBadge) left.append(stateBadge);
    head.append(left);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "output-preview-close";
    close.setAttribute("aria-label", t("Close preview"));
    close.textContent = "×";
    close.addEventListener("click", closePreview);
    head.append(close);
    return head;
  }

  function loadingBody() {
    const div = document.createElement("div");
    div.className = "output-preview-body output-preview-loading";
    div.textContent = t("Loading…");
    return div;
  }

  function fallbackBody(message) {
    const div = document.createElement("div");
    div.className = "output-preview-body output-preview-fallback";
    div.textContent = message;
    return div;
  }

  function total(payload) {
    return (payload.stacks?.length || 0) + (payload.files?.length || 0) + (payload.diagrams?.length || 0);
  }

  function setOpen(open) {
    isOpen = open;
    if (panel) panel.hidden = !open;
  }

  function updateToggle() {
    const count = total(latestPayload);
    if (!toggle) return;
    toggle.hidden = count === 0;
    if (countBadge) {
      countBadge.hidden = count === 0;
      countBadge.textContent = String(count);
    }
    if (count === 0) setOpen(false);
  }

  function renderPanel() {
    if (!body) return;
    const sections = [];
    if (latestPayload.stacks?.length) {
      sections.push(renderSection(t("Resource stacks"), latestPayload.stacks.map(renderStackRow)));
    }
    if (latestPayload.files?.length) {
      sections.push(renderSection(t("Template files"), latestPayload.files.map(renderFileRow)));
    }
    if (latestPayload.diagrams?.length) {
      sections.push(renderSection(t("Architecture diagram"), latestPayload.diagrams.map(renderDiagramRow)));
    }
    body.replaceChildren(...sections);
  }

  function renderSection(title, rows) {
    const wrap = document.createElement("section");
    wrap.className = "output-section";
    const head = document.createElement("p");
    head.className = "output-section-head";
    head.textContent = title;
    wrap.append(head, ...rows);
    return wrap;
  }

  function renderStackRow(stack) {
    const row = stack.consoleUrl ? document.createElement("a") : document.createElement("div");
    row.className = "output-row output-stack-row";
    if (stack.consoleUrl) {
      row.href = stack.consoleUrl;
      row.target = "_blank";
      row.rel = "noopener noreferrer";
    } else {
      row.classList.add("is-disabled");
    }
    const name = document.createElement("span");
    name.className = "output-row-name";
    name.textContent = stack.stackName || stack.stackId;
    const badge = document.createElement("span");
    badge.className = "output-badge " + (stack.isSuccess ? "is-success" : "is-pending");
    badge.textContent = stack.status || STATUS_LABELS[String(!!stack.isSuccess)];
    row.append(rowIcon("stack"), name, badge);
    return row;
  }

  function renderFileRow(file) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "output-row output-file-row";
    // 预览用绝对 path(cwd 外的模板 relPath 只剩文件名,拼回 cwd 会指向不存在的位置)。
    row.dataset.outputPath = file.path || file.relPath;
    const name = document.createElement("span");
    name.className = "output-row-name";
    name.textContent = file.name;
    const badge = document.createElement("span");
    badge.className = "output-badge output-format-" + file.format;
    badge.textContent = file.format.toUpperCase();
    row.append(rowIcon("file"), name, badge);
    row.addEventListener("click", () => openPreview(row.dataset.outputPath));
    return row;
  }

  function renderDiagramRow(item) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "output-row output-diagram-row";
    row.dataset.diagramId = item.diagramId;
    const name = document.createElement("span");
    name.className = "output-row-name";
    name.textContent = item.candidateName || item.sourceRelPath;
    const badge = document.createElement("span");
    badge.className = "output-badge output-format-" + item.format;
    badge.textContent = String(item.format).toUpperCase();
    row.append(rowIcon("diagram"), name, badge);
    // 候选方案架构图的优化三态徽标(待优化/优化中/无);非候选图或已优化则无徽标。
    const stateBadge = diagramStateBadge(getDiagramState(item));
    if (stateBadge) row.append(stateBadge);
    row.addEventListener("click", () => openDiagramPreview(item));
    return row;
  }

  async function refresh(sessionId) {
    const id = sessionId || getSessionId?.();
    if (!id) return;
    try {
      const payload = await api.getOutputs(id);
      if (getSessionId?.() !== id) return; // await 期间切换了会话:不得用旧数据覆盖/渲染当前会话
      latestPayload = {
        stacks: payload.stacks || [],
        files: payload.files || [],
        diagrams: payload.diagrams || [],
        // 权威候选表:必须原样透传给消费方(app.js 落到 state.webCandidates,供 confirm_and_select
        // 选择器渲染全部候选)。此前只透传 stacks/files/diagrams,candidates 在这里被丢弃,导致
        // 选择器退回「按可渲染架构图」渲染——某候选模板损坏无图时就少一行(出 2 个方案只显示 1 个)。
        candidates: payload.candidates || [],
      };
      // 把架构图/候选表外抛给消费方(app.js 落到 state.webDiagrams / state.webCandidates,
      // 供 pipeline 候选卡内联折叠图与候选选择器消费)。
      onPayload(latestPayload);
    } catch (err) {
      console.warn("[outputs] refresh failed", err);
      return; // 静默保留上次内容
    }
    renderPanel();
    updateToggle();
    if (total(latestPayload) > 0 && !autoOpenedOnce) {
      autoOpenedOnce = true;
      setOpen(true);
    }
  }

  function reset() {
    latestPayload = { stacks: [], files: [], diagrams: [], candidates: [] };
    autoOpenedOnce = false;
    setOpen(false);
    closePreview();
    renderPanel();
    updateToggle();
  }

  if (toggle) toggle.addEventListener("click", () => setOpen(!isOpen));
  if (closeBtn) closeBtn.addEventListener("click", () => setOpen(false));

  updateToggle();
  return { refresh, reset, openDiagramPreview, toggleDiagramPreview, destroy() {} };
}
