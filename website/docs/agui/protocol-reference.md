---
sidebar_position: 3
title: Protocol Reference
description: Reference for iac-code AG-UI requests, events, interrupts, resume, cancellation, and persistence.
---

# AG-UI Protocol Reference

This page describes the HTTP/SSE surface exposed by `iac-code agui` and the iac-code extension fields carried in standard AG-UI envelopes. See the [overview](./overview.md) and [getting started](./getting-started.md) pages first.

## HTTP endpoints

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | Health and protocol version information |
| `POST /` | Submit `RunAgentInput` and receive an SSE event stream |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | Namespaced cancellation extension |

The `POST /` body must use JSON, and clients should request SSE:

```http
Content-Type: application/json
Accept: text/event-stream
```

When `IAC_CODE_AGUI_AUTH_TOKEN` is configured, protected requests also require:

```http
Authorization: Bearer <token>
```

Use the standard `Accept-Language` header as an error-message fallback. `forwardedProps.iacCode.preferredLanguage` takes precedence and is also forwarded to the A2A runtime.

## RunAgentInput

Minimal normal-run example:

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [
    {"id": "message-1", "role": "user", "content": "Create a VPC template"}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1",
      "runMode": "normal"
    }
  }
}
```

### Standard fields

| Field | Requirement | iac-code behavior |
|-------|-------------|-------------------|
| `threadId` | Required non-empty string | Stable conversation identity mapped to one A2A context and iac-code session |
| `runId` | Required non-empty string | One HTTP/SSE run; cannot be reused within the thread |
| `parentRunId` | Optional | Copied to `RUN_STARTED` |
| `state` | Required | Kept in the standard envelope; not used as iac-code runtime state |
| `messages` | Required | A new run uses the latest user message; a resume need not add one |
| `tools` | Required and empty | Client-defined tools are not supported |
| `context` | Required | Kept in the envelope; not currently converted into prompt context |
| `forwardedProps` | Required | Must contain the `iacCode` extension |
| `resume` | For resume | One response for every pending interrupt |

User messages support strings, `text` parts, and `image` parts with inline base64 `data` sources. Remote image URLs, audio, video, document, and generic binary parts are not supported. A decoded image is limited to 8 MiB, all images to 10 MiB, and the full HTTP request to 12 MiB.

## `forwardedProps.iacCode`

This object uses a strict schema; unknown fields are rejected.

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `schemaVersion` | `1` | Yes | iac-code extension version |
| `rosInvocationId` | string | Yes | Current execution caller identity, up to 256 characters |
| `cwd` | string | Yes | Absolute workspace path |
| `model` | string | No | Per-request model override |
| `llmApiKey` | string | No | Per-request LLM provider key |
| `thinking.enabled` | boolean | No | Request thinking output |
| `thinking.effort` | string | No | Provider-specific thinking effort |
| `thinking.budget` | positive integer | No | Provider-specific thinking budget |
| `userId` | string | No | Telemetry and caller-binding identity |
| `channel` | string | No | Caller channel metadata |
| `preferredLanguage` | string | No | Request-local user-facing language, such as `en` |
| `candidatePresentation` | `standard` or `rich` | No | Pipeline candidate presentation |
| `runMode` | `normal` or `pipeline` | No | Execution mode; otherwise chosen by A2A |
| `pipelineName` | string | No | Pipeline name, for example `selling` |
| `cleanupOnly` | boolean | No | Request a Pipeline cleanup-only path |
| `alibabaCloud.accessKeyId` | string | No | Request-local AccessKey ID |
| `alibabaCloud.accessKeySecret` | string | No | Request-local AccessKey secret |
| `alibabaCloud.securityToken` | string | No | Request-local STS token |
| `alibabaCloud.regionId` | string | No | Request-local default region |

The initial run and its interrupt resumes must retain the same `rosInvocationId`. A later normal turn may use a new value. Cancellation must use the current execution's value.

A `threadId` is bound to the first request's `cwd` and `userId`; later requests cannot move the same thread to another workspace or caller.

## SSE and heartbeat

Each AG-UI event is emitted as an SSE `data:` record. After 15 seconds without an event, the server emits:

```text
: heartbeat
```

This is an SSE comment, not an AG-UI `CUSTOM` event. Conforming clients ignore it while it keeps the HTTP connection active.

## Standard event mapping

| A2A/iac-code signal | AG-UI output |
|---------------------|--------------|
| Accepted request | `RUN_STARTED` |
| Agent text | `TEXT_MESSAGE_START/CONTENT/END` |
| Raw thinking | `REASONING_START`, `REASONING_MESSAGE_*`, `REASONING_END` |
| Tool start and arguments | `TOOL_CALL_START/ARGS/END` |
| Tool result | `TOOL_CALL_RESULT` |
| Pipeline step lifecycle | `STEP_STARTED/STEP_FINISHED` |
| Pipeline recovery snapshot | `ACTIVITY_SNAPSHOT` |
| Normal completion | `RUN_FINISHED` with `outcome.type = "success"` |
| User input required | `RUN_FINISHED` with `outcome.type = "interrupt"` |
| Adapter or A2A error | `RUN_ERROR` |

`RUN_FINISHED` ends one AG-UI run, not necessarily the whole Pipeline. A Pipeline interrupted several times has several runs, each with its own `RUN_STARTED` and `RUN_FINISHED`. Pipeline business completion is represented by `pipeline_completed`, `pipeline_error`, and related Pipeline events.

To keep AG-UI spans balanced, the adapter closes open message, reasoning, tool, and step spans before an interrupt ends a run. The resume run reopens any durable Pipeline step still active. Raw event review may therefore show the same business step closing in one run and reopening in the next; this is not reversed execution.

## iac-code custom events

### `iac-code.session.v1`

Exposes the current adapter-to-A2A mapping, including `threadId`, `aguiRunId`, `executionId`, `contextId`, `taskId`, `rosInvocationId`, and `sessionId`. Use `executionId` with the cancellation extension. Generic clients may safely ignore this event.

### `iac-code.artifact.v1`

Carries a structured projection of an A2A task artifact for optional preview, download, or diagnostics.

### `iac-code.tool-progress.v1`

Carries intermediate tool progress without a standard equivalent. Tool start, arguments, and final result remain standard `TOOL_CALL_*` events and are not duplicated here.

### `iac-code.pipeline.v1`

Only useful Pipeline information without a complete standard equivalent is emitted. Current `eventType` values are:

- Pipeline: `pipeline_started`, `pipeline_resumed`, `pipeline_completed`, `pipeline_error`, `pipeline_warning`, `backup_blocked`;
- candidates: `candidate_started`, `candidate_completed`, `candidate_failed`, `candidate_interrupted`, `candidate_restart_requested`, `candidate_selected`, `candidate_detail_shown`, `candidate_step_failed`;
- sub-pipelines and step errors: `sub_pipeline_started`, `sub_pipeline_completed`, `sub_step_failed`, `step_failed`;
- stacks and cleanup: `stack_progress`, `stack_instances_progress`, `stack_current_changed`, `cleanup_started`, `cleanup_progress`, `cleanup_completed`, `cleanup_failed`;
- rollback: `rollback_triggered`, `rollback_completed`;
- context: `context_compaction_started`, `context_compacted`, `context_compaction_failed`, `fields_marked_stale`;
- presentation and tools: `diagram_shown`, `mcp_status`, `tool_progress`.

Signals with standard mappings are not duplicated as `CUSTOM`: `text_delta` becomes `TEXT_MESSAGE_*`, `thinking_delta` becomes `REASONING_*`, `tool_started/tool_result` become `TOOL_CALL_*`, `usage` becomes `RUN_FINISHED.usage`, and step lifecycles become `STEP_*`.

Clients should deduplicate replayed Pipeline events with `(name, value.eventId)` or the Pipeline sequence and tolerate unknown namespaced custom events.

## Interrupt

An input-required run ends with `RUN_FINISHED.outcome.type = "interrupt"`. Each interrupt includes:

- `id` and `reason`;
- a user-facing `message`;
- an optional `toolCallId`;
- a JSON `responseSchema`;
- metadata such as `title`, `purpose`, `safeSummary`, `options`, and `toolName`.

The adapter does not impose an Interrupt deadline. A pending Interrupt remains resumable until A2A resolves, cancels, or terminates the task; A2A alone owns execution and recovery lifecycle.

For a permission request, the response schema typically accepts:

```json
{"decision": "allow_once"}
```

or:

```json
{"decision": "deny"}
```

Render `message`, `responseSchema`, and descriptive metadata instead of inferring the UI from `reason` alone. Questions and option selection may use different schemas.

## Resume

A resume is a new `POST /` with the same `threadId`, a new `runId`, the same `rosInvocationId`, and one entry per pending interrupt:

```json
{
  "resume": [
    {
      "interruptId": "permission-1",
      "status": "resolved",
      "payload": {"decision": "allow_once"}
    }
  ]
}
```

Rules:

- every pending interrupt must be answered exactly once;
- duplicate and unknown IDs are rejected;
- `resolved` requires a payload matching the corresponding schema;
- `cancelled` stops that interrupt, and maps to `deny` for permissions;
- durable pending state is removed only after A2A accepts the response;
- schema errors produce `RUN_ERROR` while leaving the interrupt retryable;
- repeated accepted responses do not execute the tool again.

Before applying a resume, the adapter can ask A2A to restore the iac-code session, verifies the A2A task/context identity, and catches up missing Pipeline events.

## Turns and identities

```text
threadId (stable conversation)
  ├─ runId-1 (user turn)
  ├─ runId-2 (interrupt resume)
  ├─ runId-3 (another resume)
  └─ runId-4 (next normal message)
