---
title: HTTP+SSE 传输层
description: 通过 HTTP 和 Server-Sent Events 运行 ACP 服务器，适用于远程部署和多客户端场景。
sidebar_position: 5
---

# HTTP+SSE 传输层

iac-code 的 ACP 服务器支持两种传输模式。默认的 **Stdio** 传输通过标准输入/输出通信，适合本地 IDE 集成。**HTTP+SSE** 传输则暴露网络端点，通过 Server-Sent Events 流式传输响应，适用于远程部署、负载均衡环境和多客户端访问场景。

## 为什么选择 HTTP+SSE

Stdio 有一些固有的局限性：

- 要求服务器进程必须是客户端的直接子进程——无法远程访问。
- 阻塞式的进程管理使得同时服务多个客户端变得困难。
- 不兼容网络代理、负载均衡器或容器化部署。

HTTP+SSE 解决了这些限制：

- **网络友好** — 任何能访问端点的机器都可以连接。
- **多客户端** — 每个客户端拥有独立的连接和事件流。
- **基础设施就绪** — 可在反向代理后运行，支持容器化部署，兼容标准 HTTP 监控工具。
- **易于集成** — 任何 HTTP 客户端（curl、fetch、SDK）都能与服务器交互。

## 启动 HTTP 服务器

```bash
# 默认端口 8765
iac-code acp --transport http

# 自定义端口
iac-code acp --transport http --port 9090
```

服务器使用 [Starlette](https://www.starlette.io/) 作为 ASGI 框架，运行于 Uvicorn 之上。

## 路由

所有路由均挂载在 `/acp` 路径下，通过 HTTP 方法区分不同操作。

### `POST /acp`

向服务器发送 JSON-RPC 请求。

- **`initialize`** — 创建新连接并直接返回完整的 JSON-RPC 响应。响应中包含 `Acp-Connection-Id` 头。
- **其他所有方法** — 需要提供有效的 `Acp-Connection-Id` 头。立即返回 `202 Accepted`；实际结果通过 SSE 流异步推送。

### `GET /acp`

打开 Server-Sent Events 流以接收响应和通知。

- 需要提供 `Acp-Connection-Id` 头。
- 事件类型为 `message`，`data` 字段包含 JSON-RPC 响应/通知。
- 流中包含 `id` 和 `retry` 字段，支持自动重连。

### `DELETE /acp`

关闭连接并释放所有关联资源。

- 需要提供 `Acp-Connection-Id` 头。
- 返回 `200 OK`。

## 连接 ID

连接 ID 将客户端的请求与其 SSE 事件流关联起来。

1. 客户端发送包含 `initialize` 方法的 `POST /acp` 请求。
2. 服务器在响应中返回初始化结果，并在 `Acp-Connection-Id` 响应头中包含一个 UUID。
3. 后续所有请求（`POST`、`GET`、`DELETE`）都必须在请求头中包含 `Acp-Connection-Id` 及其值。
4. 每个连接 ID 对应一个独立的 ACP 代理会话，拥有自己的事件队列。

如果请求引用了不存在或无效的连接 ID，服务器返回 `400 Bad Request`。

## 认证

服务器通过 `IACCODE_ACP_HTTP_TOKEN` 环境变量支持可选的 Bearer Token 认证。

```bash
# 启动服务器前设置 Token
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

设置后，每个请求都必须包含：

```
Authorization: Bearer your-secret-token
```

| 场景 | 行为 |
|------|------|
| 未设置 Token | 无需认证（适合本地开发） |
| 已设置 Token，请求头匹配 | 正常处理请求 |
| 已设置 Token，请求头缺失或不匹配 | 返回 `401 Unauthorized` |

## 完整工作流

以下是使用 `curl` 进行完整交互的示例：

```bash
# 第 1 步：初始化 — 创建连接并获取连接 ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# 第 2 步：打开 SSE 流（后台运行）
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# 第 3 步：创建会话
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# 第 4 步：发送提示
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# 第 5 步：关闭连接
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# 清理后台 SSE 进程
kill $SSE_PID 2>/dev/null
```

:::tip
`initialize` 响应是同步返回的（超时时间为 30 秒）。后续所有响应均通过第 2 步打开的 SSE 流推送。
:::
