---
sidebar_position: 5
title: MCP 排障
description: 诊断 MCP 配置、连接、认证和能力发现问题。
---

# MCP 排障

MCP warnings 通常不是致命问题，除非你需要的每项能力都不可用。一个 server 失败不应阻止其他 MCP servers 或 IaC Code 内置 tools 工作。

## 检查配置

列出已配置 servers：

```bash
iac-code mcp list
```

检查脱敏后的 server config：

```bash
iac-code mcp get my-server --scope local
```

移除错误 server：

```bash
iac-code mcp remove my-server --scope local
```

清除项目 approval choices：

```bash
iac-code mcp reset-project-choices
```

## 项目 Server 待批准

症状：

```text
Project MCP server 'name' is pending approval.
```

修复：

```bash
iac-code mcp approve name
```

或在该项目中启动交互式 REPL，并在提示时回答 `y`。直接回车表示 `N`，会拒绝 server。

如果之前 approval 可用但后来失效，请检查 `.mcp.json` 是否变化。Approval 绑定到 config signature。

## 缺少环境变量

症状：

```text
Environment variable 'TOKEN' is not set for MCP config.
```

选择一种修复方式：

```bash
export TOKEN=...
```

或使用默认值：

```json
"Authorization": "${TOKEN:-}"
```

缺少必需环境变量的 servers 会被跳过。

## 连接失败

对于 stdio servers：

- 确认 `command` 存在于 `PATH`。
- 从不同目录启动时，脚本请使用绝对路径。
- 在 Windows 上，通过 `cmd /c npx` 运行 Node-based servers。
- 检查是否配置了所有必需环境变量。

对于 HTTP 或 SSE servers：

- 确认 URL 和 transport type。
- 检查 TLS 和 proxy settings。
- 确认静态 headers 存在，且不包含 plaintext secrets。
- 如果 server 要求 OAuth，运行 `iac-code mcp auth <server>`。

## 需要认证

症状：

```text
MCP server 'name' requires authentication.
```

修复：

```bash
iac-code mcp auth name --scope user
```

如果 server 使用 OAuth refresh tokens 且需要重新认证，IaC Code 会清除过期 tokens 并要求新的认证流程。

## 能力发现失败

症状可能包括：

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

Server 已连接，但某个 capability list 失败。同一 server 的其他能力仍可能可用。修复 server 侧错误后，重启 IaC Code 或触发 reconnect/auth refresh。

## Resources 缺失

只有当至少一个已连接 server 暴露 resources 时，`list_mcp_resources` 才会注册。如果该 tool 缺失：

- 确认 server 已连接。
- 确认 server 支持 `resources/list`。
- 检查启动 warnings 是否有 resource discovery errors。

## Prompt 或 Skill Command 缺失

Prompt 和 skill commands 只有成功发现后才会出现。请检查：

- MCP server 上存在对应 prompt 或 `skill://` resource。
- 规范化后的 command name 没有与 built-in command 冲突。
- Remote skill resource 可以在启动超时时间内读取。
- Skill description 和 body 没有超过 IaC Code 安全限制。

## Logs 和 Artifacts

Runtime logs 默认位于：

```text
<config-dir>/logs/
```

设置 `IAC_CODE_LOG_DIR` 时使用该目录。

MCP tool results 产生的 binary artifacts 存储在：

```text
<config-dir>/tool-results/<session-id>/mcp/
```

共享 config、log 或 artifact directories 前，请先检查是否包含 secrets。
