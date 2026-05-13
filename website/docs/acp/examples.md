---
title: Examples
description: Practical code examples for integrating with the iac-code ACP server.
sidebar_position: 4
---

# Examples

This page provides ready-to-use code examples for common ACP integration patterns.

## Prerequisites

All examples on this page have been verified against the following environment:

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python | `3.10` | Uses modern typing (`\|` unions, `match/case` statements) |
| `agent-client-protocol` | `0.9.0` | Official ACP Python SDK (imported as `acp`) |
| `httpx` | `0.28.1` | Async HTTP client used by the HTTP+SSE example |
| `iac-code` | current repo | Provides the `iac-code acp` subcommand used by `spawn_agent_process` |

Install the client-side dependencies with [uv](https://docs.astral.sh/uv/):

```bash
# Create a Python 3.10 virtualenv managed by uv
uv venv --python 3.10
source .venv/bin/activate

# Install the pinned client-side dependencies into that venv
uv pip install "agent-client-protocol==0.9.0" "httpx>=0.28.1"
```

:::warning
`AllowedOutcome.outcome` and `DeniedOutcome.outcome` are typed as `Literal['selected']` and `Literal['cancelled']` respectively since SDK 0.9.0. Using any other string will raise a `pydantic.ValidationError` at construction time.
:::

---

## Python SDK — Full Session Lifecycle

A complete example using the `agent-client-protocol` Python SDK:

```python
"""Full iac-code ACP session lifecycle."""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    """ACP client with streaming output."""

    async def session_update(
        self,
        session_id: str,
        update: (
            acp.schema.AgentMessageChunk
            | acp.schema.AgentThoughtChunk
            | acp.schema.ToolCallStart
            | acp.schema.ToolCallProgress
            | Any
        ),
        **kwargs: Any,
    ) -> None:
        match update:
            case acp.schema.AgentThoughtChunk():
                print(f"[thought] {update.content.text}", end="", flush=True)
            case acp.schema.AgentMessageChunk():
                print(f"{update.content.text}", end="", flush=True)
            case acp.schema.ToolCallStart():
                print(f"\n[tool] {update.title} (kind={update.kind})")
            case acp.schema.ToolCallProgress():
                status = update.status
                print(f"[tool] {update.tool_call_id} → {status}")

    async def request_permission(
        self, options, session_id, tool_call, **kwargs
    ) -> acp.RequestPermissionResponse:
        # Auto-approve for demonstration (use interactive approval in production)
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main():
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. Initialize
        resp = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Connected to {resp.agent_info.name} v{resp.agent_info.version}")

        # 2. Create session
        session = await conn.new_session(cwd="/path/to/project")
        sid = session.session_id
        # `models` is typed as Optional in the schema — guard against agents that don't report it.
        current_model = session.models.current_model_id if session.models else "<unknown>"
        print(f"Session: {sid}, model: {current_model}")

        # 3. Send prompt
        result = await conn.prompt(
            session_id=sid,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Create a VPC with two subnets using a ROS template",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. Close session
        await conn.close_session(session_id=sid)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Python SDK — Handling Permissions

Implement interactive permission approval:

```python
import acp
import acp.schema


class InteractiveClient(acp.Client):
    async def session_update(self, session_id, update, **kwargs):
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs
    ) -> acp.RequestPermissionResponse:
        print(f"\n⚠️  Permission request: {tool_call.title}")
        print(f"   Tool kind: {tool_call.kind}")

        # Show available options
        for opt in options:
            print(f"   [{opt.option_id}] {opt.name}")

        choice = input("Choose (allow_once/reject_once): ").strip()

        if choice.startswith("allow"):
            return acp.RequestPermissionResponse(
                outcome=acp.schema.AllowedOutcome(
                    outcome="selected",
                    option_id=choice,
                )
            )
        else:
            return acp.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled")
            )
```

:::tip
Use `InteractiveClient` the same way as `MyClient` above — pass an **instance** to `spawn_agent_process`, not the class:

```python
async with acp.spawn_agent_process(InteractiveClient(), "iac-code", "acp") as (conn, _):
    ...
```

Passing the class directly raises `TypeError: __init__() takes exactly one argument` because `acp.Client` is a `typing.Protocol` whose default `__init__` rejects positional arguments.
:::

**Permission strategies by environment:**

| Environment | Strategy |
|-------------|----------|
| Development | Auto-allow all |
| Production | Interactive approval for write/execute tools |
| CI/CD | Allow read-only, deny write/execute |

---

## Python SDK — Streaming Events

Process different event types with detailed handling:

```python
import acp
import acp.schema


