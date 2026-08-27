---
sidebar_position: 2
title: Getting Started
description: Install, start, and call the iac-code AG-UI adapter.
---

# AG-UI Getting Started

## Prerequisites

1. Python 3.10 or later is installed.
2. An LLM provider is configured for iac-code. See [Authentication](../configuration/authentication.md).
3. If the task accesses Alibaba Cloud, cloud credentials are configured or temporary credentials are supplied per request.
4. An absolute workspace path is available for iac-code to read and write.

Install the AG-UI dependencies:

```bash
pip install "iac-code[agui]"
```

When developing from the source repository:

```bash
uv sync --extra agui
```

## Option 1: Start a managed local A2A kernel

For the simplest local setup, omit `--a2a-url`:

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

The adapter chooses an available loopback port, starts a managed `iac-code a2a` child process, and stops it when the adapter exits. The child inherits the current iac-code configuration and runtime environment.

This mode is suitable for local development and single-process lifecycle management. Use the next option when production process supervision needs to manage the two services independently.

## Option 2: Connect to an independent A2A kernel

Start the A2A server first:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

Then start the AG-UI adapter:

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

The services keep separate responsibilities and ports. A2A may continue serving A2A clients while the AG-UI adapter reaches it only through the loopback interface.

`--thinking-exposure all` lets the adapter convert raw thinking into standard `REASONING_*` events. Enable raw thinking only for trusted clients. Keep the A2A default, `tool-trace`, when reasoning content should not be exposed.

If the A2A server uses a bearer token:

```bash
export IACCODE_A2A_HTTP_TOKEN="a2a-local-secret"
iac-code a2a --host 127.0.0.1 --port 41242
```

Give the adapter the same upstream token:

```bash
export IAC_CODE_AGUI_A2A_TOKEN="a2a-local-secret"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## YAML configuration

Static startup settings can be stored in YAML:

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
interrupt-ttl: 540
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

Start the adapter with:

```bash
iac-code agui --config agui-server.yml
```

Explicit CLI arguments override YAML. Inject sensitive values such as tokens through environment variables instead of storing them in the config file.

Common settings:

| CLI / YAML | Default | Meaning |
|------------|---------|---------|
| `--host` / `host` | `127.0.0.1` | AG-UI HTTP bind address |
| `--port` / `port` | `8000` | AG-UI HTTP port; deployment examples use `41243` |
| `--a2a-url` / `a2a-url` | empty | Local A2A URL; empty starts a managed child |
| `--interrupt-ttl` / `interrupt-ttl` | `540` | Seconds an interrupt remains resumable |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | AG-UI thread-state directory |
| `--idle-shutdown` / `idle-shutdown` | `0` | Idle shutdown delay; `0` disables it |
| `--debug` / `debug` | `false` | Debug logging |
| `--log-stdout` / `log-stdout` | `false` | Mirror logs to stdout |

Related environment variables:

| Variable | Purpose |
|----------|---------|
| `IAC_CODE_AGUI_HOST` | AG-UI bind address |
| `IAC_CODE_AGUI_PORT` | AG-UI port |
| `IAC_CODE_AGUI_A2A_URL` | Local A2A upstream URL |
| `IAC_CODE_AGUI_A2A_TOKEN` | A2A upstream bearer token |
| `IAC_CODE_AGUI_AUTH_TOKEN` | Bearer token protecting the AG-UI endpoint |
| `IAC_CODE_AGUI_INTERRUPT_TTL` | Interrupt lifetime |
| `IAC_CODE_AGUI_STATE_DIR` | AG-UI thread-state directory |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | Allowed workspace roots, separated with the OS path separator |
| `IAC_CODE_CONFIG_DIR` | iac-code configuration root and default AG-UI state parent |

## Health check

```bash
curl http://127.0.0.1:41243/health
```

Example response:

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "current iac-code version"
}
```

## Use the official JavaScript client

Install the verified client version:

```bash
pnpm add @ag-ui/client@0.0.58
```

This example connects directly to `iac-code agui`. It uses the standard `HttpAgent` and supplies iac-code runtime properties in `forwardedProps`:

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const threadId = randomUUID();
const rosInvocationId = randomUUID();
const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId,
  // When IAC_CODE_AGUI_AUTH_TOKEN is configured:
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId,
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "en",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "Create a VPC template with two vSwitches.",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[tool] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[step] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

When the endpoint uses a bearer token, pass `Authorization` through `HttpAgent.headers`.

A browser application normally connects through a same-origin backend or reverse proxy. The adapter does not add a cross-origin policy.

## Handle interrupts

The official client keeps `RUN_FINISHED.outcome.interrupts` in `agent.pendingInterrupts`. Build each response from its `responseSchema`, then submit it in a new run:

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent({ forwardedProps, resume: responses }, subscriber);
```

This payload applies only to permission interrupts whose schema requires `decision`. Questions and option selection have their own schemas; do not assume every interrupt has the same shape.

A resume must:

- use the original `threadId`;
- use a new `runId` (the official client creates one by default);
- retain the interrupted execution's `rosInvocationId`;
- cover every currently pending interrupt in one request;
- provide a payload matching `responseSchema` for `status: "resolved"`;
- use `status: "cancelled"` when the user chooses not to continue.

## Start a Pipeline

Set `runMode` to `pipeline` and optionally select a Pipeline:

```javascript
const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "pipeline",
    pipelineName: "selling",
    candidatePresentation: "rich",
  },
};
```

Clients should handle `STEP_*`, `TOOL_CALL_*`, `ACTIVITY_SNAPSHOT`, and `CUSTOM`. A generic client that does not recognize iac-code custom events can still process all standard events normally.

## Workspace and temporary credentials

`cwd` is not fixed at server startup. Every request must provide an absolute path under a root allowed by `IAC_CODE_AGUI_ALLOWED_CWDS` or `IACCODE_A2A_ALLOWED_CWDS`.

The caller may supply a per-request model, LLM key, and Alibaba Cloud temporary credentials through `forwardedProps.iacCode`. The adapter does not write those secrets to its thread-state file. It forwards them to the A2A execution kernel, which applies the normal A2A request override rules.

## State directory

The default layout is:

```text
<IAC_CODE_CONFIG_DIR>/agui/
  threads/
    <threadId>.json
```

Each thread is written independently, and startup does not scan all historical threads. Normal UUIDs remain readable. Unsafe IDs are encoded, and unusually long IDs use a fixed-length file key. The JSON document always stores and validates the original `threadId`.

This directory stores only adapter mappings, interrupts, and idempotency state. It does not store conversation content or request credentials. Do not edit its JSON files manually.

## Next steps

- [AG-UI overview](./overview.md)
- [Protocol reference](./protocol-reference.md)
