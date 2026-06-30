---
title: HTTP Transport
description: Run and call the iac-code A2A server over JSON-RPC HTTP.
sidebar_position: 5
---

# HTTP Transport

iac-code's default A2A server exposes JSON-RPC over HTTP, plus the A2A SDK REST routes. The server is built with Starlette and runs on Uvicorn.

## Starting the Server

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

Install the optional server dependencies first:

```bash
uv sync --extra a2a
```

## Endpoint Summary

| Route | Method | Response |
|-------|--------|----------|
| `/health` | `GET` | Plain JSON health response |
| `/.well-known/agent-card.json` | `GET` | Agent Card JSON |
| `/` | `POST` | JSON-RPC response or SSE stream |
| SDK REST routes | mixed | A2A REST endpoints registered by the SDK |

## Headers

Recommended headers:

```text
Content-Type: application/json
A2A-Version: 1.0
```

When Bearer auth is enabled:

```text
Authorization: Bearer <token>
```

## Authentication

The server supports optional Bearer token, Basic auth, and API key authentication. If no authentication options or environment variables are set, requests do not need auth. If one or more schemes are configured, a request can authenticate with any configured scheme.

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

You can also set `token` in the A2A YAML config file.

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Both username and password must be set for Basic auth to be enabled.

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

The default API key header is `X-API-Key`. You can change it in YAML:

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

or with `IACCODE_A2A_API_KEY_HEADER`.

| Scenario | Behavior |
|----------|----------|
| No auth scheme configured | No authentication required |
| One or more schemes configured, any one matches | Request proceeds |
| One or more schemes configured, no scheme matches | HTTP `401` with `{"error":"Unauthorized"}` |

## Agent Card Discovery

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Authenticated:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

With API key authentication:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

The JSON-RPC endpoint URL is advertised in `supportedInterfaces[0].url`. HTTP mode also advertises an `HTTP+JSON` interface for REST-capable clients.

## Non-streaming Message

`SendMessage` returns a single JSON-RPC response after the agent turn finishes.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Create a Terraform VPC module for Alibaba Cloud."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Streaming Message

`SendStreamingMessage` returns Server-Sent Events. Use `curl -N` to print events as they arrive.

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "stream-1",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Generate a ROS template for one VPC and two vSwitches."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Each SSE `data:` line contains one JSON-RPC response whose `result` is an A2A `StreamResponse`.

## Follow-up Message

Use the `taskId` and `contextId` returned by the first response to continue the same conversation.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-3",
        "taskId": "task-id-from-first-response",
        "contextId": "context-id-from-first-response",
        "role": "ROLE_USER",
        "parts": [{"text": "Now add tags for environment and owner."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

The workspace must remain the same for the reused `contextId`.

## Cancel a Running Task

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "CancelTask",
    "params": {
      "id": "task-id"
    }
  }'
```

Cancellation is cooperative: iac-code cancels the active agent turn, emits a canceled state, and releases the context lock. Canceling an existing task that is no longer running returns the standard A2A `TaskNotCancelableError`.

## CLI Equivalents

Most HTTP workflows have a matching CLI command:

```yaml
url: http://127.0.0.1:41242/
```

```bash
# Discover the Agent Card
iac-code a2a-client --config a2a-client.yml discover

# Send a non-streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a Terraform VPC module for Alibaba Cloud." \
  --cwd "$PWD"

# Send a streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a ROS template for one VPC and two vSwitches." \
  --cwd "$PWD" \
  --stream

# Inspect task state
iac-code a2a-client --config a2a-client.yml task-get --task-id task-id
iac-code a2a-client --config a2a-client.yml task-list --output table

# Cancel an active task
iac-code a2a-client --config a2a-client.yml task-cancel --task-id task-id
```

For the full option list, see [Command Reference](./command-reference.md).

## Operational Notes

- Bind to `127.0.0.1` for local-only usage.
- Use `token` in the A2A config or `IACCODE_A2A_HTTP_TOKEN` before binding to a shared network interface.
- A2A mode rejects tool permission requests automatically unless `auto-approve-permissions` or an explicit permission rule allows them. Permission decisions are audited locally; any allow decision that requires an audit record fails closed if that record cannot be persisted. Protected Alibaba Cloud write APIs require exact per-API authorization outside blanket bypass modes.
- Active runtime state is in memory. Persistence mirrors task and context metadata, but restarting the process does not resume in-flight asyncio work.
- One context can run only one task at a time; separate contexts can run concurrently.
