---
title: 命令参考
description: 用于通过 A2A 运行和调用 iac-code 的完整 CLI 命令参考。
sidebar_position: 3
---

# A2A 命令参考

本页面记录每个 A2A 相关的 `iac-code` 命令。当你需要准确的选项名称、常见命令模式以及每个 flag 的运行含义时，请使用本页面。

## 命令概览

| 命令 | 用途 |
|---------|---------|
| `iac-code a2a` | 将 iac-code 作为 A2A server 运行 |
| `iac-code a2a-client call` | 发现远程 Agent Card 并发送 prompt |
| `iac-code a2a-client discover` | 获取并可选验证 Agent Card |
| `iac-code a2a-client task-get` | 按 ID 获取一个 task |
| `iac-code a2a-client task-list` | 使用过滤器和分页列出 tasks |
| `iac-code a2a-client task-cancel` | 取消活动 task |
| `iac-code a2a-client task-subscribe` | 订阅活动 task event stream |
| `iac-code a2a-client push-config-create` | 创建 task push notification config |
| `iac-code a2a-client push-config-get` | 获取一个 task push notification config |
| `iac-code a2a-client push-config-list` | 列出 task push notification configs |
| `iac-code a2a-client push-config-delete` | 删除 task push notification config |
| `iac-code a2a-client extended-card` | 获取已认证的扩展 Agent Card |
| `iac-code a2a-client route-preview` | 为 `a2a-client call` 预览本地路由选择 |

所有 HTTP client 命令都接受相同的认证选项：

| 选项 | 描述 |
|--------|-------------|
| `--token` | 作为 `Authorization: Bearer <token>` 发送的 Bearer token |
| `--basic-username` | Basic auth username |
| `--basic-password` | Basic auth password |
| `--api-key` | API key 值 |
| `--api-key-header` | API key header 名称；默认为 `X-API-Key` |

## A2A Client 配置

所有 `a2a-client` 子命令都在 group 层级接受 YAML 配置文件：

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

CLI 选项会覆盖配置值。使用 config 保存稳定连接、认证、验证、路由以及重复任务或推送设置；一次性的 prompt 文本保留在命令行上。

```yaml
url: http://127.0.0.1:41242/
token: your-bearer-token
basic-username: iac-code
basic-password: your-password
api-key: your-api-key
api-key-header: X-IAC-Code-Key
verify-card-secret: your-card-signing-secret
verify-card-jwks-url: https://a2a.example.com/.well-known/jwks.json
require-card-signature: true
timeout: 30
cwd: /path/to/workspace
context-id: ctx-123
task-id: task-123
config-id: webhook-1
callback-url: https://hooks.example.com/a2a
notification-token: notification-token
auth-scheme: bearer
auth-credentials: callback-token
routes:
  - name: ros
    url: http://127.0.0.1:41242/
    skills:
      - iac_generation
    tags:
      - ros
      - template
```

## `iac-code a2a`

将 iac-code 作为 A2A server 运行。

```bash
iac-code a2a
```

默认情况下，服务器绑定到 `127.0.0.1:41242`，并通过 HTTP 提供 JSON-RPC。端口 `41242` 是 iac-code 默认值；它不是已注册的 A2A 端口。

### 基本服务器选项

| 选项 | 默认值 | 描述 |
|--------|---------|-------------|
| `--config` | 空 | 包含 A2A server 选项的 YAML 配置文件 |
| `--host` | `127.0.0.1` | HTTP server host |
| `--port` | `41242` | HTTP server port |
| `--transport` | `http` | Server transport：`http`、`stdio`、`unix`、`websocket`、`grpc`、`grpc-jsonrpc` 或 `redis-streams` |
| `--thinking-exposure` | `tool-trace` | 暴露一种 A2A thinking 信号类型；可重复指定多个。取值：`raw-thinking`、`tool-trace` |
| `--debug`, `-d` | `false` | 启用 debug logging |

示例：

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### YAML 配置

