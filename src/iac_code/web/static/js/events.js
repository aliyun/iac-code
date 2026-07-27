import { t } from "./i18n.js?v=web-repl-ui-277";

function cloneState(state) {
  if (typeof structuredClone === "function") {
    return structuredClone(state || {});
  }
  return JSON.parse(JSON.stringify(state || {}));
}

function ensureMessage(messages, messageId) {
  if (!messages[messageId]) {
    messages[messageId] = {
      messageId,
      role: "assistant",
      text: "",
      content: "",
      status: "streaming",
      toolUseIds: [],
    };
  }
  return messages[messageId];
}

function messageIdFromEvent(prefix, event, payload) {
  if (typeof payload.messageId === "string" && payload.messageId) {
    return payload.messageId;
  }
  if (typeof payload.turnId === "string" && payload.turnId) {
    return `${prefix}-${payload.turnId}`;
  }
  const sequence = typeof event.sequence === "number" && event.sequence > 0 ? event.sequence : Date.now();
  return `${prefix}-${sequence}`;
}

function ensureTool(tools, toolUseId) {
  if (!tools[toolUseId]) {
    tools[toolUseId] = {
      toolUseId,
    };
  }
  const tool = tools[toolUseId];
  if (!tool.status) {
    tool.status = "pending";
  }
  if (typeof tool.input !== "string") {
    tool.input = "";
  }
  if (!Array.isArray(tool.children)) {
    tool.children = [];
  }
  if (!Array.isArray(tool.results)) {
    tool.results = [];
  }
  if (!Array.isArray(tool.artifacts)) {
    tool.artifacts = [];
  }
  return tool;
}

function ensureLocalShell(localShell, toolUseId) {
  if (!localShell[toolUseId]) {
    localShell[toolUseId] = {
      toolUseId,
      toolName: t("Local shell"),
      status: "running",
      local: true,
      entersAgentContext: false,
    };
  }
  return localShell[toolUseId];
}

function localShellIdForStart(event, payload, localShell) {
  if (typeof payload.toolUseId === "string" && payload.toolUseId) {
    return payload.toolUseId;
  }
  if (typeof payload.shellUseId === "string" && payload.shellUseId) {
    return payload.shellUseId;
  }
  const sequence = typeof event.sequence === "number" && event.sequence > 0 ? event.sequence : null;
  return `local-shell-${sequence || Object.keys(localShell).length + 1}`;
}

function localShellIdForEnd(event, payload, localShell) {
  if (typeof payload.toolUseId === "string" && payload.toolUseId) {
    return payload.toolUseId;
  }
  if (typeof payload.shellUseId === "string" && payload.shellUseId) {
    return payload.shellUseId;
  }
  const entries = Object.entries(localShell).reverse();
  const command = typeof payload.command === "string" ? payload.command : "";
  const match = entries.find(([, shell]) => shell.command === command && shell.status === "running");
  if (match) {
    return match[0];
  }
  return localShellIdForStart(event, payload, localShell);
}

function localShellTerminalState(payload) {
  const exitCode = Number.isInteger(payload.exitCode) ? payload.exitCode : 1;
  const stderr = typeof payload.stderr === "string" ? payload.stderr : "";
  if (exitCode === 0) {
    return { status: "completed", reason: null };
  }
  if (exitCode === 130 || stderr.toLowerCase().includes("canceled")) {
    return { status: "canceled", reason: "canceled" };
  }
  if (stderr.toLowerCase().includes("permission denied")) {
    return { status: "denied", reason: "permission_denied" };
  }
  return { status: "failed", reason: null };
}

function removeChildReferences(tools, toolUseId) {
  for (const tool of Object.values(tools)) {
    if (Array.isArray(tool.children)) {
      tool.children = tool.children.filter((childId) => childId !== toolUseId);
    }
  }
}

function messageForTool(messages, payload) {
  if (typeof payload.messageId === "string" && payload.messageId && messages[payload.messageId]) {
    return messages[payload.messageId];
  }
  if (typeof payload.turnId !== "string" || !payload.turnId) {
    return null;
  }
  return Object.values(messages)
    .filter((message) => message.role === "assistant" && message.turnId === payload.turnId)
    .sort((left, right) => {
      const leftOrder = Number.isFinite(left.sequence) ? left.sequence : 0;
      const rightOrder = Number.isFinite(right.sequence) ? right.sequence : 0;
      return leftOrder - rightOrder;
    })
    .at(-1) || null;
}

