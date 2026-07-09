---
title: HTTP+SSE-Transport
description: Betreiben Sie den ACP-Server ueber HTTP mit Server-Sent Events fuer entfernte und Multi-Client-Szenarien.
sidebar_position: 5
---

# HTTP+SSE-Transport

Der ACP-Server von iac-code unterstuetzt zwei Transportmodi. Der standardmaessige **Stdio**-Transport kommuniziert ueber Standard-Ein-/Ausgabe und ist ideal fuer lokale IDE-Integrationen. Der **HTTP+SSE**-Transport stellt einen Netzwerk-Endpunkt bereit und streamt Antworten ueber Server-Sent Events, was ihn fuer entfernte Bereitstellungen, lastverteilte Umgebungen und Multi-Client-Zugriff geeignet macht.

## Warum HTTP+SSE

Stdio hat inhärente Einschraenkungen:

- Erfordert, dass der Serverprozess ein direkter Kindprozess des Clients ist -- kein Fernzugriff.
- Blockierendes Prozessmanagement erschwert die gleichzeitige Bedienung mehrerer Clients.
- Inkompatibel mit Netzwerk-Proxys, Load-Balancern oder containerisierten Bereitstellungen.

HTTP+SSE adressiert diese Einschraenkungen:

- **Netzwerkfreundlich** -- erreichbar von jedem Rechner, der den Endpunkt erreichen kann.
- **Multi-Client** -- jeder Client erhaelt eine isolierte Verbindung mit eigenem Ereignisstrom.
- **Infrastrukturbereit** -- funktioniert hinter Reverse-Proxys, in Containern und mit Standard-HTTP-Monitoring-Tools.
- **Einfache Integration** -- jeder HTTP-Client (curl, fetch, SDK) kann mit dem Server interagieren.

## Starten des HTTP-Servers

```bash
# Default port 8765
iac-code acp --transport http

# Custom port
iac-code acp --transport http --port 9090
```

Der Server verwendet [Starlette](https://www.starlette.io/) als ASGI-Framework und laeuft auf Uvicorn.

## Routen

Alle Routen werden unter dem Pfad `/acp` bereitgestellt. Die HTTP-Methode bestimmt die Operation.

### `POST /acp`

Senden Sie eine JSON-RPC-Anfrage an den Server.

- **`initialize`** -- Erstellt eine neue Verbindung und gibt die vollstaendige JSON-RPC-Antwort direkt zurueck. Die Antwort enthaelt einen `Acp-Connection-Id`-Header.
- **Alle anderen Methoden** -- Erfordert einen gueltigen `Acp-Connection-Id`-Header. Gibt sofort `202 Accepted` zurueck; das tatsaechliche Ergebnis wird asynchron ueber den SSE-Stream geliefert.

### `GET /acp`

Oeffnet einen Server-Sent Events-Stream zum Empfang von Antworten und Benachrichtigungen.

- Erfordert den `Acp-Connection-Id`-Header.
- Ereignisse haben den Typ `message` mit der JSON-RPC-Antwort/-Benachrichtigung als `data`-Feld.
- Der Stream enthaelt `id`- und `retry`-Felder fuer automatische Wiederverbindung.

### `DELETE /acp`

Schliesst die Verbindung und gibt alle zugehoerigen Ressourcen frei.

- Erfordert den `Acp-Connection-Id`-Header.
- Gibt `200 OK` zurueck.

## Verbindungs-ID

Die Verbindungs-ID verbindet die Anfragen eines Clients mit seinem SSE-Ereignisstrom.

1. Der Client sendet ein `POST /acp` mit der `initialize`-Methode.
2. Der Server antwortet mit dem Initialisierungsergebnis und einem `Acp-Connection-Id`-Antwort-Header, der eine UUID enthaelt.
3. Alle nachfolgenden Anfragen (`POST`, `GET`, `DELETE`) muessen den `Acp-Connection-Id`-Anfrage-Header mit diesem Wert enthalten.
4. Jede Verbindungs-ID wird einer unabhaengigen ACP-Agentensitzung mit eigener Ereigniswarteschlange zugeordnet.

Wenn eine Anfrage eine fehlende oder ungueltige Verbindungs-ID referenziert, gibt der Server `400 Bad Request` zurueck.

## Authentifizierung

Der Server unterstuetzt optionale Bearer-Token-Authentifizierung ueber die Umgebungsvariable `IACCODE_ACP_HTTP_TOKEN`.

```bash
# Set the token before starting the server
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

Wenn gesetzt, muss jede Anfrage Folgendes enthalten:

```
Authorization: Bearer your-secret-token
```

| Szenario | Verhalten |
|----------|----------|
| Token nicht gesetzt | Keine Authentifizierung erforderlich (geeignet fuer lokale Entwicklung) |
| Token gesetzt, Header stimmt ueberein | Anfrage wird normal verarbeitet |
| Token gesetzt, Header fehlt/falsch | `401 Unauthorized` wird zurueckgegeben |

## Vollstaendiger Workflow

Nachfolgend eine vollstaendige Interaktion mit `curl`:

```bash
# Step 1: Initialize — creates a connection and returns the Connection ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# Step 2: Open the SSE stream (run in background)
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# Step 3: Create a session
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# Step 4: Send a prompt
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# Step 5: Close the connection
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# Clean up background SSE process
kill $SSE_PID 2>/dev/null
```

:::tip
Die `initialize`-Antwort wird synchron zurueckgegeben (innerhalb eines 30-Sekunden-Timeouts). Alle nachfolgenden Antworten kommen ausschliesslich ueber den in Schritt 2 geoeffneten SSE-Stream.
:::
