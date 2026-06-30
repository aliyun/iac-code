---
sidebar_position: 3
title: 工具、资源、提示和技能
description: 了解 MCP 能力如何出现在 IaC Code 中。
---

# 工具、资源、提示和技能

已连接的 MCP server 可以向 IaC Code 暴露四类能力。

## Tools

每个 MCP tool 都会变成一个 IaC Code tool：

```text
mcp__<server>__<tool>
```

Tool description 和 JSON input schema 来自 MCP server。IaC Code 会把模型的 tool input 转发给 MCP server，然后把 MCP content blocks 转换为普通 tool result。

IaC Code 会尽可能遵循 MCP tool annotations：

| MCP annotation | IaC Code 行为 |
|---|---|
| `readOnlyHint: true` | tool 被视为只读且并发安全。 |
| `destructiveHint: true` | tool 在权限决策中被视为破坏性操作。 |

MCP tools 仍然经过 IaC Code 现有权限系统。可以使用普通 `permissions` settings 或 CLI 参数配置权限策略，例如 `--allowed-tools`、`--disallowed-tools` 和 `--permission-mode`。

MCP progress notifications 会展示在交互式渲染、headless progress 输出、ACP tool progress updates 和 A2A tool metadata 中。

## Tool Results 和 Artifacts

IaC Code 会把 MCP content blocks 转换为模型可见文本：

| MCP content | IaC Code result |
|---|---|
| Text content | 直接包含在 tool result 中。 |
| `structuredContent` | 在 structured-content section 下渲染为格式化 JSON。 |
| Text resources | 带 server 和 URI 来源信息渲染。 |
| `resource_link` | 渲染为带 URI 和 MIME type 的 resource link。 |
| Image、audio 和 blob data | 存为私有 artifact 文件，并以 artifact id 引用。 |

二进制 artifacts 存储在：

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

模型看到的是 artifact id 和 metadata，不是 raw base64 data。

## Resources

只要有一个已连接 server 暴露 resources，IaC Code 就会注册两个全局 tools：

| Tool | 用途 |
|---|---|
| `list_mcp_resources` | 列出已连接 MCP servers 的 resources。可按 server name 过滤。 |
| `read_mcp_resource` | 通过 `server` 和 `uri` 读取一个 resource。 |

Resource 行包含 server name、URI、可选 resource name 和可选 MIME type。

## Prompts

MCP prompts 会变成 slash commands：

```text
/mcp__<server>__<prompt> key=value
```

调用时，IaC Code 会执行 MCP `prompts/get`，渲染返回的 prompt messages，把渲染后的 prompt 注入对话，然后让模型继续。Prompt arguments 可以这样传：

```text
template_name=prod-vpc region=cn-hangzhou
```

也可以使用 JSON：

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Required prompt arguments 会在 MCP 调用前校验。支持 quoted values，也支持包含反斜杠的 Windows paths。

## Skills

带 `skill://` URI 的 MCP resources 会变成 skill commands：

```text
$mcp__<server>__<skill>
```

IaC Code 会读取 remote skill resource，解析 frontmatter，并注册为普通 skill command。Remote MCP skills 有安全限制：

- Remote `allowed_tools` 会被清空。
- Remote auto-trigger path rules 会被清空。
- Remote skill body 和 description 有长度上限。
- 如果 remote skill 与现有 command 冲突，会被跳过并产生 MCP warning。

MCP skill resources 可能会在启动阶段被读取，这样用户调用前 command 就已经注册好。

## 动态更新

如果 MCP server 发送 `tools/list_changed`、`resources/list_changed` 或 `prompts/list_changed`，IaC Code 会刷新受影响的 capability list，并更新 tool 或 command registry。刷新失败会报告为 MCP warning，不会中断当前会话。
