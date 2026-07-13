---
sidebar_position: 2
title: MCP 快速开始
description: 使用 Claude 风格命令添加远程 HTTP MCP server 或 stdio wrapper。
---

# MCP 快速开始

当你已经有 MCP server URL 或本地 stdio wrapper 命令时，优先使用这条主路径。

## 远程 HTTP Server

使用位置 URL 形式添加远程 server：

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

如果 server 使用 OAuth，请启动授权流程：

```bash
iac-code mcp auth yuque
```

授权完成后运行有界 health check：

```bash
iac-code mcp get yuque --check
```

## Stdio Wrapper

对于 `mcp-remote` 等 wrapper，请将 subprocess 命令放在 `--` 后：

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

然后在不连接 server 的情况下查看配置：

```bash
iac-code mcp get yuque-remote --config-only
```

## 下一步

- 查看 [MCP 配置](./configuration.md) 了解 scope、project file、headers、OAuth options 和 advanced JSON forms。
- 查看 [OAuth 和安全](./oauth-and-security.md) 了解 token storage、reset 和 approval behavior。
- server pending approval、needs auth 或 connection failed 时，查看 [疑难解答](./troubleshooting.md)。
