---
sidebar_position: 1
title: MCP 集成
description: 使用 Model Context Protocol 服务器为 IaC Code 扩展外部工具、资源、提示和技能。
---

# MCP 集成

IaC Code 可以作为 Model Context Protocol (MCP) host 使用。MCP servers 会在 IaC Code 的 permission、session、logging 和 output handling 路径内，为 agent 扩展外部 tools、resources、prompts 和 reusable skills。

当您希望 IaC Code 调用产品中未内置的本地或远程功能时，例如私有模板目录、内部部署审核器、库存查询服务或专门的云操作工具，请使用 MCP。

## Supported Surfaces

| Surface | MCP support |
|---|---|
|交互式 REPL |加载用户、本地和批准的项目服务器。在信任新项目`.mcp.json`服务器之前进行提示。 |
|非交互模式|加载用户、本地和批准的项目服务器。从不提示；待处理的项目服务器将被跳过并带有警告。 |
| ACP服务器|接受来自 ACP 客户端的会话 MCP 服务器配置，并公开该会话内发现的 MCP 功能。 |
| A2A服务器|通过正常运行时加载 MCP，并可以在 A2A 任务元数据中发布 MCP 警告和工具进度。 |
|管道模式|使用与正常模式相同的运行时集成，包括 MCP 工具进度和警告传播。 |

## Supported Capabilities

| Capability | Status |
|---|---|
| `stdio` 传输 |支持本地 MCP 服务器进程。 |
|流式 HTTP 传输 |支持远程 MCP 服务器。 |
|上交所运输 |支持远程 MCP 服务器。 |
| MCP 工具 |公开为名为`mcp__<server>__<tool>`的代理工具。 |
| MCP 资源 |通过`list_mcp_resources`和`read_mcp_resource`公开。 |
| MCP提示|公开为名为`mcp__<server>__<prompt>`的斜杠命令。 |
| MCP `skill://` 资源 |公开为名为`mcp__<server>__<skill>`的技能命令。 |
| OAuth 环回身份验证 |支持具有 OAuth 元数据的远程服务器。 |
| `roots/list` |支持。 IaC 代码将活动工作区根作为文件 URI 返回。 |
| `list_changed` 通知 |支持工具、资源和提示。注册动态刷新。 |
| MCP elicitation | 在交互式 session 中支持。非交互运行会安全取消。URL elicitation 可在用户确认后重试原始 tool call。 |
| WebSocket transport | 支持仅包含 URL 的 `ws://` 和 `wss://` server。由于已安装 SDK transport 只接受 URL，WebSocket 会拒绝 headers、`headersHelper` 和 OAuth。 |
| 动态 `headersHelper` 命令 | 对受信任的 `http` 和 `sse` server 支持。helper 不经过 shell，使用有界超时、最小环境和脱敏诊断。 |
| SDK 和 IDE 传输 |不支持。 |
| IaC 代码作为 MCP 服务器 |不支持。 IaC Code 目前仅充当 MCP 主机。 |

## How It Works

At runtime IaC Code:

1. 从用户、项目、本地和会话来源加载 MCP 配置。
2. 展开 `${VAR}` 和 `${VAR:-default}` 引用。
3. 通过用户可见 warning 跳过不安全或无效的 server。
4. 以有界并发连接已批准的 server。
5. 发现 tools、resources、prompts 和 `skill://` resources。
6. 将这些能力注册到现有 tool registry 和 command registry。
7. 将已连接 server 的 instructions 作为 server-scoped guidance 注入 agent prompt。
8. 将 MCP tool result 转换为普通 IaC Code tool result，并把二进制 artifact 和大文本 artifact 存到运行时配置目录下。
9. 在 REPL、headless run、ACP session 或 A2A runtime 关闭时断开 MCP client。

一台发生故障的 MCP 服务器不会阻止其他已配置的服务器。连接和发现失败作为 MCP 警告保持可见。

## Naming

MCP 工具和命令被标准化为公共名称：

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

字母、数字和下划线之外的字符将变为下划线。如果两个发现的功能在规范化后发生冲突，IaC 代码会附加一个简短的摘要以保持名称的唯一性。

对于 MCP 技能，IaC 代码还会注册一个兼容性别名，例如`<server>:<skill>`（当该别名与现有命令不冲突时）。即使公共名称已规范化，诊断也会保留原始服务器、工具、提示或技能名称。

## Related Pages

- [MCP 快速开始](./quick-start.md)
- [MCP配置](./configuration.md)
- [工具、资源、提示和技能](./capabilities.md)
- [OAuth 和安全](./oauth-and-security.md)
- [疑难解答](./troubleshooting.md)
