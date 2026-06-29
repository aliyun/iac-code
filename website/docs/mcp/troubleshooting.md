---
sidebar_position: 5
title: MCP Troubleshooting
description: Diagnose MCP configuration, connection, authentication, and capability discovery problems.
---

# MCP Troubleshooting

MCP warnings are non-fatal unless every capability you need is unavailable. A failed server should not prevent other MCP servers or built-in IaC Code tools from working.

## Inspect Configuration

List configured servers:

```bash
iac-code mcp list
```

Inspect a redacted server config:

```bash
iac-code mcp get my-server --scope local
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

## Pending Project Server

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

or start the interactive REPL in that project and answer `y` when prompted. Pressing Enter means `N` and rejects the server.

If approval used to work but stopped, check whether `.mcp.json` changed. Approval is tied to the config signature.

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

Servers with missing required environment variables are skipped.

## Connection Failed

For stdio servers:

- Verify `command` exists on `PATH`.
- Use absolute paths for scripts when launching from different directories.
- On Windows, run Node-based servers through `cmd /c npx`.
- Check that any required environment variables are configured.

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
- Confirm static headers are present and do not contain plaintext secrets.
- Run `iac-code mcp auth <server>` if the server requires OAuth.

## Needs Authentication

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

If the server uses OAuth refresh tokens and reauthentication is required, IaC Code clears stale tokens and asks for a fresh flow.

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

The server connected, but one capability list failed. Other capabilities from the same server may still work. Fix the server-side error, then restart IaC Code or trigger a reconnect/auth refresh.

## Resources Are Missing

`list_mcp_resources` is registered only when at least one connected server exposes resources. If the tool is missing:

- Confirm the server connected.
- Confirm the server supports `resources/list`.
- Check startup warnings for resource discovery errors.

## Prompt or Skill Command Missing

Prompt and skill commands appear only after successful discovery. Check:

- The prompt or `skill://` resource exists on the MCP server.
- The normalized command name does not conflict with a built-in command.
- The remote skill resource can be read within the startup timeout.
- The skill description and body fit IaC Code safety limits.

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

MCP binary artifacts from tool results are stored under:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Avoid sharing config, log, or artifact directories without reviewing them for secrets.
