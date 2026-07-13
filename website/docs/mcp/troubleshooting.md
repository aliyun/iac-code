---
sidebar_position: 5
title: MCP Troubleshooting
description: Diagnose MCP configuration, connection, authentication, and capability discovery problems.
---

# MCP Troubleshooting

MCP warnings are non-fatal unless every capability you need is unavailable. A failed server should not prevent other MCP servers or built-in IaC Code tools from working.

## Inspect Configuration

Inspect configured servers without connecting:

```bash
iac-code mcp list
```

Run bounded health diagnostics for configured servers:

```bash
iac-code mcp list --check
```

Inspect a redacted server config without connecting:

```bash
iac-code mcp get my-server --scope local
```

Run bounded health diagnostics for one server:

```bash
iac-code mcp get my-server --scope local --check
```

Inspect configuration explicitly, without connecting:

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

Reconnect a server or all persisted servers:

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

Symptom:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

Fix:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

Use the exact `--scope` and, for non-default persisted files, the exact `--source-path` shown by your config listing.
If the server was removed, add it again instead of authenticating a missing config.

## Pending Project Server

Status or warning code: `pending_approval`.

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

Status or warning code: `connection_failed`.

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

Status: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

If the server uses OAuth refresh tokens and reauthentication is required, IaC Code clears stale tokens and asks for a fresh flow.

## OAuth Auth Failed

Symptom (`auth-failed`):

```text
MCP auth failed for 'name':
```

This means the OAuth flow started but did not finish cleanly: the callback URL may be incomplete, the authorization code
may be expired, or the authorization server may have returned an error. IaC Code restores the previous auth state when a
new flow fails before completion.

Fix:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

Retry `auth` first. Use `reset-auth` before retrying only when the saved token or dynamic client state is stale.

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

IaC Code clears stored OAuth client and token state for that server. Run auth again:

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

The server requested additional OAuth scopes. In the current session, open `/mcp` and choose `Authenticate` or
`Re-authenticate` for that server; IaC Code includes the scopes reported by the server challenge in that flow. The
standalone `iac-code mcp auth name` command starts a normal auth flow and does not carry challenge-only scopes from a
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

Re-run the command with one of the exact `--scope` commands printed in the error message. Treat this as scope ambiguity:
the server name is valid, but the command needs one persisted scope.

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

The server connected, but one capability list failed. Other capabilities from the same server may still work. Fix the server-side error, then restart IaC Code or trigger a reconnect/auth refresh.

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

For repeated failures, check whether the remote server dropped the session or restarted.

## Headers Helper Failed

Symptoms can include helper parse errors, timeout, non-zero exit status, invalid JSON, or non-string header values. Check that the helper command is valid from the config source directory and prints a JSON object such as:

```json
{"X-Org": "platform"}
```

Secret-like stderr is redacted in diagnostics.

## WebSocket Config Rejected

WebSocket MCP servers support URL-only config. Remove `headers`, `headersHelper`, and `oauth` from `type: "ws"` servers.

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

MCP binary artifacts from tool results are stored under the session-owned directory for v2 sessions:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

Legacy sessions without a supported layout marker use:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Avoid sharing config, log, or artifact directories without reviewing them for secrets.
