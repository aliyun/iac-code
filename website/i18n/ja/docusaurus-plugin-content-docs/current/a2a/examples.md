---
title: 例
description: iac-code A2A サーバーと統合するための実用例。
sidebar_position: 6
---

# 例

このページでは、すぐに使える A2A 統合例を提供します。

## 前提条件

これらの例は次を前提としています。

| 依存関係 | バージョン | 用途 |
|------------|---------|---------|
| Python | `3.12` | プロジェクトランタイムに一致 |
| `a2a-sdk` | `>=1.0.2,<2` | A2A クライアントと protobuf 型 |
| `httpx` | `>=0.27.0` | SDK と直接例で使用される HTTP クライアント |
| `iac-code` | current repo | `iac-code a2a` サブコマンドを提供 |

サーバーを起動します。

```bash
uv sync --extra a2a
iac-code a2a --host 127.0.0.1 --port 41242
```

## Python SDK — ストリーミングセッション

この例は Agent Card を発見し、メッセージを送信し、アシスタントのテキストチャンクを出力し、ツールメタデータを報告します。

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

## CLI — エンドツーエンドワークフロー

永続化、アーティファクト、プッシュ通知サポート、署名済み Agent Card を備えたローカルサーバーを起動します。

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

安定したエンドポイントとカード検証設定のクライアント設定を作成します。

```yaml
url: http://127.0.0.1:41242/
verify-card-secret: local-card-signing-secret
require-card-signature: true
```

Agent Card を発見して検証します。

```bash
iac-code a2a-client --config a2a-client.yml discover
```

ストリーミングリクエストを送信します。

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --stream
```

タスクを一覧表示し、1 つのタスクを JSON として取得します。

```bash
iac-code a2a-client --config a2a-client.yml task-list --output table

iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

タスクのプッシュコールバックを登録します。

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

ルーティングされたエージェントを呼び出す前に、ルート選択をプレビューします。

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

## Python SDK — フォローアップメッセージ

フォローアップメッセージは同じ `context_id` と、通常は同じタスク ID を再利用します。これにより、内部 iac-code ランタイムと会話履歴が維持されます。

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

サーバーは再利用された `contextId` を、新しいメッセージが別のワークスペースを指している場合に拒否します。

## Python SDK — タスクをキャンセルする

```python
from a2a.types import CancelTaskRequest


async def cancel_running_task(client, task_id: str) -> None:
    task = await client.cancel_task(CancelTaskRequest(id=task_id))
    print(f"{task.id}: {task.status.state}")
```

## 直接 HTTP — 最小 JSON-RPC クライアント

呼び出し側に SDK 依存関係を持たせたくない場合に使用します。

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

## 直接 HTTP — ストリーミング SSE

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

## 直接 HTTP — プッシュ通知設定

プッシュ設定メソッドは、サーバーが `push-notifications: true` で実行されている場合に利用できます。

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

## iac-code メタデータの処理

ツールと使用量イベントは `TaskStatusUpdateEvent.metadata.iac_code` に届きます。

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

## よくある落とし穴

| 症状 | 修正 |
|---------|-----|
| HTTP `401` | Agent Card と JSON-RPC リクエストの両方で、`Authorization: Bearer <token>`、Basic auth、または `X-API-Key: <key>` などの設定済み認証方式を含める |
| `Invalid A2A workspace metadata.` | `metadata.iac_code.cwd` に既存の絶対パスを使用する |
| `A2A server currently accepts text input only.` | 少なくとも 1 つの空でないテキストパーツを送信する |
| `Task is already working.` | 同じコンテキストで別のメッセージを送信する前に、現在のターンの完了を待つ |
| フォローアップが別ワークスペースとして拒否される | `metadata.iac_code.cwd` を、再利用する `contextId` では変更しない |
| ローカルファイル URL が拒否される | `file://` パーツを `metadata.iac_code.cwd` 内かつ `IACCODE_A2A_ALLOWED_CWDS` 内に保つ |
| プッシュコールバックが拒否される | localhost またはリテラルな private/local IP アドレスではない HTTP(S) コールバック URL を使用する |
| Redis プッシュキューの起動に失敗する | `a2a-redis` extra をインストールし、A2A 設定で `push-redis-url` を指定する |
