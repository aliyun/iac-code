---
sidebar_position: 2
title: Erste Schritte
description: Starten Sie den A2A-Server und senden Sie Ihre erste Nachricht.
---

# Erste Schritte mit A2A

## Voraussetzungen

1. **iac-code installiert** - Siehe die Anleitung [Installation](/docs/getting-started/installation).

2. **LLM-Zugangsdaten konfiguriert** - Siehe die Anleitung [Authentication](/docs/configuration/authentication), um die Zugangsdaten Ihres Modellproviders zu konfigurieren.

3. **A2A-Serverabhaengigkeiten** - Installieren Sie iac-code mit dem Extra `a2a`:

```bash
uv sync --extra a2a
```

## A2A-Server starten

Starten Sie den Server auf der lokalen Standardschnittstelle:

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

Verwenden Sie eine YAML-Konfigurationsdatei, wenn Sie lokalen Zustand, Artifact-Speicherung, Push-Notification-Zustellung oder signierte Agent Cards benoetigen:

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
```

Fuehren Sie ihn aus mit:

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` aktiviert A2A-Task-Push-Notification-Config-Methoden und Zustellung fuer Terminalzustaende. Verwenden Sie `push-queue: redis-streams` mit `push-redis-url`, wenn mehrere Worker die Push-Zustellung koordinieren muessen.

Der Server stellt bereit:

| Route | Zweck |
|-------|---------|
| `GET /health` | Health Check |
| `GET /.well-known/agent-card.json` | Agent-Card-Erkennung |
| `POST /` | A2A JSON-RPC-Endpunkt |

Der HTTP-Server registriert ausserdem die A2A-SDK-REST-Routen und bewirbt sowohl `JSONRPC`- als auch `HTTP+JSON`-Schnittstellen in der Agent Card.

## Discovery pruefen

Rufen Sie die Agent Card ab:

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Sie sollten `name: "iac-code"`, `JSONRPC`- und `HTTP+JSON`-Schnittstellen, Cache-Header wie `ETag`, die optionale Extension `urn:iac-code:a2a:artifact-metadata:v1`, unterstuetzte Eingabemodi und Skills wie `iac_generation`, `iac_review`, `aliyun_ros_operations` und `terraform_ros_conversion` sehen.

Pruefen Sie den Health-Endpunkt:

```bash
curl http://127.0.0.1:41242/health
```

Erwartete Antwort:

```json
{"status":"healthy"}
```

## Authentifizierung verlangen

Authentifizierung ist optional. Wenn keine A2A-Authentifizierungsoptionen oder Umgebungsvariablen gesetzt sind, benoetigen Anfragen keine Authentifizierung. Sobald ein Auth-Schema konfiguriert ist, muss jede Anfrage, einschliesslich Agent-Card-Discovery, ein konfiguriertes Schema erfuellen.

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

Der entsprechende YAML-Konfigurationsschluessel ist `token`.

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Benutzername und Passwort muessen beide vorhanden sein. Die entsprechenden YAML-Konfigurationsschluessel sind `basic-username` und `basic-password`.

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

Der Standard-API-Key-Header ist:

```text
X-API-Key: <api-key>
```

Ueberschreiben Sie ihn mit dem YAML-Konfigurationsschluessel `api-key-header` oder `IACCODE_A2A_API_KEY_HEADER`:

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## Entfernten A2A-Agent aufrufen

Legen Sie stabile Client-Verbindungs- und Auth-Einstellungen in einer YAML-Datei ab:

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
```

Verwenden Sie `a2a-client call` fuer einen direkten Phase-1-Clientaufruf:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

Verwenden Sie `--stream`, wenn Sie inkrementelle Events statt einer finalen Antwort moechten:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

Befehlszeilenoptionen ueberschreiben Konfigurationswerte, wenn Sie ein einmaliges Ziel oder Token benoetigen:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

Zeigen Sie bei Multi-Agent-Routing die Routenauswahl vor dem Aufruf in der Vorschau an:

```bash
iac-code a2a-route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

Siehe [Befehlsreferenz](./command-reference.md) fuer jeden A2A-Befehl, einschliesslich Task-Verwaltung, Push-Config-CRUD, erweiterter Agent Cards und Transportoptionen.

## Erste Nachricht mit curl senden

Uebergeben Sie das Workspace-Verzeichnis ueber `message.metadata.iac_code.cwd`; der Pfad muss absolut sein, bereits existieren und innerhalb eines erlaubten Workspace-Roots liegen. Standardmaessig sind die erlaubten Roots das Server-Prozessverzeichnis und das System-Temp-Verzeichnis. Ueberschreiben Sie sie mit `IACCODE_A2A_ALLOWED_CWDS`.

Der Server akzeptiert textartige Teile, JSON-Datenteile, rohen UTF-8-Text, lokale Workspace-Textdateien mit `file://` und begrenzte multimodale Anhaenge. Die Aufnahme entfernter URLs wird nicht unterstuetzt; `url`-Teile muessen lokale `file://`-URLs innerhalb des erlaubten Workspace sein.

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

Verwenden Sie fuer Streaming-Ausgabe `SendStreamingMessage`:

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

## Minimales Python-SDK-Beispiel

Das folgende Beispiel verwendet `a2a-sdk>=1.0.2,<2`, den Versionsbereich, der vom Extra `a2a` verwendet wird.

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
Erstellen Sie fuer authentifizierte Server den `httpx.AsyncClient` mit `headers={"Authorization": "Bearer <token>"}`, damit sowohl Agent-Card-Discovery als auch JSON-RPC-Aufrufe das Token einschliessen.
:::

## Naechste Schritte

- [Befehlsreferenz](./command-reference.md) - Vollstaendige CLI-Befehls- und Optionsreferenz.
- [Protokollreferenz](./protocol-reference.md) - Details zu Methoden, Routen, Zustaenden und Metadaten.
- [HTTP-Transport](./http-transport.md) - JSON-RPC-HTTP-Verhalten, Bearer Auth und curl-Workflows.
- [Beispiele](./examples.md) - Beispiele fuer SDK, direktes HTTP, Follow-up, Abbruch und Metadatenbehandlung.
