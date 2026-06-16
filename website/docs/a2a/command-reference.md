---
title: Command Reference
description: Complete CLI command reference for running and calling iac-code over A2A.
sidebar_position: 3
---

# A2A Command Reference

This page documents every A2A-related `iac-code` command. Use it when you need exact option names, common command patterns, and the operational meaning of each flag.

## Command Overview

| Command | Purpose |
|---------|---------|
| `iac-code a2a` | Run iac-code as an A2A server |
| `iac-code a2a-client call` | Discover a remote Agent Card and send a prompt |
| `iac-code a2a-client discover` | Fetch and optionally verify an Agent Card |
| `iac-code a2a-client task-get` | Fetch one task by ID |
| `iac-code a2a-client task-list` | List tasks with filters and pagination |
| `iac-code a2a-client task-cancel` | Cancel an active task |
| `iac-code a2a-client task-subscribe` | Subscribe to an active task event stream |
| `iac-code a2a-client push-config-create` | Create a task push notification config |
| `iac-code a2a-client push-config-get` | Fetch one task push notification config |
| `iac-code a2a-client push-config-list` | List task push notification configs |
| `iac-code a2a-client push-config-delete` | Delete a task push notification config |
| `iac-code a2a-client extended-card` | Fetch the authenticated extended Agent Card |
| `iac-code a2a-client route-preview` | Preview local route selection for `a2a-client call` |

All HTTP client commands accept the same authentication options:

| Option | Description |
|--------|-------------|
| `--token` | Bearer token sent as `Authorization: Bearer <token>` |
| `--basic-username` | Basic auth username |
| `--basic-password` | Basic auth password |
| `--api-key` | API key value |
| `--api-key-header` | API key header name; defaults to `X-API-Key` |

## A2A Client Config

All `a2a-client` subcommands accept a YAML config file at the group level:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

CLI options override config values. Use config for stable connection, auth, verification, routing, and repeated task or push settings; keep one-off prompt text on the command line.

```yaml
url: http://127.0.0.1:41242/
token: your-bearer-token
basic-username: iac-code
basic-password: your-password
api-key: your-api-key
api-key-header: X-IAC-Code-Key
verify-card-secret: your-card-signing-secret
verify-card-jwks-url: https://a2a.example.com/.well-known/jwks.json
require-card-signature: true
timeout: 30
cwd: /path/to/workspace
context-id: ctx-123
iac-code-model: qwen-plus
task-id: task-123
config-id: webhook-1
callback-url: https://hooks.example.com/a2a
notification-token: notification-token
auth-scheme: bearer
auth-credentials: callback-token
routes:
  - name: ros
    url: http://127.0.0.1:41242/
    skills:
      - iac_generation
    tags:
      - ros
      - template
```

## `iac-code a2a`

Run iac-code as an A2A server.

```bash
iac-code a2a
```

By default, the server binds to `127.0.0.1:41242` and serves JSON-RPC over HTTP. Port `41242` is the iac-code default; it is not a registered A2A port.

### Basic Server Options

| Option | Default | Description |
|--------|---------|-------------|
| `--config` | empty | YAML config file containing A2A server options |
| `--host` | `127.0.0.1` | HTTP server host |
| `--port` | `41242` | HTTP server port |
| `--transport` | `http` | Server transport: `http`, `stdio`, `unix`, `websocket`, `grpc`, `grpc-jsonrpc`, or `redis-streams` |
| `--thinking-exposure` | `tool-trace` | Expose an A2A thinking signal type; repeat for multiple. Values: `raw-thinking`, `tool-trace` |
| `--debug`, `-d` | `false` | Enable debug logging |
| `--log-to-stdout` / `--no-log-to-stdout` | `false` | Mirror server logs to stdout. Not supported with `--transport stdio` because stdout carries A2A protocol frames |

Example:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### YAML Configuration

