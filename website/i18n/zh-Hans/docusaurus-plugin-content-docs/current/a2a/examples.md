---
title: 示例
description: 与 iac-code A2A server 集成的实用示例。
sidebar_position: 6
---

# 示例

本页面提供可直接使用的 A2A 集成示例。

## 前提条件

这些示例假定：

| 依赖 | 版本 | 用途 |
|------------|---------|---------|
| Python | `3.12` | 匹配项目运行时 |
| `a2a-sdk` | `>=1.0.2,<2` | A2A client 和 protobuf types |
| `httpx` | `>=0.27.0` | SDK 和直接示例使用的 HTTP client |
| `iac-code` | 当前仓库 | 提供 `iac-code a2a` 子命令 |

启动服务器：

```bash
uv sync --extra a2a
iac-code a2a --host 127.0.0.1 --port 41242
```

## Python SDK — 流式会话

此示例发现 Agent Card、发送消息、打印 assistant 文本块，并报告工具元数据。

```python
"""Streaming iac-code A2A session using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict


def metadata_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return MessageToDict(value, preserving_proto_field_name=False)


async def main() -> None:
    headers = {}
    # For authenticated servers:
    # headers["Authorization"] = "Bearer YOUR_TOKEN"

    async with httpx.AsyncClient(headers=headers, timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Create a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("task"):
                print(f"[task] {event.task.id} context={event.task.context_id}")

            elif event.HasField("status_update"):
                update = event.status_update
                status = update.status

                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

                metadata = metadata_to_dict(update.metadata)
                tool = metadata.get("iac_code", {}).get("tool")
                if tool:
                    print(f"\n[tool] {tool.get('status')} {tool.get('name', '')}".rstrip())

                usage = metadata.get("iac_code", {}).get("usage")
                if usage:
                    print(f"\n[usage] {usage['totalTokens']} total tokens")

                if status.state == "TASK_STATE_INPUT_REQUIRED":
                    print("\n[done]")

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

## CLI — 端到端工作流

启动一个带持久化、artifacts、推送通知支持和已签名 Agent Card 的本地服务器：

```yaml
host: 127.0.0.1
port: 41242
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
```

```bash
iac-code a2a --config a2a-server.yml
```

为稳定端点和 card 验证设置创建 client config：

```yaml
url: http://127.0.0.1:41242/
verify-card-secret: local-card-signing-secret
require-card-signature: true
```

发现并验证 Agent Card：

```bash
iac-code a2a-client --config a2a-client.yml discover
```

发送流式请求：

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --stream
```

列出 tasks 并以 JSON 获取一个 task：

```bash
iac-code a2a-client --config a2a-client.yml task-list --output table

iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

为 task 注册 push callback：

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

调用 routed agent 前预览路由选择：

```bash
iac-code a2a-route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

## Python SDK — 后续消息

后续消息会复用相同的 `context_id`，通常也会复用相同的 task ID。这会让内部 iac-code runtime 和 conversation history 保持活动状态。

```python
import uuid
from pathlib import Path

from a2a.types import Message, Part, Role, SendMessageRequest


def build_follow_up(task_id: str, context_id: str, text: str) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            message_id=f"msg-{uuid.uuid4().hex}",
            task_id=task_id,
            context_id=context_id,
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            metadata={"iac_code": {"cwd": str(Path.cwd())}},
        )
    )


# Usage inside an async function:
# async for event in client.send_message(
#     build_follow_up(task_id, context_id, "Add outputs for VPC and VSwitch IDs.")
# ):
#     ...
```

如果新消息指向不同工作区，服务器会拒绝复用的 `contextId`。

## Python SDK — 取消任务

```python
from a2a.types import CancelTaskRequest


async def cancel_running_task(client, task_id: str) -> None:
    task = await client.cancel_task(CancelTaskRequest(id=task_id))
    print(f"{task.id}: {task.status.state}")
```

## 直接 HTTP — 最小 JSON-RPC Client

当调用方不想引入 SDK 依赖时，请使用此方式。

```python
"""Direct A2A JSON-RPC client using httpx."""

import asyncio
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:41242"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120.0) as client:
        response = await client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "send-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Review this ROS template for missing parameters."}],
                        "metadata": {"iac_code": {"cwd": str(Path.cwd())}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )
        response.raise_for_status()
        print(response.json())


if __name__ == "__main__":
    asyncio.run(main())
```

## 直接 HTTP — 流式 SSE

```python
"""Read SendStreamingMessage events directly with httpx."""

import asyncio
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:41242"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=None) as client:
        async with client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "stream-1",
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Generate a Terraform VPC example."}],
                        "metadata": {"iac_code": {"cwd": str(Path.cwd())}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    print(line.removeprefix("data:").strip())


if __name__ == "__main__":
    asyncio.run(main())
```

## 直接 HTTP — 推送通知配置

当服务器以 `push-notifications: true` 运行时，push config 方法可用。

```python
"""Create an A2A task push notification config."""

import asyncio

import httpx

BASE_URL = "http://127.0.0.1:41242"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        response = await client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "push-1",
                "method": "CreateTaskPushNotificationConfig",
                "params": {
                    "taskId": "task-123",
                    "id": "webhook-1",
                    "url": "https://hooks.example.com/a2a",
                    "token": "notification-token",
                    "authentication": {
                        "scheme": "bearer",
                        "credentials": "callback-token",
                    },
                },
            },
        )
        response.raise_for_status()
        print(response.json())


if __name__ == "__main__":
    asyncio.run(main())
```

## 处理 iac-code 元数据

工具和用量事件会到达 `TaskStatusUpdateEvent.metadata.iac_code`。

```python
from typing import Any


def handle_iac_metadata(metadata: dict[str, Any]) -> None:
    iac = metadata.get("iac_code", {})

    if tool := iac.get("tool"):
        status = tool.get("status")
        tool_id = tool.get("toolUseId")
        name = tool.get("name", "<unknown>")
        print(f"tool={name} id={tool_id} status={status}")

    if permission := iac.get("permission"):
        if permission.get("autoApproved") is False:
            print(f"permission rejected for {permission.get('toolName')}")

    if usage := iac.get("usage"):
        print(
            "tokens="
            f"{usage.get('inputTokens', 0)}+{usage.get('outputTokens', 0)}"
            f"={usage.get('totalTokens', 0)}"
        )
```

## 常见问题

| 现象 | 修复方式 |
|---------|-----|
| HTTP `401` | 在 Agent Card 和 JSON-RPC 请求中都包含已配置的 auth scheme，例如 `Authorization: Bearer <token>`、Basic auth 或 `X-API-Key: <key>` |
| `Invalid A2A workspace metadata.` | 在 `metadata.iac_code.cwd` 中使用已存在的绝对路径 |
| `A2A server currently accepts text input only.` | 至少发送一个非空 text part |
| `Task is already working.` | 等待当前轮次完成后，再在同一 context 中发送另一条消息 |
| 后续消息因不同工作区被拒绝 | 对复用的 `contextId` 保持 `metadata.iac_code.cwd` 不变 |
| 本地 file URL 被拒绝 | 将 `file://` parts 保持在 `metadata.iac_code.cwd` 和 `IACCODE_A2A_ALLOWED_CWDS` 内 |
| Push callback 被拒绝 | 使用不是 localhost 且不是字面量 private/local IP address 的 HTTP(S) callback URL |
| Redis push queue 启动失败 | 安装 `a2a-redis` extra，并在 A2A 配置中提供 `push-redis-url` |
