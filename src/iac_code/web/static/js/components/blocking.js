import { t } from "../i18n.js?v=web-repl-ui-277";
function text(value) {
  return value === undefined || value === null ? "" : String(value);
}

function makeButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  if (typeof onClick === "function") {
    button.addEventListener("click", onClick);
  }
  return button;
}

function appendParagraph(parent, className, value) {
  const content = text(value);
  if (!content) {
    return;
  }
  const paragraph = document.createElement("p");
  paragraph.className = className;
  paragraph.textContent = content;
  parent.append(paragraph);
}

function allowsFreeText(payload) {
  return payload.allowFreeText === true || payload.allow_free_text === true;
}

function shellCommand(payload) {
  const toolInput = payload.toolInput || {};
  return text(payload.command || toolInput.command || "");
}

function suggestedRules(payload) {
  const suggestions = Array.isArray(payload.suggestions) ? payload.suggestions : [];
  return suggestions
    .map((suggestion) => text(suggestion?.ruleContent || suggestion?.rule_content || ""))
    .filter(Boolean)
    .join(", ");
}

// 记住每个权限请求当前高亮的选项序号，使其在整页重渲染后仍能保持，
// 避免用上下键选中后因状态刷新被重置。
const permissionSelection = new Map();

// 记录已经自动聚焦过的权限请求，确保面板首次出现时把焦点从输入框移到面板，
// 让键盘（上下键 / 回车）立即可用，且后续重渲染不再抢占焦点。
const permissionAutofocused = new Set();

// 计算上下键切换后的选项序号，按边界收敛（不循环）。
export function nextPermissionSelection(index, key, count) {
  const total = Number.isInteger(count) ? count : 0;
  if (total <= 0) {
    return 0;
  }
  const current = Number.isInteger(index) ? Math.min(Math.max(index, 0), total - 1) : 0;
  if (key === "ArrowDown" || key === "j") {
    return Math.min(current + 1, total - 1);
  }
  if (key === "ArrowUp" || key === "k") {
    return Math.max(current - 1, 0);
  }
  return current;
}

function permissionChoices(payload) {
  return (Array.isArray(payload.choices) ? payload.choices : [])
    .map((choice) => ({ id: text(choice?.id), label: text(choice?.label || choice?.id) }))
    .filter((choice) => choice.id);
}

function applyPermissionSelectionClasses(panel, selectedIndex) {
  const rows = panel.querySelectorAll?.(".blocking-option-row") || [];
  rows.forEach((row, index) => {
    row.className = index === selectedIndex ? "blocking-option-row is-selected" : "blocking-option-row";
  });
  panel.dataset.selectedIndex = String(selectedIndex);
}

export function renderPermissionRequest(request = {}, handlers = {}) {
  const payload = request.payload || {};
  const requestId = text(request.requestId);
  const panel = document.createElement("article");
  panel.className = "blocking-panel blocking-panel-permission";
  panel.dataset.requestId = requestId;
  // 让面板本身可聚焦，首次出现时由 app.js 抢占焦点以启用键盘导航。
  panel.tabIndex = -1;
  if (!permissionAutofocused.has(requestId)) {
    permissionAutofocused.add(requestId);
    panel.dataset.autofocus = "pending";
  }

  const choices = permissionChoices(payload);
  const stored = permissionSelection.get(requestId);
  let selectedIndex = Number.isInteger(stored) ? Math.min(Math.max(stored, 0), Math.max(choices.length - 1, 0)) : 0;
  permissionSelection.set(requestId, selectedIndex);
  panel.dataset.selectedIndex = String(selectedIndex);

  const heading = document.createElement("h3");
  heading.textContent = text(payload.message || payload.description || payload.title || payload.action || t("Authorization required"));
  panel.append(heading);

  appendParagraph(panel, "blocking-detail blocking-detail-command", shellCommand(payload));

  const submit = (index) => {
    const choice = choices[index];
    if (!choice) {
      return;
    }
    handlers.onPermissionAnswer?.(requestId, {
      sessionId: text(payload.sessionId),
      choice: choice.id,
    });
  };

  const select = (index) => {
    selectedIndex = Math.min(Math.max(index, 0), Math.max(choices.length - 1, 0));
    permissionSelection.set(requestId, selectedIndex);
    applyPermissionSelectionClasses(panel, selectedIndex);
  };

  const list = document.createElement("div");
  list.className = "blocking-option-list";
  choices.forEach((choice, index) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = index === selectedIndex ? "blocking-option-row is-selected" : "blocking-option-row";
    row.dataset.index = String(index);
    row.dataset.choiceId = choice.id;

    const badge = document.createElement("span");
    badge.className = "blocking-option-index";
    badge.textContent = String(index + 1);

    const label = document.createElement("span");
    label.className = "blocking-option-label";
    label.textContent = choice.label;

    const hint = document.createElement("span");
    hint.className = "blocking-option-hint";
    hint.textContent = "↑↓";

    row.append(badge, label, hint);
    row.addEventListener("click", () => {
      select(index);
      submit(index);
    });
    list.append(row);
  });
  panel.append(list);

  const footer = document.createElement("div");
  footer.className = "blocking-option-footer";
  const submitButton = document.createElement("button");
  submitButton.type = "button";
  submitButton.className = "blocking-submit";
  const submitLabel = document.createElement("span");
  submitLabel.className = "blocking-action-label";
  submitLabel.textContent = t("Submit");
  const submitKey = document.createElement("span");
  submitKey.className = "blocking-key-hint";
  submitKey.textContent = "⏎";
  submitButton.append(submitLabel, submitKey);
  submitButton.addEventListener("click", () => {
    submit(Number.parseInt(text(panel.dataset.selectedIndex), 10) || 0);
  });
  footer.append(submitButton);
  panel.append(footer);

  // 供全局键盘监听调用：上下键移动高亮、回车提交当前高亮项。
  panel._permissionKeyControl = {
    move: (key) => select(nextPermissionSelection(selectedIndex, key, choices.length)),
    submitSelected: () => submit(selectedIndex),
    hasChoices: choices.length > 0,
  };

  return panel;
}

