---
title: サンプル集
description: iac-code ACP サーバーとの統合に使える実践的なコード例。
sidebar_position: 4
---

# サンプル集

このページでは、一般的な ACP 統合パターンのすぐに使えるコード例を提供します。

## 前提条件

このページのすべての例は、以下の環境で検証済みです：

| 依存関係 | バージョン | 用途 |
|------------|---------|---------|
| Python | `3.10` | モダンな型記法（`\|` ユニオン、`match/case` 文）を使用 |
| `agent-client-protocol` | `0.9.0` | 公式 ACP Python SDK（`acp` としてインポート） |
| `httpx` | `0.28.1` | HTTP+SSE の例で使用する非同期 HTTP クライアント |
| `iac-code` | 現在のリポジトリ | `spawn_agent_process` で使用する `iac-code acp` サブコマンドを提供 |

クライアント側の依存関係を [uv](https://docs.astral.sh/uv/) でインストールします：

```bash
# Create a Python 3.10 virtualenv managed by uv
uv venv --python 3.10
source .venv/bin/activate

# Install the pinned client-side dependencies into that venv
uv pip install "agent-client-protocol==0.9.0" "httpx>=0.28.1"
```

:::warning
`AllowedOutcome.outcome` と `DeniedOutcome.outcome` は SDK 0.9.0 以降、それぞれ `Literal['selected']` と `Literal['cancelled']` として型付けされています。それ以外の文字列を使用すると、構築時に `pydantic.ValidationError` が発生します。
:::

---

## Python SDK — 完全なセッションライフサイクル

`agent-client-protocol` Python SDK を使用した完全な例：

```python
"""Full iac-code ACP session lifecycle."""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    """ACP client with streaming output."""

    async def session_update(
        self,
        session_id: str,
        update: (
            acp.schema.AgentMessageChunk
            | acp.schema.AgentThoughtChunk
            | acp.schema.ToolCallStart
            | acp.schema.ToolCallProgress
            | Any
        ),
        **kwargs: Any,
    ) -> None:
        match update:
            case acp.schema.AgentThoughtChunk():
                print(f"[thought] {update.content.text}", end="", flush=True)
            case acp.schema.AgentMessageChunk():
                print(f"{update.content.text}", end="", flush=True)
            case acp.schema.ToolCallStart():
                print(f"\n[tool] {update.title} (kind={update.kind})")
            case acp.schema.ToolCallProgress():
                status = update.status
                print(f"[tool] {update.tool_call_id} → {status}")

    async def request_permission(
        self, options, session_id, tool_call, **kwargs
    ) -> acp.RequestPermissionResponse:
        # Auto-approve for demonstration (use interactive approval in production)
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main():
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. Initialize
        resp = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Connected to {resp.agent_info.name} v{resp.agent_info.version}")

        # 2. Create session
        session = await conn.new_session(cwd="/path/to/project")
        sid = session.session_id
        # `models` is typed as Optional in the schema — guard against agents that don't report it.
        current_model = session.models.current_model_id if session.models else "<unknown>"
        print(f"Session: {sid}, model: {current_model}")

        # 3. Send prompt
        result = await conn.prompt(
            session_id=sid,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Create a VPC with two subnets using a ROS template",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. Close session
        await conn.close_session(session_id=sid)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Python SDK — 権限の処理

対話的な権限承認の実装：

```python
import acp
import acp.schema


class InteractiveClient(acp.Client):
    async def session_update(self, session_id, update, **kwargs):
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs
    ) -> acp.RequestPermissionResponse:
        print(f"\n⚠️  Permission request: {tool_call.title}")
        print(f"   Tool kind: {tool_call.kind}")

        # Show available options
        for opt in options:
            print(f"   [{opt.option_id}] {opt.name}")

        choice = input("Choose (allow_once/reject_once): ").strip()

        if choice.startswith("allow"):
            return acp.RequestPermissionResponse(
                outcome=acp.schema.AllowedOutcome(
                    outcome="selected",
                    option_id=choice,
                )
            )
        else:
            return acp.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled")
            )
```

:::tip
`InteractiveClient` は上記の `MyClient` と同じ方法で使用します — `spawn_agent_process` にはクラスではなく**インスタンス**を渡してください：

```python
async with acp.spawn_agent_process(InteractiveClient(), "iac-code", "acp") as (conn, _):
    ...
```

クラスを直接渡すと、`acp.Client` が位置引数を受け付けない `typing.Protocol` であるため、`TypeError: __init__() takes exactly one argument` が発生します。
:::

**環境別の権限戦略：**

| 環境 | 戦略 |
|-------------|----------|
| 開発 | すべて自動許可 |
| 本番 | 書き込み/実行ツールには対話的な承認 |
| CI/CD | 読み取り専用を許可、書き込み/実行を拒否 |

---

## Python SDK — ストリーミングイベント

さまざまなイベントタイプの詳細な処理：

```python
import acp
import acp.schema


