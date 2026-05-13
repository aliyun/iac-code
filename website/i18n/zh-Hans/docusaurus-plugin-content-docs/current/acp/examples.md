---
title: 示例
description: 与 iac-code ACP 服务端集成的实用代码示例。
sidebar_position: 4
---

# 示例

本页提供常见 ACP 集成模式的即用代码示例。

## 环境准备

本页所有示例均已在以下环境中验证通过：

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | `3.10` | 使用了现代类型语法（`\|` 联合类型、`match/case` 语句） |
| `agent-client-protocol` | `0.9.0` | 官方 ACP Python SDK（导入名为 `acp`） |
| `httpx` | `0.28.1` | HTTP+SSE 示例使用的异步 HTTP 客户端 |
| `iac-code` | 当前仓库 | 提供 `spawn_agent_process` 依赖的 `iac-code acp` 子命令 |

使用 [uv](https://docs.astral.sh/uv/) 安装客户端依赖：

```bash
# 由 uv 创建并管理 Python 3.10 虚拟环境
uv venv --python 3.10
source .venv/bin/activate

# 将固定版本的客户端依赖安装到该 venv
uv pip install "agent-client-protocol==0.9.0" "httpx>=0.28.1"
```

:::warning
自 SDK 0.9.0 起，`AllowedOutcome.outcome` 和 `DeniedOutcome.outcome` 分别被声明为 `Literal['selected']` 和 `Literal['cancelled']`。传入其他字符串会在对象构造阶段抛出 `pydantic.ValidationError`。
:::

---

## Python SDK — 完整会话生命周期

使用 `agent-client-protocol` Python SDK 的完整示例：

```python
"""完整的 iac-code ACP 会话生命周期。"""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    """支持流式输出的 ACP 客户端。"""

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
        # 演示用途自动批准（生产环境请使用交互式审批）
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main():
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. 初始化
        resp = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Connected to {resp.agent_info.name} v{resp.agent_info.version}")

        # 2. 创建会话
        session = await conn.new_session(cwd="/path/to/project")
        sid = session.session_id
        # schema 中 `models` 为 Optional，针对不上报 model state 的 agent 做防御。
        current_model = session.models.current_model_id if session.models else "<unknown>"
        print(f"Session: {sid}, model: {current_model}")

        # 3. 发送提示
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

        # 4. 关闭会话
        await conn.close_session(session_id=sid)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Python SDK — 处理权限请求

实现交互式权限审批：

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
        print(f"\n⚠️  权限请求: {tool_call.title}")
        print(f"   工具类型: {tool_call.kind}")

        # 展示可选项
        for opt in options:
            print(f"   [{opt.option_id}] {opt.name}")

        choice = input("请选择 (allow_once/reject_once): ").strip()

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
`InteractiveClient` 的使用方式与上文的 `MyClient` 一致——向 `spawn_agent_process` 传入 **实例**，而非类本身：

```python
async with acp.spawn_agent_process(InteractiveClient(), "iac-code", "acp") as (conn, _):
    ...
```

直接传入类会触发 `TypeError: __init__() takes exactly one argument`，原因是 `acp.Client` 是 `typing.Protocol`，其默认 `__init__` 不接受位置参数。
:::

**不同环境的权限策略：**

| 环境 | 策略 |
|------|------|
| 开发环境 | 自动允许所有操作 |
| 生产环境 | 写入/执行类工具需交互式审批 |
| CI/CD | 允许只读操作，拒绝写入/执行 |

---

## Python SDK — 流式事件处理

对不同事件类型进行精细化处理：

```python
import acp
import acp.schema


class StreamingClient(acp.Client):
    def __init__(self):
        self.tool_calls: dict[str, str] = {}  # tool_call_id → title

    async def session_update(self, session_id, update, **kwargs):
        match update:
            case acp.schema.AgentThoughtChunk():
                # 模型的内部推理过程（通常在 UI 中以淡色显示）
                print(f"  💭 {update.content.text}", end="", flush=True)

            case acp.schema.AgentMessageChunk():
                # 展示给用户的最终响应文本
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
                # UsageUpdate 上报上下文窗口的 token 使用情况。
                # 字段：used（当前上下文 tokens）、size（总窗口大小）、cost（可选）。
                print(f"\n  📊 Context: {update.used}/{update.size} tokens")

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )
```

:::tip
`StreamingClient` 定义了无参数的 `__init__(self)` 用于初始化内部状态。连接时仍须传入 **实例** —— `spawn_agent_process(StreamingClient(), "iac-code", "acp")`，永远不要传类本身。
:::

---

## HTTP+SSE — 最小化客户端

在无法使用 Python SDK 的环境中，可以通过 HTTP+SSE 直接连接：

```python
"""使用 httpx 实现的最小化 HTTP+SSE 客户端。"""

