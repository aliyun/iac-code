---
sidebar_position: 2
title: MCP 配置
description: 在插件中管理 MCP 服务器；查询 CLI 命令、作用域和高级配置。
---

# MCP 配置

桌面版和 Web 通过**插件**中的 **MCP** 标签页添加、管理服务器。下面也保留 CLI 命令和配置文件说明，供自动化和高级配置使用。

MCP servers 配置在 `mcpServers` object 下。IaC Code 支持与 Claude Code 兼容的核心 schema，覆盖 `stdio`, `http`, `sse`, and URL-only `ws` servers。

## 快速开始

日常使用请按 [MCP 快速开始](./quick-start.md)在**插件 → MCP**中添加服务器。下文介绍持久化作用域、CLI 自动化及高级配置字段。

## Configuration Sources

IaC 代码从以下来源读取 MCP 服务器：

| 来源 | 范围 | 文件或入口点 | 信任模型 |
|---|---|---|---|
| 用户设置 | `user` | `~/.iac-code/settings.yml` 或 `IAC_CODE_CONFIG_DIR/settings.yml` | 受到当前用户的信任。 |
| 项目本地设置 | `local` | `<workspace>/.iac-code/settings.local.yml` | 本地结账专用。 |
| 项目MCP文件 | `project` | `<workspace>/.mcp.json` | 与项目共享并需要当地批准。 |
| ACP 会话配置 | `session` | ACP 客户端传递的 `mcpServers` | 仅适用于该 ACP 会话运行时。 |

优先级是用户、项目、本地，然后是会话。后面的源按服务器名称覆盖前面的源。等效配置也通过内容签名进行重复数据删除。

项目 `.mcp.json` 文件是从工作区根目录一直到当前目录发现的。子项目文件按服务器名称覆盖父文件。

## CLI Commands

使用 `iac-code mcp` 管理持久的 MCP 配置：

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

可以使用 Claude 风格的位置 URL 形式添加远程 HTTP 服务器：

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

SSE 和 WebSocket 服务器也使用同样的位置 URL 形式，并指定对应 transport：

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

对于 stdio server，请将 subprocess 命令放在 `--` 后。远程 HTTP server 不需要 wrapper：按上文用 `--transport http` 添加即可。

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
```

可用命令：

| 命令 | 用途 |
|---|---|
| `iac-code mcp add` | 从结构化 CLI 标志添加服务器。 |
| `iac-code mcp add-json` | 从 JSON 对象添加服务器。 |
| `iac-code mcp list` | 列出已配置 server、scope、transport 和审批状态，不进行连接。 |
| `iac-code mcp list --config-only` | 默认配置列表的 alias。 |
| `iac-code mcp list --check` | 短暂连接并显示有界 health diagnostics。 |
| `iac-code mcp get` | 无需连接即可打印一份经过编辑的服务器配置。 |
| `iac-code mcp get --config-only` | 无需连接即可打印一份经过编辑的服务器配置。 |
| `iac-code mcp get --check` | 短暂连接并显示一台服务器的有限运行状况诊断。 |
| `iac-code mcp remove` | 从持久范围中删除一台服务器。 |
| `iac-code mcp approve` | 批准项目 `.mcp.json` 服务器。 |
| `iac-code mcp reject` | 拒绝项目 `.mcp.json` 服务器。 |
| `iac-code mcp reset-project-choices` | 清除存储的项目批准选择。 |
| `iac-code mcp auth` | 启动服务器的 OAuth 身份验证。 |
| `iac-code mcp reset-auth` | 删除服务器存储的 OAuth 令牌和客户端密钥。 |
| `iac-code mcp reconnect` | 使用 `--all` 重新连接一台服务器或所有持久服务器。 |
| `iac-code mcp disable` | 禁用持久服务器而不编辑共享项目配置。 |
| `iac-code mcp enable` | 重新启用持久服务器。 |

## 命令选项

下表与 `iac-code mcp <command> --help` 的 option set 保持一致：

| 命令 | 选项 |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options；仅有 `--help`。 |
| `iac-code mcp reject` | No command-specific options；仅有 `--help`。 |
| `iac-code mcp reset-project-choices` | No command-specific options；仅有 `--help`。 |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

当省略 `--scope` 时，IaC Code 将写入项目内的 `local` 和项目外的 `user`。

对于在现有持久服务器上运行的命令，当省略 `--scope` 时，IaC 代码可以跨持久范围找到唯一的服务器。如果多个作用域中存在相同的名称，则该命令会失败，并使用精确的 `--scope` 命令来消除歧义。

## 管理 MCP 服务器

在 Web 或桌面版的 MCP 设置中，可以查看连接、工具、资源和提示词，认证远程服务器，审批项目服务器，以及重连、启用、禁用或删除已保存的服务器。

会话中的 `/mcp` 命令用于查看服务器状态。自动化场景请使用 `iac-code mcp <command>`，支持的操作见上方命令参考。

## Stdio Servers

Stdio servers launch a local command:

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["./tools/catalog_mcp.py"],
      "env": {
        "CATALOG_ENV": "prod"
      }
    }
  }
}
```