使用 `--config` 配置认证、存储、签名、传输专用设置、推送投递和其他部署细节。Keys 可以使用连字符或下划线。常用 CLI flags `--host`、`--port` 和 `--transport` 会覆盖 config-file values。

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
```

使用以下命令运行：

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### HTTP 认证

认证是可选的。可以在 YAML 或环境变量中配置服务器认证。如果未配置任何 auth 设置，请求无需认证。当配置了一个或多个方案时，请求可以满足任一已配置方案。

| 配置键 | 环境变量 | 描述 |
|--------|----------------------|-------------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Bearer token |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Basic auth username |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Basic auth password |
| `api-key` | `IACCODE_A2A_API_KEY` | API key 值 |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | API key header 名称 |

Bearer token：

```yaml
token: local-dev-token
```

Basic auth：

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

API key：

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### 持久化和 Artifacts

| 配置键 | 默认值 | 描述 |
|--------|---------|-------------|
| `persistence-dir` | `~/.iac-code/a2a` | 用于 tasks、contexts、routes 和 push configs 的本地 JSON 元数据 |
| `artifact-dir` | `<persistence-dir>/artifacts` | 本地 artifact payload store |

持久化会镜像 task 和 context snapshots，用作恢复元数据。它不会在进程崩溃后重启正在运行的 asyncio task。

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Agent Card 签名

| 配置键 | 描述 |
|--------|-------------|
| `signing-secret` | 用于签名 public Agent Card 的 HMAC secret |

服务器会发出 A2A SDK `AgentCardSignature` JWS 字段。对称模式使用 `HS256`。

```yaml
signing-secret: local-card-signing-secret
```

### 推送通知投递

| 配置键 | 默认值 | 描述 |
|--------|---------|-------------|
| `push-notifications` | `false` | 启用 A2A task push notification config 方法和终态投递 |
| `push-queue` | `local-file` | Push queue backend：`local-file` 或 `redis-streams` |
| `push-redis-url` | 空 | Redis-backed push queue 的 Redis URL |
| `push-stream` | `iac-code:a2a:push` | push jobs 的 Redis stream |
| `push-retry-key` | `iac-code:a2a:push:retry` | delayed retries 的 Redis sorted set |
| `push-dead-stream` | `iac-code:a2a:push:dead` | dead-letter jobs 的 Redis stream |
| `push-consumer-group` | `iac-code-push` | push workers 的 Redis consumer group |
| `push-consumer-name` | 空 | 此 worker 的 Redis consumer name |
| `push-lease-timeout-ms` | `300000` | Redis pending lease timeout |

本地文件队列：

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

Redis Streams 队列：

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
```

Redis-backed push delivery 需要 `a2a-redis` extra。

### 传输选项

| 传输 | 命令 | 说明 |
|-----------|---------|-------|
| HTTP JSON-RPC 和 REST | `iac-code a2a --transport http` | 默认。公布 `JSONRPC` 和 `HTTP+JSON` 接口。 |
| stdio | `iac-code a2a --transport stdio` | 标准输入/输出上的实验性自定义 JSON-RPC frames。 |
| Unix socket | `iac-code a2a --config a2a-server.yml --transport unix` | 需要 config 中的 `socket-path`。 |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | 使用 config 中的 `ws-path`，默认为 `/a2a`。 |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | 使用 config 中的 `grpc-host` 和 `grpc-port`。 |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | gRPC 上的自定义 JSON-RPC envelope。 |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | 需要 config 中的 `redis-url`。 |

Redis Streams 传输选项：

| 配置键 | 默认值 | 描述 |
|--------|---------|-------------|
| `redis-url` | 空 | Redis connection URL；`--transport redis-streams` 必需 |
| `request-stream` | `iac-code:a2a:requests` | Request stream 名称 |
| `response-stream` | `iac-code:a2a:responses` | Response stream 名称 |
| `consumer-group` | `iac-code` | Request stream consumer group |