```

Every HTTP/SSE request uses a unique `runId`. Interrupt resume is a new run. After a normal turn completes, the next message creates a new execution while reusing the thread's iac-code session. Run idempotency is scoped to `(threadId, runId)`.

## Cancellation extension

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{"threadId": "thread-1", "rosInvocationId": "invocation-1"}
```

Possible outcomes are `cancelled`, `already_terminal`, or HTTP `404` with `EXECUTION_NOT_FOUND`. Cancellation clears pending interrupts and does not change standard AG-UI event formats.

## Persistence and recovery

Adapter state defaults to:

```text
<config-dir>/agui/threads/<thread-key>.json
```

Each file contains thread/context/workspace binding, session and task identity, execution identity, Pipeline recovery positions, pending interrupts, and run/resume idempotency data. The adapter lazily loads one requested thread and atomically replaces only that thread's small file.

It never stores LLM keys, AccessKey secrets, or STS tokens. This is an adapter mapping directory, not a store for conversation text or execution artifacts. A2A manages its own session and task persistence; see the [A2A documentation](../a2a/overview.md).

## Disconnections

- A run safely finished with an interrupt no longer depends on its SSE connection.
- Resume creates a new SSE connection.
- Disconnecting an ordinary active run causes the adapter to cancel the A2A task.
- Disconnecting after an interrupt does not delete its persisted recovery state.