当 `command` 存在时，可以省略 `type` 字段。 IaC Code通过安全的继承环境加上服务器`env`。在 Windows 上，对于基于节点的服务器，更喜欢 `cmd /c npx` 而不是裸 `npx`。

## HTTP and SSE Servers

远程服务器需要 `type` 和 `url`：

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "${MCP_REVIEWER_TOKEN}"
      }
    }
  }
}
```

对 SSE 服务器使用 `type: "sse"`。 `KEY=VALUE` 或 `Name: Value` CLI 语法支持静态标头。

动态标头可以由 `headersHelper` 提供：

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "X-Org": "platform"
      },
      "headersHelper": "python ./scripts/mcp_headers.py"
    }
  }
}
```

helper 必须输出一个键和值都为字符串的 JSON object。动态标头会覆盖同名静态标头。IaC Code 运行 helper 时不经过 shell、不提供 stdin、只继承最小环境、以配置源目录作为 cwd、使用 5 秒超时，并对 stderr 诊断做脱敏。`headersHelper` 命令字符串不会展开环境变量；引用到的变量会放入 helper 环境，helper 需要自行读取。项目 `.mcp.json` 中的 helper 必须在项目审批后才会运行。

## WebSocket Servers

WebSocket servers use `type: "ws"`:

```json
{
  "mcpServers": {
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/mcp"
    }
  }
}
```

安装的 MCP SDK WebSocket 传输仅接受 URL。 IaC Code 拒绝同时设置 `headers`、`headersHelper` 或 `oauth` 的 WebSocket 配置。

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

没有默认值的缺失变量会产生 MCP warning，并跳过受影响的 server。环境变量展开会递归应用到 list 和 object 中的字符串，但 `headersHelper` 命令字符串除外；该字符串保持字面量，引用到的变量会通过 helper 环境传入。

不要将明文机密存储在标头或环境值中。使用环境变量引用或 OAuth 秘密存储。

## 项目审批

项目 `.mcp.json` 不会被自动信任。请在 Web 或桌面版的 MCP 设置中批准服务器，或在项目目录中运行：

```bash
iac-code mcp approve catalog
```

审批记录保存在本地，并绑定工作区、配置来源、服务器名称和配置签名；配置变更后需要重新审批。Headless、ACP 和 A2A 会跳过待审批的项目服务器并报告警告。

## Disabled Servers

`iac-code mcp disable <name>` 在 IaC 代码配置目录下存储私有禁用状态条目。对于项目范围的服务器，这不会改变共享的 `.mcp.json` 文件。禁用条目由范围、源文件、服务器名称和配置签名键入，因此更改服务器配置会使过时的禁用状态无效。