Use `--config` for authentication, storage, signing, transport-specific settings, push delivery, and other deployment details. Keys may use dashes or underscores. The common CLI flags `--host`, `--port`, and `--transport` override config-file values.

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
log-to-stdout: true
```

Run it with:

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### HTTP Authentication

Authentication is optional. Configure server authentication in YAML or with environment variables. If no auth setting is configured, requests are unauthenticated. When one or more schemes are configured, a request may satisfy any configured scheme.

| Config key | Environment Variable | Description |
|--------|----------------------|-------------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Bearer token |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Basic auth username |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Basic auth password |
| `api-key` | `IACCODE_A2A_API_KEY` | API key value |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | API key header name |

Bearer token:

```yaml
token: local-dev-token
```

Basic auth:

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

API key:

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### Persistence and Artifacts

| Config key | Default | Description |
|--------|---------|-------------|
| `persistence-dir` | `~/.iac-code/a2a` | Local JSON metadata for tasks, contexts, routes, and push configs |
| `artifact-dir` | `<persistence-dir>/artifacts` | Local artifact payload store |

Persistence mirrors task and context snapshots for restoration metadata. It does not restart an in-flight asyncio task after a process crash.

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Agent Card Signing

| Config key | Description |
|--------|-------------|
| `signing-secret` | HMAC secret used to sign the public Agent Card |

The server emits A2A SDK `AgentCardSignature` JWS fields. The symmetric mode uses `HS256`.

```yaml
signing-secret: local-card-signing-secret
```

### Push Notification Delivery

| Config key | Default | Description |
|--------|---------|-------------|
| `push-notifications` | `false` | Enable A2A task push notification config methods and terminal-state delivery |
| `push-queue` | `local-file` | Push queue backend: `local-file` or `redis-streams` |
| `push-redis-url` | empty | Redis URL for the Redis-backed push queue |
| `push-stream` | `iac-code:a2a:push` | Redis stream for push jobs |
| `push-retry-key` | `iac-code:a2a:push:retry` | Redis sorted set for delayed retries |
| `push-dead-stream` | `iac-code:a2a:push:dead` | Redis stream for dead-letter jobs |
| `push-consumer-group` | `iac-code-push` | Redis consumer group for push workers |
| `push-consumer-name` | empty | Redis consumer name for this worker |
| `push-lease-timeout-ms` | `300000` | Redis pending lease timeout |

Local file queue:

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

Redis Streams queue:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
```

Redis-backed push delivery requires the `a2a-redis` extra.

### Transport Options

| Transport | Command | Notes |
|-----------|---------|-------|
| HTTP JSON-RPC and REST | `iac-code a2a --transport http` | Default. Advertises `JSONRPC` and `HTTP+JSON` interfaces. |
| stdio | `iac-code a2a --transport stdio` | Experimental custom JSON-RPC frames over standard input/output. |
| Unix socket | `iac-code a2a --config a2a-server.yml --transport unix` | Requires `socket-path` in config. |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | Uses `ws-path` from config, defaulting to `/a2a`. |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | Uses `grpc-host` and `grpc-port` from config. |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | Custom JSON-RPC envelope over gRPC. |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | Requires `redis-url` in config. |

Redis Streams transport options:

| Config key | Default | Description |
|--------|---------|-------------|
| `redis-url` | empty | Redis connection URL; required for `--transport redis-streams` |
| `request-stream` | `iac-code:a2a:requests` | Request stream name |
| `response-stream` | `iac-code:a2a:responses` | Response stream name |
| `consumer-group` | `iac-code` | Request stream consumer group |

### Thinking Exposure

| Config key | Default | Description |
|--------|---------|-------------|
| `thinking-exposure` | `tool-trace` | Non-answer runtime signal types exposed through A2A `metadata.iac_code`. Use a YAML list, a comma-separated string, or repeated `--thinking-exposure` flags. Supported values are `tool-trace` and `raw-thinking`. |

`tool-trace` preserves the existing tool progress, permission, and result metadata. `raw-thinking` emits provider reasoning chunks as `metadata.iac_code.thinking` updates with `type: raw_thinking` and `text`. iac-code does not currently produce separate thought-summary or progress-summary events, so those are not valid exposure types.

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### Permission Behavior

| Config key | Default | Description |
|--------|---------|-------------|
| `auto-approve-permissions` | `false` | Automatically approve tool permission requests raised during A2A turns |

Without `auto-approve-permissions: true`, A2A mode rejects permission prompts and emits permission metadata. Use it only for trusted automation environments.

## `iac-code a2a-client call`

Discover an Agent Card, choose the advertised endpoint, and send a prompt.

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --iac-code-model qwen-plus
```

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | empty | A2A agent base URL or JSON-RPC endpoint URL; may come from config |
| `--route` | repeatable | Route spec used when `--url` is omitted |
| `--route-name` | empty | Named route to select |
| `--prompt`, `-p` | required | Prompt text |
| `--cwd` | `.` | Workspace path sent as `message.metadata.iac_code.cwd` |
| `--context-id` | empty | Existing A2A context ID for a follow-up message |
| `--iac-code-model` | empty | LLM model sent as `message.metadata.iac_code.iac_code_model`; overrides server model config for this message turn only |
| `--verify-card-secret`, `--signing-secret` | empty | HMAC secret for Agent Card verification |
| `--verify-card-jwks-url` | empty | Remote JWKS URL used for Agent Card verification |
| `--require-card-signature`, `--require-signature` | `false` | Reject unsigned or invalid Agent Cards |
| `--timeout` | `30.0` | Call timeout in seconds |
| `--stream` | `false` | Use `SendStreamingMessage` and print stream events |

Follow-up in the same context:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --context-id ctx-123 \
  --prompt "Now add outputs for the VPC and vSwitch IDs." \
  --cwd "$PWD"
```

