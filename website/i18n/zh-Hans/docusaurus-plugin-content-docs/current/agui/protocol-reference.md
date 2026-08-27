---
sidebar_position: 3
title: 协议参考
description: iac-code AG-UI 请求、事件、Interrupt、恢复、取消和持久化参考。
---

# AG-UI 协议参考

本文描述 `iac-code agui` 暴露的 HTTP/SSE 接口，以及 iac-code 在标准 AG-UI envelope 中使用的扩展字段。架构和启动方式分别参见[协议概览](./overview.md)与[快速开始](./getting-started.md)。

## HTTP 接口

| 方法与路径 | 用途 |
|------------|------|
| `GET /health` | 健康检查和协议版本信息 |
| `POST /` | 提交 AG-UI `RunAgentInput`，响应为 SSE 事件流 |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | 取消 iac-code execution 的命名空间扩展 |

`POST /` 的请求体必须使用 JSON，客户端应声明接收 SSE：

```http
Content-Type: application/json
Accept: text/event-stream
```

如果配置了 `IAC_CODE_AGUI_AUTH_TOKEN`，所有受保护请求还必须携带：

```http
Authorization: Bearer <token>
```

可使用标准 `Accept-Language` 请求头作为错误消息语言的兜底。`forwardedProps.iacCode.preferredLanguage` 的优先级更高，并会继续传给 A2A runtime。

## RunAgentInput

最小 normal 请求示例：

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [
    {
      "id": "message-1",
      "role": "user",
      "content": "创建一个 VPC 模板"
    }
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1",
      "runMode": "normal"
    }
  }
}
```

### 标准字段

| 字段 | 要求 | iac-code 行为 |
|------|------|---------------|
| `threadId` | 必填、非空字符串 | 会话稳定标识；映射到一个 A2A context 和 iac-code session |
| `runId` | 必填、非空字符串 | 单次 HTTP/SSE run 标识；同一 thread 内不能复用 |
| `parentRunId` | 可选 | 原样用于 `RUN_STARTED` |
| `state` | 必填 | 保留在标准 envelope 中；不作为 iac-code runtime 状态源 |
| `messages` | 必填 | 新 run 使用最新一条 user message；Resume 可以不新增 user message |
| `tools` | 必填且必须为空数组 | client 自定义工具当前不受支持 |
| `context` | 必填 | 保留在标准 envelope 中；当前不转换为 iac-code prompt context |
| `forwardedProps` | 必填 | 必须包含 `iacCode` 扩展 |
| `resume` | Resume 时使用 | 对上一个 Interrupt run 的逐项响应 |

用户消息支持：

- 字符串文本；
- `type: "text"` 的文本 part；
- `type: "image"` 且 `source.type: "data"` 的内联 base64 图片。

不支持远程图片 URL、音频、视频、document 或通用 binary part。单张解码后图片不得超过 8 MiB，全部图片合计不得超过 10 MiB；整个 HTTP 请求体上限为 12 MiB。

## `forwardedProps.iacCode`

该对象使用严格 schema，未知字段会被拒绝。

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schemaVersion` | `1` | 是 | iac-code 扩展版本 |
| `rosInvocationId` | string | 是 | 当前 execution 的调用标识，最长 256 字符 |
| `cwd` | string | 是 | 本次执行的绝对工作区路径 |
| `model` | string | 否 | 单次请求覆盖模型 |
| `llmApiKey` | string | 否 | 单次请求覆盖 LLM provider key |
| `thinking.enabled` | boolean | 否 | 是否请求 thinking |
| `thinking.effort` | string | 否 | provider 支持时覆盖 thinking effort |
| `thinking.budget` | positive integer | 否 | provider 支持时覆盖 thinking budget |
| `userId` | string | 否 | telemetry 和调用方绑定标识 |
| `channel` | string | 否 | 调用渠道元数据 |
| `preferredLanguage` | string | 否 | 请求级用户可见语言，例如 `zh-CN` |
| `candidatePresentation` | `standard` 或 `rich` | 否 | Pipeline 候选方案呈现方式 |
| `runMode` | `normal` 或 `pipeline` | 否 | 运行模式；缺省由 A2A runtime 决定 |
| `pipelineName` | string | 否 | Pipeline 名称，例如 `selling` |
| `cleanupOnly` | boolean | 否 | 请求 Pipeline 只执行清理路径 |
| `alibabaCloud.accessKeyId` | string | 否 | 请求级 Alibaba Cloud AccessKey ID |
| `alibabaCloud.accessKeySecret` | string | 否 | 请求级 Alibaba Cloud AccessKey Secret |
| `alibabaCloud.securityToken` | string | 否 | 请求级 STS token |
| `alibabaCloud.regionId` | string | 否 | 请求级默认 region |

