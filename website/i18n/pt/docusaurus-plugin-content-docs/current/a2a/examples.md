---
title: Exemplos
description: Exemplos práticos para integrar com o servidor A2A do iac-code.
sidebar_position: 6
---

# Exemplos

Esta página fornece exemplos prontos para uso de integração A2A.

## Pré-requisitos

Os exemplos assumem:

| Dependência | Versão | Finalidade |
|-------------|--------|------------|
| Python | `3.12` | Corresponde ao runtime do projeto |
| `a2a-sdk` | `>=1.0.2,<2` | Cliente A2A e tipos protobuf |
| `httpx` | `>=0.27.0` | Cliente HTTP usado pelo SDK e pelos exemplos diretos |
| `iac-code` | repositório atual | Fornece o subcomando `iac-code a2a` |

Inicie o servidor:

```bash
uv sync --extra a2a
iac-code a2a --host 127.0.0.1 --port 41242
```

## SDK Python — Sessão em streaming

Este exemplo descobre o Agent Card, envia uma mensagem, imprime chunks de texto do assistente e relata metadados de ferramentas.

```python
"""Streaming iac-code A2A session using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict


def metadata_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return MessageToDict(value, preserving_proto_field_name=False)


async def main() -> None:
    headers = {}
    # For authenticated servers:
    # headers["Authorization"] = "Bearer YOUR_TOKEN"

    async with httpx.AsyncClient(headers=headers, timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Create a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("task"):
                print(f"[task] {event.task.id} context={event.task.context_id}")

            elif event.HasField("status_update"):
                update = event.status_update
                status = update.status

                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

                metadata = metadata_to_dict(update.metadata)
                tool = metadata.get("iac_code", {}).get("tool")
                if tool:
                    print(f"\n[tool] {tool.get('status')} {tool.get('name', '')}".rstrip())

                usage = metadata.get("iac_code", {}).get("usage")
                if usage:
                    print(f"\n[usage] {usage['totalTokens']} total tokens")

                if status.state == "TASK_STATE_INPUT_REQUIRED":
                    print("\n[done]")

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

## CLI — Workflow de ponta a ponta

Inicie um servidor local com persistência, artefatos, suporte a notificações push e um Agent Card assinado:

```yaml
host: 127.0.0.1
port: 41242
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
```

```bash
iac-code a2a --config a2a-server.yml
```

Crie uma configuração de cliente para o endpoint estável e as configurações de verificação do card:

```yaml
url: http://127.0.0.1:41242/
verify-card-secret: local-card-signing-secret
require-card-signature: true
```

Descubra e verifique o Agent Card:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

Envie uma requisição em streaming:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --stream
```

Liste tarefas e busque uma tarefa como JSON:

```bash
iac-code a2a-client --config a2a-client.yml task-list --output table

iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

Registre um callback push para uma tarefa:

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

Pré-visualize a seleção de rota antes de chamar um agente roteado:

```bash
iac-code a2a-route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

## SDK Python — Mensagem de acompanhamento

Mensagens de acompanhamento reutilizam o mesmo `context_id` e geralmente o mesmo ID de tarefa. Isso mantém vivo o runtime interno do iac-code e o histórico da conversa.

```python
import uuid
from pathlib import Path

from a2a.types import Message, Part, Role, SendMessageRequest


def build_follow_up(task_id: str, context_id: str, text: str) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            message_id=f"msg-{uuid.uuid4().hex}",
            task_id=task_id,
            context_id=context_id,
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            metadata={"iac_code": {"cwd": str(Path.cwd())}},
        )
    )


# Usage inside an async function:
# async for event in client.send_message(
#     build_follow_up(task_id, context_id, "Add outputs for VPC and VSwitch IDs.")
# ):
#     ...
```

O servidor rejeita um `contextId` reutilizado se a nova mensagem apontar para um workspace diferente.

## SDK Python — Cancelar uma tarefa

