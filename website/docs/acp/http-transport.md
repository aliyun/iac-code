---
title: HTTP+SSE Transport
description: Run the ACP server over HTTP with Server-Sent Events for remote and multi-client scenarios.
sidebar_position: 5
---

# HTTP+SSE Transport

iac-code's ACP server supports two transport modes. The default **Stdio** transport communicates over standard input/output and is ideal for local IDE integrations. The **HTTP+SSE** transport exposes a network endpoint and streams responses via Server-Sent Events, making it suitable for remote deployments, load-balanced environments, and multi-client access.

## Why HTTP+SSE

Stdio has inherent limitations:

- Requires the server process to be a direct child of the client — no remote access.
- Blocking process management makes it difficult to serve multiple clients concurrently.
- Incompatible with network proxies, load balancers, or containerized deployments.

HTTP+SSE addresses these constraints:

- **Network-friendly** — accessible from any machine that can reach the endpoint.
- **Multi-client** — each client gets an isolated connection with its own event stream.
- **Infrastructure-ready** — works behind reverse proxies, in containers, and with standard HTTP monitoring tools.
- **Easy integration** — any HTTP client (curl, fetch, SDK) can interact with the server.

## Starting the HTTP Server

```bash
# Default port 8765
iac-code acp --transport http

# Custom port
iac-code acp --transport http --port 9090
```

The server uses [Starlette](https://www.starlette.io/) as the ASGI framework and runs on Uvicorn.

## Routes

All routes are served at the `/acp` path. The HTTP method determines the operation.

### `POST /acp`

Send a JSON-RPC request to the server.

- **`initialize`** — Creates a new connection and returns the full JSON-RPC response directly. The response includes an `Acp-Connection-Id` header.
- **All other methods** — Requires a valid `Acp-Connection-Id` header. Returns `202 Accepted` immediately; the actual result is delivered asynchronously over the SSE stream.

### `GET /acp`

Opens a Server-Sent Events stream to receive responses and notifications.

- Requires the `Acp-Connection-Id` header.
- Events have type `message` with the JSON-RPC response/notification as the `data` field.
- The stream includes `id` and `retry` fields for automatic reconnection.

### `DELETE /acp`

Closes the connection and releases all associated resources.

- Requires the `Acp-Connection-Id` header.
- Returns `200 OK`.

## Connection ID

The Connection ID ties together a client's requests and its SSE event stream.

1. The client sends a `POST /acp` with the `initialize` method.
2. The server responds with the initialize result and an `Acp-Connection-Id` response header containing a UUID.
3. All subsequent requests (`POST`, `GET`, `DELETE`) must include the `Acp-Connection-Id` request header with this value.
4. Each Connection ID maps to an independent ACP agent session with its own event queue.

If a request references a missing or invalid Connection ID, the server returns `400 Bad Request`.

## Authentication

The server supports optional Bearer token authentication via the `IACCODE_ACP_HTTP_TOKEN` environment variable.

```bash
# Set the token before starting the server
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

When set, every request must include:

```
Authorization: Bearer your-secret-token
```

| Scenario | Behavior |
|----------|----------|
| Token not set | No authentication required (suitable for local development) |
| Token set, header matches | Request proceeds normally |
| Token set, header missing/wrong | `401 Unauthorized` returned |

## Complete Workflow

Below is a full interaction using `curl`:

```bash
# Step 1: Initialize — creates a connection and returns the Connection ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# Step 2: Open the SSE stream (run in background)
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# Step 3: Create a session
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# Step 4: Send a prompt
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# Step 5: Close the connection
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# Clean up background SSE process
kill $SSE_PID 2>/dev/null
```

:::tip
The `initialize` response is returned synchronously (within a 30-second timeout). All subsequent responses arrive exclusively through the SSE stream opened in Step 2.
:::
