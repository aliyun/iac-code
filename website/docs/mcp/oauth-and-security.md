---
sidebar_position: 4
title: OAuth and Security
description: Authenticate remote MCP servers and understand the MCP security model in IaC Code.
---

# OAuth and Security

MCP can start local processes and call remote services, so IaC Code treats MCP configuration and authentication as security-sensitive.

## OAuth

Remote `http` and `sse` servers can use OAuth. Standards-compliant servers that publish OAuth metadata and support Dynamic Client Registration do not require you to provide a client id. Add the server, then run auth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

If a server requires a pre-provisioned client, configure OAuth metadata in the server config:

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
| `clientMetadataUrl` | Optional HTTPS client metadata document URL for authorization servers that support client-id metadata documents. |

Plaintext `oauth.clientSecret` is rejected. Use `clientSecretEnv` or the secure CLI prompt.

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code opens or prints an authorization URL and starts a loopback callback server on `127.0.0.1`. If the browser cannot open or the callback cannot complete automatically, paste the callback URL or authorization code into the CLI prompt. After authorization, IaC Code exchanges the code for tokens and stores them securely.

For DCR-capable servers, IaC Code registers an OAuth client with the server and stores the returned client id and optional client secret through MCP secret storage. Token exchange and refresh include the resource parameter selected by the MCP SDK semantics when protected-resource metadata requires it.

If a server needs authentication during a normal session, IaC Code registers an authentication tool:

```text
mcp__<server>__authenticate
```

The model can call that tool to provide the user with the OAuth URL. After the flow completes, IaC Code reconnects the MCP server and refreshes discovered capabilities.

## Token Storage

IaC Code stores OAuth tokens and MCP client secrets through `MCPSecretStorage`:

1. It stores encrypted data in `<config-dir>/mcp/secrets.json.enc`.
2. The encryption key is stored in `<config-dir>/mcp/secrets.key`.
3. File permissions are restricted for both files.

MCP secret storage does not access the operating-system keyring, avoiding system authorization prompts during
background status checks. Auth state that existed only in the keyring is not migrated automatically; authorize the
MCP server once to create the encrypted local entry.

Use this command to clear stored auth state:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` clears OAuth token state, dynamic client registration state, stored `client_id`, optional
`client_secret`, and the OAuth signature index for the selected persisted scope, while keeping the server config.
Removing a persisted server performs the same auth-state cleanup before deleting the config:

```bash
iac-code mcp remove secure-reviewer --scope user
```

Use `reset-auth` when you want to reauthorize an existing server. Use `mcp remove` when the server config itself
should disappear; both paths clear encrypted entries managed by `MCPSecretStorage`.

## Project Trust

Project `.mcp.json` files are not trusted automatically because a repository can add a `stdio` server that runs arbitrary local code. Interactive approval is per server config signature. Changing command, args, env, URL, headers, or OAuth config invalidates previous approval.

Headless and protocol server modes skip unapproved project servers rather than prompting.

## Secret Handling

IaC Code protects secrets in several ways:

- Config output from `iac-code mcp get` and `iac-code mcp get --config-only` redacts keys that look like tokens, secrets, passwords, API keys, and authorization headers.
- Plaintext sensitive header or env values are rejected when adding servers via `iac-code mcp add` or `mcp add-json` unless they use an environment-variable reference. Configuration files edited by hand are not re-validated on load, so avoid storing plaintext secrets directly.
- MCP stdio servers inherit only an allowlist of safe environment variables plus the explicit server env.
- Proxy environment variables with embedded usernames or passwords are not inherited by stdio MCP servers.
- `headersHelper` commands run without a shell, no stdin, a minimal environment, bounded stdout/stderr capture, and redacted private stderr diagnostics.
- MCP artifact files are written under the private IaC Code runtime configuration directory.

## Permissions

MCP tools use the same permission framework as built-in tools. A remote MCP server cannot bypass IaC Code permission checks simply by advertising a tool. Keep these rules in mind:

- Read-only MCP tools may be auto-allowed depending on the active permission policy.
- Destructive MCP tools should require approval unless explicitly allowed.
- In headless automation, combine `--permission-mode`, `--allowed-tools`, and `--disallowed-tools` to restrict what MCP tools can do.
- Remote MCP skills do not grant their own `allowed_tools`.

## Unsupported Security-Sensitive Features

IaC Code intentionally rejects or omits these MCP features for now:

- Enterprise managed MCP policy.
- IDE and SDK transports.
- WebSocket headers, WebSocket `headersHelper`, and WebSocket OAuth.
- IaC Code acting as an MCP server.
