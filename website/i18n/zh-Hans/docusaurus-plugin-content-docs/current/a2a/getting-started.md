---
sidebar_position: 2
title: 快速开始
description: 启动 A2A server 并发送第一条消息。
---

# A2A 快速开始

## 前提条件

1. **已安装 iac-code** — 请参阅 [安装](/docs/getting-started/installation) 指南。

2. **已配置 LLM 凭据** — 请参阅 [认证](/docs/configuration/authentication) 指南来配置模型 provider 凭据。

3. **A2A server 依赖** — 使用 `a2a` extra 安装 iac-code：

```bash
uv sync --extra a2a
```

## 启动 A2A Server

在默认本地接口上启动服务器：

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

当你需要本地状态、artifact 存储、推送通知投递或已签名 Agent Cards 时，请使用 YAML 配置文件：

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
thinking-exposure:
  - tool-trace
  - raw-thinking
```

使用以下命令运行：

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` 会启用 A2A 任务推送通知配置方法和终态投递。当多个 worker 需要协调推送投递时，请将 `push-queue: redis-streams` 与 `push-redis-url` 搭配使用。

`thinking-exposure` 控制流式过程中通过 `metadata.iac_code` 暴露哪些非回答运行时信号。默认值是 `tool-trace`；只有允许看到 provider 推理内容的受信任客户端才应添加 `raw-thinking`。

服务器暴露：

| 路由 | 用途 |
|-------|---------|
| `GET /health` | 健康检查 |
| `GET /.well-known/agent-card.json` | Agent Card 发现 |
| `POST /` | A2A JSON-RPC 端点 |

HTTP 服务器还会注册 A2A SDK REST 路由，并在 Agent Card 中公布 `JSONRPC` 和 `HTTP+JSON` 两种接口。

## 验证发现

获取 Agent Card：

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

你应该能看到 `name: "iac-code"`、`JSONRPC` 和 `HTTP+JSON` 接口、`ETag` 等缓存标头、可选的 `urn:iac-code:a2a:artifact-metadata:v1` 扩展、受支持的输入模式，以及 `iac_generation`、`iac_review`、`aliyun_ros_operations` 和 `terraform_ros_conversion` 等技能。

检查健康检查端点：

```bash
curl http://127.0.0.1:41242/health
```

预期响应：

```json
{"status":"healthy"}
```

## 要求认证

认证是可选的。如果未设置任何 A2A 认证选项或环境变量，请求不需要认证。当配置了任意认证方案时，包括 Agent Card 发现以内的每个请求都必须满足一个已配置方案。

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

等价的 YAML 配置键是 `token`。

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

用户名和密码必须同时存在。等价的 YAML 配置键是 `basic-username` 和 `basic-password`。

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

默认 API key 标头是：

```text
X-API-Key: <api-key>
```

可使用 `api-key-header` YAML 配置键或 `IACCODE_A2A_API_KEY_HEADER` 覆盖它：

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## 调用远程 A2A Agent

将稳定的客户端连接和认证设置放入 YAML 文件：

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
iac-code-model: qwen-plus
iac-code-api-key: provider-api-key
# 可选：单次 message 的 thinking 策略 metadata：
# thinking-enabled: false
# thinking-effort: low
# thinking-budget: 2048
```

使用 `a2a-client call` 进行直接的 Phase 1 client 调用：

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

当你想要增量事件而不是单个最终响应时，使用 `--stream`：

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

当你需要一次性的目标或 token 时，命令行选项会覆盖配置值：

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

对于多 agent 路由，请在调用前预览路由选择：

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

请参阅 [命令参考](./command-reference.md) 了解所有 A2A 命令，包括任务管理、推送配置 CRUD、扩展 Agent Cards 和传输选项。

## 使用 curl 发送第一条消息

通过 `message.metadata.iac_code.cwd` 传递工作区目录；该路径必须是绝对路径，并且 resolve 后必须位于允许的工作区根目录内。已存在的路径必须是目录；缺失路径会在允许根目录下创建。默认情况下，允许的根目录是服务器进程目录和系统临时目录。可以用 `IACCODE_A2A_ALLOWED_CWDS` 覆盖它们。

如需按任务覆盖 telemetry attribution，请在 `message.metadata.iac_code` 下传入非空字符串 `user_id`。它只覆盖当前任务的 telemetry user ID，不会改变 A2A task 或 context identifiers。

如需按调用覆盖 LLM，请在 `message.metadata.iac_code` 下传入非空字符串 `iac_code_model`。该字段是 `IAC_CODE_MODEL` 的小写形式；它只在当前 A2A message turn 中覆盖 `IAC_CODE_MODEL`、`settings.yml` 和 server 启动默认 model。

如需按调用覆盖 LLM provider key，请在 `message.metadata.iac_code` 下传入非空字符串 `iac_code_api_key`。该字段是 `IAC_CODE_API_KEY` 的小写形式；它只在当前 A2A message turn 中覆盖 `IAC_CODE_API_KEY` 和 `.credentials.yml`。它和用于保护 A2A endpoint 的 HTTP 认证（`api-key` / `IACCODE_A2A_API_KEY`）是两件事。

如需按请求传入 Alibaba Cloud 凭据，请在 `message.metadata.iac_code` 下包含 `alibaba_cloud_access_key_id`、`alibaba_cloud_access_key_secret`、`alibaba_cloud_region_id` 和可选的 `alibaba_cloud_security_token`。这些任务凭据只在本次 A2A 执行中覆盖 server 环境和 `.cloud-credentials.yml`。

服务器接受类文本 parts、JSON 数据 parts、原始 UTF-8 文本、本地工作区 `file://` 文本文件和有界多模态附件。不支持远程 URL 摄取；`url` parts 必须是位于允许工作区内的本地 `file://` URLs。

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Generate a ROS VPC template with two vSwitches."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project",
            "iac_code_model": "qwen-plus",
            "iac_code_api_key": "provider-api-key"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

对于流式输出，请使用 `SendStreamingMessage`：

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Review my Terraform files and suggest ROS equivalents."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## 最小 Python SDK 示例

下面的示例使用 `a2a-sdk>=1.0.2,<2`，这是 `a2a` extra 使用的版本范围。

```python
"""Minimal iac-code A2A client using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Generate a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("status_update"):
                status = event.status_update.status
                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

:::tip
对于启用了认证的服务器，请使用 `headers={"Authorization": "Bearer <token>"}` 构造 `httpx.AsyncClient`，以便 Agent Card 发现和 JSON-RPC 调用都包含 token。
:::

## 后续步骤

- [命令参考](./command-reference.md) — 完整 CLI 命令和选项参考。
- [协议参考](./protocol-reference.md) — 方法、路由、状态和元数据细节。
- [HTTP 传输](./http-transport.md) — JSON-RPC HTTP 行为、bearer auth 和 curl 工作流。
- [示例](./examples.md) — SDK、直接 HTTP、后续消息、取消和元数据处理示例。