```python
from a2a.types import CancelTaskRequest


async def cancel_running_task(client, task_id: str) -> None:
    task = await client.cancel_task(CancelTaskRequest(id=task_id))
    print(f"{task.id}: {task.status.state}")
```

## HTTP direto — Cliente JSON-RPC mínimo

Use isto quando você não quiser a dependência do SDK no chamador.

```python
"""Direct A2A JSON-RPC client using httpx."""

import asyncio
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:41242"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120.0) as client:
        response = await client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "send-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Review this ROS template for missing parameters."}],
                        "metadata": {"iac_code": {"cwd": str(Path.cwd())}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        )
        response.raise_for_status()
        print(response.json())


if __name__ == "__main__":
    asyncio.run(main())
```

## HTTP direto — Streaming SSE

```python
"""Read SendStreamingMessage events directly with httpx."""

import asyncio
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:41242"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=None) as client:
        async with client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "stream-1",
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "messageId": "msg-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Generate a Terraform VPC example."}],
                        "metadata": {"iac_code": {"cwd": str(Path.cwd())}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    print(line.removeprefix("data:").strip())


if __name__ == "__main__":
    asyncio.run(main())
```

## HTTP direto — Configuração de notificação push

Os métodos de configuração push estão disponíveis quando o servidor executa com `push-notifications: true`.

```python
"""Create an A2A task push notification config."""

import asyncio

import httpx

BASE_URL = "http://127.0.0.1:41242"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        response = await client.post(
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "push-1",
                "method": "CreateTaskPushNotificationConfig",
                "params": {
                    "taskId": "task-123",
                    "id": "webhook-1",
                    "url": "https://hooks.example.com/a2a",
                    "token": "notification-token",
                    "authentication": {
                        "scheme": "bearer",
                        "credentials": "callback-token",
                    },
                },
            },
        )
        response.raise_for_status()
        print(response.json())


if __name__ == "__main__":
    asyncio.run(main())
```

## Tratando metadados do iac-code

Eventos de ferramentas e uso chegam em `TaskStatusUpdateEvent.metadata.iac_code`.

```python
from typing import Any


def handle_iac_metadata(metadata: dict[str, Any]) -> None:
    iac = metadata.get("iac_code", {})

    if tool := iac.get("tool"):
        status = tool.get("status")
        tool_id = tool.get("toolUseId")
        name = tool.get("name", "<unknown>")
        print(f"tool={name} id={tool_id} status={status}")

    if permission := iac.get("permission"):
        if permission.get("autoApproved") is False:
            print(f"permission rejected for {permission.get('toolName')}")

    if usage := iac.get("usage"):
        print(
            "tokens="
            f"{usage.get('inputTokens', 0)}+{usage.get('outputTokens', 0)}"
            f"={usage.get('totalTokens', 0)}"
        )
```

## Armadilhas comuns

| Sintoma | Correção |
|---------|----------|
| HTTP `401` | Inclua um esquema de autenticação configurado, como `Authorization: Bearer <token>`, Basic auth ou `X-API-Key: <key>`, tanto nas requisições de Agent Card quanto nas JSON-RPC |
| `Invalid A2A workspace metadata.` | Use um caminho absoluto existente em `metadata.iac_code.cwd` |
| `A2A server currently accepts text input only.` | Envie pelo menos uma parte de texto não vazia |
| `Task is already working.` | Aguarde o turno atual terminar antes de enviar outra mensagem no mesmo contexto |
| Acompanhamento rejeitado como workspace diferente | Mantenha `metadata.iac_code.cwd` inalterado para um `contextId` reutilizado |
| URL de arquivo local rejeitada | Mantenha partes `file://` dentro de `metadata.iac_code.cwd` e dentro de `IACCODE_A2A_ALLOWED_CWDS` |
| Callback push rejeitado | Use uma URL de callback HTTP(S) que não seja localhost nem um endereço IP literal privado/local |
| Fila push Redis falha ao iniciar | Instale o extra `a2a-redis` e forneça `push-redis-url` na configuração A2A |