class StreamingClient(acp.Client):
    def __init__(self):
        self.tool_calls: dict[str, str] = {}  # tool_call_id → title

    async def session_update(self, session_id, update, **kwargs):
        match update:
            case acp.schema.AgentThoughtChunk():
                # Model's internal reasoning (dimmed in UI typically)
                print(f"  💭 {update.content.text}", end="", flush=True)

            case acp.schema.AgentMessageChunk():
                # Final response text shown to user
                print(update.content.text, end="", flush=True)

            case acp.schema.ToolCallStart():
                self.tool_calls[update.tool_call_id] = update.title
                print(f"\n  🔧 [{update.kind}] {update.title}")

            case acp.schema.ToolCallProgress():
                title = self.tool_calls.get(update.tool_call_id, "unknown")
                if update.status == "completed":
                    print(f"  ✅ {title} completed")
                elif update.status == "failed":
                    print(f"  ❌ {title} failed")
                    if update.raw_output:
                        print(f"     Error: {str(update.raw_output)[:200]}")
                else:
                    print(f"  ⏳ {title} in progress...")

            case acp.schema.UsageUpdate():
                # UsageUpdate reports context-window usage in tokens.
                # Fields: used (current context tokens), size (total window), cost (optional).
                print(f"\n  📊 Context: {update.used}/{update.size} tokens")

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )
```

:::tip
`StreamingClient` defines `__init__(self)` with no arguments to initialize internal state. When wiring it up, still pass an **instance** — `spawn_agent_process(StreamingClient(), "iac-code", "acp")` — never the class itself.
:::

---

## HTTP+SSE — Minimal Client

For environments where you can't use the Python SDK, connect directly via HTTP+SSE:

```python
"""Minimal HTTP+SSE client using httpx."""

import asyncio
import httpx

BASE_URL = "http://127.0.0.1:8765"
HEADERS = {"Authorization": "Bearer YOUR_TOKEN"}


async def main():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # 1. Initialize — get connection ID
        resp = await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "http-client", "version": "1.0"},
                "capabilities": {}
            }
        }, headers=HEADERS)
        resp.raise_for_status()
        conn_id = resp.headers["Acp-Connection-Id"]
        print(f"Connection ID: {conn_id}")

        session_headers = {**HEADERS, "Acp-Connection-Id": conn_id}

        # 2. Subscribe to SSE stream (background)
        async def listen_sse():
            async with client.stream("GET", "/acp", headers=session_headers) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data:"):
                        print(f"[SSE] {line[5:].strip()}")

        sse_task = asyncio.create_task(listen_sse())

        # 3. Create session (response arrives via SSE)
        resp = await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 2,
            "method": "session/new",
            "params": {"cwd": "/workspace"}
        }, headers=session_headers)
        # Returns 202 Accepted; actual result delivered via SSE

        await asyncio.sleep(2)  # Wait for session creation

        # 4. Send prompt
        await client.post("/acp", json={
            "jsonrpc": "2.0", "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": "<session-id-from-sse>",
                "prompt": [{"type": "text", "text": "List files in current directory"}]
            }
        }, headers=session_headers)

        await asyncio.sleep(10)  # Wait for streaming response
        sse_task.cancel()

        # 5. Close connection
        await client.request("DELETE", "/acp", headers=session_headers)
        print("Connection closed")


if __name__ == "__main__":
    asyncio.run(main())
```

**Key points:**
- `POST /acp` with `method: "initialize"` returns `Acp-Connection-Id` in response headers
- All subsequent requests must include both `Authorization` and `Acp-Connection-Id` headers
- `POST /acp` returns `202 Accepted`; actual responses are delivered via the SSE stream
- `GET /acp` opens the SSE stream for receiving server-pushed events
- `DELETE /acp` closes the connection and releases server resources

---

## Session Management Patterns

### Fork for Experimentation

Create a branch from an existing session to try different approaches without affecting the original:

```python
async def fork_and_experiment(conn, original_session_id: str, cwd: str):
    """Fork a session to experiment without affecting the original."""
    # Fork creates a copy with the same history
    forked = await conn.fork_session(
        session_id=original_session_id,
        cwd=cwd,
    )
    forked_sid = forked.session_id
    print(f"Forked session: {forked_sid}")

    # Experiment on the fork
    result = await conn.prompt(
        session_id=forked_sid,
        prompt=[acp.schema.TextContentBlock(
            type="text",
            text="Try an alternative approach: use Terraform instead of ROS",
        )],
    )

    # Close fork when done (original session is unaffected)
    await conn.close_session(session_id=forked_sid)
    return result
```

### Load and Resume Historical Sessions

Restore a previous session to continue where you left off:

```python
async def resume_previous_session(conn, cwd: str):
    """List sessions and resume the most recent one."""
    # List available sessions
    listing = await conn.list_sessions(cwd=cwd)

    if not listing.sessions:
        print("No previous sessions found")
        return None

    # Resume the first session
    target = listing.sessions[0]
    print(f"Resuming session: {target.session_id}")

    session = await conn.resume_session(
        session_id=target.session_id,
        cwd=cwd,
    )
    return session.session_id
```

### Parallel Multi-Session

Run multiple independent tasks concurrently:

```python
async def parallel_tasks(conn, cwd: str, prompts: list[str]):
    """Run multiple prompts in parallel sessions."""
    sessions = []

    # Create sessions
    for _ in prompts:
        s = await conn.new_session(cwd=cwd)
        sessions.append(s.session_id)

    # Run prompts concurrently
    tasks = [
        conn.prompt(
            session_id=sid,
            prompt=[acp.schema.TextContentBlock(type="text", text=text)],
        )
        for sid, text in zip(sessions, prompts)
    ]
    results = await asyncio.gather(*tasks)

    # Cleanup
    for sid in sessions:
        await conn.close_session(session_id=sid)

    return results


# Usage
# results = await parallel_tasks(conn, "/workspace", [
#     "Create a VPC template",
#     "Create a security group template",
#     "Create an ECS instance template",
# ])
```

:::warning
Each session holds an LLM connection. Running too many parallel sessions may trigger API rate limits.
:::
