---
title: Protocol Reference
description: Complete ACP protocol method and event reference for iac-code integration.
sidebar_position: 3
---

# Protocol Reference

This document provides a complete reference for the ACP (Agent Client Protocol) methods and streaming events exposed by the iac-code server.

## Lifecycle Overview

A typical ACP session follows this flow:

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** — Handshake. Negotiate protocol version and discover server capabilities.
2. **session/new** — Create a fresh session with an independent agent runtime.
3. **session/prompt** — Send user input; receive streaming events until a final response.
4. **session/close** — Release the session and its resources.

Sessions can also be loaded from history (`session/load`) or resumed (`session/resume`) instead of creating new ones.

---

## Session Backups

ACP sessions use the same v2 session backup behavior as interactive and headless runs. When `IAC_CODE_CONFIG_BACKUP_DIR` is set, a normal prompt completion records a non-blocking `normal_turn_end` backup; if that backup fails, the failure is logged as a `warning` and recorded in `.backup-state.json`, while the final response still completes without a promised warning field. Pipeline mode has its own critical backup gates.

---

## Methods

### initialize

Protocol handshake. Must be the first call on every connection.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `protocolVersion` | integer | Yes | Requested protocol version (currently `1`) |
| `clientInfo` | object | No | Client name and version |
| `clientCapabilities` | object | No | Capabilities the client supports |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `protocolVersion` | integer | Negotiated protocol version |
| `agentCapabilities` | object | Server capabilities (see below) |
| `agentInfo` | object | Server name and version |
| `authMethods` | array | Available authentication methods (empty if using built-in credentials) |

**Agent Capabilities**

| Capability | Value | Meaning |
|-----------|-------|---------|
| `loadSession` | `true` | Supports restoring sessions from history |
| `promptCapabilities.embeddedContext` | `true` | Accepts embedded resource content in prompts |
| `promptCapabilities.image` | `false` | Image input not supported (degrades to text marker) |
| `promptCapabilities.audio` | `false` | Audio input not supported (degrades to text marker) |
| `mcpCapabilities.http` | `true` | Accepts HTTP (streamable) MCP servers at session creation |
| `mcpCapabilities.sse` | `true` | Accepts SSE MCP servers at session creation |
| `sessionCapabilities.list` | `{}` | Supports listing sessions |
| `sessionCapabilities.close` | `{}` | Supports closing sessions |

---

### session/new

Create a new session with an independent agent runtime, tool registry, and LLM context.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Absolute path to the working directory |
| `mcpServers` | array | No | MCP server configuration array injected into the session runtime; HTTP (streamable) and SSE servers are connected for the session |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `sessionId` | string | Unique session identifier for subsequent calls |
| `modes` | object | Available modes and current mode |
| `models` | object | Available models and current model |

:::note
Each session creates an independent AgentLoop. Multiple sessions can run concurrently but each consumes an LLM connection.
:::

---

### session/load

Load a previously persisted session from disk, restoring its message history.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Working directory path |
| `sessionId` | string | Yes | ID of the session to load |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `models` | object | Available models and current model state |
| `modes` | object | Available modes and current mode state |

:::note
Loading a session reads history from `~/.iac-code/sessions/`, auto-repairs interrupted messages, and injects history into a fresh AgentLoop.
:::

---

### session/fork

Fork an existing session to create an independent branch with the same history.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Working directory path |
| `sessionId` | string | Yes | ID of the session to fork |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `sessionId` | string | New session ID for the forked branch |
| `models` | object | Available models and current model state |
| `modes` | object | Available modes and current mode state |

---

### session/resume

Resume or reconnect to an existing session. Automatically loads history if needed.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Working directory path |
| `sessionId` | string | Yes | ID of the session to resume |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `models` | object | Available models and current model state (optional) |
| `modes` | object | Available modes and current mode state (optional) |

:::note
Unlike `session/new`, the response does not include a `sessionId` field since the client already knows the session ID from the request.
:::

