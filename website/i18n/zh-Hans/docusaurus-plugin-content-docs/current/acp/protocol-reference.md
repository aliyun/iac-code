---
title: 协议参考
description: iac-code 集成的完整 ACP 协议方法与事件参考。
sidebar_position: 3
---

# 协议参考

本文档提供了 iac-code 服务器暴露的 ACP（Agent Client Protocol）方法和流式事件的完整参考。

## 生命周期概览

一个典型的 ACP 会话遵循以下流程：

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** — 握手。协商协议版本并发现服务器能力。
2. **session/new** — 创建一个拥有独立代理运行时的新会话。
3. **session/prompt** — 发送用户输入；接收流式事件直到最终响应。
4. **session/close** — 释放会话及其资源。

会话也可以从历史记录加载（`session/load`）或恢复（`session/resume`），而不必创建新会话。

---

## 方法

### initialize

协议握手。必须是每个连接上的第一个调用。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `protocolVersion` | integer | 是 | 请求的协议版本（当前为 `1`） |
| `clientInfo` | object | 否 | 客户端名称和版本 |
| `clientCapabilities` | object | 否 | 客户端支持的能力 |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `protocolVersion` | integer | 协商后的协议版本 |
| `agentCapabilities` | object | 服务器能力（见下方） |
| `agentInfo` | object | 服务器名称和版本 |
| `authMethods` | array | 可用的认证方法（使用内置凭证时为空） |

**Agent 能力**

| 能力 | 值 | 含义 |
|-----------|-------|---------|
| `loadSession` | `true` | 支持从历史记录恢复会话 |
| `promptCapabilities.embeddedContext` | `true` | 接受在提示中嵌入资源内容 |
| `promptCapabilities.image` | `false` | 不支持图片输入（降级为文本标记） |
| `promptCapabilities.audio` | `false` | 不支持音频输入（降级为文本标记） |
| `sessionCapabilities.list` | `{}` | 支持列出会话 |
| `sessionCapabilities.close` | `{}` | 支持关闭会话 |

---

### session/new

创建一个拥有独立代理运行时、工具注册表和 LLM 上下文的新会话。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `cwd` | string | 是 | 工作目录的绝对路径 |
| `mcpServers` | object | 否 | MCP 服务器配置（已接受但尚未生效） |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `sessionId` | string | 用于后续调用的唯一会话标识符 |
| `modes` | object | 可用模式和当前模式 |
| `models` | object | 可用模型和当前模型 |

:::note
每个会话创建一个独立的 AgentLoop。多个会话可以并发运行，但每个会话都会消耗一个 LLM 连接。
:::

---

### session/load

从磁盘加载先前持久化的会话，恢复其消息历史。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `cwd` | string | 是 | 工作目录路径 |
| `sessionId` | string | 是 | 要加载的会话 ID |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `models` | object | 可用模型和当前模型状态 |
| `modes` | object | 可用模式和当前模式状态 |

:::note
加载会话时会从 `~/.iac-code/sessions/` 读取历史记录，自动修复中断的消息，并将历史注入到新的 AgentLoop 中。
:::

---

### session/fork

Fork 一个现有会话，创建一个具有相同历史记录的独立分支。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `cwd` | string | 是 | 工作目录路径 |
| `sessionId` | string | 是 | 要 fork 的会话 ID |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `sessionId` | string | Fork 后新分支的会话 ID |
| `models` | object | 可用模型和当前模型状态 |
| `modes` | object | 可用模式和当前模式状态 |

---

### session/resume

恢复或重新连接到现有会话。如有需要会自动加载历史记录。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `cwd` | string | 是 | 工作目录路径 |
| `sessionId` | string | 是 | 要恢复的会话 ID |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `models` | object | 可用模型和当前模型状态（可选） |
| `modes` | object | 可用模式和当前模式状态（可选） |

:::note
与 `session/new` 不同，响应中不包含 `sessionId` 字段，因为客户端已经从请求中得知了会话 ID。
:::

---

### session/prompt

发送用户输入并触发流式代理响应。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `sessionId` | string | 是 | 目标会话 ID |
| `prompt` | array | 是 | 内容块数组（见下方内容块类型） |

**内容块类型**

| 类型 | 描述 |
|------|-------------|
| `TextContentBlock` | 纯文本用户输入 |
| `EmbeddedResourceContentBlock` | 内联嵌入的文件内容 |
| `ResourceContentBlock` | 资源链接引用 |
| `ImageContentBlock` | 图片（降级为 `[image: mime/type]` 文本标记） |
| `AudioContentBlock` | 音频（降级为 `[audio: mime/type]` 文本标记） |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `stopReason` | string | 提示完成的原因（见停止原因） |
| `usage` | object | Token 用量：`inputTokens`、`outputTokens`、`totalTokens` |

**停止原因**

| 值 | 含义 |
|-------|---------|
| `end_turn` | 模型正常完成 |
| `max_turn_requests` | 达到最大工具调用循环次数限制 |
| `max_tokens` | 达到输出 token 限制 |
| `cancelled` | 客户端取消了提示 |
| `refusal` | 模型拒绝回答 |