### Thinking 暴露

| 配置键 | 默认值 | 描述 |
|--------|---------|-------------|
| `thinking-exposure` | `tool-trace` | 通过 A2A `metadata.iac_code` 暴露的非回答运行时信号类型。可使用 YAML 列表、逗号分隔字符串，或重复的 `--thinking-exposure` flag。支持值为 `tool-trace` 和 `raw-thinking`。 |

`tool-trace` 保留现有的工具进度、权限和结果元数据。`raw-thinking` 会将 provider 推理 chunk 作为 `metadata.iac_code.thinking` 更新发出，包含 `type: raw_thinking` 和 `text`。iac-code 目前不会生成独立的 thought-summary 或 progress-summary 事件，因此这些不是有效的暴露类型。

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### 权限行为

| 配置键 | 默认值 | 描述 |
|--------|---------|-------------|
| `auto-approve-permissions` | `false` | 自动批准 A2A 轮次期间发起的工具权限请求 |

如果没有 `auto-approve-permissions: true`，A2A 模式会拒绝权限提示并发出权限元数据。仅在受信任的自动化环境中使用它。

## `iac-code a2a-client call`

发现 Agent Card、选择已公布端点并发送 prompt。

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD"
```

| 选项 | 默认值 | 描述 |
|--------|---------|-------------|
| `--url` | 空 | A2A agent base URL 或 JSON-RPC endpoint URL；可来自 config |
| `--route` | 可重复 | 省略 `--url` 时使用的 route spec |
| `--route-name` | 空 | 要选择的 named route |
| `--prompt`, `-p` | 必需 | Prompt text |
| `--cwd` | `.` | 作为 `message.metadata.iac_code.cwd` 发送的 workspace path |
| `--context-id` | 空 | 用于后续消息的现有 A2A context ID |
| `--verify-card-secret`, `--signing-secret` | 空 | Agent Card verification 的 HMAC secret |
| `--verify-card-jwks-url` | 空 | 用于 Agent Card verification 的远程 JWKS URL |
| `--require-card-signature`, `--require-signature` | `false` | 拒绝未签名或无效的 Agent Cards |
| `--timeout` | `30.0` | 调用 timeout（秒） |
| `--stream` | `false` | 使用 `SendStreamingMessage` 并打印 stream events |

在同一 context 中发送后续消息：

```bash
iac-code a2a-client --config a2a-client.yml call \
  --context-id ctx-123 \
  --prompt "Now add outputs for the VPC and vSwitch IDs." \
  --cwd "$PWD"
```

流式：

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this Terraform module." \
  --cwd "$PWD" \
  --stream
```

要求已签名 Agent Card：

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

使用远程 JWKS URL 验证：

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

获取并打印远程 Agent Card。

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| 选项 | 描述 |
|--------|-------------|
| `--url` | A2A agent base URL；可来自 config |
| `--verify-card-secret`, `--signing-secret` | 用于验证的 HMAC secret |
| `--verify-card-jwks-url` | 用于验证的远程 JWKS URL |
| `--require-card-signature`, `--require-signature` | 要求有效签名 |

已认证发现：

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## Task 命令

Task 命令会直接调用 JSON-RPC task methods。它们适用于运维工具、仪表板和调试。

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| 选项 | 描述 |
|--------|-------------|
| `--url` | A2A JSON-RPC endpoint URL；可来自 config |
| `--task-id` | Task ID；可来自 config |
| `--history-length` | 要返回的最大 task history entries 数 |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| 选项 | 默认值 | 描述 |
|--------|---------|-------------|
| `--url` | 空 | A2A JSON-RPC endpoint URL；可来自 config |
| `--context-id` | 空 | 按 context ID 过滤 |
| `--status` | 空 | 按 task state 过滤 |
| `--page-size` | 空 | 要返回的最大 tasks 数 |
| `--page-token` | 空 | Pagination token |
| `--include-artifacts` | `false` | 在响应中包含 task artifacts |
| `--output` | `table` | `table` 或 `json` |

