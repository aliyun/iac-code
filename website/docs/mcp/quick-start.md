---
sidebar_position: 2
title: MCP Quick Start
description: Add and connect MCP servers from the Desktop or Web Plugins panel.
---

# MCP Quick Start

Use the MCP tab in **Plugins** in Desktop or Web to add and manage servers. CLI commands and configuration files are documented below for automation and advanced setup.

## Connect a remote HTTP server

1. Open **Plugins → MCP**, select the target project, and click **Add server**.

2. Enter the server **Name**, choose **Scope**, and set **Transport type** to **http (remote)**.

3. Enter the server **URL**. Add required request headers using environment-variable references for secrets.

4. Click **Create**. If a project-scoped server is pending approval, review its configuration and click **Approve**.

5. If authentication is required, click **Authenticate** and complete browser authorization. Return to the panel after it succeeds.

6. Click **Check connection**, then **View capabilities**. Confirm the expected tools, resources, or prompts appear before using the server in a conversation.

## Connect a local stdio server

1. Open **Plugins → MCP**, choose the project, and click **Add server**.

2. Enter **Name** and **Scope**, then select **stdio (local command)**.

3. Fill **Command**, put one argument on each line in **Arguments**, and enter required environment variables. The executable must exist on the machine running IaC Code.

4. Click **Create**; approve a project-scoped configuration if prompted.

5. Click **Check connection** and **View capabilities** to confirm discovery succeeded.

:::note

User scope applies across projects; Local is private to the selected checkout; Project writes `.mcp.json` for sharing and requires local approval. To modify an existing server, use its edit action and click **Save**.

:::

## CLI automation

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
iac-code mcp get yuque --check
```

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
iac-code mcp get local-files --config-only
```

## Next Steps

- Use [MCP Configuration](./configuration.md) for scopes, project files, headers, OAuth options, and advanced JSON forms.
- Use [OAuth and Security](./oauth-and-security.md) for token storage, reset, and approval behavior.
- Use [Troubleshooting](./troubleshooting.md) when a server needs approval or authentication, or fails to connect.
