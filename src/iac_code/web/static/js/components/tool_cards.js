import { t } from "../i18n.js?v=web-repl-ui-277";

function text(value) {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value, null, 2);
}

function compact(value, limit = 112) {
  const normalized = text(value).replace(/\s+/g, " ").trim();
  if (normalized.length <= limit) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function createText(className, value) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text(value);
  return span;
}

// 流光相位对齐（见 styles.css 的 iac-shimmer-sweep，周期须一致）。
// 活动轮次里 message-stack 每帧全量重建，流光标题元素随之被重新创建，CSS 动画本会从头
// （background-position:180%，亮带在文本右侧不可见，约需 1s 才扫入可视区）重启；高频重渲染
// 下亮带永远进不来 → 用户「看不到滑光 / 完全没有变化」。用负 animation-delay 让新建元素直接
// 续到 performance.now() 对应的相位，跨重建平滑推进（所有流光元素同源同相）。
export const SHIMMER_PERIOD_S = 2.8;
export function applyShimmerPhase(el) {
  if (!el || !el.style) {
    return;
  }
  const now = typeof performance !== "undefined" && performance.now ? performance.now() : 0;
  el.style.animationDelay = `-${((now / 1000) % SHIMMER_PERIOD_S).toFixed(3)}s`;
}

// 转圈相位对齐（见 styles.css 的 iac-thread-spin）。侧栏会话列表、命令面板、流水线步骤的转圈都挂在
// 会被 replaceChildren 全量重建的容器里：每次重渲染 / 后台刷新都会重建 spinner 的 <span>，CSS 旋转
// 动画随之从 0° 重启——用户看到「转着转着被拽回原点又重转」。用负 animation-delay 让新建元素直接续到
// performance.now() 对应的相位（periodS 须与该 spinner 的 CSS 动画周期秒数一致），跨重建平滑推进。
// reduced-motion 下 animation:none，此 delay 无副作用。
export function applySpinPhase(el, periodS) {
  if (!el || !el.style || !periodS) {
    return;
  }
  const now = typeof performance !== "undefined" && performance.now ? performance.now() : 0;
  el.style.animationDelay = `-${((now / 1000) % periodS).toFixed(3)}s`;
}

function displayValue(value) {
  if (Array.isArray(value)) {
    const lines = value
      .map((item) => {
        if (item && typeof item === "object") {
          return text(item.content || item.summary || item.path || item.name || item.toolUseId || "");
        }
        return text(item);
      })
      .filter(Boolean);
    if (lines.length > 0) {
      return lines.join("\n\n");
    }
  }
  if (value && typeof value === "object") {
    const input = value;
    const focused = input.path || input.file || input.filename || input.command || input.cmd || input.pattern;
    if (focused) {
      return text(focused);
    }
  }
  return text(value);
}

function parseObject(value) {
  if (!value) {
    return null;
  }
  if (typeof value === "object") {
    return value;
  }
  if (typeof value !== "string") {
    return null;
  }
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function inputObject(tool = {}) {
  return parseObject(tool.input) || parseObject(tool.args) || parseObject(tool.arguments) || {};
}

function firstTextField(source = {}, keys = []) {
  for (const key of keys) {
    const value = source?.[key];
    if (value !== undefined && value !== null && value !== "") {
      return text(value);
    }
  }
  return "";
}

function commandFromTool(tool = {}) {
  const input = inputObject(tool);
  return text(
    tool.command ||
      input.command ||
      input.cmd ||
      input.shell ||
      input.script ||
      input.argv?.join?.(" ") ||
      "",
  );
}

function toolName(tool = {}) {
  return text(tool.toolName || tool.name || tool.kind || tool.toolUseId || "tool");
}

function lowerToolName(tool = {}) {
  return toolName(tool).toLowerCase();
}

const ROS_ACTION_GROUP_TOOLS = new Set([
  "ros_stack_group",
  "ros_template",
  "ros_template_scratch",
  "ros_diagnostic",
  "ros_resource_type_registration",
  "ros_tag",
]);

export function isAliyunApiTool(tool = {}) {
  const name = lowerToolName(tool);
  return name === "aliyun_api" || name === "aliyun-api" || ROS_ACTION_GROUP_TOOLS.has(name);
}

function aliyunApiProduct(tool = {}) {
  const input = inputObject(tool);
  const product = firstTextField(tool, ["product", "Product", "service", "Service"]) ||
    firstTextField(input, ["product", "Product", "service", "Service"]);
  if (product) {
    return product.toUpperCase();
  }
  return ROS_ACTION_GROUP_TOOLS.has(lowerToolName(tool)) ? "ROS" : "";
}

function aliyunApiAction(tool = {}) {
  const input = inputObject(tool);
  return (
    firstTextField(tool, ["action", "Action", "apiName", "api_name", "operation", "Operation"]) ||
    firstTextField(input, ["action", "Action", "apiName", "api_name", "operation", "Operation"])
  );
}

function aliyunApiLabel(tool = {}) {
  const parts = [aliyunApiProduct(tool), aliyunApiAction(tool)].filter(Boolean);
  return parts.length > 0 ? parts.join(" ") : t("Alibaba Cloud API");
}

// MCP 工具在注册时被命名为 mcp__{server}__{tool}(见后端 mcp/manager.py)。
export function isMcpTool(tool = {}) {
  return lowerToolName(tool).startsWith("mcp__");
}

// 解析 mcp__server__tool → { server, tool }。首个「__」定界 server 与 tool:
// server 先注册(单段标识符),tool 段可能自带下划线,故取剩余全部。
function mcpToolParts(tool = {}) {
  const raw = toolName(tool);
  const rest = raw.slice("mcp__".length);
  const boundary = rest.indexOf("__");
  if (boundary < 0) {
    return { server: "", tool: rest };
  }
  return { server: rest.slice(0, boundary), tool: rest.slice(boundary + 2) };
}

// 渲染成「server · tool」;缺 server 时退化为 tool 段本身。
function mcpToolLabel(tool = {}) {
  const { server, tool: toolPart } = mcpToolParts(tool);
  const parts = [server, toolPart].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : toolName(tool);
}

function basename(pathValue) {
  const value = text(pathValue).trim();
  if (!value) {
    return "";
  }
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) || value;
}

function targetFromTool(tool = {}) {
  const input = inputObject(tool);
  return (
    basename(input.path || input.file || input.filename || input.cwd || input.directory || input.dir || input.pattern) ||
    basename(tool.summary)
  );
}

