---
title: Exemplos
description: Exemplos praticos de codigo para integracao com o servidor ACP do iac-code.
sidebar_position: 4
---

# Exemplos

Esta pagina fornece exemplos de codigo prontos para uso para padroes comuns de integracao ACP.

## Pre-requisitos

Todos os exemplos nesta pagina foram verificados com o seguinte ambiente:

| Dependencia | Versao | Finalidade |
|------------|---------|---------|
| Python | `3.10` | Usa tipagem moderna (unioes `\|`, instrucoes `match/case`) |
| `agent-client-protocol` | `0.9.0` | SDK oficial ACP em Python (importado como `acp`) |
| `httpx` | `0.28.1` | Cliente HTTP assincrono usado pelo exemplo HTTP+SSE |
| `iac-code` | repositorio atual | Fornece o subcomando `iac-code acp` usado por `spawn_agent_process` |

Instale as dependencias do lado do cliente com [uv](https://docs.astral.sh/uv/):

```bash
# Create a Python 3.10 virtualenv managed by uv
uv venv --python 3.10
source .venv/bin/activate

# Install the pinned client-side dependencies into that venv
uv pip install "agent-client-protocol==0.9.0" "httpx>=0.28.1"
```

:::warning
`AllowedOutcome.outcome` e `DeniedOutcome.outcome` sao tipados como `Literal['selected']` e `Literal['cancelled']` respectivamente desde o SDK 0.9.0. Usar qualquer outra string levantara um `pydantic.ValidationError` no momento da construcao.
:::

---

## Python SDK — Ciclo de vida completo da sessao

Um exemplo completo usando o SDK Python `agent-client-protocol`:

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

## Python SDK — Tratamento de permissoes

Implemente aprovacao interativa de permissoes:

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
Use `InteractiveClient` da mesma forma que `MyClient` acima — passe uma **instancia** para `spawn_agent_process`, nao a classe:

```python
async with acp.spawn_agent_process(InteractiveClient(), "iac-code", "acp") as (conn, _):
    ...
```

Passar a classe diretamente levanta `TypeError: __init__() takes exactly one argument` porque `acp.Client` e um `typing.Protocol` cujo `__init__` padrao rejeita argumentos posicionais.
:::

**Estrategias de permissao por ambiente:**

| Ambiente | Estrategia |
|-------------|----------|
| Desenvolvimento | Auto-aprovar tudo |
| Producao | Aprovacao interativa para ferramentas de escrita/execucao |
| CI/CD | Permitir somente leitura, negar escrita/execucao |

---

## Python SDK — Eventos de streaming

Processe diferentes tipos de eventos com tratamento detalhado:

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
`StreamingClient` define `__init__(self)` sem argumentos para inicializar o estado interno. Ao conecta-lo, ainda passe uma **instancia** — `spawn_agent_process(StreamingClient(), "iac-code", "acp")` — nunca a classe em si.
:::

---

## HTTP+SSE — Cliente minimo

Para ambientes onde nao pode usar o SDK Python, conecte-se diretamente via HTTP+SSE:

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
                "clientCapabilities": {}
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

**Pontos-chave:**
- `POST /acp` com `method: "initialize"` retorna `Acp-Connection-Id` nos cabecalhos de resposta
- Todas as requisicoes subsequentes devem incluir os cabecalhos `Authorization` e `Acp-Connection-Id`
- `POST /acp` retorna `202 Accepted`; as respostas reais sao entregues atraves do stream SSE
- `GET /acp` abre o stream SSE para receber eventos enviados pelo servidor
- `DELETE /acp` fecha a conexao e libera os recursos do servidor

---

## Padroes de gerenciamento de sessoes

### Bifurcar para experimentacao

Crie uma ramificacao a partir de uma sessao existente para experimentar abordagens diferentes sem afetar a original:

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

### Carregar e retomar sessoes historicas

Restaure uma sessao anterior para continuar de onde parou:

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

### Multi-sessao paralela

Execute multiplas tarefas independentes simultaneamente:

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
Cada sessao mantem uma conexao LLM. Executar muitas sessoes paralelas pode acionar limites de taxa da API.
:::
