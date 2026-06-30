---
title: HTTP 传输
description: 通过 JSON-RPC HTTP 运行和调用 iac-code A2A server。
sidebar_position: 5
---

# HTTP 传输

iac-code 默认的 A2A server 通过 HTTP 暴露 JSON-RPC，并同时暴露 A2A SDK REST 路由。该服务器基于 Starlette 构建，并运行在 Uvicorn 上。

## 启动服务器

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

请先安装可选服务器依赖：

```bash
uv sync --extra a2a
```

## 端点概要

| 路由 | 方法 | 响应 |
|-------|--------|----------|
| `/health` | `GET` | 普通 JSON 健康检查响应 |
| `/.well-known/agent-card.json` | `GET` | Agent Card JSON |
| `/` | `POST` | JSON-RPC 响应或 SSE stream |
| SDK REST routes | mixed | 由 SDK 注册的 A2A REST 端点 |

## 标头

推荐 headers：

```text
Content-Type: application/json
A2A-Version: 1.0
```

启用 Bearer auth 时：

```text
Authorization: Bearer <token>
```

## 认证

服务器支持可选的 Bearer token、Basic auth 和 API key authentication。如果没有设置认证选项或环境变量，请求不需要认证。如果配置了一个或多个方案，请求可以使用任一已配置方案进行认证。

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

你也可以在 A2A YAML 配置文件中设置 `token`。

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

必须同时设置用户名和密码，Basic auth 才会启用。

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

默认 API key header 是 `X-API-Key`。你可以在 YAML 中更改它：

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

也可以使用 `IACCODE_A2A_API_KEY_HEADER`。

| 场景 | 行为 |
|----------|----------|
| 未配置 auth scheme | 不需要认证 |
| 配置了一个或多个 schemes，且任意一个匹配 | 请求继续 |
| 配置了一个或多个 schemes，但没有 scheme 匹配 | HTTP `401`，响应 `{"error":"Unauthorized"}` |

## Agent Card 发现

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

已认证：

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

使用 API key authentication：

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

JSON-RPC 端点 URL 会在 `supportedInterfaces[0].url` 中公布。HTTP 模式还会为支持 REST 的客户端公布 `HTTP+JSON` 接口。

## 非流式消息

`SendMessage` 会在 agent 轮次完成后返回单个 JSON-RPC 响应。

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Create a Terraform VPC module for Alibaba Cloud."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## 流式消息

`SendStreamingMessage` 返回 Server-Sent Events。使用 `curl -N` 可以在事件到达时打印它们。

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "stream-1",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Generate a ROS template for one VPC and two vSwitches."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

每一行 SSE `data:` 都包含一个 JSON-RPC 响应，其 `result` 是 A2A `StreamResponse`。

## 后续消息

使用第一个响应返回的 `taskId` 和 `contextId` 继续同一段对话。

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-3",
        "taskId": "task-id-from-first-response",
        "contextId": "context-id-from-first-response",
        "role": "ROLE_USER",
        "parts": [{"text": "Now add tags for environment and owner."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

对于复用的 `contextId`，工作区必须保持相同。

## 取消运行中的任务

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "CancelTask",
    "params": {
      "id": "task-id"
    }
  }'
```

取消是协作式的：iac-code 会取消活动 agent 轮次，发出 canceled 状态，并释放上下文锁。取消一个已存在但不再运行的任务会返回标准 A2A `TaskNotCancelableError`。

## CLI 等价命令

大多数 HTTP 工作流都有对应的 CLI 命令：

```yaml
url: http://127.0.0.1:41242/
```

```bash
# Discover the Agent Card
iac-code a2a-client --config a2a-client.yml discover

# Send a non-streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a Terraform VPC module for Alibaba Cloud." \
  --cwd "$PWD"

# Send a streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a ROS template for one VPC and two vSwitches." \
  --cwd "$PWD" \
  --stream

# Inspect task state
iac-code a2a-client --config a2a-client.yml task-get --task-id task-id
iac-code a2a-client --config a2a-client.yml task-list --output table

# Cancel an active task
iac-code a2a-client --config a2a-client.yml task-cancel --task-id task-id
```

完整选项列表请参阅 [命令参考](./command-reference.md)。

## 运行说明

- 对仅本地使用，请绑定到 `127.0.0.1`。
- 绑定到共享网络接口前，请在 A2A 配置中使用 `token` 或设置 `IACCODE_A2A_HTTP_TOKEN`。
- A2A 模式会自动拒绝工具权限请求，除非 `auto-approve-permissions` 或显式权限规则允许。权限决策会在本地审计；任何需要审计记录的允许决策在审计记录无法持久化时都会 fail closed。在非 blanket bypass 模式下，受保护的阿里云写 API 需要按 API 精确授权。
- 活动运行时状态位于内存中。持久化会镜像任务和上下文元数据，但重启进程不会恢复正在运行的 asyncio 工作。
- 一个上下文同一时间只能运行一个任务；不同上下文可以并发运行。
