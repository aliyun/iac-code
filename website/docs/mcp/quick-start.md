---
sidebar_position: 2
title: MCP Quick Start
description: Add a remote HTTP MCP server or stdio wrapper with Claude-style commands.
---

# MCP Quick Start

Use this path when you already have an MCP server URL or a local stdio wrapper command.

## Remote HTTP Server

Add the remote server with the positional URL form:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

If the server uses OAuth, start the authorization flow:

```bash
iac-code mcp auth yuque
```

After authorization, run a bounded health check:

```bash
iac-code mcp get yuque --check
```

## Stdio Wrapper

For wrappers such as `mcp-remote`, put the subprocess command after `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Then inspect the configured server without connecting:

```bash
iac-code mcp get yuque-remote --config-only
```

## Next Steps

- Use [MCP Configuration](./configuration.md) for scopes, project files, headers, OAuth options, and advanced JSON forms.
- Use [OAuth and Security](./oauth-and-security.md) for token storage, reset, and approval behavior.
- Use [Troubleshooting](./troubleshooting.md) when a server is pending approval, needs auth, or fails to connect.
