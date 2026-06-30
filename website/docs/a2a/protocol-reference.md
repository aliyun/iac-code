---
title: Protocol Reference
description: Complete A2A protocol reference for iac-code integration.
sidebar_position: 4
---

# Protocol Reference

This document describes the A2A 1.0 surface exposed by the iac-code server and the Phase 1 client behavior used by `iac-code a2a-client call`. For exact CLI options, see [Command Reference](./command-reference.md).

## Lifecycle Overview

A typical A2A interaction follows this flow:

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **Discover** — Fetch `/.well-known/agent-card.json`.
2. **Send** — Submit a text message to the JSON-RPC endpoint at `/`.
3. **Stream** — Receive `Task`, `Message`, and `TaskStatusUpdateEvent` payloads.
4. **Continue** — Send a follow-up message with the same `contextId`.
5. **Cancel or query** — Use `CancelTask`, `GetTask`, or `ListTasks`.

## Agent Card

The Agent Card is available at:

```text
GET /.well-known/agent-card.json
```

Important fields:

| Field | Value | Meaning |
|-------|-------|---------|
| `name` | `iac-code` | Agent name |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | Transport binding |
| `supportedInterfaces[0].protocolVersion` | `1.0` | A2A protocol version |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | JSON-RPC endpoint |
| `capabilities.streaming` | `true` | Supports streaming task updates |
| `capabilities.pushNotifications` | `false` or `true` | `true` when `push-notifications: true` is configured |
| `capabilities.extendedAgentCard` | `true` | Authenticated callers can request extended runtime details |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | Optional iac-code metadata namespace for tool status and stored artifact metadata |
| `defaultInputModes` | text, JSON, YAML, image, audio, and binary MIME types | Accepted input MIME modes |
| `defaultOutputModes` | `["text/plain"]` | Text output only |

Agent Card responses include `Cache-Control: public, max-age=60`, `ETag`, and `Last-Modified`. Clients may send `If-None-Match` and receive `304 Not Modified` when the card has not changed.

Advertised skills:

| Skill ID | Purpose |
|----------|---------|
| `iac_generation` | Generate Alibaba Cloud ROS and Terraform templates from natural language |
| `iac_review` | Inspect IaC templates and suggest fixes |
| `aliyun_ros_operations` | Assist with Alibaba Cloud ROS stack workflows |
| `terraform_ros_conversion` | Assist Terraform-to-ROS conversion using bundled skill resources |

When authentication is enabled, the Agent Card advertises the configured security schemes:

| Scheme | When advertised |
|--------|-----------------|
| `bearerAuth` | `token` or `IACCODE_A2A_HTTP_TOKEN` is set |
| `basicAuth` | Basic username and password are both set |
| `apiKeyAuth` | `api-key` or `IACCODE_A2A_API_KEY` is set |

## Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/health` | `GET` | Returns `{"status":"healthy"}` |
| `/.well-known/agent-card.json` | `GET` | Returns the Agent Card |
| `/` | `POST` | Handles A2A JSON-RPC requests |
| REST routes | mixed | The A2A SDK REST routes registered by `create_rest_routes` |

## Phase 1 Client and Transport Notes

The default interoperable Phase 1 transport is JSON-RPC over HTTP. HTTP mode also advertises `HTTP+JSON` for the SDK REST routes.

The server also has optional transports for stdio, Unix sockets, WebSocket, official gRPC, gRPC JSON-RPC envelope, and Redis Streams. stdio, Unix sockets, WebSocket, gRPC JSON-RPC, and Redis Streams are custom JSON-RPC transports. Official gRPC is advertised as `grpc` and requires optional gRPC dependencies.

The built-in client uses Agent Card discovery (`GET /.well-known/agent-card.json`) before message calls, selects the first advertised runnable `supportedInterfaces[].url`, then sends JSON-RPC requests with `A2A-Version: 1.0` and A2A 1.0 method names such as `SendMessage`.

`push-notifications: true` enables A2A push notification configuration methods and terminal-state delivery.