export function isShellTool(tool = {}) {
  const name = lowerToolName(tool);
  return (
    tool.local === true ||
    Boolean(commandFromTool(tool)) ||
    name === "bash" ||
    name.includes("shell") ||
    name.includes("command") ||
    name.includes("exec")
  );
}

function isReadTool(tool = {}) {
  const name = lowerToolName(tool);
  return name.includes("read") || name.includes("fetch_file") || name.includes("open_file");
}

function isListTool(tool = {}) {
  const name = lowerToolName(tool);
  return name.includes("list") || name.includes("search") || name === "rg";
}

function isWriteTool(tool = {}) {
  const name = lowerToolName(tool);
  return name.includes("write") || name.includes("edit") || name.includes("patch") || name.includes("create");
}

export function isCompleteStepTool(tool = {}) {
  return lowerToolName(tool) === "complete_step";
}

// 拥有专属中文动作短语的工具（complete_step 或命中 TOOL_ACTION_LABELS）。
// 分组摘要要把它们排除在读/写/列/命令/API 计数之外，改用各自的短语，
// 避免 read_memory 被算成「已读取文件」、complete_step 被算成「已使用工具」。
function isLabeledActionTool(tool = {}) {
  return isCompleteStepTool(tool) || Boolean(TOOL_ACTION_LABELS[lowerToolName(tool)]);
}

// 分组摘要里某个带标签工具的动作短语：complete_step → 「Completed step」，
// 其余取 TOOL_ACTION_LABELS(过去时) / TOOL_ACTION_LABELS_PROGRESS(进行时) 里的现成短语。
function labeledActionPhrase(tool = {}, tense = "done") {
  if (isCompleteStepTool(tool)) {
    return tense === "progress" ? t("Completing step") : t("Completed step");
  }
  const name = lowerToolName(tool);
  if (tense === "progress") {
    return TOOL_ACTION_LABELS_PROGRESS[name] || TOOL_ACTION_LABELS[name] || "";
  }
  return TOOL_ACTION_LABELS[name] || "";
}

const CONCLUSION_LABELS = {
  is_infra_intent: t("Is infrastructure intent"),
  confidence: t("Confidence"),
  user_message_summary: t("Requirement summary"),
  cloud_platform: t("Cloud platform"),
  business_type: t("Business type"),
  core_requirements: t("Core requirements"),
  resource_intents: t("Resource intents"),
  non_functional: t("Non-functional requirements"),
  additional_notes: t("Additional notes"),
  candidates: t("Candidates"),
  name: t("Name"),
  output_path: t("Output path"),
  products: t("Cloud products"),
  product: t("Cloud products"),
  action: t("Action"),
  role: t("Role"),
  source: t("Source"),
  notes: t("Notes"),
  region_preference: t("Region preference"),
  scale_hint: t("Scale hint"),
  topology: t("Topology"),
  monthly_estimate: t("Monthly estimate"),
  pros: t("Pros"),
  cons: t("Cons"),
  costs: t("Cost"),
  cost: t("Cost"),
  currency: t("Currency"),
  summary: t("Summary"),
  description: t("Description"),
  reason: t("Reason"),
  status: t("Status"),
  stack_id: t("Stack ID"),
  // 模板生成
  template: t("Template"),
  template_sha256: t("Template SHA256"),
  template_fixed: t("Template fixed"),
  file_path: t("File path"),
  region: t("Region"),
  validated: t("Validated"),
  type: t("Type"),
  key: t("Key"),
  // 模板评审 / InfraGuard
  review_passed: t("Review passed"),
  review_issues: t("Review issues"),
  selected_review_aspects: t("Selected review aspects"),
  skipped_review_aspects: t("Skipped review aspects"),
  resolved_infraguard_policies: t("Resolved InfraGuard policies"),
  infraguard_summary: t("InfraGuard summary"),
  passed: t("Passed"),
  blocking_findings: t("Blocking findings"),
  total_violations: t("Total violations"),
  severity_counts: t("Severity counts"),
  files_scanned: t("Files scanned"),
  files_with_violations: t("Files with violations"),
  fix_summary: t("Fix summary"),
  high: t("High risk"),
  medium: t("Medium risk"),
  low: t("Low risk"),
  // 成本 / 部署
  resources: t("Resource inventory"),
  deployment_parameters: t("Deployment parameters"),
  missing_deployment_parameters: t("Missing deployment parameters"),
  parameter_set_summary: t("Parameter set summary"),
  api_raw_summary: t("API summary"),
  error: t("Error"),
  // 部署结果
  resources_created: t("Resources created"),
  outputs: t("Outputs"),
  // ros_deploy 部署结果
  stack_name: t("Stack name"),
  status_reason: t("Status reason"),
  progress_percentage: t("Progress percentage"),
  elapsed_seconds: t("Elapsed seconds"),
  is_success: t("Is success"),
  error_code: t("Error code"),
  recommended_action: t("Recommended action"),
  message: t("Message"),
  // 方案选择 / 用户确认
  user_prompt: t("User prompt"),
  user_input: t("User input"),
  options: t("Options"),
  candidate_index: t("Candidate index"),
  selected_candidate_name: t("Selected candidate name"),
  selected_candidate_index: t("Selected candidate index"),
  // ask_user_question
  question: t("Question"),
  selected_id: t("Selected ID"),
  selected_label: t("Selected item"),
  free_text: t("Free text"),
  // InfraGuard 补充
  command: t("Command"),
  exit_code: t("Exit code"),
  mode: t("Mode"),
  findings: t("Findings"),
  file_sha256: t("File SHA256"),
  stderr: t("Error output"),
  // ROS OpenAPI 原始响应体常见字段
  Parameters: t("Parameters"),
  Resources: t("Resources"),
  ResourceTypes: t("Resource types"),
  Outputs: t("Outputs"),
  RequestId: t("Request ID"),
  Stack: t("Stack"),
  ParameterConstraints: t("Parameter constraints"),
  UpdateInfo: t("Update info"),
};