## Errors

Errors before SSE begins use an HTTP JSON envelope. Errors during execution use standard `RUN_ERROR` events. Common codes include:

| Code | Meaning |
|------|---------|
| `INVALID_INPUT` | Invalid envelope, extension fields, message content, or workspace |
| `DUPLICATE_RUN_ID` | The same request digest used an existing run ID |
| `RUN_ID_CONFLICT` | A different request reused an existing run ID |
| `THREAD_BUSY` | The thread already has an active run |
| `THREAD_BINDING_CONFLICT` | The thread's workspace or caller conflicts with its binding |
| `RESUME_REQUIRED` | The thread is waiting for interrupt responses |
| `INCOMPLETE_RESUME` | Missing pending interrupts or duplicate IDs |
| `UNKNOWN_INTERRUPT` | Resume references an unknown interrupt |
| `RESUME_PAYLOAD_INVALID` | Missing payload or schema mismatch |
| `RESUME_ALREADY_APPLIED` | The response was already applied or conflicts with it |
| `EXECUTION_LOST` | Adapter, A2A task, or iac-code session could not be recovered |
| `STATE_PERSISTENCE_FAILED` | Recovery-critical state could not be committed |
| `A2A_UNAVAILABLE` | The local A2A execution service is unavailable |
| `A2A_PROTOCOL_ERROR` | A2A task/context/session identity conflicts with the mapping |
| `A2A_EXECUTION_FAILED` | The A2A task ended in failure |
| `CANCELLED` | The execution was cancelled |

Recovery-critical writes fail closed. The adapter does not announce a recoverable task, session, or interrupt before its mapping is durable, and cancels the matching A2A task when necessary.
