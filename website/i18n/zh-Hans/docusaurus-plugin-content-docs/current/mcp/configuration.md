---
sidebar_position: 2
title: MCP 配置
description: 通过 CLI 命令、settings 文件、项目文件和 ACP 会话配置 MCP server。
---

# MCP 配置

MCP server 配置在 `mcpServers` 对象下。IaC Code 支持与 Claude Code 兼容的核心 schema，覆盖 `stdio`、`http` 和 `sse` servers。

## 配置来源

IaC Code 会从这些来源读取 MCP servers：

| 来源 | Scope | 文件或入口 | 信任模型 |
|---|---|---|---|
| 用户 settings | `user` | `~/.iac-code/settings.yml` 或 `IAC_CODE_CONFIG_DIR/settings.yml` | 由当前用户信任。 |
| 项目本地 settings | `local` | `<workspace>/.iac-code/settings.local.yml` | 仅当前本地 checkout 私有。 |
| 项目 MCP 文件 | `project` | `<workspace>/.mcp.json` | 随项目共享，需要本地批准。 |
| ACP 会话配置 | `session` | ACP client 传入的 `mcp_servers` | 只作用于该 ACP session runtime。 |

优先级为 user、project、local、session。后面的来源会按 server name 覆盖前面的来源。内容等价的配置还会按 content signature 去重。

项目 `.mcp.json` 文件会从 workspace root 向当前目录发现。子项目文件会按 server name 覆盖父级文件。

## CLI 命令

使用 `iac-code mcp` 管理持久化 MCP 配置：

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --type http \
  --url https://mcp.example.com/mcp \
  --header "Authorization=${MCP_REVIEWER_TOKEN}"
```

可用命令：

| 命令 | 用途 |
|---|---|
| `iac-code mcp add` | 通过结构化 CLI 参数添加 server。 |
| `iac-code mcp add-json` | 通过 JSON object 添加 server。 |
| `iac-code mcp list` | 列出已配置 server、scope、transport 和 approval 状态。 |
| `iac-code mcp get` | 打印一个已脱敏的 server config。 |
| `iac-code mcp remove` | 从持久化 scope 中移除一个 server。 |
| `iac-code mcp approve` | 批准一个项目 `.mcp.json` server。 |
| `iac-code mcp reject` | 拒绝一个项目 `.mcp.json` server。 |
| `iac-code mcp reset-project-choices` | 清除已保存的项目 approval 选择。 |
| `iac-code mcp auth` | 为 server 启动 OAuth 认证。 |
| `iac-code mcp reset-auth` | 删除 server 已保存的 OAuth tokens 和 client secret。 |

省略 `--scope` 时，IaC Code 在项目内写入 `local`，在项目外写入 `user`。

## Stdio Servers

Stdio servers 会启动本地命令：

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

存在 `command` 时可以省略 `type` 字段。IaC Code 会传入安全的继承环境以及 server 自己的 `env`。在 Windows 上，Node-based server 建议使用 `cmd /c npx`，不要直接使用裸 `npx`。

## HTTP 和 SSE Servers

远程 server 需要 `type` 和 `url`：

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

SSE server 使用 `type: "sse"`。支持静态 headers。动态 `headersHelper` commands 会被拒绝，因为它需要独立的可信执行设计。

## 环境变量展开

字符串值支持：

```text
${VAR}
${VAR:-default-value}
```

没有默认值的缺失变量会产生 MCP warning，受影响的 server 会被跳过。环境变量展开会递归应用到 list 和 object 中的字符串。

不要把 plaintext secrets 存在 headers 或 env 值里。请使用环境变量引用或 OAuth secret storage。

## 项目批准

项目 `.mcp.json` 可以提交到仓库，因此 IaC Code 不会自动信任它。

交互式 REPL 启动时会询问：

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

直接回车会采用默认 `N`，拒绝这个项目 server config。输入 `y` 或 `yes` 才会批准。Approval 保存在本地 IaC Code config 目录下，并包含 workspace path、project file path、server name 和 config signature。如果 `.mcp.json` 中的 server config 改变，approval 会失效，server 会重新变成 pending。

Headless、ACP 和 A2A 启动时不会进行交互式 approval 询问。未批准的项目 servers 会被跳过并报告 warning。
