---
sidebar_position: 2
title: MCP Quick Start
description: Add a remote HTTP MCP server or stdio server with Claude-style commands.
---

# MCP Quick Start

Use this path when you already have an MCP server URL or a local stdio server command.

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

## Stdio Server

For stdio servers, put the subprocess command after `--`. A remote HTTP server does not need a wrapper: add it with `--transport http` as shown above.

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
```

Then inspect the configured server without connecting:

```bash
iac-code mcp get local-files --config-only
```

## Next Steps

- Use [MCP Configuration](./configuration.md) for scopes, project files, headers, OAuth options, and advanced JSON forms.
- Use [OAuth and Security](./oauth-and-security.md) for token storage, reset, and approval behavior.
- Use [Troubleshooting](./troubleshooting.md) when a server is pending approval, needs auth, or fails to connect.