`rosInvocationId` 的生命周期：

- initial run 与其 Interrupt Resume 必须使用相同值；
- normal turn 成功结束后，下一 turn 可以使用新值；
- Cancel 请求也必须提供当前 execution 的值。

同一 `threadId` 会绑定到首次请求的 `cwd` 和 `userId`。后续请求不能用同一 thread 切换到另一个工作区或调用方。

## SSE 格式与 heartbeat

每个 AG-UI 事件以 SSE `data:` 行发送：

```text
data: {"type":"RUN_STARTED",...}

data: {"type":"TEXT_MESSAGE_START",...}

data: {"type":"TEXT_MESSAGE_CONTENT",...}
```

当 15 秒内没有事件时，服务器发送 SSE 注释：

```text
: heartbeat
```

它不是 AG-UI `CUSTOM` 事件，不进入客户端事件模型。标准 SSE 实现应忽略注释，同时用它保持 HTTP 连接活跃。

## 标准事件映射

| A2A/iac-code 信号 | AG-UI 输出 |
|-------------------|------------|
| 新请求被接受 | `RUN_STARTED` |
| agent 文本 | `TEXT_MESSAGE_START/CONTENT/END` |
| raw thinking | `REASONING_START`、`REASONING_MESSAGE_*`、`REASONING_END` |
| 工具开始与参数 | `TOOL_CALL_START/ARGS/END` |
| 工具结果 | `TOOL_CALL_RESULT` |
| Pipeline step 生命周期 | `STEP_STARTED/STEP_FINISHED` |
| Pipeline 恢复快照 | `ACTIVITY_SNAPSHOT` |
| 正常完成 | `RUN_FINISHED`，`outcome.type = "success"` |
| 等待用户输入 | `RUN_FINISHED`，`outcome.type = "interrupt"` |
| adapter 或 A2A 错误 | `RUN_ERROR` |

`RUN_FINISHED` 表示一个 AG-UI run 的结束，不等同于整个 Pipeline 的结束。一次 Pipeline 可能因为多个 Interrupt 产生多个 run，每个 run 都有自己的 `RUN_STARTED` 和 `RUN_FINISHED`。Pipeline 业务终态由 `pipeline_completed` 或 `pipeline_error` 等 Pipeline 事件表达。

为满足 AG-UI 的 span 平衡约束，Interrupt 结束当前 run 前会关闭打开的 message、reasoning、tool 和 step span。Resume 的新 run 会重新打开仍处于活动状态的持久 Pipeline step。因此跨 Interrupt 查看原始事件时，可能看到同一业务 step 在不同 run 中分别关闭和重新开始；这不是 Pipeline 倒序执行。

## iac-code 自定义事件

### `iac-code.session.v1`

该事件暴露 adapter 与 A2A 的当前映射：

```json
{
  "type": "CUSTOM",
  "name": "iac-code.session.v1",
  "value": {
    "schemaVersion": 1,
    "threadId": "...",
    "aguiRunId": "...",
    "executionId": "...",
    "contextId": "...",
    "taskId": "...",
    "rosInvocationId": "...",
    "sessionId": "..."
  }
}
```

`executionId` 可用于取消扩展。普通客户端可以安全忽略这个映射事件。

### `iac-code.artifact.v1`

承载 A2A task artifact 的结构化投影。客户端可以按需提供下载、预览或调试展示。

### `iac-code.tool-progress.v1`

承载没有标准 AG-UI 等价物的工具中间进度。工具开始、参数和最终结果仍使用标准 `TOOL_CALL_*`，不会在该事件中重复发送。

### `iac-code.pipeline.v1`

只发送对 UI 有价值且没有完整标准等价物的 Pipeline 信息。当前允许的 `eventType` 包括：