// ROS 资源栈状态码 → 中文。作为结果里的「值」出现(如 ros_deploy 的 status),
// 按整串精确匹配翻译;未知状态原样保留(不猜测)。
const CONCLUSION_VALUE_LABELS = {
  CREATE_IN_PROGRESS: t("Creating"),
  CREATE_FAILED: t("Create failed"),
  CREATE_COMPLETE: t("Create complete"),
  UPDATE_IN_PROGRESS: t("Updating"),
  UPDATE_FAILED: t("Update failed"),
  UPDATE_COMPLETE: t("Update complete"),
  ROLLBACK_IN_PROGRESS: t("Rolling back"),
  ROLLBACK_FAILED: t("Rollback failed"),
  ROLLBACK_COMPLETE: t("Rollback complete"),
  DELETE_IN_PROGRESS: t("Deleting"),
  DELETE_FAILED: t("Delete failed"),
  DELETE_COMPLETE: t("Delete complete"),
  CHECK_IN_PROGRESS: t("Checking"),
  CHECK_FAILED: t("Check failed"),
  CHECK_COMPLETE: t("Check complete"),
};

function conclusionLabel(key) {
  if (Object.prototype.hasOwnProperty.call(CONCLUSION_LABELS, key)) {
    return CONCLUSION_LABELS[key];
  }
  return String(key)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function conclusionScalarText(value) {
  if (value === true) {
    return t("Yes");
  }
  if (value === false) {
    return t("No");
  }
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" && Object.prototype.hasOwnProperty.call(CONCLUSION_VALUE_LABELS, value)) {
    return CONCLUSION_VALUE_LABELS[value];
  }
  return text(value);
}

function renderConclusionValue(value) {
  if (value === null || value === undefined || typeof value !== "object") {
    const span = document.createElement("span");
    span.className = "conclusion-scalar";
    span.textContent = conclusionScalarText(value);
    return span;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      const span = document.createElement("span");
      span.className = "conclusion-scalar";
      span.textContent = "—";
      return span;
    }
    const list = document.createElement("ul");
    list.className = "conclusion-list";
    for (const item of value) {
      const li = document.createElement("li");
      li.className = "conclusion-item";
      li.append(renderConclusionValue(item));
      list.append(li);
    }
    return list;
  }
  const entries = Object.entries(value).filter(([, val]) => val !== undefined);
  if (entries.length === 0) {
    const span = document.createElement("span");
    span.className = "conclusion-scalar";
    span.textContent = "—";
    return span;
  }
  const dl = document.createElement("dl");
  dl.className = "conclusion-object";
  for (const [key, val] of entries) {
    const row = document.createElement("div");
    row.className = "conclusion-field";
    const dt = document.createElement("dt");
    dt.className = "conclusion-key";
    dt.textContent = conclusionLabel(key);
    const dd = document.createElement("dd");
    dd.className = "conclusion-value";
    dd.append(renderConclusionValue(val));
    row.append(dt, dd);
    dl.append(row);
  }
  return dl;
}

function completeStepConclusion(tool = {}) {
  const input = inputObject(tool);
  if (input && Object.prototype.hasOwnProperty.call(input, "conclusion")) {
    return input.conclusion;
  }
  return Object.keys(input).length > 0 ? input : null;
}

function renderCompleteStepDetail(tool = {}) {
  const conclusion = completeStepConclusion(tool);
  if (conclusion === null || conclusion === undefined) {
    return renderGenericDetail(tool);
  }

  const detail = document.createElement("section");
  detail.className = "tool-complete-step-detail";

  const heading = document.createElement("h4");
  heading.className = "conclusion-heading";
  heading.textContent = t("Conclusion");
  detail.append(heading, renderConclusionValue(conclusion));

  return detail;
}

// 已知工具名 → 完整英文动作短语（过去时）。命中时直接作为卡片标题，
// 优先于下方基于名称子串的读/写/列启发式判断。
const TOOL_ACTION_LABELS = {
  read_memory: t("Read memory"),
  write_memory: t("Saved memory"),
  infraguard_scan: t("Ran InfraGuard scan"),
  show_architecture_diagram: t("Showed architecture diagram"),
  show_candidate_detail: t("Showed candidate details"),
  ros_validate_template: t("Validated template"),
  ros_get_template_parameter_constraints: t("Fetched template parameter constraints"),
  ros_preview_template: t("Previewed stack changes"),
  ros_estimate_template_cost: t("Estimated resource cost"),
  ros_deploy: t("Deployed stack"),
  ros_stack: t("Operated ROS stack"),
  ros_stack_instances: t("Queried ROS stack instances"),
  aliyun_doc_search: t("Searched Alibaba Cloud docs"),
  aliyun_api_doc: t("Looked up Alibaba Cloud API reference"),
  ask_user_question: t("Asked the user"),
};

// 同一批工具名 → 进行时短语。执行中的卡片用它替代过去时，避免"正在运行时却显示已完成"。
const TOOL_ACTION_LABELS_PROGRESS = {
  read_memory: t("Reading memory"),
  write_memory: t("Saving memory"),
  infraguard_scan: t("Running InfraGuard scan"),
  show_architecture_diagram: t("Showing architecture diagram"),
  show_candidate_detail: t("Showing candidate details"),
  ros_validate_template: t("Validating template"),
  ros_get_template_parameter_constraints: t("Fetching template parameter constraints"),
  ros_preview_template: t("Previewing stack changes"),
  ros_estimate_template_cost: t("Estimating resource cost"),
  ros_deploy: t("Deploying stack"),
  ros_stack: t("Operating ROS stack"),
  ros_stack_instances: t("Querying ROS stack instances"),
  aliyun_doc_search: t("Searching Alibaba Cloud docs"),
  aliyun_api_doc: t("Looking up Alibaba Cloud API reference"),
  ask_user_question: t("Asking the user"),
};

// 工具卡片标题的动作目标(命令 / 文件 / API 标签等),用于 canceled/denied 时保留目标。
function toolActionTarget(tool = {}) {
  if (isCompleteStepTool(tool)) {
    return "";
  }
  if (TOOL_ACTION_LABELS[lowerToolName(tool)]) {
    return "";
  }
  if (isAliyunApiTool(tool)) {
    return compact(aliyunApiLabel(tool), 96);
  }
  if (isMcpTool(tool)) {
    return compact(mcpToolLabel(tool), 96);
  }
  if (isShellTool(tool)) {
    return compact(commandFromTool(tool) || toolName(tool), 140);
  }
  if (isReadTool(tool)) {
    return compact(targetFromTool(tool), 96);
  }
  if (isListTool(tool)) {
    return compact(targetFromTool(tool), 96);
  }
  if (isWriteTool(tool)) {
    return compact(targetFromTool(tool) || toolName(tool), 96);
  }
  return compact(toolName(tool), 96);
}

