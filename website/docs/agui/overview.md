---
sidebar_position: 1
title: AG-UI Protocol
description: Architecture, capabilities, and use cases for iac-code's AG-UI integration.
---

# AG-UI Protocol

## What is AG-UI?

The [Agent-User Interaction Protocol (AG-UI)](https://docs.ag-ui.com/concepts/architecture) is an event-stream protocol between agents and user-facing applications. A client starts a run with `RunAgentInput` and receives structured text, reasoning, tool-call, step, state, and interrupt events over HTTP Server-Sent Events (SSE).

AG-UI is a good fit for web consoles, chat clients, IDE extensions, and other applications that need to show agent execution in real time. Instead of consuming only final text, an AG-UI client can render model output, tool arguments and results, Pipeline steps, and operations awaiting user confirmation separately.

## iac-code architecture

iac-code uses an **A2A execution kernel with an AG-UI protocol adapter**:

```text
AG-UI client
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Agent loop / Pipeline / LLM / Alibaba Cloud API
```

`iac-code a2a` is the sole execution kernel. It owns:

- normal conversations and Pipeline execution;
- iac-code sessions, A2A contexts, and tasks;
- tool permissions, questions, option selection, and recovery;
- execution lifecycle and cancellation;
- LLM and Alibaba Cloud API calls.

`iac-code agui` does not create a second Agent runtime or execute Pipelines directly. It only:

- converts AG-UI `RunAgentInput` into A2A requests;
- projects A2A events into standard AG-UI events;
- maps `threadId/runId` to A2A `contextId/taskId`;
- converts AG-UI `resume[]` into A2A input recovery;
- persists protocol mappings and pending interrupts;
- forwards cancellation to A2A.

As a result, AG-UI and A2A do not maintain separate execution semantics. Model selection, cloud credentials, permission rules, and Pipeline behavior are ultimately handled by the same A2A runtime.

## Standard protocol and iac-code extensions

The external stream uses standard AG-UI events, including:

- `RUN_STARTED`, `RUN_FINISHED`, and `RUN_ERROR`;
- `TEXT_MESSAGE_*`;
- `REASONING_*`;
- `TOOL_CALL_*`;
- `STEP_STARTED` and `STEP_FINISHED`;
- `ACTIVITY_SNAPSHOT`.

Only useful iac-code Pipeline information without a standard equivalent is emitted as a namespaced `CUSTOM` event. A generic AG-UI client may ignore those events without affecting text, tool calls, interrupts, or the run lifecycle.

Requests remain standard `RunAgentInput` envelopes. iac-code uses the standard `forwardedProps` field for the workspace, run mode, and other required runtime data:

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "request-identity",
      "cwd": "/absolute/workspace/path",
      "runMode": "normal"
    }
  }
}
```

A generic AG-UI client can therefore consume iac-code's standard events directly. When it calls `iac-code agui` directly, it must still provide runtime fields such as `cwd` under `forwardedProps.iacCode`.

## Supported interactions

### Multi-turn normal conversations

Keep the same `threadId` for the conversation and use a new `runId` for each user turn. The adapter binds the thread to one iac-code session. After one turn completes, the next message starts with a new HTTP/SSE request; it never continues on the previous, already completed SSE response.

### Pipeline

Set `forwardedProps.iacCode.runMode` to `pipeline`. The A2A Pipeline kernel still performs the execution. Top-level steps become standard `STEP_*` events, and agent text, reasoning, and tools use their corresponding standard events. Candidate information, stack progress, and cleanup progress that have no standard equivalent are emitted through `iac-code.pipeline.v1` custom events.

Parallel sub-pipelines use distinct message and step identities, so text from multiple agent loops is not merged into one message.

### Interrupt and resume

When a permission request, question, or option selection needs user input, the current run ends with:

```json
{
  "type": "RUN_FINISHED",
  "outcome": {
    "type": "interrupt",
    "interrupts": []
  }
}
```

The interrupt is persisted before it becomes visible to the client. The client collects answers, then starts a new request with the same `threadId`, a new `runId`, and `resume[]`. The resume stream belongs to this new request; it does not reconnect to the old stream.

### Adapter state

The adapter stores protocol mappings, idempotency data, and pending interrupts in one file per thread. This directory does not contain conversation text, LLM keys, or cloud credentials, and it is not a conversation export directory.

## When to use AG-UI

| Requirement | Recommended mode |
|-------------|------------------|
| Build a chat UI with live text, reasoning, tools, and steps | **AG-UI** |
| Handle permissions, questions, and option selection in a UI | **AG-UI** |
| Let another agent or orchestrator call iac-code directly | **A2A** |
| Integrate an IDE/editor with ACP sessions and terminal features | **ACP** |
| Operate iac-code manually | **Interactive REPL or Web/Desktop** |

AG-UI and A2A can run at the same time. They expose separate HTTP endpoints while sharing the same iac-code execution implementation.

## Current boundaries

- The AG-UI transport is HTTP POST plus SSE.
- The A2A upstream must use a loopback address; the adapter refuses arbitrary remote A2A URLs.
- `cwd` is required per request and must be under an allowed workspace root.
- Client-defined `tools` are not currently accepted; iac-code owns the tool set.
- User messages support text and inline base64 images, not remote media URLs.
- If the client disconnects from an active SSE run before it reaches an interrupt, the adapter cancels the matching A2A task.
- The SSE stream sends a comment heartbeat every 15 seconds. Conforming clients ignore it.

## Next steps

- [Getting started](./getting-started.md) — Install, start, and connect a first client.
- [Protocol reference](./protocol-reference.md) — Request fields, events, interrupt/resume, persistence, and error semantics.