class StreamingClient(acp.Client):
    def __init__(self):
        self.tool_calls: dict[str, str] = {}  # tool_call_id → title

    async def session_update(self, session_id, update, **kwargs):
        match update:
            case acp.schema.AgentThoughtChunk():
                # Model's internal reasoning (dimmed in UI typically)
                print(f"  💭 {update.content.text}", end="", flush=True)

            case acp.schema.AgentMessageChunk():
                # Final response text shown to user
                print(update.content.text, end="", flush=True)

            case acp.schema.ToolCallStart():
                self.tool_calls[update.tool_call_id] = update.title
                print(f"\n  🔧 [{update.kind}] {update.title}")

            case acp.schema.ToolCallProgress():
                title = self.tool_calls.get(update.tool_call_id, "unknown")
                if update.status == "completed":
                    print(f"  ✅ {title} completed")
                elif update.status == "failed":
                    print(f"  ❌ {title} failed")
                    if update.raw_output:
                        print(f"     Error: {str(update.raw_output)[:200]}")
                else:
                    print(f"  ⏳ {title} in progress...")

            case acp.schema.UsageUpdate():
                # UsageUpdate reports context-window usage in tokens.
                # Fields: used (current context tokens), size (total window), cost (optional).
                print(f"\n  📊 Context: {update.used}/{update.size} tokens")

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )
```

:::tip
`StreamingClient` は内部状態を初期化するために引数なしの `__init__(self)` を定義しています。接続時には同様に**インスタンス**を渡してください — `spawn_agent_process(StreamingClient(), "iac-code", "acp")` — クラス自体を渡さないでください。
:::

---

## HTTP+SSE — 最小限のクライアント

Python SDK を使用できない環境では、HTTP+SSE で直接接続します：

```python
"""Minimal HTTP+SSE client using httpx."""

import asyncio
import httpx

BASE_URL = "http://127.0.0.1:8765"
HEADERS = {"Authorization": "Bearer YOUR_TOKEN"}


async def main():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # 1. Initialize — get connection ID
        resp = await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "http-client", "version": "1.0"},
                "clientCapabilities": {}
            }
        }, headers=HEADERS)
        resp.raise_for_status()
        conn_id = resp.headers["Acp-Connection-Id"]
        print(f"Connection ID: {conn_id}")

        session_headers = {**HEADERS, "Acp-Connection-Id": conn_id}

        # 2. Subscribe to SSE stream (background)
        async def listen_sse():
            async with client.stream("GET", "/acp", headers=session_headers) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data:"):
                        print(f"[SSE] {line[5:].strip()}")

        sse_task = asyncio.create_task(listen_sse())

        # 3. Create session (response arrives via SSE)
        resp = await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 2,
            "method": "session/new",
            "params": {"cwd": "/workspace"}
        }, headers=session_headers)
        # Returns 202 Accepted; actual result delivered via SSE

        await asyncio.sleep(2)  # Wait for session creation

        # 4. Send prompt
        await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": "<session-id-from-sse>",
                "prompt": [{"type": "text", "text": "List files in current directory"}]
            }
        }, headers=session_headers)

        await asyncio.sleep(10)  # Wait for streaming response
        sse_task.cancel()

        # 5. Close connection
        await client.request("DELETE", "/acp", headers=session_headers)
        print("Connection closed")


if __name__ == "__main__":
    asyncio.run(main())
```

**ポイント：**
- `POST /acp` で `method: "initialize"` を送信すると、レスポンスヘッダーに `Acp-Connection-Id` が返されます
- 以降のすべてのリクエストには `Authorization` と `Acp-Connection-Id` の両方のヘッダーが必要です
- `POST /acp` は `202 Accepted` を返します。実際のレスポンスは SSE ストリーム経由で配信されます
- `GET /acp` はサーバープッシュイベントを受信するための SSE ストリームを開きます
- `DELETE /acp` は接続を閉じてサーバーリソースを解放します

---

## セッション管理パターン

### 実験用のフォーク

既存のセッションからブランチを作成し、元のセッションに影響を与えずに異なるアプローチを試します：

```python
async def fork_and_experiment(conn, original_session_id: str, cwd: str):
    """Fork a session to experiment without affecting the original."""
    # Fork creates a copy with the same history
    forked = await conn.fork_session(
        session_id=original_session_id,
        cwd=cwd,
    )
    forked_sid = forked.session_id
    print(f"Forked session: {forked_sid}")

    # Experiment on the fork
    result = await conn.prompt(
        session_id=forked_sid,
        prompt=[acp.schema.TextContentBlock(
            type="text",
            text="Try an alternative approach: use Terraform instead of ROS",
        )],
    )

    # Close fork when done (original session is unaffected)
    await conn.close_session(session_id=forked_sid)
    return result
```

### 過去のセッションの読み込みと再開

前回のセッションを復元して続きから作業します：

```python
async def resume_previous_session(conn, cwd: str):
    """List sessions and resume the most recent one."""
    # List available sessions
    listing = await conn.list_sessions(cwd=cwd)

    if not listing.sessions:
        print("No previous sessions found")
        return None

    # Resume the first session
    target = listing.sessions[0]
    print(f"Resuming session: {target.session_id}")

    session = await conn.resume_session(
        session_id=target.session_id,
        cwd=cwd,
    )
    return session.session_id
```

### 並列マルチセッション

複数の独立したタスクを同時に実行します：

```python
async def parallel_tasks(conn, cwd: str, prompts: list[str]):
    """Run multiple prompts in parallel sessions."""
    sessions = []

    # Create sessions
    for _ in prompts:
        s = await conn.new_session(cwd=cwd)
        sessions.append(s.session_id)

    # Run prompts concurrently
    tasks = [
        conn.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text=text)],
        )
        for sid, text in zip(sessions, prompts)
    ]
    results = await asyncio.gather(*tasks)

    # Cleanup
    for sid in sessions:
        await conn.close_session(session_id=sid)

    return results


# Usage
# results = await parallel_tasks(conn, "/workspace", [
#     "Create a VPC template",
#     "Create a security group template",
#     "Create an ECS instance template",
# ])
```

:::warning
各セッションは LLM 接続を保持します。並列セッションが多すぎると API レート制限がトリガーされる可能性があります。
:::