import asyncio
import httpx

BASE_URL = "http://127.0.0.1:8765"
HEADERS = {"Authorization": "Bearer YOUR_TOKEN"}


async def main():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # 1. 初始化 — 获取连接 ID
        resp = await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "http-client", "version": "1.0"},
                "capabilities": {}
            }
        }, headers=HEADERS)
        resp.raise_for_status()
        conn_id = resp.headers["Acp-Connection-Id"]
        print(f"Connection ID: {conn_id}")

        session_headers = {**HEADERS, "Acp-Connection-Id": conn_id}

        # 2. 订阅 SSE 事件流（后台运行）
        async def listen_sse():
            async with client.stream("GET", "/acp", headers=session_headers) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data:"):
                        print(f"[SSE] {line[5:].strip()}")

        sse_task = asyncio.create_task(listen_sse())

        # 3. 创建会话（响应通过 SSE 返回）
        resp = await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 2,
            "method": "session/new",
            "params": {"cwd": "/workspace"}
        }, headers=session_headers)
        # 返回 202 Accepted；实际结果通过 SSE 推送

        await asyncio.sleep(2)  # 等待会话创建完成

        # 4. 发送提示
        await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": "<session-id-from-sse>",
                "prompt": [{"type": "text", "text": "List files in current directory"}]
            }
        }, headers=session_headers)

        await asyncio.sleep(10)  # 等待流式响应完成
        sse_task.cancel()

        # 5. 关闭连接
        await client.request("DELETE", "/acp", headers=session_headers)
        print("Connection closed")


if __name__ == "__main__":
    asyncio.run(main())
```

**要点说明：**
- `POST /acp` 配合 `method: "initialize"` 会在响应头中返回 `Acp-Connection-Id`
- 后续所有请求必须同时包含 `Authorization` 和 `Acp-Connection-Id` 请求头
- `POST /acp` 返回 `202 Accepted`；实际响应通过 SSE 事件流推送
- `GET /acp` 用于打开 SSE 事件流，接收服务端推送的事件
- `DELETE /acp` 关闭连接并释放服务端资源

---

## 会话管理模式

### 分叉进行实验

从现有会话创建分支，在不影响原始会话的情况下尝试不同方案：

```python
async def fork_and_experiment(conn, original_session_id: str, cwd: str):
    """分叉会话进行实验，不影响原始会话。"""
    # 分叉会创建一个拥有相同历史记录的副本
    forked = await conn.fork_session(
        session_id=original_session_id,
        cwd=cwd,
    )
    forked_sid = forked.session_id
    print(f"Forked session: {forked_sid}")

    # 在分叉上进行实验
    result = await conn.prompt(
        session_id=forked_sid,
        prompt=[acp.schema.TextContentBlock(
            type="text",
            text="Try an alternative approach: use Terraform instead of ROS",
        )],
    )

    # 实验完成后关闭分叉（原始会话不受影响）
    await conn.close_session(session_id=forked_sid)
    return result
```

### 加载并恢复历史会话

恢复之前的会话，从上次中断的地方继续：

```python
async def resume_previous_session(conn, cwd: str):
    """列出会话并恢复最近的一个。"""
    # 列出可用会话
    listing = await conn.list_sessions(cwd=cwd)

    if not listing.sessions:
        print("No previous sessions found")
        return None

    # 恢复第一个会话
    target = listing.sessions[0]
    print(f"Resuming session: {target.session_id}")

    session = await conn.resume_session(
        session_id=target.session_id,
        cwd=cwd,
    )
    return session.session_id
```

### 并行多会话

同时运行多个独立任务：

```python
async def parallel_tasks(conn, cwd: str, prompts: list[str]):
    """在并行会话中运行多个提示。"""
    sessions = []

    # 创建会话
    for _ in prompts:
        s = await conn.new_session(cwd=cwd)
        sessions.append(s.session_id)

    # 并发运行提示
    tasks = [
        conn.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text=text)],
        )
        for sid, text in zip(sessions, prompts)
    ]
    results = await asyncio.gather(*tasks)

    # 清理资源
    for sid in sessions:
        await conn.close_session(session_id=sid)

    return results


# 用法示例
# results = await parallel_tasks(conn, "/workspace", [
#     "Create a VPC template",
#     "Create a security group template",
#     "Create an ECS instance template",
# ])
```

:::warning
每个会话都会占用一个 LLM 连接。并行运行过多会话可能会触发 API 速率限制。
:::
