---
title: Ejemplos
description: Ejemplos prácticos para integrarse con el servidor A2A de iac-code.
sidebar_position: 6
---

# Ejemplos

Esta página proporciona ejemplos de integración A2A listos para usar.

## Requisitos previos

Los ejemplos asumen:

| Dependencia | Versión | Propósito |
|-------------|---------|-----------|
| Python | `3.12` | Coincide con el runtime del proyecto |
| `a2a-sdk` | `>=1.0.2,<2` | Cliente A2A y tipos protobuf |
| `httpx` | `>=0.27.0` | Cliente HTTP usado por el SDK y ejemplos directos |
| `iac-code` | current repo | Proporciona el subcomando `iac-code a2a` |

Inicia el servidor:

```bash
uv sync --extra a2a
iac-code a2a --host 127.0.0.1 --port 41242
```

## SDK de Python — Sesión en streaming

Este ejemplo descubre la Agent Card, envía un mensaje, imprime fragmentos de texto del asistente y reporta metadatos de herramientas.

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

## CLI — Flujo de trabajo completo

Inicia un servidor local con persistencia, artefactos, soporte de notificaciones push y una Agent Card firmada:

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

Crea una configuración de cliente para el endpoint estable y los ajustes de verificación de tarjeta:

```yaml
url: http://127.0.0.1:41242/
verify-card-secret: local-card-signing-secret
require-card-signature: true
```

Descubre y verifica la Agent Card:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

Envía una solicitud en streaming:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --stream
```

Lista tareas y obtiene una tarea como JSON:

```bash
iac-code a2a-client --config a2a-client.yml task-list --output table

iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

Registra un callback push para una tarea:

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

Previsualiza la selección de ruta antes de llamar a un agente enrutado:

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

## SDK de Python — Mensaje de seguimiento

Los mensajes de seguimiento reutilizan el mismo `context_id` y normalmente el mismo ID de tarea. Esto mantiene vivos el runtime interno de iac-code y el historial de conversación.

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

El servidor rechaza un `contextId` reutilizado si el nuevo mensaje apunta a un espacio de trabajo diferente.

## SDK de Python — Cancelar una tarea

```python
from a2a.types import CancelTaskRequest


async def cancel_running_task(client, task_id: str) -> None:
    task = await client.cancel_task(CancelTaskRequest(id=task_id))
    print(f"{task.id}: {task.status.state}")
```

## HTTP directo — Cliente JSON-RPC mínimo

Usa esto cuando no quieras la dependencia del SDK en el llamador.

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

## HTTP directo — SSE en streaming

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

## HTTP directo — Configuración de notificaciones push

Los métodos de configuración push están disponibles cuando el servidor se ejecuta con `push-notifications: true`.

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

## Manejo de metadatos de iac-code

Los eventos de herramientas y uso llegan en `TaskStatusUpdateEvent.metadata.iac_code`.

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

## Errores comunes

| Síntoma | Solución |
|---------|----------|
| HTTP `401` | Incluye un esquema de autenticación configurado, como `Authorization: Bearer <token>`, Basic auth o `X-API-Key: <key>`, tanto en las solicitudes de Agent Card como en las JSON-RPC |
| `Invalid A2A workspace metadata.` | Usa una ruta absoluta existente en `metadata.iac_code.cwd` |
| `A2A server currently accepts text input only.` | Envía al menos una parte de texto no vacía |
| `Task is already working.` | Espera a que termine el turno actual antes de enviar otro mensaje en el mismo contexto |
| Seguimiento rechazado como espacio de trabajo diferente | Mantén `metadata.iac_code.cwd` sin cambios para un `contextId` reutilizado |
| URL de archivo local rechazada | Mantén las partes `file://` dentro de `metadata.iac_code.cwd` y dentro de `IACCODE_A2A_ALLOWED_CWDS` |
| Callback push rechazado | Usa una URL de callback HTTP(S) que no sea localhost ni una dirección IP literal privada/local |
| La cola push de Redis no inicia | Instala el extra `a2a-redis` y proporciona `push-redis-url` en la configuración A2A |
