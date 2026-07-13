---
sidebar_position: 5
title: MCP 故障排查
description: 诊断 MCP 配置、连接、认证和能力发现问题。
---

# MCP 故障排查

MCP warnings 通常不是 fatal；除非你需要的所有 capabilities 都不可用。某个 server 失败不应阻止其他 MCP servers 或内置 IaC Code tools 工作。

## Inspect Configuration

不连接服务器，查看已配置的 servers：

```bash
iac-code mcp list
```

对已配置的 servers 运行有界 health diagnostics：

```bash
iac-code mcp list --check
```

在不连接的情况下检查编辑后的服务器配置：

```bash
iac-code mcp get my-server --scope local
```

为一台服务器运行有界健康诊断：

```bash
iac-code mcp get my-server --scope local --check
```

显式检查配置，无需连接：

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

重新连接服务器或所有持久服务器：

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

Symptom:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

Fix:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

使用配置列表显示的精确 `--scope`；非默认持久化文件还要带上对应 `--source-path`。如果该 server 已被删除，
请重新 add，而不是对不存在的配置运行 auth。

## Pending Project Server

状态或 warning code: `pending_approval`.

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

或者在该项目中启动交互式 REPL，并在出现提示时回答“y”。按 Enter 表示`N`并拒绝服务器。

如果审批曾经有效但停止了，请检查`.mcp.json`是否发生更改。批准与配置签名相关。

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

缺少所需环境变量的服务器将被跳过。

## Connection Failed

状态或 warning code: `connection_failed`.

For stdio servers:

- Verify `command` exists on `PATH`.
- 从不同目录启动时使用脚本的绝对路径。
- 在 Windows 上，通过`cmd /c npx`运行基于节点的服务器。
- 检查是否配置了任何必需的环境变量。

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
- 确认静态标头存在并且不包含明文秘密。
- 如果服务器需要 OAuth，请运行 `iac-code mcp auth <server>`。

## Needs Authentication

Status: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

如果服务器使用 OAuth 刷新令牌并且需要重新身份验证，IaC 代码会清除过时的令牌并请求新的流程。

## OAuth Auth Failed

Symptom (`auth-failed`):

```text
MCP auth failed for 'name':
```

这表示 OAuth flow 已启动但没有正常完成：callback URL 可能不完整，authorization code 可能过期，或者
authorization server 返回了错误。新 flow 在完成前失败时，IaC Code 会恢复之前的 auth state。

Fix:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

先重试 `auth`；只有在保存的 token 或 dynamic client state 已失效时，才先运行 `reset-auth` 再重试。

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

IaC 代码清除该服务器存储的 OAuth 客户端和令牌状态。再次运行身份验证：

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

服务器请求额外的 OAuth 范围。在当前会话中，打开`/mcp`并选择`认证`或
该服务器的`重新认证`； IaC 代码包括该流程中服务器质询报告的范围。的
独立的`iac-code mcp auth name`命令启动正常的身份验证流程，并且不携带来自
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

请使用错误消息中打印的精确 `--scope` command 重新运行。这是 scope ambiguity：server name 有效，但命令需要一个持久化 scope。

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

服务器已连接，但一项功能列表失败。同一服务器的其他功能可能仍然有效。修复服务器端错误，然后重新启动 IaC 代码或触发重新连接/身份验证刷新。

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

对于重复失败，请检查远程服务器是否丢弃会话或重新启动。

## Headers Helper Failed

症状可能包括帮助程序解析错误、超时、非零退出状态、无效 JSON 或非字符串标头值。检查配置源目录中的帮助程序命令是否有效并打印 JSON 对象，例如：

```json
{"X-Org": "platform"}
```

类似秘密的 stderr 在诊断中被编辑。

## WebSocket Config Rejected

WebSocket MCP 服务器支持仅 URL 配置。从 `type: "ws"` 服务器中删除 `headers`、`headersHelper` 和 `oauth`。

## Resources Are Missing

仅当至少一台连接的服务器公开资源时才注册`list_mcp_resources`。如果缺少该工具：

- Confirm the server connected.
- 确认服务器支持`resources/list`。
- 检查启动警告是否有资源发现错误。

## Prompt or Skill Command Missing

成功发现后才会出现提示和技能命令。检查：

- MCP 服务器上存在提示或 `skill://` 资源。
- 规范化的命令名称不会与内置命令冲突。
- 启动超时内可以读取远程技能资源。
- 技能描述和身体贴合 IaC 代码安全限制。

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

工具结果中的 MCP 二进制工件存储在 v2 会话的会话拥有的目录下：

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

不支持布局标记的旧版会话使用：

```text
<config-dir>/tool-results/<session-id>/mcp/
```

避免在未检查机密的情况下共享配置、日志或工件目录。