Agent Card signing uses the A2A SDK signing utility and emits standard `AgentCardSignature` JWS fields. The symmetric-key mode uses `HS256`; verification can select a configured secret by protected-header `kid`, a local octet-key JWKS, or a remote JWKS URL. Server-side asymmetric signing and automatic key rotation are not implemented in Phase 1.

For the canonical list of unsupported Phase 1 behavior, see [A2A Protocol](./overview.md#phase-1-unsupported).

## Push Notification Delivery Backends

`iac-code a2a --config a2a-server.yml` supports two push delivery queues:

- `push-queue: local-file` stores jobs below the A2A persistence directory and is intended for local single-node use.
- `push-queue: redis-streams` stores jobs in Redis Streams and coordinates workers through a Redis consumer group.

Redis-backed push delivery requires the optional `a2a-redis` extra and is at-least-once. Callback receivers should handle task updates idempotently because a job can be delivered again after worker crashes, lease expiry, reconnects, or retry races.

Common Redis options:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

Callback URLs are validated before storage and again before dispatch. The default validator rejects non-HTTP(S) URLs, localhost hostnames, and literal private/local IP addresses. Callback receivers should still enforce their own authentication and idempotency policy.

## JSON-RPC Methods

### SendMessage

Runs a non-streaming A2A message turn. The response contains a task or message after the turn has completed.

**Request**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {
          "cwd": "/absolute/path/to/project",
          "user_id": "client-user-123",
          "iac_code_model": "qwen-plus",
          "iac_code_api_key": "provider-api-key"
        }
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**Required message fields**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `messageId` | string | Yes | Unique client message ID |
| `role` | string | Yes | Use `ROLE_USER` for user input |
| `parts` | array | Yes | Text-like, JSON data, raw text, local file URL, or bounded multimodal parts |
| `metadata.iac_code.cwd` | string | Recommended | Absolute workspace path; defaults to the server process directory if omitted |
| `metadata.iac_code.user_id` | string | Optional | Per-task telemetry user ID override; ignored when blank or non-string |
| `metadata.iac_code.iac_code_model` | string | Optional | Per-call LLM model override; this is the lowercase form of `IAC_CODE_MODEL` and is ignored when blank or non-string |
| `metadata.iac_code.iac_code_api_key` | string | Optional | Per-call LLM provider API key override; this is the lowercase form of `IAC_CODE_API_KEY` and is ignored when blank or non-string |
| `metadata.iac_code.alibaba_cloud_access_key_id` | string | Optional | Alibaba Cloud AccessKey ID for this task |
| `metadata.iac_code.alibaba_cloud_access_key_secret` | string | Optional | Alibaba Cloud AccessKey Secret for this task |
| `metadata.iac_code.alibaba_cloud_region_id` | string | Optional | Alibaba Cloud region for this task; defaults to `cn-hangzhou` when omitted with task credentials |
| `metadata.iac_code.alibaba_cloud_security_token` | string | Optional | Alibaba Cloud STS token for this task |

`metadata.iac_code.cwd` must be an absolute directory path when provided. It must resolve inside an allowed workspace root. If it already exists, it must be a directory; if it does not exist, the executor creates it under the allowed root. By default, allowed roots are the server process directory and the system temp directory; `IACCODE_A2A_ALLOWED_CWDS` can provide an OS-path-separated allowlist.

When `metadata.iac_code` includes both `alibaba_cloud_access_key_id` and `alibaba_cloud_access_key_secret`, the A2A executor uses those Alibaba Cloud credentials only for the current task. They take priority over process environment variables and `.cloud-credentials.yml`; if the task metadata is incomplete or absent, normal credential fallback still applies.

`metadata.iac_code.user_id` only affects telemetry identity for the current task. It does not change the A2A `contextId`, `taskId`, or iac-code's internal session ID.

`metadata.iac_code.iac_code_model` only affects the current A2A message turn. It takes priority over `IAC_CODE_MODEL`, `settings.yml`, and the server startup default model. Follow-up turns without this metadata field fall back to the server default model even when they reuse the same `contextId`.

`metadata.iac_code.iac_code_api_key` only affects the current A2A message turn. It takes priority over `IAC_CODE_API_KEY` and `.credentials.yml` for the provider selected by the effective model. Follow-up turns without this metadata field reload normal credentials, so a per-call key does not leak across reused `contextId`s. This field is for the LLM provider key and is separate from A2A transport authentication such as `api-key` / `IACCODE_A2A_API_KEY`.

Supported input categories:

| Category | Accepted Shape | Limits and Behavior |
|----------|----------------|---------------------|
| Text-like parts | `text` with `text/plain`, JSON, Markdown, YAML, or configured extra text MIME types | Appended directly to the prompt |
| JSON data parts | `data` with `application/json` | Serialized into compact JSON; max 1 MiB inline |
| Raw text parts | `raw` with a text-like MIME type | Must be valid UTF-8; max 1 MiB inline |
| Local text file URLs | `url` with `file://...` and text-like MIME type | File must exist inside `cwd` and allowed roots; max 1 MiB |
| Multimodal raw/data/file parts | image, audio, or configured multimodal MIME types | Converted into a prompt manifest with filename, media type, byte size, hash, and source; raw/data max 5 MiB, file URL max 25 MiB |

Remote HTTP(S) URL ingestion is not supported. File URL parts must use local `file://` URLs and remain inside the allowed workspace.

### SendStreamingMessage

Runs a streaming A2A message turn. The request body has the same shape as `SendMessage`, but the server streams JSON-RPC responses as Server-Sent Events.

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

Returns the saved A2A task by ID. Use `historyLength` to limit returned history without mutating stored task history. Omit it to receive the server's current default history.

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

Returns known tasks visible to the authenticated caller. Results are sorted by status timestamp descending, then task ID descending for stable ordering. The server supports `contextId`, `status`, `pageSize`, `pageToken`, `historyLength`, and `includeArtifacts`.

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

`nextPageToken` is returned when another page is available. `includeArtifacts` defaults to `false`, so list responses omit task artifacts unless explicitly requested.

### CancelTask

Requests cancellation for a running task.

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

If the task is active, the server cancels the running agent turn and emits a canceled task state. If the task exists but is not running, the server returns the standard A2A `TaskNotCancelableError`.

### SubscribeToTask

Subscribes to an active task update stream when supported by the client transport.

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

For active tasks, the stream starts with the current `Task`, then emits subsequent task events and closes when the active turn finishes. Subscribing to a completed, failed, canceled, or input-required task returns a task-not-found style error instead of waiting indefinitely. For new turns, prefer `SendStreamingMessage`; it starts execution and streams the response in one request.

### Push Notification Config Methods

When the server starts with `push-notifications: true`, it supports:

| Method | Purpose |
|--------|---------|
| `CreateTaskPushNotificationConfig` | Store a callback config for a task |
| `GetTaskPushNotificationConfig` | Fetch one callback config |
| `ListTaskPushNotificationConfigs` | List callback configs for a task |
| `DeleteTaskPushNotificationConfig` | Delete a callback config |

Example create request:

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

The server encrypts stored notification tokens and callback authentication credentials when the local push keyring is available.

### GetExtendedAgentCard

Authenticated clients can request the extended Agent Card:

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

The extended card includes the public card plus authenticated runtime details.

## Task and Context Behavior

iac-code maps A2A contexts to internal agent runtimes:

| Concept | Behavior |
|---------|----------|
| `contextId` omitted | The SDK/server generates a new context ID |
| Same `contextId` | Reuses the same internal iac-code session and conversation state |
| Same `contextId`, different `cwd` | Rejected as a different workspace |
| Same `contextId`, concurrent message | Rejected with `Task is already working.` |
| Different `contextId` values | Can execute concurrently |
| Idle context | Evicted from memory after the configured idle timeout |

Task and context IDs must be non-empty, at most 128 characters, and contain only letters, digits, `_`, `.`, `:`, or `-`.

## Task States

| State | Meaning |
|-------|---------|
| `TASK_STATE_SUBMITTED` | The task was accepted |
| `TASK_STATE_WORKING` | iac-code is running the agent turn |
| `TASK_STATE_INPUT_REQUIRED` | The turn completed and the agent is ready for follow-up input |
| `TASK_STATE_CANCELED` | Cancellation was requested and applied |
| `TASK_STATE_FAILED` | The task failed validation or execution |

iac-code uses `TASK_STATE_INPUT_REQUIRED` as the normal completed state because the context remains available for follow-up messages.

## Streaming Updates

During execution, iac-code emits `TaskStatusUpdateEvent` updates.

Assistant text is delivered as a status message:

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

Tool and usage details are delivered through `metadata.iac_code`:

| Metadata Path | Description |
|---------------|-------------|
| `iac_code.tool.status` | `started`, `input_delta`, `input_complete`, `completed`, or `failed` |
| `iac_code.tool.toolUseId` | Stable tool-use ID for correlating tool events |
| `iac_code.tool.name` | Tool name when available |
| `iac_code.tool.inputSummary` | Shape-only summary for completed tool input; string values and non-whitelisted field names are represented without raw business payloads |
| `iac_code.tool.result` | Tool result, truncated to 4000 characters per field |
| `iac_code.iacCodeSessionId` | Internal iac-code execution session ID for correlating logs and local session artifacts |
| `iac_code.permission.autoApproved` | Whether A2A approved the permission request automatically or through a resolver |
| `iac_code.permission.toolName` | Tool that requested permission |
| `iac_code.permission.safeSummary` | Redacted human-readable permission summary |
| `iac_code.permission.inputSummary` | Safe operation summary for `aliyun_api` permission requests |
| `iac_code.permission.toolInput` | Shape-only tool input for non-`aliyun_api` permission requests; string values use type, length, and fingerprint, and non-whitelisted field names may be fingerprinted |
| `iac_code.thinking.type` | `raw_thinking` when `raw-thinking` is enabled in `thinking-exposure` |
| `iac_code.thinking.text` | Raw provider reasoning chunk, truncated to 4000 characters, emitted only for trusted configurations that enable `raw-thinking` |
| `iac_code.usage.inputTokens` | Input token count for the turn |
| `iac_code.usage.outputTokens` | Output token count for the turn |
| `iac_code.usage.totalTokens` | Total token count for the turn |

When a tool result includes a supported text artifact payload, the server stores the payload locally, emits a standard `TaskArtifactUpdateEvent`, and records the artifact in the task `artifacts` field. The artifact part uses a `file://` URL plus metadata such as `mediaType`, `byteSize`, and `sha256`; the original artifact content is not duplicated inside tool metadata.

## Extensions

The Agent Card advertises the optional iac-code artifact metadata extension:

```text
urn:iac-code:a2a:artifact-metadata:v1
```

This extension identifies the `metadata.iac_code` namespace used for tool progress, permission decisions, token usage, and local artifact metadata. If the server is configured with any required extension, clients must include its URI in the `A2A-Extensions` header. Missing required extensions return the standard A2A `ExtensionSupportRequiredError`.

When `thinking-exposure` enables one or more signal types, the Agent Card also advertises:

```text
urn:iac-code:a2a:thinking-exposure:v1
```

The extension params include `enabledTypes`, such as `["tool_trace", "raw_thinking"]`. This is an iac-code metadata extension, not a core A2A streaming event type.

## Error Handling

| Scenario | Result |
|----------|--------|
| Empty text input | `TASK_STATE_FAILED` with `A2A server currently accepts text input only.` |
| Unsupported media type | Validation error or standard A2A content-type error, depending on where the SDK rejects the request |
| Remote URL part | Validation error because URL parts must use local `file://` URLs |
| File URL outside allowed workspace | Validation error |
| Missing required A2A extension | Standard A2A `ExtensionSupportRequiredError` |
| Invalid workspace metadata | `TASK_STATE_FAILED` with an invalid workspace message |
| Missing or invalid authentication | HTTP `401` with `{"error":"Unauthorized"}` |
| Missing A2A server dependencies | CLI exits with an install hint for the `a2a` extra |
| Provider credentials missing | Sanitized authentication error |
| Unexpected runtime error | Sanitized internal error |

The server avoids returning local paths, secrets, and provider details in unexpected error messages.