// 按工具类别 + 状态(done/progress/failed)生成卡片标题。英文没有中文「已→正在」的
// 统一前缀变形,故每个状态直接选用对应 msgid。
function toolPhrase(tool = {}, state = "done") {
  if (isCompleteStepTool(tool)) {
    // 步骤名由所在流水线步骤组标题承载(服务端按当前 UI 语言重算),卡片无需重复;
    // 卡片只能拿到「生成时语言」的结果文本、无法本地化,故仅给状态短语。
    if (state === "progress") return t("Completing step");
    if (state === "failed") return t("Step failed");
    return t("Completed step");
  }
  const name = lowerToolName(tool);
  if (TOOL_ACTION_LABELS[name]) {
    // 带标签工具:进行时用进行时短语;失败时保留过去时短语(失败态由红色边框 / 状态胶囊体现)。
    if (state === "progress") {
      return TOOL_ACTION_LABELS_PROGRESS[name] || TOOL_ACTION_LABELS[name];
    }
    return TOOL_ACTION_LABELS[name];
  }
  if (isAliyunApiTool(tool)) {
    const label = compact(aliyunApiLabel(tool), 96);
    if (state === "progress") {
      return t("Calling {label}", { label });
    }
    if (state === "failed") {
      return t("Call failed: {label}", { label });
    }
    return t("Called {label}", { label });
  }
  // MCP 工具:必须先于 read/list/write 子串启发式,否则 mcp__x__list_* 会被误判成「列出文件」。
  // 复用 Calling/Called/Call failed 短语(与阿里云 API 一致),不新增 msgid。
  if (isMcpTool(tool)) {
    const label = compact(mcpToolLabel(tool), 96);
    if (state === "progress") {
      return t("Calling {label}", { label });
    }
    if (state === "failed") {
      return t("Call failed: {label}", { label });
    }
    return t("Called {label}", { label });
  }
  if (isShellTool(tool)) {
    const command = compact(commandFromTool(tool) || toolName(tool), 140);
    if (state === "progress") {
      return t("Running {command}", { command });
    }
    if (state === "failed") {
      return t("Run failed: {command}", { command });
    }
    return t("Ran {command}", { command });
  }
  if (isReadTool(tool)) {
    const target = compact(targetFromTool(tool), 96);
    if (!target) {
      if (state === "progress") {
        return t("Reading file");
      }
      return state === "failed" ? t("Read failed") : t("Read file");
    }
    if (state === "progress") {
      return t("Reading {target}", { target });
    }
    if (state === "failed") {
      return t("Read failed: {target}", { target });
    }
    return t("Read {target}", { target });
  }
  if (isListTool(tool)) {
    const target = compact(targetFromTool(tool), 96);
    if (!target) {
      if (state === "progress") {
        return t("Listing files");
      }
      return state === "failed" ? t("List failed") : t("Listed files");
    }
    if (state === "progress") {
      return t("Listing {target}", { target });
    }
    if (state === "failed") {
      return t("List failed: {target}", { target });
    }
    return t("Listed {target}", { target });
  }
  if (isWriteTool(tool)) {
    const target = compact(targetFromTool(tool) || toolName(tool), 96);
    if (state === "progress") {
      return t("Modifying {target}", { target });
    }
    if (state === "failed") {
      return t("Modify failed: {target}", { target });
    }
    return t("Modified {target}", { target });
  }
  const generic = compact(toolName(tool), 96);
  if (state === "progress") {
    return t("Using {name}", { name: generic });
  }
  if (state === "failed") {
    return t("Use failed: {name}", { name: generic });
  }
  return t("Used {name}", { name: generic });
}

export function toolCommandText(tool = {}) {
  // 回合已结束(被停止/刷新丢弃)后显示"Canceled/Denied 目标",避免遗留卡片停在"正在运行"。
  const status = text(tool.status).toLowerCase();
  if (status === "canceled") {
    const target = toolActionTarget(tool);
    return target ? t("Canceled {target}", { target }) : t("Canceled");
  }
  if (status === "denied") {
    const target = toolActionTarget(tool);
    return target ? t("Denied {target}", { target }) : t("Denied");
  }
  // 进行中 → 进行时;失败 → 失败态(避免失败只显示"已完成", Issue 4);其余 → 过去时。
  if (isToolInProgress(tool)) {
    return toolPhrase(tool, "progress");
  }
  if (isToolFailed(tool)) {
    return toolPhrase(tool, "failed");
  }
  return toolPhrase(tool, "done");
}

// 工具是否仍在执行中：尚未拿到终态状态、也没有结果/退出码。
// 命中时给卡片加 is-active，标题会显示流光动画，与"正在思考"呼应。
export function isToolInProgress(tool = {}) {
  const status = text(tool.status).toLowerCase();
  if (status === "completed" || status === "failed" || status === "canceled" || status === "denied") {
    return false;
  }
  if (Array.isArray(tool.results) && tool.results.length > 0) {
    return false;
  }
  if (Number.isInteger(tool.exitCode)) {
    return false;
  }
  return status === "running" || status === "pending" || status === "input_complete" || status === "started";
}

// 工具是否失败：仅认显式 failed 状态。shell 非零退出码不算失败——像
// `git show-ref --quiet` 这类探测命令会故意返回非零，且退出码已由状态胶囊
//（toolStatusText 的"退出码 N"）体现，标题/边框再标红只会误报。
export function isToolFailed(tool = {}) {
  return text(tool.status).toLowerCase() === "failed";
}

export function toolStatusText(tool = {}) {
  const exitCode = shellExitCode(tool);
  if (Number.isInteger(exitCode)) {
    return exitCode === 0 ? `✓ ${t("Success")}` : t("Exit code {code}", { code: exitCode });
  }
  if (tool.status === "completed") {
    return `✓ ${t("Complete")}`;
  }
  if (tool.status === "failed") {
    return t("Failed");
  }
  if (tool.status === "canceled") {
    return t("Canceled");
  }
  if (tool.status === "denied") {
    return t("Denied");
  }
  return text(tool.status) || t("Waiting");
}

function toolResultContent(tool = {}) {
  return (tool.results || [])
    .map((item) => {
      if (item && typeof item === "object") {
        return text(item.content || item.summary || item.result || "");
      }
      return text(item);
    })
    .filter(Boolean)
    .join("\n\n");
}

function shellExitCode(tool = {}) {
  if (Number.isInteger(tool.exitCode)) {
    return tool.exitCode;
  }
  const match = toolResultContent(tool).match(/(?:Exit code|退出码):\s*(-?\d+)/i);
  return match ? Number.parseInt(match[1], 10) : null;
}