- Pipeline：`pipeline_started`、`pipeline_resumed`、`pipeline_completed`、`pipeline_error`、`pipeline_warning`、`backup_blocked`；
- 候选方案：`candidate_started`、`candidate_completed`、`candidate_failed`、`candidate_interrupted`、`candidate_restart_requested`、`candidate_selected`、`candidate_detail_shown`、`candidate_step_failed`；
- sub-pipeline 与步骤错误：`sub_pipeline_started`、`sub_pipeline_completed`、`sub_step_failed`、`step_failed`；
- 资源栈与清理：`stack_progress`、`stack_instances_progress`、`stack_current_changed`、`cleanup_started`、`cleanup_progress`、`cleanup_completed`、`cleanup_failed`；
- 回滚：`rollback_triggered`、`rollback_completed`；
- 上下文：`context_compaction_started`、`context_compacted`、`context_compaction_failed`、`fields_marked_stale`；
- 展示与工具：`diagram_shown`、`mcp_status`、`tool_progress`。

以下 A2A Pipeline 信号已有标准映射，因此不会再作为 `CUSTOM` 重复发送：

- `text_delta` → `TEXT_MESSAGE_*`；
- `thinking_delta` → `REASONING_*`；
- `tool_started` / `tool_result` → `TOOL_CALL_*`；
- `usage` → `RUN_FINISHED.usage`；
- step lifecycle → `STEP_*`。

客户端应按 `(name, value.eventId)` 或 Pipeline sequence 处理可能的重放，并允许忽略未知的 namespaced 自定义事件。

## Interrupt

等待输入时，一个 run 以 `RUN_FINISHED` 结束：

```json
{
  "type": "RUN_FINISHED",
  "threadId": "thread-1",
  "runId": "run-1",
  "outcome": {
    "type": "interrupt",
    "interrupts": [
      {
        "id": "permission-1",
        "reason": "tool_call",
        "message": "Create a cloud resource. Allow once?",
        "toolCallId": "call-1",
        "responseSchema": {
          "type": "object",
          "properties": {
            "decision": {
              "type": "string",
              "enum": ["allow_once", "deny"]
            }
          },
          "required": ["decision"],
          "additionalProperties": false
        },
        "expiresAt": "2026-08-27T03:00:00Z",
        "metadata": {
          "schemaVersion": 1,
          "kind": "permission",
          "toolName": "ros_stack",
          "title": "Create a cloud resource",
          "purpose": "Deploy the approved resource stack",
          "safeSummary": "ros_stack: create stack"
        }
      }
    ]
  }
}
```

客户端渲染时应优先使用：

- `message`：面向用户的完整问题；
- `responseSchema`：合法响应结构；
- `metadata.title/purpose/safeSummary`：通用说明和安全摘要；
- `metadata.options`：可用选择；
- `toolCallId`：关联之前的标准工具调用事件。

不要只根据 `reason` 推断 UI。权限、自由文本提问和方案选择可能使用不同 schema。

## Resume

Resume 是一个新的 `POST /`，使用同一 `threadId` 和新 `runId`：

```json
{
  "threadId": "thread-1",
  "runId": "run-2",
  "state": {},
  "messages": [],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1"
    }
  },
  "resume": [
    {
      "interruptId": "permission-1",
      "status": "resolved",
      "payload": {"decision": "allow_once"}
    }
  ]
}
```

规则：

- 当前所有 pending Interrupt 必须恰好响应一次；
- 不允许重复或未知的 `interruptId`；
- `resolved` 必须携带符合对应 `responseSchema` 的 `payload`；
- `cancelled` 表示不继续该 Interrupt；权限 Interrupt 会按 `deny` 处理；
- 响应在 A2A 接受后才会从 durable pending 状态中移除；
- schema 校验失败时返回 `RUN_ERROR`，原 Interrupt 保持可重试；
- 重复提交同一已接受响应会返回幂等相关错误，而不会重复执行工具。

在 Resume 前，adapter 会按需请求 A2A 恢复 iac-code session，然后校验 A2A task/context 身份，并恢复遗漏的 Pipeline 增量事件。

## 多 turn 与标识

```text
threadId（稳定会话）
  ├─ runId-1（用户 turn）
  ├─ runId-2（Interrupt Resume）
  ├─ runId-3（下一次 Resume）
  └─ runId-4（会话下一条普通消息）
```

