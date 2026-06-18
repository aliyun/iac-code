---
title: Beispiele
description: Praktische Beispiele fuer die Integration mit dem iac-code-A2A-Server.
sidebar_position: 6
---

# Beispiele

Diese Seite bietet sofort verwendbare A2A-Integrationsbeispiele.

## Voraussetzungen

Die Beispiele setzen voraus:

| Abhaengigkeit | Version | Zweck |
|------------|---------|---------|
| Python | `3.12` | Entspricht der Projektlaufzeit |
| `a2a-sdk` | `>=1.0.2,<2` | A2A-Client und Protobuf-Typen |
| `httpx` | `>=0.27.0` | HTTP-Client, der vom SDK und direkten Beispielen verwendet wird |
| `iac-code` | aktuelles Repo | Stellt den Unterbefehl `iac-code a2a` bereit |

Starten Sie den Server:

```bash
uv sync --extra a2a
iac-code a2a --host 127.0.0.1 --port 41242
```

## Python SDK - Streaming-Sitzung

Dieses Beispiel entdeckt die Agent Card, sendet eine Nachricht, gibt Assistant-Text-Chunks aus und meldet Tool-Metadaten.

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

## CLI - End-to-End-Workflow

Starten Sie einen lokalen Server mit Persistenz, Artifacts, Push-Notification-Unterstuetzung und einer signierten Agent Card:

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

Erstellen Sie eine Client-Konfiguration fuer den stabilen Endpunkt und die Kartenverifikationseinstellungen:

```yaml
url: http://127.0.0.1:41242/
verify-card-secret: local-card-signing-secret
require-card-signature: true
```

Entdecken und verifizieren Sie die Agent Card:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

Senden Sie eine Streaming-Anfrage:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --stream
```

Listen Sie Tasks auf und rufen Sie einen Task als JSON ab:

```bash
iac-code a2a-client --config a2a-client.yml task-list --output table

iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

Registrieren Sie einen Push-Callback fuer einen Task:

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

Zeigen Sie vor dem Aufrufen eines gerouteten Agent die Routenauswahl in der Vorschau an:

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

## Python SDK - Follow-up-Nachricht

Follow-up-Nachrichten verwenden dieselbe `context_id` und normalerweise dieselbe Task-ID wieder. Dadurch bleiben die interne iac-code-Laufzeit und der Unterhaltungsverlauf erhalten.

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

Der Server lehnt ein wiederverwendetes `contextId` ab, wenn die neue Nachricht auf einen anderen Workspace zeigt.

## Python SDK - Task abbrechen

```python
from a2a.types import CancelTaskRequest


async def cancel_running_task(client, task_id: str) -> None:
    task = await client.cancel_task(CancelTaskRequest(id=task_id))
    print(f"{task.id}: {task.status.state}")
```

## Direktes HTTP - Minimaler JSON-RPC-Client

Verwenden Sie dies, wenn der Aufrufer die SDK-Abhaengigkeit nicht haben soll.

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

## Direktes HTTP - Streaming-SSE

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

## Direktes HTTP - Push-Notification-Config

Die Push-Config-Methoden sind verfuegbar, wenn der Server mit `push-notifications: true` laeuft.

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

## iac-code-Metadaten behandeln

Tool- und Nutzungs-Events treffen in `TaskStatusUpdateEvent.metadata.iac_code` ein.

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

## Haeufige Stolperfallen

| Symptom | Behebung |
|---------|-----|
| HTTP `401` | Fuegen Sie ein konfiguriertes Auth-Schema wie `Authorization: Bearer <token>`, Basic Auth oder `X-API-Key: <key>` sowohl bei Agent-Card- als auch JSON-RPC-Anfragen hinzu |
| `Invalid A2A workspace metadata.` | Verwenden Sie einen vorhandenen absoluten Pfad in `metadata.iac_code.cwd` |
| `A2A server currently accepts text input only.` | Senden Sie mindestens einen nicht leeren Textteil |
| `Task is already working.` | Warten Sie, bis der aktuelle Turn abgeschlossen ist, bevor Sie eine weitere Nachricht im selben Kontext senden |
| Follow-up als anderer Workspace abgelehnt | Lassen Sie `metadata.iac_code.cwd` fuer ein wiederverwendetes `contextId` unveraendert |
| Lokale File-URL abgelehnt | Halten Sie `file://`-Teile innerhalb von `metadata.iac_code.cwd` und innerhalb von `IACCODE_A2A_ALLOWED_CWDS` |
| Push-Callback abgelehnt | Verwenden Sie eine HTTP(S)-Callback-URL, die nicht localhost und keine literale private/lokale IP-Adresse ist |
| Redis-Push-Queue startet nicht | Installieren Sie das Extra `a2a-redis` und geben Sie `push-redis-url` in der A2A-Konfiguration an |