function shellResultSection(tool = {}, label) {
  const content = toolResultContent(tool);
  const pattern = new RegExp(`${label}:\\s*([\\s\\S]*?)(?=\\n\\n(?:STDOUT|STDERR|Exit code|退出码):|$)`, "i");
  const match = content.match(pattern);
  return match ? match[1].trim() : "";
}

function shellStdout(tool = {}) {
  return text(tool.stdout).trim() || shellResultSection(tool, "STDOUT");
}

function shellStderr(tool = {}) {
  return text(tool.stderr).trim() || shellResultSection(tool, "STDERR");
}

function toolMetaText(tool = {}) {
  const pieces = [];
  if (Number.isInteger(tool.elapsedMs)) {
    pieces.push(`${tool.elapsedMs}ms`);
  }
  return pieces.join(" · ");
}

function isToolCanceled(tool = {}) {
  return text(tool.status).toLowerCase() === "canceled";
}

// 分组标题各部件均以小写动词开头(便于用逗号拼接成一句),最终由 toolGroupSummary 把
// 整句首字母大写。英文无中文「已→正在」的统一变形,故进行时通过 tense 参数选用对应 msgid。
function capitalizeFirst(value) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

function commandSummaryPart(n, tense) {
  if (tense === "progress") {
    return n === 1 ? t("running {n} command", { n }) : t("running {n} commands", { n });
  }
  return n === 1 ? t("ran {n} command", { n }) : t("ran {n} commands", { n });
}

function aliyunSummaryPart(n, tense) {
  if (tense === "progress") {
    return n === 1 ? t("calling {n} Alibaba Cloud API", { n }) : t("calling {n} Alibaba Cloud APIs", { n });
  }
  return n === 1 ? t("called {n} Alibaba Cloud API", { n }) : t("called {n} Alibaba Cloud APIs", { n });
}

function readSummaryPart(n, tense) {
  if (tense === "progress") {
    return n === 1 ? t("reading {n} file", { n }) : t("reading {n} files", { n });
  }
  return n === 1 ? t("read {n} file", { n }) : t("read {n} files", { n });
}

function listSummaryPart(n, tense) {
  if (tense === "progress") {
    return n === 1 ? t("listing files") : t("listing {n} times", { n });
  }
  return n === 1 ? t("listed files") : t("listed {n} times", { n });
}

function writeSummaryPart(n, tense) {
  if (tense === "progress") {
    return n === 1 ? t("modifying {n} item", { n }) : t("modifying {n} items", { n });
  }
  return n === 1 ? t("modified {n} item", { n }) : t("modified {n} items", { n });
}

function otherSummaryPart(n, tense) {
  if (tense === "progress") {
    return n === 1 ? t("using {n} tool", { n }) : t("using {n} tools", { n });
  }
  return n === 1 ? t("used {n} tool", { n }) : t("used {n} tools", { n });
}

export function toolGroupSummary(tools = []) {
  // 分组里只要还有工具在执行，就整体切换为进行时表述。
  if (tools.some(isToolInProgress)) {
    return capitalizeFirst(toolGroupSummaryParts(tools, "progress"));
  }
  // 整组都因回合结束被取消时，用"Canceled …"表述而非"Ran/Called …"。
  if (tools.length > 0 && tools.every(isToolCanceled)) {
    const n = tools.length;
    return n === 1 ? t("Canceled {n} tool", { n }) : t("Canceled {n} tools", { n });
  }
  return capitalizeFirst(toolGroupSummaryParts(tools, "done"));
}

function toolGroupSummaryParts(tools = [], tense = "done") {
  // 带专属短语的工具（complete_step / read_memory 等）先拎出来单独统计，
  // 不落入下面基于名称子串的读/写/列启发式，避免「读取记忆」被误报成「读取文件」。
  const plain = (tool) => !isLabeledActionTool(tool);
  const shellCount = tools.filter((tool) => plain(tool) && isShellTool(tool)).length;
  const aliyunApiCount = tools.filter((tool) => plain(tool) && isAliyunApiTool(tool)).length;
  const readCount = tools.filter(
    (tool) => plain(tool) && !isShellTool(tool) && !isAliyunApiTool(tool) && isReadTool(tool),
  ).length;
  const listCount = tools.filter(
    (tool) => plain(tool) && !isShellTool(tool) && !isAliyunApiTool(tool) && isListTool(tool),
  ).length;
  const writeCount = tools.filter(
    (tool) => plain(tool) && !isShellTool(tool) && !isAliyunApiTool(tool) && isWriteTool(tool),
  ).length;

  // 带标签工具按短语归并计数（同一短语只出现一次，>1 时带次数）。
  const labeledCounts = new Map();
  for (const tool of tools) {
    if (!isLabeledActionTool(tool)) {
      continue;
    }
    const phrase = labeledActionPhrase(tool, tense);
    if (phrase) {
      labeledCounts.set(phrase, (labeledCounts.get(phrase) || 0) + 1);
    }
  }
  const labeledCount = tools.filter(isLabeledActionTool).length;

  if (shellCount === tools.length) {
    return commandSummaryPart(shellCount, tense);
  }
  if (aliyunApiCount === tools.length) {
    return aliyunSummaryPart(aliyunApiCount, tense);
  }

  const parts = [];
  if (readCount > 0) {
    parts.push(readSummaryPart(readCount, tense));
  }
  if (listCount > 0) {
    parts.push(listSummaryPart(listCount, tense));
  }
  if (writeCount > 0) {
    parts.push(writeSummaryPart(writeCount, tense));
  }
  if (shellCount > 0) {
    parts.push(commandSummaryPart(shellCount, tense));
  }
  if (aliyunApiCount > 0) {
    parts.push(aliyunSummaryPart(aliyunApiCount, tense));
  }
  for (const [phrase, count] of labeledCounts) {
    parts.push(count > 1 ? t("{phrase} {count} times", { phrase, count }) : phrase);
  }

  const covered = shellCount + aliyunApiCount + readCount + listCount + writeCount + labeledCount;
  const otherCount = Math.max(0, tools.length - covered);
  if (otherCount > 0) {
    parts.push(otherSummaryPart(otherCount, tense));
  }

  return parts.length > 0 ? parts.join(", ") : otherSummaryPart(tools.length, tense);
}

