---
sidebar_position: 2
title: Getting Started
description: Start the A2A server and send your first message.
---

# Getting Started with A2A

## Prerequisites

1. **iac-code installed** — See the [Installation](/docs/getting-started/installation) guide.

2. **LLM credentials configured** — See the [Authentication](/docs/configuration/authentication) guide to configure your model provider credentials.

3. **A2A server dependencies** — Install iac-code with the `a2a` extra:

```bash
uv sync --extra a2a
```

## Starting the A2A Server

Start the server on the default local interface:

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

Use a YAML config file when you need local state, artifact storage, push notification delivery, or signed Agent Cards:

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
thinking-exposure:
  - tool-trace
  - raw-thinking
```

Run it with:

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` enables A2A task push notification config methods and terminal-state delivery. Use `push-queue: redis-streams` with `push-redis-url` when multiple workers need to coordinate push delivery.

`thinking-exposure` controls which non-answer runtime signals are exposed through `metadata.iac_code` during streaming. The default is `tool-trace`; add `raw-thinking` only for trusted clients that are allowed to see provider reasoning content.

The server exposes:

| Route | Purpose |
|-------|---------|
| `GET /health` | Health check |
| `GET /.well-known/agent-card.json` | Agent Card discovery |
| `POST /` | A2A JSON-RPC endpoint |

The HTTP server also registers the A2A SDK REST routes and advertises both `JSONRPC` and `HTTP+JSON` interfaces in the Agent Card.

## Verify Discovery

Fetch the Agent Card:

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

You should see `name: "iac-code"`, `JSONRPC` and `HTTP+JSON` interfaces, cache headers such as `ETag`, the optional `urn:iac-code:a2a:artifact-metadata:v1` extension, supported input modes, and skills such as `iac_generation`, `iac_review`, `aliyun_ros_operations`, and `terraform_ros_conversion`.

Check the health endpoint:

```bash
curl http://127.0.0.1:41242/health
```

Expected response:

```json
{"status":"healthy"}
```

## Require Authentication

Authentication is optional. If no A2A authentication options or environment variables are set, requests do not need auth. When any auth scheme is configured, every request, including Agent Card discovery, must satisfy one configured scheme.

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

The equivalent YAML config key is `token`.

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

The username and password must both be present. The equivalent YAML config keys are `basic-username` and `basic-password`.

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

The default API key header is:

```text
X-API-Key: <api-key>
```

Override it with the `api-key-header` YAML config key or `IACCODE_A2A_API_KEY_HEADER`:

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## Call a Remote A2A Agent

Put stable client connection and auth settings in a YAML file:

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
iac-code-model: qwen-plus
iac-code-api-key: provider-api-key
# Optional per-message thinking policy metadata:
# thinking-enabled: false
# thinking-effort: low
# thinking-budget: 2048
```

Use `a2a-client call` for a direct Phase 1 client call:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

Use `--stream` when you want incremental events instead of one final response:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

Command-line options override config values when you need a one-off target or token:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

For multi-agent routing, preview route selection before calling:

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

See [Command Reference](./command-reference.md) for every A2A command, including task management, push config CRUD, extended Agent Cards, and transport options.

## Send a First Message with curl

Pass the workspace directory through `message.metadata.iac_code.cwd`; the path must be absolute and must resolve inside an allowed workspace root. Existing paths must be directories; missing paths are created under the allowed root. By default, allowed roots are the server process directory and the system temp directory. Override them with `IACCODE_A2A_ALLOWED_CWDS`.

For per-task telemetry attribution, include a non-empty string `user_id` under `message.metadata.iac_code`. This overrides the telemetry user ID for the current task only; it does not change A2A task or context identifiers.

For a per-call LLM override, include a non-empty string `iac_code_model` under `message.metadata.iac_code`. This field is the lowercase form of `IAC_CODE_MODEL`; it overrides `IAC_CODE_MODEL`, `settings.yml`, and the server startup default model only for the current A2A message turn.

For a per-call LLM provider key override, include a non-empty string `iac_code_api_key` under `message.metadata.iac_code`. This field is the lowercase form of `IAC_CODE_API_KEY`; it overrides `IAC_CODE_API_KEY` and `.credentials.yml` only for the current A2A message turn. It is separate from A2A HTTP authentication (`api-key` / `IACCODE_A2A_API_KEY`), which protects access to the A2A endpoint.

For per-request Alibaba Cloud credentials, include `alibaba_cloud_access_key_id`, `alibaba_cloud_access_key_secret`, `alibaba_cloud_region_id`, and optional `alibaba_cloud_security_token` under `message.metadata.iac_code`. These task credentials override the server environment and `.cloud-credentials.yml` for that A2A execution only.

The server accepts text-like parts, JSON data parts, raw UTF-8 text, local workspace `file://` text files, and bounded multimodal attachments. Remote URL ingestion is not supported; `url` parts must be local `file://` URLs inside the allowed workspace.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Generate a ROS VPC template with two vSwitches."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project",
            "iac_code_model": "qwen-plus",
            "iac_code_api_key": "provider-api-key"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

For streaming output, use `SendStreamingMessage`:

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Review my Terraform files and suggest ROS equivalents."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Minimal Python SDK Example

The example below uses `a2a-sdk>=1.0.2,<2`, which is the version range used by the `a2a` extra.

```python
"""Minimal iac-code A2A client using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Generate a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("status_update"):
                status = event.status_update.status
                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

:::tip
For authenticated servers, construct the `httpx.AsyncClient` with `headers={"Authorization": "Bearer <token>"}` so both Agent Card discovery and JSON-RPC calls include the token.
:::

## Next Steps

- [Command Reference](./command-reference.md) — Complete CLI command and option reference.
- [Protocol Reference](./protocol-reference.md) — Method, route, state, and metadata details.
- [HTTP Transport](./http-transport.md) — JSON-RPC HTTP behavior, bearer auth, and curl workflows.
- [Examples](./examples.md) — SDK, direct HTTP, follow-up, cancellation, and metadata handling examples.