function attachToolToMessage(messages, tool, payload) {
  const message = messageForTool(messages, payload);
  if (!message || !tool?.toolUseId) {
    return;
  }
  if (!Array.isArray(message.toolUseIds)) {
    message.toolUseIds = [];
  }
  if (!message.toolUseIds.includes(tool.toolUseId)) {
    message.toolUseIds.push(tool.toolUseId);
  }
  tool.messageId = message.messageId;
  if (message.turnId || payload.turnId) {
    tool.turnId = message.turnId || payload.turnId;
  }
}

export function reduceEvent(state = {}, event = {}) {
  const next = cloneState(state);
  if (!next.messages || typeof next.messages !== "object") {
    next.messages = {};
  }
  if (!next.tools || typeof next.tools !== "object") {
    next.tools = {};
  }
  if (!next.permissions || typeof next.permissions !== "object") {
    next.permissions = {};
  }
  if (!next.localShell || typeof next.localShell !== "object") {
    next.localShell = {};
  }
  if (!next.questions || typeof next.questions !== "object") {
    next.questions = {};
  }
  if (!next.elicitations || typeof next.elicitations !== "object") {
    next.elicitations = {};
  }
  if (!next.resolvedPermissions || typeof next.resolvedPermissions !== "object") {
    next.resolvedPermissions = {};
  }
  if (!next.resolvedQuestions || typeof next.resolvedQuestions !== "object") {
    next.resolvedQuestions = {};
  }
  if (!next.resolvedElicitations || typeof next.resolvedElicitations !== "object") {
    next.resolvedElicitations = {};
  }
  if (!next.turns || typeof next.turns !== "object") {
    next.turns = {};
  }
  if (!Array.isArray(next.queuedInputs)) {
    next.queuedInputs = [];
  }
  if (!Array.isArray(next.commands)) {
    next.commands = [];
  }
  if (!Array.isArray(next.subagentEvents)) {
    next.subagentEvents = [];
  }
  if (!Array.isArray(next.taskNotifications)) {
    next.taskNotifications = [];
  }
  if (!Array.isArray(next.resources)) {
    next.resources = [];
  }
  if (!Array.isArray(next.pipelineEvents)) {
    next.pipelineEvents = [];
  }
  if (!Array.isArray(next.candidateDetails)) {
    next.candidateDetails = [];
  }
  if (!Array.isArray(next.diagrams)) {
    next.diagrams = [];
  }
  if (!next.diagramOptimizing || typeof next.diagramOptimizing !== "object") {
    next.diagramOptimizing = {};
  }
  if (!next.diagramOptimized || typeof next.diagramOptimized !== "object") {
    next.diagramOptimized = {};
  }
  if (!Array.isArray(next.debugEvents)) {
    next.debugEvents = [];
  }
  if (!next.activeContextWindows || typeof next.activeContextWindows !== "object") {
    next.activeContextWindows = {};
  }

  const payload = event.payload || {};
  const currentSequence = typeof next.lastSequence === "number" ? next.lastSequence : 0;
  if (typeof event.sequence === "number" && Number.isFinite(event.sequence) && event.sequence > 0) {
    if (event.sequence <= currentSequence) {
      return next;
    }
    next.lastSequence = event.sequence;
  }

  // 会话快照已把“排队中”列表恢复到 latestSequence;而 SSE 从缓冲 floor 回放时会再次投递这些
  // 队列变更事件(accepted 用 push,非幂等),若在种子之上重复应用就会出现重复行/错位。凡序号不
  // 晚于快照高水位(queuedInputsSeedSequence)的队列事件都已反映在种子里,直接跳过。
  if (
    typeof event.type === "string" &&
    event.type.startsWith("queued-input.") &&
    typeof event.sequence === "number" &&
    Number.isFinite(event.sequence) &&
    event.sequence <= (typeof next.queuedInputsSeedSequence === "number" ? next.queuedInputsSeedSequence : 0)
  ) {
    return next;
  }

  switch (event.type) {
    case "session.started":
    case "session.updated": {
      next.currentSession = {
        ...(next.currentSession || {}),
        ...payload,
      };
      if (event.type === "session.updated" && payload.cleared) {
        next.messages = {};
        next.tools = {};
        next.localShell = {};
      }
      break;
    }
    case "session.resync.required": {
      next.resyncRequired = {
        afterSequence: payload.afterSequence,
        floorSequence: payload.floorSequence,
      };
      break;
    }
    case "user.message": {
      const messageId = messageIdFromEvent("user", event, payload);
      const message = ensureMessage(next.messages, messageId);
      message.role = "user";
      message.turnId = payload.turnId;
      message.text = typeof payload.text === "string" ? payload.text : "";
      message.content = message.text;
      message.imageIds = Array.isArray(payload.imageIds) ? payload.imageIds : [];
      message.fileRefs = Array.isArray(payload.fileRefs) ? payload.fileRefs : [];
      message.status = "completed";
      // 同进程 reload：A2A 回放的用户气泡已带正确转录序号（种子里的 seq）。事件缓冲区随后又会
      // 从 floor 回放本轮 live 的 user.message（其 web 序号更大），若在此覆盖就会把用户气泡挪到
      // 流水线步骤之后、错位嵌进步骤体内。序号一旦确定即为该气泡的转录锚点，不再被后续事件改写
      // （与 pipeline.step.marker 的处理一致）。
      if (!message.sequence) {
        message.sequence = event.sequence || 0;
      }
      next.currentTurnActive = true;
      if (payload.turnId) {
        // 新一轮开始:清掉归属于其它轮次的历史错误横幅。lastError 是单例,app.js 把它当作栈底
        // 唯一错误横幅渲染;若不清,上一轮(如失效 provider)的报错会一直悬在栈底、随每条新用户
        // 气泡「往下移」,让用户误以为新一轮「没反应」。本轮若再次失败,error 事件会在其后重新
        // 写入 lastError;同一 turnId 的错误(本轮自身)保留不动。
        if (next.lastError && next.lastError.payload?.turnId !== payload.turnId) {
          next.lastError = null;
        }
        // 新一轮开始也清掉「压缩已结束」的一次性提示（内容过短/失败等），避免它一直悬在栈底。
        if (next.compaction?.status === "completed") {
          next.compaction = null;
        }
        const turnEntry = next.turns[payload.turnId] || {};
        turnEntry.startedAt = turnEntry.startedAt || event.createdAt || null;
        turnEntry.done = false;
        next.turns[payload.turnId] = turnEntry;
      }
      break;
    }
    case "pipeline.step.marker": {
      // Pipeline step / candidate / sub-step boundary. Rendered as a nesting marker
      // bubble (kind + pipelineStep) exactly like reload's stored pipeline rows.
      // Completion re-emits the same markerId to flip status in place; keep the
      // original sequence so the marker holds its transcript position.
      const message = ensureMessage(next.messages, payload.markerId);
      message.role = "assistant";
      if (payload.kind) {
        message.kind = payload.kind;
      }
      if (payload.pipelineStep) {
        message.pipelineStep = payload.pipelineStep;
      }
      message.text = payload.content || "";
      message.content = message.text;
      message.status = "completed";
      if (!message.sequence) {
        message.sequence = event.sequence || 0;
      }
      // 压缩边界（context_compaction_boundary）复用候选的 live groupId、且 status="completed"，
      // 但候选并未结束——压缩只是它中途的一步。若按终态删窗，圆环会在压缩期间凭空消失
      // （问题 #1：多候选时圈数 2→1；问题 #4：单候选压缩时悬浮显示「普通会话」）。
      // 仅真正的步骤/候选终态 marker 才删窗；压缩边界保留窗口（携最后一次已知用量）。
      const terminalContextStatuses = new Set(["completed", "failed", "canceled", "early_exit"]);
      const stepGroupId = payload.pipelineStep?.groupId;
      if (
        stepGroupId &&
        payload.kind !== "context_compaction_boundary" &&
        terminalContextStatuses.has(payload.pipelineStep?.status)
      ) {
        delete next.activeContextWindows[stepGroupId];
      }
      next.currentTurnActive = true;
      break;
    }
    case "pipeline.step.context": {
      if (payload.groupId) {
        next.activeContextWindows[payload.groupId] = {
          groupId: payload.groupId,
          level: payload.level || "",
          title: payload.title || "",
          candidateName: payload.candidateName || "",
          contextUsage: payload.contextUsage && typeof payload.contextUsage === "object" ? payload.contextUsage : {},
        };
      }
      break;
    }
    case "assistant.message.start": {
      const message = ensureMessage(next.messages, payload.messageId);
      // 同进程 reload：磁盘快照已把整段正文塞进该消息（stored=true），随后事件缓冲区又会从
      // floor 回放本轮的 assistant.text.delta。delta 是“追加”语义，不清空快照会导致正文翻倍。
      // 收到 start 即意味着后续 delta 会完整重建这条消息（start 的 sequence 必早于其 delta，
      // 只要 start 被回放，其 delta 也一定在缓冲区内），故先把快照正文清空、去掉 stored 标记。
      if (message.stored) {
        message.text = "";
        message.content = "";
        message.thinking = "";
        message.stored = false;
      }
      message.turnId = payload.turnId;
      message.status = "streaming";
      message.provider = payload.provider || message.provider;
      message.model = payload.model || message.model;
      // 保留已有序号（reload：磁盘快照按转录顺序给 index+1；pipeline marker 内容段按其
      // 稳定 id 落位）。若在此用 live 的大序号无条件覆盖，回放会把该消息踢到序号排序的末尾，
      // 于是流水线 step 的正文/工具从其 marker 分组里脱落、整段错乱漂到底部（Issue 7a/e）。
      if (!message.sequence) {
        message.sequence = event.sequence || 0;
      }
      next.currentTurnActive = true;
      break;
    }
    case "assistant.text.delta": {
      const message = ensureMessage(next.messages, payload.messageId);
      message.text = `${message.text || message.content || ""}${payload.delta || ""}`;
      message.content = message.text;
      break;
    }
    case "assistant.thinking.delta": {
      const messageId = messageIdFromEvent("assistant", event, payload);
      const message = ensureMessage(next.messages, messageId);
      message.thinking = `${message.thinking || ""}${payload.delta || ""}`;
      break;
    }
    case "assistant.message.tombstone": {
      delete next.messages[payload.messageId];
      for (const toolUseId of payload.affectedToolUseIds || []) {
        delete next.tools[toolUseId];
        removeChildReferences(next.tools, toolUseId);
      }
      break;
    }
    case "assistant.message.end": {
      const messageId = messageIdFromEvent("assistant", event, payload);
      const message = ensureMessage(next.messages, messageId);
      message.status = "completed";
      message.finishReason = payload.finishReason || null;
      message.usage = payload.usage || null;
      break;
    }
    case "tool.started": {
      const tool = ensureTool(next.tools, payload.toolUseId);
      // 同上：reload 快照已带完整 input/results/artifacts，回放的 tool.input.delta / tool.result
      // 会再叠加一遍——input 变成两段 JSON 拼接（解析失败，complete_step 结论退回原始 JSON）、
      // 结果重复。start 表示后续事件会完整重建该工具，先清空快照累积字段、去掉 stored 标记。
      if (tool.stored) {
        tool.input = "";
        tool.results = [];
        tool.artifacts = [];
        tool.stored = false;
      }
      tool.toolName = payload.toolName;
      if (payload.turnId) {
        tool.turnId = payload.turnId;
      }
      tool.parentToolUseId = payload.parentToolUseId || null;
      tool.status = payload.status || "running";
      attachToolToMessage(next.messages, tool, payload);
      if (payload.parentToolUseId) {
        const parentTool = ensureTool(next.tools, payload.parentToolUseId);
        if (!parentTool.children.includes(payload.toolUseId)) {
          parentTool.children.push(payload.toolUseId);
        }
      }
      break;
    }
    case "tool.input.delta": {
      const tool = ensureTool(next.tools, payload.toolUseId);
      tool.input = `${tool.input || ""}${payload.delta || ""}`;
      attachToolToMessage(next.messages, tool, payload);
      break;
    }
    case "tool.progress": {
      const tool = ensureTool(next.tools, payload.toolUseId);
      tool.status = "running";
      if (payload.publicName) {
        tool.publicName = payload.publicName;
      }
      if (!tool.toolName && payload.publicName) {
        tool.toolName = payload.publicName;
      }
      if (Number.isFinite(payload.progress)) {
        tool.progress = payload.progress;
      }
      if (Number.isFinite(payload.total)) {
        tool.total = payload.total;
      }
      if (typeof payload.message === "string" && payload.message) {
        tool.progressMessage = payload.message;
        tool.summary = payload.message;
      }
      attachToolToMessage(next.messages, tool, payload);
      break;
    }
    case "tool.result": {
      const tool = ensureTool(next.tools, payload.toolUseId);
      tool.resultKind = payload.resultKind;
      tool.status = payload.resultKind === "error" ? "failed" : "completed";
      tool.summary = payload.summary;
      tool.results.push(payload);
      tool.artifacts = payload.artifacts || [];
      attachToolToMessage(next.messages, tool, payload);
      break;
    }
    case "tool.finished": {
      const tool = ensureTool(next.tools, payload.toolUseId);
      tool.status = payload.status;
      tool.elapsedMs = payload.elapsedMs;
      tool.summary = payload.summary;
      attachToolToMessage(next.messages, tool, payload);
      break;
    }
    case "local.shell.start": {
      const toolUseId = localShellIdForStart(event, payload, next.localShell);
      const shell = ensureLocalShell(next.localShell, toolUseId);
      shell.command = typeof payload.command === "string" ? payload.command : "";
      shell.status = "running";
      if (payload.turnId) {
        shell.turnId = payload.turnId;
      }
      delete shell.reason;
      shell.local = true;
      shell.entersAgentContext = false;
      attachToolToMessage(next.messages, shell, { ...payload, toolUseId });
      break;
    }
    case "local.shell.end": {
      const toolUseId = localShellIdForEnd(event, payload, next.localShell);
      const shell = ensureLocalShell(next.localShell, toolUseId);
      const exitCode = Number.isInteger(payload.exitCode) ? payload.exitCode : 1;
      const terminalState = localShellTerminalState(payload);
      shell.command = typeof payload.command === "string" ? payload.command : shell.command || "";
      shell.status = terminalState.status;
      if (terminalState.reason) {
        shell.reason = terminalState.reason;
      } else {
        delete shell.reason;
      }
      shell.exitCode = exitCode;
      shell.stdout = typeof payload.stdout === "string" ? payload.stdout : "";
      shell.stderr = typeof payload.stderr === "string" ? payload.stderr : "";
      shell.local = true;
      shell.entersAgentContext = false;
      attachToolToMessage(next.messages, shell, { ...payload, toolUseId });
      break;
    }
    case "permission.request": {
      if (payload.requestId) {
        next.permissions[payload.requestId] = {
          requestId: payload.requestId,
          payload: payload.payload || {},
        };
      }
      break;
    }
    case "permission.resolved": {
      if (payload.requestId) {
        delete next.permissions[payload.requestId];
        next.resolvedPermissions[payload.requestId] = {
          requestId: payload.requestId,
          answer: payload.answer,
        };
      }
      break;
    }
    case "question.request": {
      if (payload.requestId) {
        next.questions[payload.requestId] = {
          requestId: payload.requestId,
          payload: payload.payload || {},
        };
      }
      break;
    }
    case "question.resolved": {
      if (payload.requestId) {
        delete next.questions[payload.requestId];
        next.resolvedQuestions[payload.requestId] = {
          requestId: payload.requestId,
          answer: payload.answer || {},
        };
      }
      break;
    }
    case "elicitation.request": {
      if (payload.requestId) {
        next.elicitations[payload.requestId] = {
          requestId: payload.requestId,
          payload: payload.payload || {},
        };
      }
      break;
    }
    case "elicitation.resolved": {
      if (payload.requestId) {
        delete next.elicitations[payload.requestId];
        next.resolvedElicitations[payload.requestId] = {
          requestId: payload.requestId,
          answer: payload.answer || {},
        };
      }
      break;
    }
    case "draft.updated": {
      next.draft = typeof payload.draft === "string" ? payload.draft : "";
      next.draftReason = payload.reason || null;
      break;
    }
    case "queued-input.accepted": {
      const queuedInput = {
        text: typeof payload.text === "string" ? payload.text : "",
        draft: typeof payload.draft === "string" ? payload.draft : "",
      };
      const insertionIndex = Number(payload.index);
      if (payload.restored === true && Number.isInteger(insertionIndex) && insertionIndex >= 0) {
        next.queuedInputs.splice(Math.min(insertionIndex, next.queuedInputs.length), 0, queuedInput);
      } else {
        next.queuedInputs.push(queuedInput);
      }
      break;
    }
    case "queued-input.submitted": {
      // 队列消息被 agent 消费、变成消息流里正式的一轮后，把对应的“排队中”条目移除：
      // 它已经出现在对话里，输入框下方再留一个 chip 只会让人误以为还在排队。提交时 agent 会对
      // 文本做 strip，而入队时保留了原始文本，所以按 trim 后的文本匹配第一条待提交项。
      const submittedText = (typeof payload.text === "string" ? payload.text : "").trim();
      const pendingIndex = next.queuedInputs.findIndex(
        (item) => !item.submitted && String(item.text || item.draft || "").trim() === submittedText,
      );
      if (pendingIndex !== -1) {
        next.queuedInputs.splice(pendingIndex, 1);
      }
      break;
    }
    case "queued-input.removed": {
      // 逐条删除/引导(插队)后端发来的移除事件：按下标从“排队中”列表剔除。
      const index = Number(payload.index);
      if (Number.isInteger(index) && index >= 0 && index < next.queuedInputs.length) {
        next.queuedInputs.splice(index, 1);
      }
      break;
    }
    case "queued-input.updated": {
      // 逐条编辑后端发来的更新事件：按下标替换该排队项文本。
      const index = Number(payload.index);
      if (Number.isInteger(index) && index >= 0 && index < next.queuedInputs.length) {
        next.queuedInputs[index] = {
          ...next.queuedInputs[index],
          text: typeof payload.text === "string" ? payload.text : "",
        };
      }
      break;
    }
    case "interrupt.accepted": {
      next.permissions = {};
      next.questions = {};
      next.lastInterrupt = {
        message: typeof payload.message === "string" ? payload.message : "",
        mode: payload.mode || null,
      };
      break;
    }
    case "command.started": {
      next.commands.push({
        command: typeof payload.command === "string" ? payload.command : "",
        status: "running",
      });
      break;
    }
    case "command.finished": {
      next.commands.push({
        command: typeof payload.command === "string" ? payload.command : "",
        status: payload.result?.accepted ? "completed" : "failed",
        result: payload.result || {},
      });
      break;
    }
    case "stream.disconnected": {
      next.streamConnectionError = {
        message: typeof payload.message === "string" ? payload.message : "Event stream disconnected",
      };
      break;
    }
    case "stream.connected": {
      next.streamConnectionError = null;
      break;
    }
    case "error": {
      next.lastError = {
        message: typeof payload.message === "string" ? payload.message : t("Unknown error"),
        payload,
      };
      break;
    }
    case "subagent.event": {
      next.subagentEvents.push(payload);
      break;
    }
    case "task.notification": {
      next.taskNotifications.push(payload);
      break;
    }
    case "resource.observed": {
      next.resources.push(payload);
      break;
    }
    case "plan.updated": {
      next.plan = payload;
      break;
    }
    case "pipeline.event": {
      next.pipelineEvents.push(payload);
      // 栈生命周期进度(ros_deploy / ros_stack / ros_stack_instances)带 toolUseId 时,
      // 直接挂到发起该操作的工具卡上,让 normal 模式也能像 REPL 那样在卡内实时刷新进度;
      // pipeline 模式仍走上面的 pipelineEvents 面板,故此处只是「额外」挂卡,零回归。
      // 无 toolUseId 的旧/外来事件保持原样(只进 pipelineEvents)。
      if (
        payload.toolUseId &&
        (payload.kind === "stack.progress" || payload.kind === "stack.instances.progress")
      ) {
        const tool = ensureTool(next.tools, payload.toolUseId);
        // 覆盖式保存最新一帧(REPL 的进度也是单块刷新,不累积历史帧)。
        tool.stackProgress = {
          kind: payload.kind,
          stackName: payload.stackName,
          stackGroupName: payload.stackGroupName,
          stackId: payload.stackId,
          operationId: payload.operationId,
          regionId: payload.regionId,
          status: payload.status,
          progressPercentage: payload.progressPercentage ?? payload.progress,
          resources: payload.resources,
          instances: payload.instances,
          elapsedSeconds: payload.elapsedSeconds,
          deploymentComplete: payload.deploymentComplete === true,
          // 本帧抵达客户端的墙钟时刻(必须是客户端 Date.now(),供 tool_cards 在两帧间隙插值「已用 N 秒」;
          // 服务端 epoch 会引入时钟偏差,故仅在此实时归并处打点,重放路径不写)。
          receivedAtMs: Date.now(),
        };
        if (payload.deploymentComplete !== true) {
          tool.status = "running";
        }
        attachToolToMessage(next.messages, tool, payload);
      }
      break;
    }
    case "pipeline.snapshot": {
      next.pipelineSnapshot = payload.snapshot ?? payload;
      break;
    }
    case "candidate.detail": {
      next.candidateDetails.push(payload);
      break;
    }
    case "diagram.render": {
      next.diagrams.push(payload);
      break;
    }
    case "diagram.optimizing": {
      const idx = String(payload.candidateIndex);
      next.diagramOptimizing = { ...next.diagramOptimizing, [idx]: true };
      break;
    }
    case "diagram.optimized": {
      const idx = String(payload.candidateIndex);
      const optimizing = { ...next.diagramOptimizing };
      delete optimizing[idx];
      next.diagramOptimizing = optimizing;
      if (payload.status === "done") {
        let views = Array.isArray(payload.views) ? payload.views : null;
        if (!views && typeof payload.mermaidSource === "string") {
          views = [{ id: "overview", title: "", mermaidSource: payload.mermaidSource }];
        }
        if (views && views.length) {
          next.diagramOptimized = { ...next.diagramOptimized, [idx]: views };
        }
      }
      break;
    }
    case "cleanup.status": {
      next.cleanupStatus = payload;
      break;
    }
    case "mcp.status.updated": {
      next.mcpStatus = payload;
      break;
    }
    case "compaction.started": {
      next.compaction = { status: "running", ...payload };
      break;
    }
    case "compaction.finished": {
      next.compaction = { status: "completed", ...payload };
      break;
    }
    case "debug.stream_event": {
      next.debugEvents.push(payload);
      break;
    }
    case "turn.done": {
      next.currentTurnActive = false;
      // Cancellation can tear down the async generator while automatic compaction is
      // awaiting its provider call, so no compaction.finished event can be yielded.
      // turn.done is the authoritative terminal fallback for the composer state.
      if (next.compaction?.status === "running") {
        next.compaction = { ...next.compaction, status: "completed", state: "canceled" };
      }
      const turnId = payload.turnId || null;
      let elapsedMs = typeof payload.elapsedMs === "number" ? payload.elapsedMs : null;
      if (turnId) {
        const turnEntry = next.turns[turnId] || {};
        turnEntry.done = true;
        turnEntry.endedAt = event.createdAt || turnEntry.endedAt || null;
        if (elapsedMs === null && turnEntry.startedAt && turnEntry.endedAt) {
          const start = Date.parse(turnEntry.startedAt);
          const end = Date.parse(turnEntry.endedAt);
          if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
            elapsedMs = end - start;
          }
        }
        turnEntry.elapsedMs = elapsedMs;
        next.turns[turnId] = turnEntry;
      }
      next.lastTurn = {
        turnId,
        interrupted: Boolean(payload.interrupted),
        canceled: Boolean(payload.canceled),
        elapsedMs,
        usage: payload.usage || null,
      };
      break;
    }
    default:
      break;
  }

  return next;
}