function appendValueBlock(parent, className, label, value) {
  if (value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0)) {
    return;
  }
  const section = document.createElement("section");
  section.className = className;

  const title = document.createElement("h4");
  title.textContent = label;

  const block = document.createElement("pre");
  block.textContent = displayValue(value);

  section.append(title, block);
  parent.append(section);
}

function appendShellOutput(parent, label, value) {
  const content = text(value).trim();
  if (!content) {
    return;
  }

  const title = document.createElement("h4");
  title.textContent = label;

  const block = document.createElement("pre");
  block.className = "tool-shell-output";
  block.textContent = content;

  parent.append(title, block);
}

function renderShellDetail(tool = {}) {
  const detail = document.createElement("section");
  detail.className = "tool-shell-detail";

  const label = document.createElement("p");
  label.className = "tool-shell-label";
  label.textContent = t("Shell");

  const command = document.createElement("pre");
  command.className = "tool-shell-command";
  command.textContent = `$ ${text(commandFromTool(tool) || toolName(tool))}`;

  detail.append(label, command);

  const stdout = shellStdout(tool);
  const stderr = shellStderr(tool);
  if (stdout || stderr) {
    appendShellOutput(detail, t("Output"), stdout);
    appendShellOutput(detail, t("Error output"), stderr);
  } else {
    const empty = document.createElement("p");
    empty.className = "tool-shell-empty";
    empty.textContent = t("No output");
    detail.append(empty);
  }

  const footer = document.createElement("p");
  footer.className = "tool-shell-status";
  footer.textContent = toolStatusText(tool);
  detail.append(footer);

  return detail;
}

// 从 tool.results 里取出首个可解析为「非空对象/数组」的 JSON 结果。
// pipeline 工具的结果串就存在 item.content（后端 json.dumps 的原始串），
// 命中时可交给 renderConclusionValue 做带中文字段标签的结构化渲染；
// 解析不出（纯文本 / 空对象）则返回 null，回退到原始 <pre> 文本块。
function structuredResultValue(tool = {}) {
  const items = Array.isArray(tool.results) ? tool.results : [];
  for (const item of items) {
    const raw = item && typeof item === "object" ? (item.content ?? item.summary ?? item.result) : item;
    const parsed = parseObject(raw);
    if (parsed && typeof parsed === "object") {
      const size = Array.isArray(parsed) ? parsed.length : Object.keys(parsed).length;
      if (size > 0) {
        return parsed;
      }
    }
  }
  return null;
}

function renderGenericDetail(tool = {}, options = {}) {
  const detail = document.createElement("section");
  detail.className = "tool-generic-detail";

  if (tool.summary) {
    const summary = document.createElement("p");
    summary.className = "tool-card-summary";
    summary.textContent = text(tool.summary);
    detail.append(summary);
  }

  if (options.includeInput !== false) {
    appendValueBlock(detail, "tool-card-input", t("Input"), tool.input);
  }
  if (options.includeResults !== false) {
    const structured = structuredResultValue(tool);
    if (structured !== null) {
      const section = document.createElement("section");
      section.className = "tool-card-results";
      const title = document.createElement("h4");
      title.textContent = t("Result");
      section.append(title, renderConclusionValue(structured));
      detail.append(section);
    } else {
      appendValueBlock(detail, "tool-card-results", t("Result"), tool.results);
    }
  }
  appendValueBlock(detail, "tool-card-artifacts", t("Artifacts"), tool.artifacts);
  appendValueBlock(detail, "tool-card-children", t("Sub-tools"), tool.children);

  if (detail.children.length === 0 && options.allowEmpty === false) {
    return null;
  }

  if (detail.children.length === 0) {
    const empty = document.createElement("p");
    empty.className = "tool-detail-empty";
    empty.textContent = t("No details yet");
    detail.append(empty);
  }

  return detail;
}

// 工具卡是否默认展开的纯决策（抽出便于测试）：
// - 流水线会话（collapseNonComplete=true）里所有工具（含 complete_step）一律默认收起，
//   避免逐条事件到达时"最新/进行中"卡每帧展开又收起造成的闪烁；用户可手动展开，其态经
//   applyDetailsOpenOverrides 跨帧保留；
// - 进行中的工具一律默认收起：执行态频繁重渲染，展开会抖动，且用户明确要求收起
//   （latestToolUseId 通常指向进行中的尾部工具，故须在 isLatest 之前拦截）；
// - 回合进行中（turnActive）：尾部「最新」的那张工具卡即便已完成也保持收起——活动轮次里
//   工具接连到来、每帧都在重渲染，让尾部卡展开会造成抖动，且用户要求进行中的会话里工具
//   一律收起。仅当回合结束（静息）后，才让转录尾部最新的已完成卡展开；
// - 其余非流水线维持原样：complete_step 结论卡展开、（静息态）转录尾部最新卡展开。
export function shouldOpenToolCard({ isCompleteStep, collapseNonComplete, inProgress, isLatest, turnActive, hasActiveStackProgress } = {}) {
  // 部署/删除等栈操作进行中且已挂到实时进度帧时,强制默认展开——用户要求「部署时自动展开
  // 看到实时进度」,故须先于 collapseNonComplete/inProgress 的收起短路。用户仍可手动收起
  // (态经 applyDetailsOpenOverrides 跨帧保留)。
  if (hasActiveStackProgress) {
    return true;
  }
  if (collapseNonComplete) {
    return false;
  }
  if (inProgress) {
    return false;
  }
  if (isCompleteStep) {
    return true;
  }
  if (turnActive) {
    return false;
  }
  return Boolean(isLatest);
}