:::note
执行期间，服务器在返回最终响应之前会推送包含流式事件的 `session/update` 通知。
:::

---

### session/cancel

取消正在运行的提示任务。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `sessionId` | string | 是 | 正在运行提示的会话 |

**行为**

- 停止消费流式事件
- 正在运行的工具不会被强制终止，但其结果会被丢弃
- 挂起的 `prompt` 调用将以 `stopReason: "cancelled"` 返回

---

### session/close

关闭会话并释放其资源。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `sessionId` | string | 是 | 要关闭的会话 |

**行为**

- 从内存中移除会话
- 持久化的历史记录保留在磁盘上
- 后续对此会话的 `prompt` 调用将返回错误

---

### sessions/list

列出给定工作目录下所有已持久化的会话。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `cwd` | string | 是 | 用于限定列表范围的工作目录 |

**响应字段**

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `sessions` | array | 包含 `sessionId` 和元数据的会话对象列表 |

---

### config/set

动态设置会话的配置选项。

**请求参数**

| 字段 | 类型 | 必填 | 描述 |
|-------|------|----------|-------------|
| `sessionId` | string | 是 | 目标会话 |
| `configId` | string | 是 | 要设置的配置键 |
| `value` | any | 是 | 新值 |

---

## 流式事件

在 `session/prompt` 执行期间，服务器推送包含流式事件数据的 `session/update` 通知。

### 事件格式

每个 `session/update` 通知携带一个具有特定类型的更新对象：

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### 事件类型映射

| 内部事件 | ACP 更新类型 | 描述 |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | 增量代理文本输出 |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | 模型推理/思考内容 |
| `ToolUseStartEvent` | `ToolCallStart` | 工具调用开始 |
| `ToolResultEvent` | `ToolCallProgress` | 工具结果（完成或失败） |
| `CompactionEvent` | `AgentMessageChunk` | 上下文压缩通知 |
| `ErrorEvent` | `AgentMessageChunk` | 错误信息 |

### 工具调用生命周期

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, 输入摘要 / 安全提示输入)
    │
    ├── ToolCallProgress (status=completed, raw_output=结果)   ← 成功
    │
    └── ToolCallProgress (status=failed, raw_output=错误)       ← 失败
```

**工具类别映射**

| 工具 | ACP ToolKind |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| 其他 | `other` |

---

## 权限请求

在执行高风险工具之前，iac-code 会向客户端发送 `request_permission` 回调。

### 工具权限分类

| 分类 | 工具 | 自动允许 |
|----------|-------|-------------|
| 只读 | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | 是 |
| 只读云 API | 被判定为只读的 `aliyun_api` 动作 | 是 |
| 写入 | `write_file`, `edit_file` | 否 — 需要审批 |
| 执行 | `bash`, `agent` | 否 — 需要审批 |
| 云写 API | 非只读 `aliyun_api` 调用 | 否 — 需要按 API 批准 |

### request_permission 事件

服务器发送包含以下内容的 `request_permission` 回调：

| 字段 | 类型 | 描述 |
|-------|------|-------------|
| `options` | array | 可用的权限选项 |
| `sessionId` | string | 请求权限的会话 |
| `toolCall` | object | 工具调用详情（标题、类别、输入） |

### 权限选项

| 选项 ID | 含义 |
|-----------|---------|
| `allow_once` | 允许本次调用 |
| `allow_always` | 当工具支持整工具永久允许时，允许本会话中该工具的所有后续调用；`bash` 和 `aliyun_api` 默认不提供 |
| `allow_rule:<rules>` | 允许本会话中匹配建议规则的后续调用 |
| `deny_rule:<rules>` | 拒绝本会话中匹配建议规则的后续调用 |
| `reject_once` | 拒绝本次调用 |
| `reject_always` | 拒绝本会话中该工具的所有后续调用 |

对于 `aliyun_api`，只读动作会自动允许。非只读 RPC 和 ROA 动作都可以提供形如 `aliyun_api(ros:CreateStack)` 或 `aliyun_api(cs:CreateCluster)` 的精确规则。通配允许规则仍不会批准非只读调用。

### 响应格式

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

或拒绝：

```json
{
  "outcome": "denied"
}
```

| 客户端响应 | 工具行为 |
|----------------|---------------|
| `AllowedOutcome` | 工具正常执行 |
| `DeniedOutcome` | 工具被跳过；模型收到 "Permission denied." 错误 |

---

## 错误处理

### RequestError 格式

错误遵循 JSON-RPC 2.0 错误格式：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### 常见错误码

| 错误码 | 名称 | 描述 |
|------|------|-------------|
| `-32700` | Parse error | 无效的 JSON |
| `-32600` | Invalid request | 格式错误的 JSON-RPC |
| `-32601` | Method not found | 未知方法 |
| `-32602` | Invalid params | 缺少或无效的参数（如未知的会话 ID） |
| `-32603` | Internal error | 服务端内部错误 |
