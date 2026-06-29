---
sidebar_position: 1
title: MCP 集成
description: 使用 Model Context Protocol 服务器为 IaC Code 扩展外部工具、资源、提示和技能。
---

# MCP 集成

IaC Code 可以作为 Model Context Protocol (MCP) host 运行。MCP 服务器可以为 agent 扩展外部工具、资源、提示和可复用技能，同时仍然经过 IaC Code 的权限、会话、日志和输出处理链路。

当你希望 IaC Code 调用产品内置能力之外的本地或远程能力时，可以使用 MCP，例如私有模板目录、内部部署审查器、资产查询服务或专用云操作工具。

## 支持的入口

| 入口 | MCP 支持 |
|---|---|
| 交互式 REPL | 加载用户、本地和已批准的项目服务器。信任新的项目 `.mcp.json` 服务器前会提示确认。 |
| 非交互模式 | 加载用户、本地和已批准的项目服务器。不会提示；待批准的项目服务器会被跳过并产生 warning。 |
| ACP server | 接收 ACP client 在会话中传入的 MCP server 配置，并在该会话中暴露发现到的 MCP 能力。 |
| A2A server | 通过普通 runtime 加载 MCP，并可在 A2A task metadata 中发布 MCP warning 和工具进度。 |
| Pipeline 模式 | 使用与 normal 模式相同的 runtime 集成，包括 MCP 工具进度和 warning 传播。 |

## 支持的能力

| 能力 | 状态 |
|---|---|
| `stdio` transport | 支持本地 MCP server 进程。 |
| Streamable HTTP transport | 支持远程 MCP server。 |
| SSE transport | 支持远程 MCP server。 |
| MCP tools | 作为 agent 工具暴露，名称为 `mcp__<server>__<tool>`。 |
| MCP resources | 通过 `list_mcp_resources` 和 `read_mcp_resource` 暴露。 |
| MCP prompts | 作为 slash command 暴露，名称为 `mcp__<server>__<prompt>`。 |
| MCP `skill://` resources | 作为 skill command 暴露，名称为 `mcp__<server>__<skill>`。 |
| OAuth loopback auth | 支持带 OAuth metadata 的远程服务器。 |
| `roots/list` | 支持。IaC Code 返回当前 workspace root 的 file URI。 |
| `list_changed` notifications | 支持 tools、resources 和 prompts。注册信息会动态刷新。 |
| MCP elicitation | 暂不支持。请求 elicitation 的服务器会收到明确的不支持错误。 |
| WebSocket、SDK、IDE transports | 不支持。 |
| 动态 `headersHelper` commands | 不支持。请使用静态 headers 或环境变量引用。 |
| IaC Code 作为 MCP server | 不支持。当前 IaC Code 只作为 MCP host。 |

## 工作方式

运行时 IaC Code 会：

1. 从用户、本地、项目和会话来源加载 MCP 配置。
2. 展开 `${VAR}` 和 `${VAR:-default}` 引用。
3. 通过用户可见 warning 跳过不安全或无效的 server。
4. 以有界并发连接已批准的 server。
5. 发现 tools、resources、prompts 和 `skill://` resources。
6. 将这些能力注册到现有 tool registry 和 command registry。
7. 将 MCP tool result 转换为普通 IaC Code tool result，并把二进制 artifact 存到运行时配置目录下。
8. 在 REPL、headless run、ACP session 或 A2A runtime 关闭时断开 MCP client。

单个 MCP server 失败不会阻塞其他已配置 server。连接和发现失败会作为 MCP warning 保持可见。

## 命名

MCP tools 和 commands 会被规范化为公开名称：

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

字母、数字和下划线之外的字符会变成下划线。如果发现到的能力在规范化后重名，IaC Code 会追加短摘要以保持名称唯一。

## 相关页面

- [MCP 配置](./configuration.md)
- [工具、资源、提示和技能](./capabilities.md)
- [OAuth 和安全](./oauth-and-security.md)
- [排障](./troubleshooting.md)