// 栈生命周期实时进度(ros_deploy / ros_stack / ros_stack_instances)——镜像 REPL 的
// renderer.py:_render_stack_progress:标题「栈: 名(id) [中文状态] 百分比」+ 资源/实例列表 + 用时。
// 仅当 reducer 已把最新一帧挂到 tool.stackProgress 时渲染(见 events.js 的 pipeline.event 分支)。
function renderStackProgressDetail(tool = {}) {
  const progress = tool.stackProgress;
  if (!progress || typeof progress !== "object") {
    return null;
  }
  const isInstances = progress.kind === "stack.instances.progress";
  const section = document.createElement("section");
  section.className = "tool-stack-progress";

  const name = text(progress.stackName || progress.stackGroupName);
  const id = text(progress.stackId || progress.operationId);
  const statusText = conclusionScalarText(progress.status);
  const rawStatus = text(progress.status);
  const isFailed = /FAIL|ROLLBACK|DELETE_FAILED|CREATE_FAILED|UPDATE_FAILED/i.test(rawStatus);
  const pct = Number(progress.progressPercentage);
  const pctText = Number.isFinite(pct) ? `${pct}%` : "";
  const isDone = Number.isFinite(pct) && pct >= 100 && !isFailed;

  // 头部一行:栈名(id) + 状态徽标 + 百分比,布局交给 CSS(flex)而非拼字符串,更整洁。
  const head = document.createElement("div");
  head.className = "tool-stack-progress-head";
  const title = createText(
    "tool-stack-progress-title",
    name ? (id ? t("Stack: {name} ({id})", { name, id }) : t("Stack: {name}", { name })) : t("Stack"),
  );
  if (isToolInProgress(tool)) {
    // 进行中:标题流光,与工具卡标题一致(对齐相位避免重建闪回)。
    applyShimmerPhase(title);
  }
  head.append(title);
  if (statusText) {
    const stateClass = isFailed ? " is-error" : isDone ? " is-done" : "";
    head.append(createText(`tool-stack-progress-status${stateClass}`, statusText));
  }
  if (pctText) {
    head.append(createText("tool-stack-progress-pct", pctText));
  }
  section.append(head);

  // 进度条:仅在有有效百分比时渲染;失败/完成/进行中用不同色。
  if (Number.isFinite(pct)) {
    const track = document.createElement("div");
    track.className = "tool-stack-progress-bar";
    const fill = document.createElement("div");
    const fillStateClass = isFailed ? " is-error" : isDone ? " is-done" : "";
    fill.className = `tool-stack-progress-bar-fill${fillStateClass}`;
    if (fill.style) {
      // 测试用的 DOM 桩件无 style;与 applyShimmerPhase 同样容错。
      fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    }
    track.append(fill);
    section.append(track);
  }

  const rows = isInstances
    ? Array.isArray(progress.instances)
      ? progress.instances
      : []
    : Array.isArray(progress.resources)
      ? progress.resources
      : [];
  if (rows.length > 0) {
    // 资源/实例进度改用表格,列对齐比「·」拼接更易读。资源列:资源/类型/状态;实例列:账号/地域/状态。
    // 任一行带 status_reason 时追加「原因」列。状态单元格按 完成/失败 上色(复用 head 的配色变量)。
    const table = document.createElement("table");
    table.className = "tool-stack-progress-table";
    const hasReason = rows.some((row) => text(row.status_reason));
    const headers = isInstances
      ? [t("Account"), t("Region"), t("Status")]
      : [t("Resource"), t("Type"), t("Status")];
    if (hasReason) {
      headers.push(t("Reason"));
    }
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const heading of headers) {
      const th = document.createElement("th");
      th.textContent = heading;
      headRow.append(th);
    }
    thead.append(headRow);
    table.append(thead);
    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      const rowStatus = text(row.status);
      const rowFailed = /FAIL|ROLLBACK|DELETE_FAILED|CREATE_FAILED|UPDATE_FAILED/i.test(rowStatus);
      const rowDone = /COMPLETE|SUCCESS/i.test(rowStatus) && !rowFailed;
      const cells = isInstances
        ? [text(row.account_id), text(row.region_id), conclusionScalarText(row.status)]
        : [text(row.name), text(row.resource_type), conclusionScalarText(row.status)];
      if (hasReason) {
        cells.push(text(row.status_reason));
      }
      cells.forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 2) {
          const statusClass = rowFailed ? " is-error" : rowDone ? " is-done" : "";
          td.className = `tool-stack-progress-cell-status${statusClass}`;
        }
        tr.append(td);
      });
      tbody.append(tr);
    }
    table.append(tbody);
    section.append(table);
  }

  const baseElapsed = Number(progress.elapsedSeconds);
  const receivedAt = Number(progress.receivedAtMs);
  const deploymentComplete = progress.deploymentComplete === true;
  // 部署进行中且带帧到达时刻:在两帧之间按墙钟插值,让「已用 N 秒」每秒自增(心跳每秒原地重算,见
  // app.js 的 syncStackProgressElapsed);完成/失败/无时刻则原样显示后端上报的秒数。
  const ticking =
    Number.isFinite(baseElapsed) && Number.isFinite(receivedAt) && !deploymentComplete && !isFailed && !isDone;
  const liveElapsed = ticking
    ? baseElapsed + Math.max(0, Math.floor((Date.now() - receivedAt) / 1000))
    : baseElapsed;
  if (Number.isFinite(liveElapsed) && liveElapsed > 0) {
    const meta = createText("tool-stack-progress-meta", t("Elapsed {n}s", { n: liveElapsed }));
    if (ticking && meta.dataset) {
      // 心跳据此在两帧间隙每秒原地续算:基准秒数 + 距帧到达的墙钟秒数。
      meta.dataset.stackElapsedBase = String(baseElapsed);
      meta.dataset.stackReceivedAt = String(receivedAt);
    }
    section.append(meta);
  }

  return section;
}

function renderToolCard(tool = {}, options = {}) {
  const card = document.createElement("details");
  const isLatest = Boolean(options.openToolUseId) && text(tool.toolUseId) === options.openToolUseId;
  const isCompleteStep = isCompleteStepTool(tool);
  card.className = [
    "tool-card",
    tool.local ? "tool-card-local" : "",
    isAliyunApiTool(tool) ? "tool-card-aliyun-api" : "",
    isToolInProgress(tool) ? "is-active" : "",
    isToolFailed(tool) ? "is-error" : "",
  ]
    .filter(Boolean)
    .join(" ");
  card.dataset.toolUseId = text(tool.toolUseId);
  // MCP 工具挂 data-tool-kind="mcp",供 CSS 换用专属图标(区别于通用 >_ 终端字形)。
  if (isMcpTool(tool)) {
    card.dataset.toolKind = "mcp";
  }
  // 稳定键（toolUseId）让用户手动展开/收起的态跨帧重建保留（app.js 的展开态存储）。
  card.dataset.openKey = `tool:${text(tool.toolUseId)}`;
  card.open = shouldOpenToolCard({
    isCompleteStep,
    collapseNonComplete: Boolean(options.collapseNonComplete),
    inProgress: isToolInProgress(tool),
    isLatest,
    turnActive: options.turnActive === true,
    // 进行中且已有实时栈进度帧 → 自动展开(部署/删除进度可见)。
    hasActiveStackProgress: Boolean(tool.stackProgress) && isToolInProgress(tool),
  });
  const summary = document.createElement("summary");
  summary.className = "tool-card-row";
  const cardTitle = createText("tool-card-title", toolCommandText(tool));
  if (isToolInProgress(tool)) {
    // 执行中的标题走流光；对齐相位，避免每帧重建把动画重置到不可见起点。
    applyShimmerPhase(cardTitle);
  }
  summary.append(
    createText("tool-card-icon", ""),
    cardTitle,
    createText("tool-card-meta", toolMetaText(tool)),
    createText("tool-card-chevron", "›"),
  );

  card.append(summary);
  // 栈实时进度(若有)紧跟标题、先于常规详情——保证部署/删除时进度总在卡内显眼处。
  const stackProgress = renderStackProgressDetail(tool);
  if (stackProgress) {
    card.append(stackProgress);
  }
  if (isCompleteStepTool(tool)) {
    card.append(renderCompleteStepDetail(tool));
  } else if (isShellTool(tool)) {
    card.append(renderShellDetail(tool));
    const extraDetail = renderGenericDetail(tool, { allowEmpty: false, includeInput: false, includeResults: false });
    if (extraDetail) {
      card.append(extraDetail);
    }
  } else {
    card.append(renderGenericDetail(tool));
  }
  return card;
}

