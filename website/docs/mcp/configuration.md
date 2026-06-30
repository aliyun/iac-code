---
sidebar_position: 2
title: MCP Configuration
description: Configure MCP servers through CLI commands, settings files, project files, and ACP sessions.
---

# MCP Configuration

MCP servers are configured under the `mcpServers` object. IaC Code supports a Claude Code-compatible core schema for `stdio`, `http`, and `sse` servers.

## Configuration Sources

IaC Code reads MCP servers from these sources:

| Source | Scope | File or entry point | Trust model |
|---|---|---|---|
| User settings | `user` | `~/.iac-code/settings.yml` or `IAC_CODE_CONFIG_DIR/settings.yml` | Trusted by the current user. |
| Project local settings | `local` | `<workspace>/.iac-code/settings.local.yml` | Private to the local checkout. |
| Project MCP file | `project` | `<workspace>/.mcp.json` | Shared with the project and requires local approval. |
| ACP session config | `session` | `mcp_servers` passed by an ACP client | Applies only to that ACP session runtime. |

Precedence is user, project, local, then session. Later sources override earlier sources by server name. Equivalent configs are also deduplicated by content signature.

Project `.mcp.json` files are discovered from the workspace root down to the current directory. Child project files override parent files by server name.

## CLI Commands

Use `iac-code mcp` to manage persisted MCP configuration:

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

Available commands:

| Command | Purpose |
|---|---|
| `iac-code mcp add` | Add a server from structured CLI flags. |
| `iac-code mcp add-json` | Add a server from a JSON object. |
| `iac-code mcp list` | List configured servers, scopes, transports, and approval status. |
| `iac-code mcp get` | Print one redacted server config. |
| `iac-code mcp remove` | Remove one server from a persisted scope. |
| `iac-code mcp approve` | Approve a project `.mcp.json` server. |
| `iac-code mcp reject` | Reject a project `.mcp.json` server. |
| `iac-code mcp reset-project-choices` | Clear stored project approval choices. |
| `iac-code mcp auth` | Start OAuth authentication for a server. |
| `iac-code mcp reset-auth` | Delete stored OAuth tokens and client secret for a server. |

When `--scope` is omitted, IaC Code writes to `local` inside a project and `user` outside a project.

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

The `type` field can be omitted when `command` is present. IaC Code passes a safe inherited environment plus the server `env`. On Windows, prefer `cmd /c npx` instead of bare `npx` for Node-based servers.

## HTTP and SSE Servers

Remote servers require `type` and `url`:

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

Use `type: "sse"` for SSE servers. Static headers are supported. Dynamic `headersHelper` commands are rejected because they need a separate trusted-execution design.

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

Missing variables without defaults produce an MCP warning and the affected server is skipped. Environment expansion applies recursively to strings inside lists and objects.

Do not store plaintext secrets in headers or env values. Use environment-variable references or OAuth secret storage.

## Project Approval

Project `.mcp.json` can be committed to a repository, so IaC Code does not trust it automatically.

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Pressing Enter keeps the default `N` and rejects that exact project server config. Type `y` or `yes` to approve it. Approval is stored locally under the IaC Code config directory and includes the workspace path, project file path, server name, and config signature. If the `.mcp.json` server config changes, approval is invalidated and the server becomes pending again.

Headless, ACP, and A2A startup never ask interactive approval questions. Pending project servers are skipped and reported as warnings.
