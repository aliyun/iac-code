---
sidebar_position: 3
title: 工具、资源、提示和技能
description: 了解 MCP 能力如何出现在 IaC Code 中。
---

# 工具、资源、提示和技能

已连接的 MCP servers 会向 IaC Code 暴露四类 capabilities。

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

工具描述和 JSON 输入模式来自 MCP 服务器。 IaC 代码将模型的工具输入转发到 MCP 服务器，然后将 MCP 内容块转换为正常的工具结果。

权限提示和审计元数据包括MCP服务器名称、原始工具名称、公共规范化工具名称和只读/破坏性注释。

MCP 工具注释在可能的情况下受到尊重：

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` | 该工具被视为只读且并发安全的。 |
| `destructiveHint: true` | 该工具被视为对权限决策具有破坏性。 |

MCP工具仍然通过IaC Code现有的权限系统。使用正常的 `permissions` 设置或 CLI 标志（例如 `--allowed-tools`、`--disallowed-tools` 和 `--permission-mode`）配置权限策略。

MCP 进度通知出现在交互式渲染、无头进度输出、ACP 工具进度更新和 A2A 工具元数据中。

## Tool Results and Artifacts

IaC 代码将 MCP 内容块转换为模型可见文本：

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result when small; 大文本会保存为私有 `.txt`、`.json` 或 `.md` artifact. |
| `structuredContent` | 在结构化内容部分下呈现为格式化 JSON。 |
| 文本资源 | 使用服务器和 URI 来源进行渲染。 |
| `resource_link` | 呈现为具有 URI 和 MIME 类型的资源链接。 |
| 图像、音频和 Blob 数据 | 存储为私有工件文件并由工件 ID 引用。 |

对于 v2 会话，二进制工件存储在会话拥有的 MCP 工具结果目录中：

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/<server>/<tool>/
```

没有受支持的布局标记的旧会话继续使用：

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data. 大文本 artifact 会包含 path so the full output can be read without flooding the conversation.

## Resources

当任何连接的服务器公开资源时，IaC Code 会注册两个全局工具：

| Tool | Purpose |
|---|---|
| `list_mcp_resources` | 列出来自连接的 MCP 服务器的资源。可以选择按服务器名称进行过滤。 |
| `read_mcp_resource` | 通过 `server` 和 `uri` 读取一项资源。 |

资源行包括服务器名称、URI、可选资源名称和可选 MIME 类型。

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

调用时，IaC Code 调用 MCP `prompts/get`，呈现返回的提示消息，将呈现的提示注入对话中，并让模型继续。提示参数可以传递为：

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

在 MCP 调用之前验证所需的提示参数。支持带引号的值，包括带反斜杠的 Windows 路径。

## Skills

具有 `skill://` URI 的 MCP 资源成为技能命令：

```text
$mcp__<server>__<skill>
```

IaC Code读取远程技能资源，解析frontmatter，并将其注册为普通技能命令。远程 MCP 技能受到安全限制：

- Remote `allowed_tools` are cleared.
- 远程自动触发路径规则被清除。
- 远程技能主体和描述长度有限制。
- 如果远程技能与现有命令冲突，则会跳过该命令并显示 MCP 警告。

MCP 技能资源可以在启动期间读取，因此可以在用户调用命令之前注册该命令。

当没有命令冲突时，MCP技能还会获得一个兼容性别名：

```text
$<server>:<skill>
```

例如，`$mcp__yuque__search` 和 `$yuque:search` 可以解析为相同的远程技能。

## Server Instructions（服务器指令）

如果连接的服务器从初始化返回 `instructions`，IaC 代码会将它们作为专用 MCP 服务器指令部分注入到代理提示符中。这些说明被视为服务器范围的指导，不会覆盖本地项目说明。

## Elicitation（交互式请求）

交互式 session 可以把 MCP elicitation request 转给用户。URL 模式的 elicitation 可以要求用户完成外部 URL flow，然后在有界重试次数内重试原始 MCP tool call。非交互上下文会安全取消 elicitation。

## Dynamic Updates

如果 MCP 服务器发送 `tools/list_changed`、`resources/list_changed` 或 `prompts/list_changed`，IaC Code 会刷新受影响的功能列表并更新工具或命令注册表。刷新失败将报告为 MCP 警告，并且不会停止活动会话。