---

### session/prompt

Send user input and trigger streaming agent responses.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Yes | Target session ID |
| `prompt` | array | Yes | Array of content blocks (see Content Block Types below) |

**Content Block Types**

| Type | Description |
|------|-------------|
| `TextContentBlock` | Plain text user input |
| `EmbeddedResourceContentBlock` | File content embedded inline |
| `ResourceContentBlock` | Resource link reference |
| `ImageContentBlock` | Image (degrades to `[image: mime/type]` text marker) |
| `AudioContentBlock` | Audio (degrades to `[audio: mime/type]` text marker) |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `stopReason` | string | Why the prompt completed (see Stop Reasons) |
| `_meta.usage` | object | Token usage delivered under the response `_meta` object: `input_tokens`, `output_tokens`, `total_tokens` |

**Stop Reasons**

| Value | Meaning |
|-------|---------|
| `end_turn` | Model completed normally |
| `max_turn_requests` | Hit maximum tool-call loop limit |
| `max_tokens` | Output token limit reached |
| `cancelled` | Client cancelled the prompt |
| `refusal` | Model refused to answer |

:::note
During execution, the server pushes `session/update` notifications with streaming events before returning the final response.
:::

---

### session/cancel

Cancel a running prompt task.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Yes | Session with the running prompt |

**Behavior**

- Stops consuming stream events
- Running tools are not forcefully terminated, but results are discarded
- The pending `prompt` call returns with `stopReason: "cancelled"`

---

### session/close

Close a session and release its resources.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Yes | Session to close |

**Behavior**

- Session removed from memory
- Persisted history remains on disk
- Subsequent `prompt` calls to this session return an error

---

### session/list

List all persisted sessions for a given working directory.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Working directory to scope the listing |

**Response Fields**

| Field | Type | Description |
|-------|------|-------------|
| `sessions` | array | List of session objects with `sessionId` and metadata |

---

### session/set_config_option

Dynamically set a configuration option for a session.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Yes | Target session |
| `configId` | string | Yes | Configuration key to set |
| `value` | any | Yes | New value |

---

## Streaming Events

During `session/prompt` execution, the server pushes `session/update` notifications containing streaming event data.

### Event Format

Each `session/update` notification carries an update object with a specific type:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### Event Type Mapping

| Internal Event | ACP Update Type | Description |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | Incremental agent text output |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | Model reasoning/thinking content |
| `ToolUseStartEvent` | `ToolCallStart` | Tool invocation begins |
| `ToolResultEvent` | `ToolCallProgress` | Tool result (completed or failed) |
| `CompactionEvent` | `AgentMessageChunk` | Context compaction notification |
| `ErrorEvent` | `AgentMessageChunk` | Error information |

### MCP Status and Warnings

ACP exposes MCP runtime state in `session/update.params.update._meta.iac_code` on the wire:

| Metadata key | ACP update type | Description |
|---|---|---|
| `mcpStatus` | `session_info_update` | Current MCP server state, pushed after session creation and when MCP auth, connection, or capability state changes. |
| `mcpWarning` | `agent_message_chunk` | One startup or configuration warning, paired with a short user-visible warning message. |

`mcpStatus` contains `servers` and `warnings`. Server entries include `serverName`, `state`, `authState`, `toolsCount`, `resourcesCount`, and `promptsCount`. `authState` values include `configured`, `needs-auth`, and `not-configured`.

Common server states are `connected`, `failed`, `pending`, `needs-auth`, `pending-approval`, and `disabled`.

Large status frames may include `truncated`, `truncationReason` set to `acp-frame-size-limit`, `serversOmittedCount`, `warningsOmittedCount`, or capability-list omission counts such as `toolsOmittedCount`.

Tool entries under `servers[].tools[]` include `publicName`, `originalServerName`, `originalToolName`, and may include `annotations`. MCP annotations can carry hints such as `readOnlyHint` and `destructiveHint`.