Streaming:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this Terraform module." \
  --cwd "$PWD" \
  --stream
```

Require a signed Agent Card:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

Verify using a remote JWKS URL:

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

Fetch and print a remote Agent Card.

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| Option | Description |
|--------|-------------|
| `--url` | A2A agent base URL; may come from config |
| `--verify-card-secret`, `--signing-secret` | HMAC secret for verification |
| `--verify-card-jwks-url` | Remote JWKS URL for verification |
| `--require-card-signature`, `--require-signature` | Require a valid signature |

Authenticated discovery:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## Task Commands

Task commands call JSON-RPC task methods directly. They are useful for operational tools, dashboards, and debugging.

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| Option | Description |
|--------|-------------|
| `--url` | A2A JSON-RPC endpoint URL; may come from config |
| `--task-id` | Task ID; may come from config |
| `--history-length` | Maximum task history entries to return |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | empty | A2A JSON-RPC endpoint URL; may come from config |
| `--context-id` | empty | Filter by context ID |
| `--status` | empty | Filter by task state |
| `--page-size` | empty | Maximum tasks to return |
| `--page-token` | empty | Pagination token |
| `--include-artifacts` | `false` | Include task artifacts in the response |
| `--output` | `table` | `table` or `json` |

JSON output:

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --include-artifacts \
  --output json
```

### `iac-code a2a-client task-cancel`

```bash
iac-code a2a-client --config a2a-client.yml task-cancel \
  --task-id task-123
```

Cancellation is cooperative. A completed, failed, canceled, or input-required task returns the standard A2A task-not-cancelable error.

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

The command streams events for active tasks. For a new turn, prefer `a2a-client call --stream`; it starts the task and streams updates in one command.

## Push Notification Config Commands

These commands require a server started with `push-notifications: true`. They manage standard A2A task push notification configs.

### `iac-code a2a-client push-config-create`

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

| Option | Description |
|--------|-------------|
| `--url` | A2A JSON-RPC endpoint URL; may come from config |
| `--task-id` | Task ID; may come from config |
| `--config-id` | Push config ID; may come from config |
| `--callback-url` | HTTP(S) callback URL; may come from config |
| `--notification-token` | Token sent as `X-A2A-Notification-Token` |
| `--auth-scheme` | Callback auth scheme, such as `bearer` or `basic` |
| `--auth-credentials` | Callback auth credentials |

Callback URLs are validated before storage and dispatch. The default validator rejects non-HTTP(S) URLs, localhost names, and literal private/local IP addresses.

### `iac-code a2a-client push-config-get`

```bash
iac-code a2a-client --config a2a-client.yml push-config-get \
  --task-id task-123 \
  --config-id webhook-1
```

### `iac-code a2a-client push-config-list`

```bash
iac-code a2a-client --config a2a-client.yml push-config-list \
  --task-id task-123 \
  --page-size 10
```

### `iac-code a2a-client push-config-delete`

```bash
iac-code a2a-client --config a2a-client.yml push-config-delete \
  --task-id task-123 \
  --config-id webhook-1
```

## `iac-code a2a-client extended-card`

Fetch the authenticated extended Agent Card.

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

The public Agent Card advertises `capabilities.extendedAgentCard=true`. The extended card adds authenticated runtime details, including task management and push configuration capability metadata.

## `iac-code a2a-client route-preview`

Preview how `a2a-client call` resolves configured routes when `--url` is omitted.

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| Option | Description |
|--------|-------------|
| `--route` | Repeatable route spec in `name=url;skills=a,b;tags=x,y` format |
| `--name` | Route name to resolve |
| `--skill` | Skill ID to resolve |
| `--prompt` | Prompt text used for name/tag matching |
| `--route-state-dir`, `--persistence-dir` | Directory used to persist route snapshots |
| `--save-routes` | Save provided routes to the route state directory |

Save route snapshots:

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

Call through routes:

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `IACCODE_A2A_HTTP_TOKEN` | Server/client Bearer token default |
| `IACCODE_A2A_BASIC_USERNAME` | Server/client Basic auth username default |
| `IACCODE_A2A_BASIC_PASSWORD` | Server/client Basic auth password default |
| `IACCODE_A2A_API_KEY` | Server/client API key default |
| `IACCODE_A2A_API_KEY_HEADER` | API key header name default |
| `IACCODE_A2A_ALLOWED_CWDS` | OS-path-separated list of allowed workspace roots for incoming message metadata and file URLs |
| `IACCODE_A2A_TEXT_MIME_TYPES` | Extra comma- or semicolon-separated text-like MIME types |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | Extra comma- or semicolon-separated multimodal MIME types |
| `IAC_CODE_A2A_PUSH_KEYRING` | Environment-managed encrypted push secret keyring |
