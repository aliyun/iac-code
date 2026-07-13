---
sidebar_position: 2
title: MCP Configuration
description: Configure MCP servers through CLI commands, settings files, project files, and ACP sessions.
---

# MCP Configuration

MCP servers are configured under the `mcpServers` object. IaC Code supports a Claude Code-compatible core schema for `stdio`, `http`, `sse`, and URL-only `ws` servers.

## Quick Start

For a remote HTTP MCP server such as Yuque, add the server with the positional URL form, then start OAuth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

For stdio wrappers such as `mcp-remote`, put the subprocess command after `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

## Configuration Sources

IaC Code reads MCP servers from these sources:

| Source | Scope | File or entry point | Trust model |
|---|---|---|---|
| User settings | `user` | `~/.iac-code/settings.yml` or `IAC_CODE_CONFIG_DIR/settings.yml` | Trusted by the current user. |
| Project local settings | `local` | `<workspace>/.iac-code/settings.local.yml` | Private to the local checkout. |
| Project MCP file | `project` | `<workspace>/.mcp.json` | Shared with the project and requires local approval. |
| ACP session config | `session` | `mcpServers` passed by an ACP client | Applies only to that ACP session runtime. |

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
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

Remote HTTP servers can be added with the Claude-style positional URL form:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

SSE and WebSocket servers use the same positional URL form with their own transport:

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

For stdio wrappers such as `mcp-remote`, put the subprocess command after `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Available commands:

| Command | Purpose |
|---|---|
| `iac-code mcp add` | Add a server from structured CLI flags. |
| `iac-code mcp add-json` | Add a server from a JSON object. |
| `iac-code mcp list` | List configured servers, scopes, transports, and approval status without connecting. |
| `iac-code mcp list --config-only` | Alias for the default config listing. |
| `iac-code mcp list --check` | Connect briefly and show bounded health diagnostics. |
| `iac-code mcp get` | Print one redacted server config without connecting. |
| `iac-code mcp get --config-only` | Print one redacted server config without connecting. |
| `iac-code mcp get --check` | Connect briefly and show bounded health diagnostics for one server. |
| `iac-code mcp remove` | Remove one server from a persisted scope. |
| `iac-code mcp approve` | Approve a project `.mcp.json` server. |
| `iac-code mcp reject` | Reject a project `.mcp.json` server. |
| `iac-code mcp reset-project-choices` | Clear stored project approval choices. |
| `iac-code mcp auth` | Start OAuth authentication for a server. |
| `iac-code mcp reset-auth` | Delete stored OAuth tokens and client secret for a server. |
| `iac-code mcp reconnect` | Reconnect one server, or all persisted servers with `--all`. |
| `iac-code mcp disable` | Disable a persisted server without editing shared project config. |
| `iac-code mcp enable` | Re-enable a persisted server. |

## Command Options

The option set below mirrors `iac-code mcp <command> --help`:

| Command | Options |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options beyond `--help`. |
| `iac-code mcp reject` | No command-specific options beyond `--help`. |
| `iac-code mcp reset-project-choices` | No command-specific options beyond `--help`. |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

When `--scope` is omitted, IaC Code writes to `local` inside a project and `user` outside a project.

For commands that operate on an existing persisted server, IaC Code can find a unique server across persisted scopes when `--scope` is omitted. If the same name exists in multiple scopes, the command fails with exact `--scope` commands to disambiguate.

## Interactive MCP Manager

Inside the interactive REPL, `/mcp` opens a full-screen MCP manager. It groups servers by source, shows connection state, authentication state, config diagnostics, failure details, and configured location.

From the manager, you can inspect a connected server's tools, resources, and prompts; authenticate, re-authenticate, or clear authentication for remote servers; reconnect servers; enable or disable persisted servers; approve or reject project `.mcp.json` servers; and remove persisted entries. OAuth flows show the authorization URL, support copying it, and accept a pasted callback URL or authorization code when the browser redirect cannot reach the local callback listener.

`/mcp enable <name>`, `/mcp disable <name>`, and `/mcp reconnect <name>` run quick actions without opening the manager. If `/mcp` is received through piped stdin or another non-TTY input, IaC Code prints a terminal-required message; use `iac-code mcp <command>` for non-interactive automation.

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

Use `type: "sse"` for SSE servers. Static headers are supported with either `KEY=VALUE` or `Name: Value` CLI syntax.

Dynamic headers can be provided by `headersHelper`:

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

The helper must print a JSON object whose keys and values are strings. Dynamic headers override static headers with the same name. IaC Code runs helpers without a shell, with no stdin, a minimal inherited environment, the config source directory as cwd, a 5 second timeout, and redacted stderr diagnostics. The `headersHelper` command string is not environment-expanded; referenced variables are passed in the helper environment, and the helper must read them itself. Project `.mcp.json` helpers require project approval before they can run.

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

The installed MCP SDK WebSocket transport accepts only a URL. IaC Code rejects WebSocket configs that also set `headers`, `headersHelper`, or `oauth`.

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

Missing variables without defaults produce an MCP warning and the affected server is skipped. Environment expansion applies recursively to strings inside lists and objects except the `headersHelper` command string, which is kept literal and receives referenced variables through the helper environment.

Do not store plaintext secrets in headers or env values. Use environment-variable references or OAuth secret storage.

## Project Approval

Project `.mcp.json` can be committed to a repository, so IaC Code does not trust it automatically.

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Pressing Enter keeps the default `N` and rejects that exact project server config. Type `y` or `yes` to approve it. Approval is stored locally under the IaC Code config directory and includes the workspace path, project file path, server name, and config signature. If the `.mcp.json` server config changes, approval is invalidated and the server becomes pending again.

Headless, ACP, and A2A startup never ask interactive approval questions. Pending project servers are skipped and reported as warnings.

## Disabled Servers

`iac-code mcp disable <name>` stores a private disabled-state entry under the IaC Code config directory. For project-scoped servers, this does not mutate the shared `.mcp.json` file. Disabled entries are keyed by scope, source file, server name, and config signature, so changing the server config invalidates stale disabled state.
