---
title: 协议参考
description: 用于 iac-code 集成的完整 A2A 协议参考。
sidebar_position: 4
---

# 协议参考

本文档描述 iac-code server 暴露的 A2A 1.0 接口面，以及 `iac-code a2a-client call` 使用的 Phase 1 client 行为。准确的 CLI 选项请参阅 [命令参考](./command-reference.md)。

## 生命周期概览

典型 A2A 交互遵循以下流程：

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **发现** — 获取 `/.well-known/agent-card.json`。
2. **发送** — 向 `/` 上的 JSON-RPC 端点提交文本消息。
3. **流式接收** — 接收 `Task`、`Message` 和 `TaskStatusUpdateEvent` payloads。
4. **继续** — 使用相同 `contextId` 发送后续消息。
5. **取消或查询** — 使用 `CancelTask`、`GetTask` 或 `ListTasks`。

## Agent Card

Agent Card 可通过以下位置获取：

```text
GET /.well-known/agent-card.json
```

重要字段：

| 字段 | 值 | 含义 |
|-------|-------|---------|
| `name` | `iac-code` | Agent 名称 |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | 传输绑定 |
| `supportedInterfaces[0].protocolVersion` | `1.0` | A2A 协议版本 |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | JSON-RPC 端点 |
| `capabilities.streaming` | `true` | 支持流式任务更新 |
| `capabilities.pushNotifications` | `false` 或 `true` | 配置了 `push-notifications: true` 时为 `true` |
| `capabilities.extendedAgentCard` | `true` | 已认证调用方可以请求扩展运行时细节 |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | 用于工具状态和已存储 artifact 元数据的可选 iac-code 元数据命名空间 |
| `defaultInputModes` | text、JSON、YAML、image、audio 和 binary MIME types | 接受的输入 MIME 模式 |
| `defaultOutputModes` | `["text/plain"]` | 仅文本输出 |

Agent Card 响应包含 `Cache-Control: public, max-age=60`、`ETag` 和 `Last-Modified`。客户端可以发送 `If-None-Match`，当 card 未更改时会收到 `304 Not Modified`。

公布的技能：

| 技能 ID | 用途 |
|----------|---------|
| `iac_generation` | 根据自然语言生成 Alibaba Cloud ROS 和 Terraform 模板 |
| `iac_review` | 检查 IaC 模板并建议修复 |
| `aliyun_ros_operations` | 协助 Alibaba Cloud ROS stack 工作流 |
| `terraform_ros_conversion` | 使用 bundled skill resources 协助 Terraform 到 ROS 转换 |

启用认证后，Agent Card 会公布已配置的安全方案：

| 方案 | 公布时机 |
|--------|-----------------|
| `bearerAuth` | 设置了 `token` 或 `IACCODE_A2A_HTTP_TOKEN` |
| `basicAuth` | 同时设置了 Basic username 和 password |
| `apiKeyAuth` | 设置了 `api-key` 或 `IACCODE_A2A_API_KEY` |

## 路由

| 路由 | 方法 | 描述 |
|-------|--------|-------------|
| `/health` | `GET` | 返回 `{"status":"healthy"}` |
| `/.well-known/agent-card.json` | `GET` | 返回 Agent Card |
| `/` | `POST` | 处理 A2A JSON-RPC 请求 |
| REST 路由 | mixed | 由 `create_rest_routes` 注册的 A2A SDK REST 路由 |

## Phase 1 Client 和传输说明

默认的可互操作 Phase 1 传输是基于 HTTP 的 JSON-RPC。HTTP 模式还会为 SDK REST 路由公布 `HTTP+JSON`。

服务器还提供 stdio、Unix sockets、WebSocket、官方 gRPC、gRPC JSON-RPC envelope 和 Redis Streams 的可选传输。stdio、Unix sockets、WebSocket、gRPC JSON-RPC 和 Redis Streams 是自定义 JSON-RPC 传输。官方 gRPC 公布为 `grpc`，并需要可选 gRPC 依赖。

内置客户端在消息调用前使用 Agent Card 发现（`GET /.well-known/agent-card.json`），选择第一个已公布且可运行的 `supportedInterfaces[].url`，然后使用 `A2A-Version: 1.0` 和 `SendMessage` 等 A2A 1.0 方法名发送 JSON-RPC 请求。

`push-notifications: true` 启用 A2A 推送通知配置方法和终态投递。