function orderedTools(state = {}) {
  return [...Object.values(state.tools || {}), ...Object.values(state.localShell || {})].filter(
    (tool) => tool && typeof tool === "object",
  );
}

function renderToolGroup(tools, openToolUseId, collapseNonComplete = false, turnActive = false) {
  const group = document.createElement("details");
  const groupActive = tools.some(isToolInProgress);
  const holdsLatest = Boolean(openToolUseId) && tools.some((tool) => text(tool.toolUseId) === openToolUseId);
  // 组内有栈操作正在进行且已挂实时进度帧(部署/删除)——须先于 collapseNonComplete 的收起短路:
  // 否则该 ros_stack 卡自身虽由 shouldOpenToolCard 展开,外层收起的分组 <details> 仍把实时进度藏起来
  // (镜像 shouldOpenToolCard 的同名短路,让「部署时自动展开看进度」在分组场景也成立)。
  const hasActiveStackProgress = tools.some((tool) => Boolean(tool.stackProgress) && isToolInProgress(tool));
  group.className = groupActive ? "tool-group is-active" : "tool-group";
  // 分组以首个工具的 toolUseId 作稳定键（流式追加时首项不变），跨帧重建保留展开态。
  group.dataset.openKey = `grp:${text(tools[0]?.toolUseId)}`;
  // 工具组展开态统一由「组内有工具执行中(groupActive)，或组内含转录尾部最新工具卡(holdsLatest)」
  // 驱动——回合进行中与静息态同一套规则：
  //   · 运行中/刚追加的工具组因 groupActive 或 holdsLatest 为真而展开(用户要看到执行列表);
  //   · 组内所有工具跑完、且助手已产出正文(非工具事件)后, latestToolUseIdForTranscript 返回空,
  //     holdsLatest 转假, 该组随即自动收起(Issue：所有工具完成且下一事件非工具相关时自动收起)。
  // 组内每张卡仍由 shouldOpenToolCard 保持收起(消除逐条事件到达时卡片反复展开/收起的抖动)。
  // 静息的流水线转录(collapseNonComplete)：整组强制收起(避免 reload 后一屏铺开)。
  if (hasActiveStackProgress) {
    group.open = true;
  } else if (collapseNonComplete) {
    group.open = false;
  } else {
    group.open = groupActive || holdsLatest;
  }

  const summary = document.createElement("summary");
  summary.className = "tool-group-summary";
  const groupTitle = createText("tool-group-title", toolGroupSummary(tools));
  if (groupActive) {
    // 组内有工具执行中：标题走流光；对齐相位，避免每帧重建把动画重置到不可见起点。
    applyShimmerPhase(groupTitle);
  }
  summary.append(createText("tool-group-icon", ""), groupTitle, createText("tool-group-chevron", "›"));

  const list = document.createElement("div");
  list.className = "tool-group-list";
  for (const tool of tools) {
    // 组内卡片必须同样收到 turnActive/collapseNonComplete：否则 shouldOpenToolCard 走
    // 默认(turnActive=false)分支,让 openToolUseId 命中的尾部卡片展开——正是「运行中组里
    // 最后一个工具仍展开」的成因。回合进行中/流水线转录里组内卡一律保持收起。
    list.append(renderToolCard(tool, { openToolUseId, collapseNonComplete, turnActive }));
  }

  group.append(summary, list);
  return group;
}

// 回合已结束时，把仍停在执行中的工具收敛为"已取消"，
// 避免停止/刷新后遗留卡片一直显示"正在运行"。
function finalizeOrphanedTool(tool = {}) {
  if (isToolInProgress(tool)) {
    return { ...tool, status: "canceled" };
  }
  return tool;
}

export function renderToolCards(state = {}, options = {}) {
  const container = document.createElement("section");
  container.className = "tool-cards";
  const openToolUseId = options.openToolUseId ? text(options.openToolUseId) : "";
  // 流水线会话：非 complete_step 工具一律默认收起，避免逐条事件到达时的展开/收起闪烁。
  const collapseNonComplete = Boolean(options.collapseNonComplete);
  // 回合进行中：尾部最新的工具卡/组也默认收起（详见 shouldOpenToolCard 注释）。
  const turnActive = options.turnActive === true;

  let tools = orderedTools(state);
  if (options.turnActive === false) {
    tools = tools.map(finalizeOrphanedTool);
  }
  if (options.grouped === true && tools.length > 1) {
    // complete_step 的结论要完整展示，不能折进收起的分组里。把它拎出来单独渲染成
    // 默认展开的卡片（含翻译后的结论），其余工具照常进分组。
    const standalone = tools.filter(isCompleteStepTool);
    const grouped = tools.filter((tool) => !isCompleteStepTool(tool));
    if (grouped.length > 1) {
      container.append(renderToolGroup(grouped, openToolUseId, collapseNonComplete, turnActive));
    } else {
      for (const tool of grouped) {
        container.append(renderToolCard(tool, { openToolUseId, collapseNonComplete, turnActive }));
      }
    }
    for (const tool of standalone) {
      container.append(renderToolCard(tool, { openToolUseId, collapseNonComplete, turnActive }));
    }
    return container;
  }

  for (const tool of tools) {
    container.append(renderToolCard(tool, { openToolUseId, collapseNonComplete, turnActive }));
  }

  return container;
}
