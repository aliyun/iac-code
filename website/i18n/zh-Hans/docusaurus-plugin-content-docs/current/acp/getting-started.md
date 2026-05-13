---
sidebar_position: 2
title: 快速开始
description: 启动 ACP Server 并连接你的第一个客户端。
---

# ACP 快速开始

## 前置条件

1. **已安装 iac-code** — 参见 [安装](../getting-started/installation.md) 文档。

2. **已配置 LLM 凭证** — 参见 [认证](../configuration/authentication.md) 文档，通过 `/auth` 命令设置模型提供商凭证。

3. **Python ACP SDK**（可选，用于编程式客户端）

   官方 Python SDK 在 PyPI 上的发行包名为 **`agent-client-protocol`**（导入路径为 `acp`）。本页示例基于版本 `0.9.0` 验证：

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## 启动 ACP Server

### Stdio 模式（默认）

```bash
iac-code acp
```

Server 通过 stdin/stdout 使用 JSON-RPC 进行通信。当 IDE 将 iac-code 作为子进程启动时，使用的就是这种模式。

### HTTP+SSE 模式

```bash
iac-code acp --transport http --port 8765
```

在指定端口监听。客户端通过 HTTP 发送请求，并通过 Server-Sent Events 接收流式更新。适用于远程或多客户端场景。

你可以通过设置 `IACCODE_ACP_HTTP_TOKEN` 环境变量来保护 HTTP 端点 — Server 将要求客户端提供匹配的 `Authorization: Bearer <token>` 请求头。

### 验证是否正常工作

```bash
# Stdio：进程应启动并在 stdin 上等待 JSON-RPC 输入
iac-code acp

# HTTP：检查健康检查端点
curl http://127.0.0.1:8765/health
```

## 最小示例

使用官方 `agent-client-protocol` SDK 的最小 Python 示例。更完整的用法（工具调用渲染、思考片段、HTTP+SSE 传输层等）请见 [示例](./examples.md)。

```python
"""基于 agent-client-protocol==0.9.0 的 iac-code ACP 最小客户端。"""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        # 将助手的文本流输出到 stdout；本最小示例忽略其他 update 种类。
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        # 演示用途：自动批准。生产环境请改用交互式批准。
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main() -> None:
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. 初始化 — 协商能力
        init_result = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Protocol version: {init_result.protocol_version}")

        # 2. 创建绑定到项目目录的会话
        session = await conn.new_session(cwd="/path/to/project")
        print(f"Session ID: {session.session_id}")

        # 3. 发送提示；流式输出通过 MyClient.session_update 回调交付
        result = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Generate a VPC template with 2 VSwitches",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. 清理资源
        await conn.close_session(session_id=session.session_id)


asyncio.run(main())
```

要点说明：

- `acp.spawn_agent_process` 将 `iac-code acp` 作为子进程启动，并管理其 stdio 生命周期。
- `new_session(cwd=...)` 将文件操作限定在指定目录内。
- 流式更新（文本片段、思考片段、工具调用）通过你的 `acp.Client` 子类上的 `session_update` 回调推送；`prompt()` 本身在本轮对话结束后一次性返回一个包含最终 `stop_reason` 的 `PromptResponse`。
- 当收到权限请求时，`request_permission` 必须返回 `AllowedOutcome(outcome="selected", option_id=...)` 或 `DeniedOutcome(outcome="cancelled")`——传入其他值会触发 `pydantic.ValidationError`。

## 客户端配置

iac-code 可与任何兼容 ACP 的编辑器或客户端配合使用。以下配置同时适用于 **Zed** 和 **VSCode**：

```json
{
  "agent_servers": {
    "iac-code": {
      "type": "custom",
      "command": "iac-code",
      "args": ["acp"]
    }
  }
}
```

- **Zed** — 将上述片段添加到 Zed 的 `settings.json` 中。Zed 原生支持 ACP 代理服务器。
- **VSCode** — 需先安装 ACP 客户端扩展（任何支持 Agent Client Protocol 的扩展），然后在该扩展的设置中应用相同的配置格式。

## 下一步

- [协议参考](./protocol-reference.md) — 完整的方法和事件文档
- [HTTP+SSE 传输层](./http-transport.md) — 远程部署与 Token 认证
