---
sidebar_position: 2
title: Getting Started
description: Launch the ACP server and connect your first client.
---

# Getting Started with ACP

## Prerequisites

1. **iac-code installed** — See the [Installation](../getting-started/installation.md) guide.

2. **LLM credentials configured** — See the [Authentication](../configuration/authentication.md) guide to set up your model provider credentials via the `/auth` command.

3. **Python ACP SDK** (optional, for programmatic clients)

   The official Python SDK is published on PyPI as **`agent-client-protocol`** (imported as `acp`). The examples on this page are verified against version `0.9.0`:

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## Starting the ACP Server

### Stdio mode (default)

```bash
iac-code acp
```

The server communicates over stdin/stdout using JSON-RPC. This is the mode used when an IDE spawns iac-code as a subprocess.

### HTTP+SSE mode

```bash
iac-code acp --transport http --port 8765
```

Listens on the specified port. Clients connect via HTTP for requests and receive streaming updates over Server-Sent Events. Suitable for remote or multi-client scenarios.

You can secure the HTTP endpoint by setting the `IACCODE_ACP_HTTP_TOKEN` environment variable — the server will require a matching `Authorization: Bearer <token>` header.

### Verify it works

```bash
# Stdio: the process should start and wait for JSON-RPC input on stdin
iac-code acp

# HTTP: check the health endpoint
curl http://127.0.0.1:8765/health
```

## Minimal Example

A minimal Python example using the official `agent-client-protocol` SDK. For a richer walkthrough (tool call rendering, thought chunks, HTTP+SSE transport), see [Examples](./examples.md).

```python
"""Minimal iac-code ACP client using agent-client-protocol==0.9.0."""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        # Stream assistant text to stdout; ignore other update kinds in this minimal demo.
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        # Auto-approve for demonstration — use interactive approval in production.
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main() -> None:
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. Initialize — negotiate capabilities
        init_result = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Protocol version: {init_result.protocol_version}")

        # 2. Create a session tied to your project directory
        session = await conn.new_session(cwd="/path/to/project")
        print(f"Session ID: {session.session_id}")

        # 3. Send a prompt; streaming output is delivered via MyClient.session_update
        result = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Generate a VPC template with 2 VSwitches",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. Clean up
        await conn.close_session(session_id=session.session_id)


asyncio.run(main())
```

Key points:

- `acp.spawn_agent_process` launches `iac-code acp` as a subprocess and manages its stdio lifecycle.
- `new_session(cwd=...)` scopes file operations to the given directory.
- Streaming updates (text chunks, thoughts, tool calls) arrive through the `session_update` callback on your `acp.Client` subclass — `prompt()` itself returns a single `PromptResponse` once the turn ends, with the final `stop_reason`.
- When a permission request arrives, `request_permission` must return either an `AllowedOutcome(outcome="selected", option_id=...)` or a `DeniedOutcome(outcome="cancelled")` — any other value triggers a `pydantic.ValidationError`.

## Client Configuration

iac-code works with any ACP-compatible editor or client. The configuration below applies to **Zed** and **VSCode**:

```json
{
  "agent_servers": {
    "iac-code": {
      "type": "custom",
      "command": "iac-code",
      "args": ["acp"]
    }
  }
}
```

- **Zed** — Add the snippet to your Zed `settings.json`. Zed natively supports ACP agent servers.
- **VSCode** — You need to install an ACP client extension first (any extension that supports the Agent Client Protocol), then apply the same configuration in the extension's settings.

## Next Steps

- [Protocol Reference](./protocol-reference.md) — Full method and event documentation
- [HTTP+SSE Transport](./http-transport.md) — Remote deployment and token auth
