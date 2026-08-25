---
sidebar_position: 4
title: OAuth 和安全
description: 认证远程 MCP 服务器，并了解 IaC Code 中的 MCP 安全模型。
---

# OAuth 和安全

MCP 可以启动本地进程并调用远程服务，因此 IaC Code 将 MCP 配置和身份验证视为安全敏感的。

## OAuth

远程 `http` 和 `sse` servers 可以使用 OAuth。发布 OAuth metadata 并支持 Dynamic Client Registration 的标准服务器不需要你预先提供 client id。添加 server 后运行 auth：

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

如果服务器需要预先配置的客户端，请在服务器配置中配置 OAuth 元数据：

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

Supported OAuth fields:

| Field | Purpose |
|---|---|
| `clientId` | OAuth client id. |
| `clientSecretEnv` | 包含客户端密钥的环境变量。 |
| `callbackPort` | 可选的环回回调端口。使用 `0` 或省略它来选择空闲端口。 |
| `authServerMetadataUrl` | 可选的显式授权服务器元数据 URL。 |
| `clientMetadataUrl` | 支持客户端 ID 元数据文档的授权服务器的可选 HTTPS 客户端元数据文档 URL。 |

明文 `oauth.clientSecret` 被拒绝。使用 `clientSecretEnv` 或安全 CLI 提示符。

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code 打开或打印授权 URL 并在 `127.0.0.1` 上启动环回回调服务器。如果浏览器无法打开或回调无法自动完成，请将回调 URL 或授权码粘贴到 CLI 提示符中。授权后，IaC Code 将代码交换为令牌并安全存储。

对于支持 DCR 的服务器，IaC 代码向服务器注册 OAuth 客户端，并通过 MCP 机密存储存储返回的客户端 ID 和可选的客户端机密。当受保护资源元数据需要时，令牌交换和刷新包括 MCP SDK 语义选择的资源参数。

如果服务器在正常会话期间需要身份验证，IaC Code 会注册一个身份验证工具：

```text
mcp__<server>__authenticate
```

该模型可以调用该工具来向用户提供 OAuth URL。流程完成后，IaC 代码重新连接 MCP 服务器并刷新发现的功能。

## Token Storage

IaC 代码通过 `MCPSecretStorage` 存储 OAuth 令牌和 MCP 客户端机密：

1. 加密数据存储在 `<config-dir>/mcp/secrets.json.enc`。
2. 加密密钥存储在 `<config-dir>/mcp/secrets.key`。
3. 两个文件都会限制访问权限。

MCP 密钥存储不会访问操作系统密钥环，从而避免后台状态检查引发系统授权弹窗。仅存在于密钥环中的旧认证状态不会自动迁移；重新授权一次 MCP server 即可创建本地加密记录。

使用此命令清除存储的身份验证状态：

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` 会清除所选持久化 scope 的 OAuth token state、dynamic client registration state、
已存储的 `client_id`、可选 `client_secret` 和 OAuth signature index，但保留 server config。
删除持久化 server 时，会先执行同样的 auth-state cleanup，再删除配置：

```bash
iac-code mcp remove secure-reviewer --scope user
```

当你只想重新授权现有 server 时使用 `reset-auth`。当 server config 本身也应消失时使用 `mcp remove`；
两条路径都会清理 `MCPSecretStorage` 管理的本地加密记录。

## Project Trust

项目 `.mcp.json` 文件不会自动受信任，因为存储库可以添加运行任意本地代码的 `stdio` 服务器。交互式批准是根据服务器配置签名进行的。更改命令、参数、环境变量、URL、标头或 OAuth 配置会使之前的批准失效。

无头和协议服务器模式会跳过未经批准的项目服务器而不是进行提示。

## Secret Handling

IaC 代码通过多种方式保护秘密：

- `iac-code mcp get` 和 `iac-code mcp get --config-only` 的配置输出会对看起来像 token、secret、password、API key 和 authorization header 的字段脱敏。
- 通过 `iac-code mcp add` 或 `mcp add-json` 添加 server 时，明文敏感 header 或 env 值会被拒绝（除非使用环境变量引用）。手动编辑的配置文件在加载时不会被重新校验，因此请避免直接存储明文 secret。
- MCP stdio server 只会继承安全环境变量 allowlist 以及显式 server env。
- 带 username 或 password 的 proxy 环境变量不会被 stdio MCP server 继承。
- `headersHelper` 命令不经过 shell 运行、没有 stdin、使用最小环境、限制 stdout/stderr 捕获，并对私有 stderr 诊断脱敏。
- MCP artifact 文件会写入私有的 IaC Code runtime configuration directory。

## Permissions

MCP 工具使用与内置工具相同的权限框架。远程 MCP 服务器无法仅通过通告工具来绕过 IaC 代码权限检查。请记住这些规则：

- 根据活动权限策略，只读 MCP 工具可能会自动允许。
- 除非明确允许，否则破坏性 MCP 工具应需要批准。
- 在无头自动化中，结合 `--permission-mode`、`--allowed-tools` 和 `--disallowed-tools` 来限制 MCP 工具可以执行的操作。
- 远程 MCP 技能不授予自己的 `allowed_tools`。

## 不支持的安全敏感功能

IaC 代码暂时拒绝或忽略这些 MCP 功能：

- Enterprise managed MCP policy.
- IDE and SDK transports.
- WebSocket headers、WebSocket `headersHelper` 和 WebSocket OAuth。
- IaC Code acting as an MCP server.
