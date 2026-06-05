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
| `sessionCapabilities.list` | `{}` | Supports listing sessions |
| `sessionCapabilities.close` | `{}` | Supports closing sessions |

---

### session/new

Create a new session with an independent agent runtime, tool registry, and LLM context.

**Request Parameters**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Absolute path to the working directory |
| `mcpServers` | object | No | MCP server configuration (accepted but not yet functional) |

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
| `usage` | object | Token usage: `inputTokens`, `outputTokens`, `totalTokens` |

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

### sessions/list

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

### config/set

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

### Tool Call Lifecycle

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, raw_input=tool input)
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
| Write | `write_file`, `edit_file` | No — requires approval |
| Execute | `bash`, `agent` | No — requires approval |

### request_permission Event

The server sends a `request_permission` callback with:

| Field | Type | Description |
|-------|------|-------------|
| `options` | array | Available permission choices |
| `sessionId` | string | Session requesting permission |
| `toolCall` | object | Tool call details (title, kind, input) |

### Permission Options

| Option ID | Meaning |
|-----------|---------|
| `allow_once` | Allow this specific invocation |
| `allow_always` | Allow all future calls of this tool in this session when the tool supports blanket allow; not offered for `bash` by default |
| `allow_rule:<rules>` | Allow future calls matching the suggested rule(s) in this session |
| `deny_rule:<rules>` | Deny future calls matching the suggested rule(s) in this session |
| `reject_once` | Deny this specific invocation |
| `reject_always` | Deny all future calls of this tool in this session |

### Response Format

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

Or to deny:

```json
{
  "outcome": "denied"
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