JSON 输出：

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --include-artifacts \
  --output json
```

### `iac-code a2a-client task-cancel`

```bash
iac-code a2a-client --config a2a-client.yml task-cancel \
  --task-id task-123
```

取消是协作式的。已完成、失败、已取消或需要输入的 task 会返回标准 A2A task-not-cancelable 错误。

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

该命令会为活动 tasks 流式传输 events。对于新轮次，优先使用 `a2a-client call --stream`；它会在一个命令中启动 task 并流式传输更新。

## 推送通知配置命令

这些命令需要服务器以 `push-notifications: true` 启动。它们管理标准 A2A task push notification configs。

### `iac-code a2a-client push-config-create`

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

| 选项 | 描述 |
|--------|-------------|
| `--url` | A2A JSON-RPC endpoint URL；可来自 config |
| `--task-id` | Task ID；可来自 config |
| `--config-id` | Push config ID；可来自 config |
| `--callback-url` | HTTP(S) callback URL；可来自 config |
| `--notification-token` | 作为 `X-A2A-Notification-Token` 发送的 token |
| `--auth-scheme` | Callback auth scheme，例如 `bearer` 或 `basic` |
| `--auth-credentials` | Callback auth credentials |

Callback URLs 会在存储和分发前校验。默认 validator 会拒绝非 HTTP(S) URLs、localhost names 和字面量 private/local IP addresses。

### `iac-code a2a-client push-config-get`

```bash
iac-code a2a-client --config a2a-client.yml push-config-get \
  --task-id task-123 \
  --config-id webhook-1
```

### `iac-code a2a-client push-config-list`

```bash
iac-code a2a-client --config a2a-client.yml push-config-list \
  --task-id task-123 \
  --page-size 10
```

### `iac-code a2a-client push-config-delete`

```bash
iac-code a2a-client --config a2a-client.yml push-config-delete \
  --task-id task-123 \
  --config-id webhook-1
```

## `iac-code a2a-client extended-card`

获取已认证的扩展 Agent Card。

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

Public Agent Card 会公布 `capabilities.extendedAgentCard=true`。扩展 card 会添加已认证运行时细节，包括任务管理和推送配置能力元数据。

## `iac-code a2a-client route-preview`

预览省略 `--url` 时 `a2a-client call` 如何解析已配置路由。

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| 选项 | 描述 |
|--------|-------------|
| `--route` | `name=url;skills=a,b;tags=x,y` 格式的可重复 route spec |
| `--name` | 要解析的 route name |
| `--skill` | 要解析的 Skill ID |
| `--prompt` | 用于 name/tag matching 的 prompt text |
| `--route-state-dir`, `--persistence-dir` | 用于持久化 route snapshots 的目录 |
| `--save-routes` | 将提供的 routes 保存到 route state directory |

保存 route snapshots：

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

通过 routes 调用：

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## 环境变量

| 变量 | 描述 |
|----------|-------------|
| `IACCODE_A2A_HTTP_TOKEN` | Server/client Bearer token 默认值 |
| `IACCODE_A2A_BASIC_USERNAME` | Server/client Basic auth username 默认值 |
| `IACCODE_A2A_BASIC_PASSWORD` | Server/client Basic auth password 默认值 |
| `IACCODE_A2A_API_KEY` | Server/client API key 默认值 |
| `IACCODE_A2A_API_KEY_HEADER` | API key header 名称默认值 |
| `IACCODE_A2A_ALLOWED_CWDS` | 用于传入 message metadata 和 file URLs 的按 OS path separator 分隔的 allowed workspace roots 列表 |
| `IACCODE_A2A_TEXT_MIME_TYPES` | 额外的以逗号或分号分隔的 text-like MIME types |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | 额外的以逗号或分号分隔的 multimodal MIME types |
| `IAC_CODE_A2A_PUSH_KEYRING` | 由环境管理的 encrypted push secret keyring |