Agent Card 签名使用 A2A SDK 签名工具，并发出标准 `AgentCardSignature` JWS 字段。对称密钥模式使用 `HS256`；验证可以通过 protected-header `kid` 选择已配置 secret、本地 octet-key JWKS 或远程 JWKS URL。Phase 1 未实现服务器端非对称签名和自动密钥轮换。

Phase 1 不支持行为的规范列表请参阅 [A2A 协议](./overview.md#phase-1-unsupported)。

## 推送通知投递后端

`iac-code a2a --config a2a-server.yml` 支持两种推送投递队列：

- `push-queue: local-file` 将 jobs 存储在 A2A 持久化目录下方，适用于本地单节点使用。
- `push-queue: redis-streams` 将 jobs 存储在 Redis Streams 中，并通过 Redis consumer group 协调 workers。

Redis 后端推送投递需要可选的 `a2a-redis` extra，并且是至少一次投递。Callback 接收方应以幂等方式处理任务更新，因为 worker 崩溃、lease 过期、重连或重试竞争后，一个 job 可能会再次投递。

常用 Redis 选项：

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

Callback URLs 会在存储前以及分发前再次校验。默认 validator 会拒绝非 HTTP(S) URLs、localhost hostnames 和字面量 private/local IP addresses。Callback 接收方仍应执行自己的认证和幂等策略。

## JSON-RPC 方法

### SendMessage

运行非流式 A2A 消息轮次。响应会在轮次完成后包含一个 task 或 message。

**请求**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {
          "cwd": "/absolute/path/to/project",
          "user_id": "client-user-123",
          "iac_code_model": "qwen-plus",
          "iac_code_api_key": "provider-api-key"
        }
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**必需消息字段**

| 字段 | 类型 | 必需 | 描述 |
|-------|------|----------|-------------|
| `messageId` | string | 是 | 唯一客户端消息 ID |
| `role` | string | 是 | 对用户输入使用 `ROLE_USER` |
| `parts` | array | 是 | 类文本、JSON 数据、原始文本、本地 file URL 或有界多模态 parts |
| `metadata.iac_code.cwd` | string | 建议 | 绝对工作区路径；省略时默认为服务器进程目录 |
| `metadata.iac_code.user_id` | string | 可选 | 当前任务的 telemetry user ID 覆盖；空字符串或非字符串会被忽略 |
| `metadata.iac_code.iac_code_model` | string | 可选 | 当前调用的 LLM model 覆盖；这是 `IAC_CODE_MODEL` 的小写形式，空字符串或非字符串会被忽略 |
| `metadata.iac_code.iac_code_api_key` | string | 可选 | 当前调用的 LLM provider API key 覆盖；这是 `IAC_CODE_API_KEY` 的小写形式，空字符串或非字符串会被忽略 |
| `metadata.iac_code.alibaba_cloud_access_key_id` | string | 可选 | 当前任务使用的 Alibaba Cloud AccessKey ID |
| `metadata.iac_code.alibaba_cloud_access_key_secret` | string | 可选 | 当前任务使用的 Alibaba Cloud AccessKey Secret |
| `metadata.iac_code.alibaba_cloud_region_id` | string | 可选 | 当前任务使用的 Alibaba Cloud region；和任务凭据一起省略时默认为 `cn-hangzhou` |
| `metadata.iac_code.alibaba_cloud_security_token` | string | 可选 | 当前任务使用的 Alibaba Cloud STS token |

提供 `metadata.iac_code.cwd` 时，它必须是一个绝对目录路径，并且 resolve 后位于允许的工作区根目录内。如果路径已存在，它必须是目录；如果路径不存在，executor 会在允许根目录下创建它。默认情况下，允许的根目录是服务器进程目录和系统临时目录；`IACCODE_A2A_ALLOWED_CWDS` 可以提供按 OS path separator 分隔的 allowlist。

当 `metadata.iac_code` 同时包含 `alibaba_cloud_access_key_id` 和 `alibaba_cloud_access_key_secret` 时，A2A executor 只会在当前任务中使用这些 Alibaba Cloud 凭据。它们优先于进程环境变量和 `.cloud-credentials.yml`；如果任务 metadata 不完整或不存在，则继续使用正常凭据 fallback。

`metadata.iac_code.user_id` 只影响当前任务的 telemetry identity，不会改变 A2A `contextId`、`taskId` 或 iac-code 内部 session ID。

`metadata.iac_code.iac_code_model` 只影响当前 A2A message turn。它优先于 `IAC_CODE_MODEL`、`settings.yml` 和 server 启动默认 model；复用同一个 `contextId` 的后续轮次如果不再传该字段，会回落到 server 默认 model。

`metadata.iac_code.iac_code_api_key` 只影响当前 A2A message turn。它优先于 `IAC_CODE_API_KEY` 和 `.credentials.yml` 中当前有效 model 对应 provider 的 key；复用同一个 `contextId` 的后续轮次如果不再传该字段，会重新加载正常凭据，因此单次调用 key 不会串到后续请求。这个字段用于 LLM provider key，和 A2A transport 认证里的 `api-key` / `IACCODE_A2A_API_KEY` 是两件事。

支持的输入类别：

| 类别 | 接受的形状 | 限制和行为 |
|----------|----------------|---------------------|
| 类文本 parts | 带 `text/plain`、JSON、Markdown、YAML 或已配置额外 text MIME types 的 `text` | 直接追加到 prompt |
| JSON 数据 parts | 带 `application/json` 的 `data` | 序列化为紧凑 JSON；inline 最大 1 MiB |
| 原始文本 parts | 带 text-like MIME type 的 `raw` | 必须是有效 UTF-8；inline 最大 1 MiB |
| 本地文本 file URLs | 带 `file://...` 和 text-like MIME type 的 `url` | 文件必须存在于 `cwd` 和允许的根目录内；最大 1 MiB |
| 多模态 raw/data/file parts | image、audio 或已配置 multimodal MIME types | 转换为包含 filename、media type、byte size、hash 和 source 的 prompt manifest；raw/data 最大 5 MiB，file URL 最大 25 MiB |

不支持远程 HTTP(S) URL 摄取。File URL parts 必须使用本地 `file://` URLs，并留在允许的工作区内。

### SendStreamingMessage

运行流式 A2A 消息轮次。请求体形状与 `SendMessage` 相同，但服务器会以 Server-Sent Events 形式流式传输 JSON-RPC 响应。

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

按 ID 返回已保存的 A2A task。使用 `historyLength` 可以限制返回的历史记录，且不会改变已存储的任务历史。省略它则接收服务器当前默认历史。

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

返回认证调用方可见的已知 tasks。结果按 status timestamp 降序排序，然后按 task ID 降序排序以实现稳定顺序。服务器支持 `contextId`、`status`、`pageSize`、`pageToken`、`historyLength` 和 `includeArtifacts`。

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

当还有下一页可用时会返回 `nextPageToken`。`includeArtifacts` 默认为 `false`，因此 list 响应会省略 task artifacts，除非显式请求。

### CancelTask

请求取消一个运行中的 task。

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

如果 task 是活动的，服务器会取消正在运行的 agent 轮次并发出 canceled task state。如果 task 存在但未运行，服务器会返回标准 A2A `TaskNotCancelableError`。

### SubscribeToTask

当客户端传输支持时，订阅活动 task 更新流。

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

对于活动 tasks，stream 会从当前 `Task` 开始，然后发出后续 task events，并在活动轮次完成时关闭。订阅 completed、failed、canceled 或 input-required task 会返回 task-not-found 风格错误，而不是无限等待。对于新轮次，优先使用 `SendStreamingMessage`；它会在一个请求中启动执行并流式传输响应。

### 推送通知配置方法

当服务器以 `push-notifications: true` 启动时，它支持：

| 方法 | 用途 |
|--------|---------|
| `CreateTaskPushNotificationConfig` | 为 task 存储 callback config |
| `GetTaskPushNotificationConfig` | 获取一个 callback config |
| `ListTaskPushNotificationConfigs` | 列出 task 的 callback configs |
| `DeleteTaskPushNotificationConfig` | 删除一个 callback config |

创建请求示例：

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

当本地 push keyring 可用时，服务器会加密已存储的 notification tokens 和 callback authentication credentials。

### GetExtendedAgentCard

已认证客户端可以请求扩展 Agent Card：

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

扩展 card 包含 public card 以及已认证运行时细节。

## Task 和 Context 行为

iac-code 将 A2A contexts 映射到内部 agent runtimes：

| 概念 | 行为 |
|---------|----------|
| 省略 `contextId` | SDK/server 生成新的 context ID |
| 相同 `contextId` | 复用同一内部 iac-code session 和 conversation state |
| 相同 `contextId`，不同 `cwd` | 作为不同工作区被拒绝 |
| 相同 `contextId`，并发消息 | 以 `Task is already working.` 拒绝 |
| 不同 `contextId` 值 | 可以并发执行 |
| 空闲 context | 在配置的 idle timeout 后从内存中逐出 |

Task 和 context IDs 必须非空，最多 128 个字符，并且只能包含字母、数字、`_`、`.`、`:` 或 `-`。

## Task 状态

| 状态 | 含义 |
|-------|---------|
| `TASK_STATE_SUBMITTED` | task 已被接受 |
| `TASK_STATE_WORKING` | iac-code 正在运行 agent 轮次 |
| `TASK_STATE_INPUT_REQUIRED` | 轮次已完成，agent 已准备好接收后续输入 |
| `TASK_STATE_CANCELED` | 已请求并应用取消 |
| `TASK_STATE_FAILED` | task 验证或执行失败 |

iac-code 使用 `TASK_STATE_INPUT_REQUIRED` 作为正常完成状态，因为 context 仍可用于后续消息。

## 流式更新

执行期间，iac-code 会发出 `TaskStatusUpdateEvent` 更新。

Assistant text 作为 status message 投递：

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

工具和用量细节通过 `metadata.iac_code` 投递：

| 元数据路径 | 描述 |
|---------------|-------------|
| `iac_code.tool.status` | `started`、`input_delta`、`input_complete`、`completed` 或 `failed` |
| `iac_code.tool.toolUseId` | 用于关联工具事件的稳定 tool-use ID |
| `iac_code.tool.name` | 可用时的工具名称 |
| `iac_code.tool.input` | 已完成工具输入，每个字段截断为 4000 个字符 |
| `iac_code.tool.result` | 工具结果，每个字段截断为 4000 个字符 |
| `iac_code.permission.autoApproved` | A2A server 模式拒绝工具权限请求时为 `false` |
| `iac_code.thinking.type` | 在 `thinking-exposure` 中启用 `raw-thinking` 时为 `raw_thinking` |
| `iac_code.thinking.text` | 原始 provider 推理 chunk，截断为 4000 个字符，仅在受信任配置启用 `raw-thinking` 时发出 |
| `iac_code.usage.inputTokens` | 该轮次的 input token 数 |
| `iac_code.usage.outputTokens` | 该轮次的 output token 数 |
| `iac_code.usage.totalTokens` | 该轮次的 total token 数 |

当工具结果包含受支持的文本 artifact payload 时，服务器会在本地存储该 payload，发出标准 `TaskArtifactUpdateEvent`，并在 task `artifacts` 字段中记录该 artifact。Artifact part 使用 `file://` URL 以及 `mediaType`、`byteSize` 和 `sha256` 等元数据；原始 artifact 内容不会在工具元数据中重复。

## 扩展

Agent Card 会公布可选的 iac-code artifact 元数据扩展：

```text
urn:iac-code:a2a:artifact-metadata:v1
```

该扩展标识 `metadata.iac_code` 命名空间，该命名空间用于工具进度、权限决策、token 用量和本地 artifact 元数据。如果服务器配置了任何必需扩展，客户端必须在 `A2A-Extensions` header 中包含其 URI。缺少必需扩展会返回标准 A2A `ExtensionSupportRequiredError`。

当 `thinking-exposure` 启用一种或多种信号类型时，Agent Card 还会公布：

```text
urn:iac-code:a2a:thinking-exposure:v1
```

扩展 params 包含 `enabledTypes`，例如 `["tool_trace", "raw_thinking"]`。这是 iac-code 元数据扩展，不是 A2A 核心流式事件类型。

## 错误处理

| 场景 | 结果 |
|----------|--------|
| 空文本输入 | `TASK_STATE_FAILED`，消息为 `A2A server currently accepts text input only.` |
| 不支持的 media type | 验证错误或标准 A2A content-type 错误，取决于 SDK 在何处拒绝请求 |
| 远程 URL part | 验证错误，因为 URL parts 必须使用本地 `file://` URLs |
| 允许工作区之外的 File URL | 验证错误 |
| 缺少必需的 A2A 扩展 | 标准 A2A `ExtensionSupportRequiredError` |
| 无效的工作区元数据 | `TASK_STATE_FAILED`，带 invalid workspace 消息 |
| 缺少认证或认证无效 | HTTP `401`，响应 `{"error":"Unauthorized"}` |
| 缺少 A2A server 依赖 | CLI 退出，并提示安装 `a2a` extra |
| 缺少 provider 凭据 | 已清理的认证错误 |
| 意外运行时错误 | 已清理的内部错误 |

服务器会避免在意外错误消息中返回本地路径、secrets 和 provider 细节。