// 全局键盘导航：焦点不在输入框时，上下键切换、回车提交当前权限面板的高亮项。
function isEditableTarget(target) {
  if (!target) {
    return false;
  }
  const tag = text(target.tagName).toLowerCase();
  return tag === "input" || tag === "textarea" || target.isContentEditable === true;
}

function handlePermissionKeydown(event) {
  const key = event.key;
  if (key !== "ArrowUp" && key !== "ArrowDown" && key !== "Enter" && key !== "j" && key !== "k") {
    return;
  }
  if (isEditableTarget(event.target)) {
    return;
  }
  const panel = document.querySelector?.(".blocking-panel-permission");
  const control = panel?._permissionKeyControl;
  if (!control || !control.hasChoices) {
    return;
  }
  if (key === "Enter") {
    event.preventDefault();
    control.submitSelected();
    return;
  }
  event.preventDefault();
  control.move(key);
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("keydown", handlePermissionKeydown);
}

export function renderQuestionRequest(request = {}, handlers = {}) {
  const payload = request.payload || {};
  const panel = document.createElement("article");
  panel.className = "blocking-panel blocking-panel-question";
  panel.dataset.requestId = text(request.requestId);
  const allowFreeText = allowsFreeText(payload);
  let selectedOption = null;

  const title = document.createElement("h3");
  title.textContent = text(payload.question || payload.title || t("Input required"));
  panel.append(title);

  let freeTextInput = null;
  if (allowFreeText) {
    freeTextInput = document.createElement("textarea");
    freeTextInput.className = "blocking-free-text-input";
    freeTextInput.rows = 3;
    freeTextInput.placeholder = text(payload.freeTextPrompt || payload.free_text_prompt || t("Additional details"));
  }

  const options = Array.isArray(payload.options) ? payload.options : [];
  if (options.length > 0) {
    // 竖直选项列表,复用权限面板的 .blocking-option-row 结构(编号徽标 + 标签),
    // 而非横向按钮流,读感更接近权限界面。
    const list = document.createElement("div");
    list.className = "blocking-option-list";
    const optionRows = [];
    const refreshOptionRows = () => {
      for (const row of optionRows) {
        row.className =
          row.dataset.optionId === text(selectedOption?.id)
            ? "blocking-option-row is-selected"
            : "blocking-option-row";
      }
    };
    options.forEach((option, index) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "blocking-option-row";
      row.dataset.optionId = text(option.id);

      const badge = document.createElement("span");
      badge.className = "blocking-option-index";
      badge.textContent = String(index + 1);

      const label = document.createElement("span");
      label.className = "blocking-option-label";
      label.textContent = text(option.label || option.id || t("Select"));

      row.append(badge, label);

      const description = text(option.description);
      if (description) {
        const hint = document.createElement("span");
        hint.className = "blocking-option-desc";
        hint.textContent = description;
        row.append(hint);
      }

      row.addEventListener("click", () => {
        if (allowFreeText) {
          selectedOption = option;
          refreshOptionRows();
          if (typeof freeTextInput?.focus === "function") {
            freeTextInput.focus();
          }
          return;
        }
        handlers.onQuestionAnswer?.(request.requestId, {
          sessionId: text(payload.sessionId),
          selected_id: text(option.id),
          selected_label: text(option.label || option.id),
          free_text: "",
        });
      });
      optionRows.push(row);
      list.append(row);
    });
    panel.append(list);
  }

  if (allowFreeText) {
    const freeTextForm = document.createElement("form");
    freeTextForm.className = "blocking-free-text";

    const submit = makeButton(t("Submit"), "blocking-action blocking-action-primary", null);
    submit.type = "submit";
    freeTextForm.append(freeTextInput, submit);
    freeTextForm.addEventListener("submit", (event) => {
      event.preventDefault();
      handlers.onQuestionAnswer?.(request.requestId, {
        sessionId: text(payload.sessionId),
        selected_id: selectedOption ? text(selectedOption.id) : "",
        selected_label: selectedOption ? text(selectedOption.label || selectedOption.id) : "",
        free_text: freeTextInput.value,
      });
    });
    panel.append(freeTextForm);
  }

  return panel;
}

export function renderBlockingPanels(state = {}, handlers = {}) {
  const container = document.createElement("section");
  container.className = "blocking-panels";

  for (const request of Object.values(state.permissions || {})) {
    container.append(renderPermissionRequest(request, handlers));
  }
  for (const request of Object.values(state.questions || {})) {
    container.append(renderQuestionRequest(request, handlers));
  }

  return container;
}
