---
sidebar_position: 4
title: OAuth 和安全
description: 认证远程 MCP server，并了解 IaC Code 的 MCP 安全模型。
---

# OAuth 和安全

MCP 可以启动本地进程并调用远程服务，因此 IaC Code 会把 MCP 配置和认证视为安全敏感内容。

## OAuth

远程 `http` 和 `sse` servers 可以使用 OAuth。在 server config 中配置 OAuth metadata：

```json
{
  "mcpServers": {
    "secure-reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "clientId": "iac-code",
        "clientSecretEnv": "MCP_CLIENT_SECRET",
        "callbackPort": 38487,
        "authServerMetadataUrl": "https://auth.example.com/.well-known/oauth-authorization-server"
      }
    }
  }
}
```

支持的 OAuth 字段：

| 字段 | 用途 |
|---|---|
| `clientId` | OAuth client id。 |
| `clientSecretEnv` | 包含 client secret 的环境变量。 |
| `callbackPort` | 可选 loopback callback port。使用 `0` 或省略可自动选择空闲端口。 |
| `authServerMetadataUrl` | 可选的显式 authorization server metadata URL。 |

Plaintext `oauth.clientSecret` 会被拒绝。请使用 `clientSecretEnv` 或安全 CLI prompt。

## 认证

运行：

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code 会打开或打印 authorization URL，并在 `127.0.0.1` 上启动 loopback callback server。Provider 重定向回 authorization code 后，IaC Code 会交换 token 并安全存储。

如果 server 在普通会话中需要认证，IaC Code 会注册一个 authentication tool：

```text
mcp__<server>__authenticate
```

模型可以调用该 tool，把 OAuth URL 提供给用户。认证流程完成后，IaC Code 会重连 MCP server 并刷新发现到的能力。

## Token 存储

IaC Code 通过 `MCPSecretStorage` 存储 OAuth tokens 和 MCP client secrets：

1. 可用时优先使用操作系统 keyring。
2. 如果 keyring 被禁用或不可用，则在 `<config-dir>/mcp/` 下保存加密 fallback 数据。
3. fallback key 和加密 secret store 会限制文件权限。

设置 `IAC_CODE_MCP_DISABLE_KEYRING=1` 可以强制使用加密 fallback storage，适合隔离测试。

使用这个命令清除已存储的 auth state：

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## 项目信任

项目 `.mcp.json` 不会被自动信任，因为仓库可以添加会运行任意本地代码的 `stdio` server。交互式 approval 绑定到每个 server config signature。修改 command、args、env、URL、headers 或 OAuth config 都会使之前的 approval 失效。

Headless 和 protocol server 模式会跳过未批准的项目 servers，而不是提示用户。

## Secret 处理

IaC Code 通过多种方式保护 secrets：

- `iac-code mcp get` 的 config 输出会对看起来像 token、secret、password、API key 和 authorization header 的字段脱敏。
- 敏感 header 或 env 的 plaintext values 会被拒绝，除非使用环境变量引用。
- MCP stdio servers 只会继承安全环境变量 allowlist 以及显式 server env。
- 带 username 或 password 的 proxy 环境变量不会被 stdio MCP servers 继承。
- MCP artifact files 会写入私有的 IaC Code runtime configuration directory。

## 权限

MCP tools 使用与内置 tools 相同的权限框架。远程 MCP server 不能仅通过声明一个 tool 就绕过 IaC Code 权限检查。请记住：

- Read-only MCP tools 可能会根据当前权限策略自动允许。
- Destructive MCP tools 应要求批准，除非被显式允许。
- 在 headless automation 中，组合使用 `--permission-mode`、`--allowed-tools` 和 `--disallowed-tools` 限制 MCP tools 能做什么。
- Remote MCP skills 不会授予自己的 `allowed_tools`。

## 暂不支持的安全敏感功能

IaC Code 当前有意拒绝或省略这些 MCP 功能：

- `headersHelper` dynamic commands。
- MCP elicitation UI。
- WebSocket、IDE 和 SDK transports。
- Enterprise managed MCP policy。
- IaC Code 作为 MCP server。
