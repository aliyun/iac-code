---
title: Exemples
description: Exemples pratiques pour l'intégration avec le serveur A2A iac-code.
sidebar_position: 6
---

# Exemples

Cette page fournit des exemples d'intégration A2A prêts à l'emploi.

## Prérequis

Les exemples supposent :

| Dépendance | Version | Objectif |
|------------|---------|---------|
| Python | `3.12` | Correspond au runtime du projet |
| `a2a-sdk` | `>=1.0.2,<2` | Client A2A et types protobuf |
| `httpx` | `>=0.27.0` | Client HTTP utilisé par le SDK et les exemples directs |
| `iac-code` | dépôt actuel | Fournit la sous-commande `iac-code a2a` |

Démarrez le serveur :

```bash
uv sync --extra a2a
iac-code a2a --host 127.0.0.1 --port 41242
```

## SDK Python — Session en streaming

Cet exemple découvre l'Agent Card, envoie un message, affiche les fragments de texte de l'assistant et rapporte les métadonnées d'outil.

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

## CLI — Workflow de bout en bout

Démarrez un serveur local avec persistance, artefacts, prise en charge des notifications push et Agent Card signée :

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

Créez une configuration client pour l'endpoint stable et les paramètres de vérification de carte :

```yaml
url: http://127.0.0.1:41242/
verify-card-secret: local-card-signing-secret
require-card-signature: true
```

Découvrez et vérifiez l'Agent Card :

```bash
iac-code a2a-client --config a2a-client.yml discover
```

Envoyez une requête en streaming :

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD" \
  --stream
```

Listez les tâches et récupérez une tâche en JSON :

```bash
iac-code a2a-client --config a2a-client.yml task-list --output table

iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

Enregistrez un callback push pour une tâche :

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

Prévisualisez la sélection de route avant d'appeler un agent routé :

```bash
iac-code a2a-route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

## SDK Python — Message de suivi

Les messages de suivi réutilisent le même `context_id` et généralement le même ID de tâche. Cela garde en vie le runtime iac-code interne et l'historique de conversation.

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

Le serveur rejette un `contextId` réutilisé si le nouveau message pointe vers un espace de travail différent.

## SDK Python — Annuler une tâche

```python
from a2a.types import CancelTaskRequest


async def cancel_running_task(client, task_id: str) -> None:
    task = await client.cancel_task(CancelTaskRequest(id=task_id))
    print(f"{task.id}: {task.status.state}")
```

## HTTP direct — Client JSON-RPC minimal

Utilisez ceci lorsque vous ne voulez pas de dépendance au SDK dans l'appelant.

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

## HTTP direct — Streaming SSE

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

## HTTP direct — Configuration des notifications push

Les méthodes de configuration push sont disponibles lorsque le serveur s'exécute avec `push-notifications: true`.

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

## Gérer les métadonnées iac-code

Les événements d'outils et d'utilisation arrivent dans `TaskStatusUpdateEvent.metadata.iac_code`.

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

## Pièges courants

| Symptôme | Correction |
|---------|-----|
| HTTP `401` | Incluez un schéma d'authentification configuré, comme `Authorization: Bearer <token>`, Basic auth ou `X-API-Key: <key>`, sur les requêtes Agent Card et JSON-RPC |
| `Invalid A2A workspace metadata.` | Utilisez un chemin absolu existant dans `metadata.iac_code.cwd` |
| `A2A server currently accepts text input only.` | Envoyez au moins une partie texte non vide |
| `Task is already working.` | Attendez que le tour actuel se termine avant d'envoyer un autre message dans le même contexte |
| Suivi rejeté comme espace de travail différent | Gardez `metadata.iac_code.cwd` inchangé pour un `contextId` réutilisé |
| URL de fichier local rejetée | Gardez les parties `file://` dans `metadata.iac_code.cwd` et dans `IACCODE_A2A_ALLOWED_CWDS` |
| Callback push rejeté | Utilisez une URL de callback HTTP(S) qui n'est pas localhost ni une adresse IP littérale privée/locale |
| Échec du démarrage de la file push Redis | Installez l'extra `a2a-redis` et fournissez `push-redis-url` dans la configuration A2A |