Permission audit operation records may include `isReadOnly` and `isDestructive` when MCP annotations provide read-only or destructive hints.

`mcpWarning` entries include `serverName`, `code`, `message`, `severity`, `source`, and optionally `sourcePath` or `scope`. Common warning codes are `duplicate_config`, `invalid_name`, `invalid_config`, `missing_env`, `pending_approval`, `needs_auth`, `connection_failed`, `command_conflict`, `skill_read_failed`, `skill_truncated`, `alias_conflict`, and dynamic capability warnings such as `<capability>_failed`.

### Tool Call Lifecycle

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, input summary / safe prompt input)
    │
    ├── ToolCallProgress (status=completed, raw_output=result)   ← success
    │
    └── ToolCallProgress (status=failed, raw_output=error)       ← failure
```

**Tool Kind Mapping**

| Tool | ACP ToolKind |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| Others | `other` |

---

## Permission Requests

Before executing high-risk tools, iac-code sends a `request_permission` callback to the client.

### Tool Permission Categories

| Category | Tools | Auto-allowed |
|----------|-------|-------------|
| Read-only | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | Yes |
| Read-only cloud API | `aliyun_api` actions classified as read-only | Yes |
| Write | `write_file`, `edit_file` | No — requires approval |
| Execute | `bash`, `agent` | No — requires approval |
| Cloud write API | Non-read-only `aliyun_api` calls | No — requires per-API approval |

### request_permission Event

The server sends a `request_permission` callback with:

| Field | Type | Description |
|-------|------|-------------|
| `options` | array | Available permission choices |
| `sessionId` | string | Session requesting permission |
| `toolCall` | object | Tool call details (title, kind, input) |

For MCP tool permission prompts, the ACP `ToolCallUpdate` `title` uses the public operation label and the content carries a redacted input summary. Through the ACP `_meta` extensibility field, the `toolCall._meta.iac_code.permission` object includes `permissionId`, `toolName`, `toolUseId`, `scope`, `inputSummary`, and, when available, `publicName`, `originalServerName`, `originalToolName`, `isReadOnly`, and `isDestructive`. `inputSummary` is shape and redaction metadata, not raw sensitive input.

### Permission Options

| Option ID | Meaning |
|-----------|---------|
| `allow_once` | Allow this specific invocation |
| `allow_always` | Allow all future calls of this tool in this session when the tool supports blanket allow; not offered for `bash` or `aliyun_api` by default |
| `allow_rule:<rules>` | Allow future calls matching the suggested rule(s) in this session |
| `deny_rule:<rules>` | Deny future calls matching the suggested rule(s) in this session |
| `reject_once` | Deny this specific invocation |
| `reject_always` | Deny all future calls of this tool in this session |

For `aliyun_api`, read-only actions are auto-allowed. Non-read-only RPC and ROA actions can offer an exact rule such as `aliyun_api(ros:CreateStack)` or `aliyun_api(cs:CreateCluster)`. Wildcard allow rules still do not approve non-read-only calls.

### Response Format

```json
{
  "outcome": "selected",
  "optionId": "allow_once"
}
```

Or to deny:

```json
{
  "outcome": "cancelled"
}
```

| Client Response | Tool Behavior |
|----------------|---------------|
| `AllowedOutcome` | Tool executes normally |
| `DeniedOutcome` | Tool skipped; model receives "Permission denied." error |

---

## Error Handling

### RequestError Format

Errors follow JSON-RPC 2.0 error format:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### Common Error Codes

| Code | Name | Description |
|------|------|-------------|
| `-32700` | Parse error | Invalid JSON |
| `-32600` | Invalid request | Malformed JSON-RPC |
| `-32601` | Method not found | Unknown method |
| `-32602` | Invalid params | Missing or invalid parameters (e.g., unknown session ID) |
| `-32603` | Internal error | Server-side failure |