- `threadId` 在整个会话中稳定。
- 每次 HTTP/SSE 请求使用唯一 `runId`。
- Interrupt Resume 仍是一个新 run。
- 一个普通 turn 成功完成后，下一条用户消息创建新的 execution，但继续复用 thread 对应的 iac-code session。
- `runId` 幂等范围是 `(threadId, runId)`；已使用的 run ID 不能再次用于新请求。

## 取消扩展

取消请求：

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{
  "threadId": "thread-1",
  "rosInvocationId": "invocation-1"
}
```

`executionId` 来自 `iac-code.session.v1`。响应状态可能是：

| `status` | 含义 |
|----------|------|
| `cancelled` | 已请求 A2A 取消，并把 execution 标记为终态 |
| `already_terminal` | execution 已结束 |
| HTTP `404` / `EXECUTION_NOT_FOUND` | execution 不存在或身份不匹配 |

取消会清除 pending Interrupt。该扩展不改变标准 AG-UI run 事件格式。

## 持久化与恢复

AG-UI 状态默认保存在：

```text
<config-dir>/agui/threads/<thread-key>.json
```

单个文件保存：

- `threadId`、`contextId`、`cwd` 和 `userId` 绑定；
- iac-code session ID；
- 当前 `executionId`、`rosInvocationId` 和 A2A `taskId`；
- Pipeline sequence、打开的步骤和文本快照摘要；
- pending Interrupt 与有效期；
- run、Resume 和终态幂等信息。

adapter 启动时不扫描全部目录。收到 thread 请求后才懒加载对应文件，每次只原子替换当前 thread 的小文件。

AG-UI 状态不保存 LLM key、AccessKey secret 或 STS token。该目录只属于协议 adapter，不是 iac-code 对话正文或执行产物的存储位置。A2A 的会话和任务持久化由 A2A server 自己管理，详见 [A2A 文档](../a2a/overview.md)。

如果 Interrupt 超过 `expiresAt`，下一次访问会拒绝 Resume、清理 pending，并尽力取消对应 A2A task。

## 断开连接

- 已经以 Interrupt 安全结束的 run 不占用原 SSE 连接。
- Resume 到来时会创建新的 SSE 连接。
- 客户端在普通活动 run 中主动断开时，adapter 会取消对应 A2A task，避免后台无限运行。
- 对于已持久化的 pending Interrupt，断开不会删除其恢复状态。

## 错误响应

在 SSE 建立前发生的错误使用 HTTP JSON：

```json
{
  "error": {
    "code": "INVALID_INPUT",
    "message": "Invalid AG-UI RunAgentInput envelope."
  }
}
```

运行期间错误使用标准 `RUN_ERROR`。常见 code：

| code | 含义 |
|------|------|
| `INVALID_INPUT` | envelope、扩展字段、消息内容或工作区无效 |
| `DUPLICATE_RUN_ID` | 相同请求摘要使用了已存在的 run ID |
| `RUN_ID_CONFLICT` | 不同请求复用了已存在的 run ID |
| `THREAD_BUSY` | 同一 thread 已有活动 run |
| `THREAD_BINDING_CONFLICT` | thread 的 cwd 或 userId 与已有绑定冲突 |
| `RESUME_REQUIRED` | thread 正在等待 Interrupt 响应 |
| `INCOMPLETE_RESUME` | 未覆盖全部 pending Interrupt，或包含重复 ID |
| `UNKNOWN_INTERRUPT` | Resume 引用了未知 Interrupt |
| `RESUME_PAYLOAD_INVALID` | payload 缺失或不符合 response schema |
| `RESUME_ALREADY_APPLIED` | 响应已应用，或重复请求与已应用内容冲突 |
| `EXECUTION_EXPIRED` | Interrupt 已过期 |
| `EXECUTION_LOST` | adapter、A2A task 或 iac-code session 无法恢复 |
| `STATE_PERSISTENCE_FAILED` | 恢复关键状态无法可靠写入 |
| `A2A_UNAVAILABLE` | 本地 A2A execution service 不可用 |
| `A2A_PROTOCOL_ERROR` | A2A task/context/session 身份不符合既有映射 |
| `A2A_EXECUTION_FAILED` | A2A task 以失败状态结束 |
| `CANCELLED` | execution 已取消 |

adapter 对需要可靠恢复的写入采用 fail closed：如果 task、session 或 Interrupt 映射不能持久化，它不会先向客户端宣告可恢复成功，必要时会取消对应 A2A task。
