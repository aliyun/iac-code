---
title: HTTP-Transport
description: Fuehren Sie den iac-code-A2A-Server ueber JSON-RPC HTTP aus und rufen Sie ihn auf.
sidebar_position: 5
---

# HTTP-Transport

Der standardmaessige A2A-Server von iac-code stellt JSON-RPC ueber HTTP sowie die A2A-SDK-REST-Routen bereit. Der Server ist mit Starlette gebaut und laeuft auf Uvicorn.

## Server starten

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

Installieren Sie zuerst die optionalen Serverabhaengigkeiten:

```bash
uv sync --extra a2a
```

## Endpunktuebersicht

| Route | Methode | Antwort |
|-------|--------|----------|
| `/health` | `GET` | Einfache JSON-Health-Antwort |
| `/.well-known/agent-card.json` | `GET` | Agent-Card-JSON |
| `/` | `POST` | JSON-RPC-Antwort oder SSE-Stream |
| SDK-REST-Routen | gemischt | Vom SDK registrierte A2A-REST-Endpunkte |

## Header

Empfohlene Header:

```text
Content-Type: application/json
A2A-Version: 1.0
```

Wenn Bearer Auth aktiviert ist:

```text
Authorization: Bearer <token>
```

## Authentifizierung

Der Server unterstuetzt optionale Bearer-Token-, Basic-Auth- und API-Key-Authentifizierung. Wenn keine Authentifizierungsoptionen oder Umgebungsvariablen gesetzt sind, benoetigen Anfragen keine Authentifizierung. Wenn ein oder mehrere Schemas konfiguriert sind, kann sich eine Anfrage mit jedem konfigurierten Schema authentifizieren.

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

Sie koennen `token` auch in der A2A-YAML-Konfigurationsdatei setzen.

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Sowohl Benutzername als auch Passwort muessen gesetzt sein, damit Basic Auth aktiviert wird.

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

Der Standard-API-Key-Header ist `X-API-Key`. Sie koennen ihn in YAML aendern:

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

oder mit `IACCODE_A2A_API_KEY_HEADER`.

| Szenario | Verhalten |
|----------|----------|
| Kein Auth-Schema konfiguriert | Keine Authentifizierung erforderlich |
| Ein oder mehrere Schemas konfiguriert, eines passt | Anfrage wird fortgesetzt |
| Ein oder mehrere Schemas konfiguriert, kein Schema passt | HTTP `401` mit `{"error":"Unauthorized"}` |

## Agent-Card-Discovery

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Authentifiziert:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

Mit API-Key-Authentifizierung:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

Die JSON-RPC-Endpunkt-URL wird in `supportedInterfaces[0].url` beworben. Der HTTP-Modus bewirbt ausserdem eine `HTTP+JSON`-Schnittstelle fuer REST-faehige Clients.

## Nicht streamende Nachricht

`SendMessage` gibt eine einzelne JSON-RPC-Antwort zurueck, nachdem der Agent-Turn abgeschlossen ist.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Create a Terraform VPC module for Alibaba Cloud."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Streaming-Nachricht

`SendStreamingMessage` gibt Server-Sent Events zurueck. Verwenden Sie `curl -N`, um Events auszugeben, sobald sie eintreffen.

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "stream-1",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Generate a ROS template for one VPC and two vSwitches."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Jede SSE-`data:`-Zeile enthaelt eine JSON-RPC-Antwort, deren `result` eine A2A-`StreamResponse` ist.

## Follow-up-Nachricht

Verwenden Sie das von der ersten Antwort zurueckgegebene `taskId` und `contextId`, um dieselbe Unterhaltung fortzusetzen.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-3",
        "taskId": "task-id-from-first-response",
        "contextId": "context-id-from-first-response",
        "role": "ROLE_USER",
        "parts": [{"text": "Now add tags for environment and owner."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Der Workspace muss fuer das wiederverwendete `contextId` gleich bleiben.

## Laufenden Task abbrechen

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "CancelTask",
    "params": {
      "id": "task-id"
    }
  }'
```

Der Abbruch ist kooperativ: iac-code bricht den aktiven Agent-Turn ab, gibt einen abgebrochenen Zustand aus und gibt die Kontext-Sperre frei. Das Abbrechen eines vorhandenen Tasks, der nicht mehr laeuft, gibt den standardmaessigen A2A-`TaskNotCancelableError` zurueck.

## CLI-Aequivalente

Die meisten HTTP-Workflows haben einen passenden CLI-Befehl:

```yaml
url: http://127.0.0.1:41242/
```

```bash
# Discover the Agent Card
iac-code a2a-client --config a2a-client.yml discover

# Send a non-streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a Terraform VPC module for Alibaba Cloud." \
  --cwd "$PWD"

# Send a streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a ROS template for one VPC and two vSwitches." \
  --cwd "$PWD" \
  --stream

# Inspect task state
iac-code a2a-client --config a2a-client.yml task-get --task-id task-id
iac-code a2a-client --config a2a-client.yml task-list --output table

# Cancel an active task
iac-code a2a-client --config a2a-client.yml task-cancel --task-id task-id
```

Die vollstaendige Optionsliste finden Sie in der [Befehlsreferenz](./command-reference.md).

## Betriebshinweise

- Binden Sie fuer rein lokale Nutzung an `127.0.0.1`.
- Verwenden Sie `token` in der A2A-Konfiguration oder `IACCODE_A2A_HTTP_TOKEN`, bevor Sie an eine gemeinsam genutzte Netzwerkschnittstelle binden.
- Der A2A-Modus lehnt Tool-Berechtigungsanfragen automatisch ab; schuetzen Sie unauthentifizierte Endpunkte wie lokale Automatisierungsservices.
- Aktiver Laufzeitzustand liegt im Speicher. Persistenz spiegelt Task- und Kontextmetadaten, aber ein Prozessneustart setzt laufende asyncio-Arbeit nicht fort.
- Ein Kontext kann jeweils nur einen Task ausfuehren; getrennte Kontexte koennen parallel laufen.
