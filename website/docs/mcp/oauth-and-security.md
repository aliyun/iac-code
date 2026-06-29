---
sidebar_position: 4
title: OAuth and Security
description: Authenticate remote MCP servers and understand the MCP security model in IaC Code.
---

# OAuth and Security

MCP can start local processes and call remote services, so IaC Code treats MCP configuration and authentication as security-sensitive.

## OAuth

Remote `http` and `sse` servers can use OAuth. Configure OAuth metadata in the server config:

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
| `clientSecretEnv` | Environment variable that contains the client secret. |
| `callbackPort` | Optional loopback callback port. Use `0` or omit it to choose a free port. |
| `authServerMetadataUrl` | Optional explicit authorization server metadata URL. |

Plaintext `oauth.clientSecret` is rejected. Use `clientSecretEnv` or the secure CLI prompt.

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code opens or prints an authorization URL and starts a loopback callback server on `127.0.0.1`. After the provider redirects back with an authorization code, IaC Code exchanges it for tokens and stores them securely.

If a server needs authentication during a normal session, IaC Code registers an authentication tool:

```text
mcp__<server>__authenticate
```

The model can call that tool to provide the user with the OAuth URL. After the flow completes, IaC Code reconnects the MCP server and refreshes discovered capabilities.

## Token Storage

IaC Code stores OAuth tokens and MCP client secrets through `MCPSecretStorage`:

1. It tries the operating-system keyring when available.
2. If keyring is disabled or unavailable, it stores encrypted fallback data under `<config-dir>/mcp/`.
3. File permissions are restricted for the fallback key and encrypted secret store.

Set `IAC_CODE_MCP_DISABLE_KEYRING=1` to force encrypted fallback storage, which is useful for isolated tests.

Use this command to clear stored auth state:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## Project Trust

Project `.mcp.json` files are not trusted automatically because a repository can add a `stdio` server that runs arbitrary local code. Interactive approval is per server config signature. Changing command, args, env, URL, headers, or OAuth config invalidates previous approval.

Headless and protocol server modes skip unapproved project servers rather than prompting.

## Secret Handling

IaC Code protects secrets in several ways:

- Config output from `iac-code mcp get` redacts keys that look like tokens, secrets, passwords, API keys, and authorization headers.
- Plaintext sensitive header or env values are rejected unless they use an environment-variable reference.
- MCP stdio servers inherit only an allowlist of safe environment variables plus the explicit server env.
- Proxy environment variables with embedded usernames or passwords are not inherited by stdio MCP servers.
- MCP artifact files are written under the private IaC Code runtime configuration directory.

## Permissions

MCP tools use the same permission framework as built-in tools. A remote MCP server cannot bypass IaC Code permission checks simply by advertising a tool. Keep these rules in mind:

- Read-only MCP tools may be auto-allowed depending on the active permission policy.
- Destructive MCP tools should require approval unless explicitly allowed.
- In headless automation, combine `--permission-mode`, `--allowed-tools`, and `--disallowed-tools` to restrict what MCP tools can do.
- Remote MCP skills do not grant their own `allowed_tools`.

## Unsupported Security-Sensitive Features

IaC Code intentionally rejects or omits these MCP features for now:

- `headersHelper` dynamic commands.
- MCP elicitation UI.
- WebSocket, IDE, and SDK transports.
- Enterprise managed MCP policy.
- IaC Code acting as an MCP server.
