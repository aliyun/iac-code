---
title: Protokollreferenz
description: Vollstaendige A2A-Protokollreferenz fuer die iac-code-Integration.
sidebar_position: 4
---

# Protokollreferenz

Dieses Dokument beschreibt die vom iac-code-Server bereitgestellte A2A 1.0-Oberflaeche und das Phase-1-Clientverhalten, das von `iac-code a2a-client call` verwendet wird. Exakte CLI-Optionen finden Sie in der [Befehlsreferenz](./command-reference.md).

## Lifecycle-Uebersicht

Eine typische A2A-Interaktion folgt diesem Ablauf:

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **Entdecken** - `/.well-known/agent-card.json` abrufen.
2. **Senden** - Eine Textnachricht an den JSON-RPC-Endpunkt unter `/` einreichen.
3. **Streamen** - `Task`-, `Message`- und `TaskStatusUpdateEvent`-Payloads empfangen.
4. **Fortsetzen** - Eine Follow-up-Nachricht mit derselben `contextId` senden.
5. **Abbrechen oder abfragen** - `CancelTask`, `GetTask` oder `ListTasks` verwenden.

## Agent Card

Die Agent Card ist verfuegbar unter:

```text
GET /.well-known/agent-card.json
```

Wichtige Felder:

| Feld | Wert | Bedeutung |
|-------|-------|---------|
| `name` | `iac-code` | Agent-Name |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | Transport-Binding |
| `supportedInterfaces[0].protocolVersion` | `1.0` | A2A-Protokollversion |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | JSON-RPC-Endpunkt |
| `capabilities.streaming` | `true` | Unterstuetzt Streaming-Task-Aktualisierungen |
| `capabilities.pushNotifications` | `false` oder `true` | `true`, wenn `push-notifications: true` konfiguriert ist |
| `capabilities.extendedAgentCard` | `true` | Authentifizierte Aufrufer koennen erweiterte Laufzeitdetails anfordern |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | Optionaler iac-code-Metadaten-Namespace fuer Tool-Status und gespeicherte Artifact-Metadaten |
| `defaultInputModes` | Text-, JSON-, YAML-, Bild-, Audio- und Binaer-MIME-Typen | Akzeptierte Eingabe-MIME-Modi |
| `defaultOutputModes` | `["text/plain"]` | Nur Textausgabe |

Agent-Card-Antworten enthalten `Cache-Control: public, max-age=60`, `ETag` und `Last-Modified`. Clients koennen `If-None-Match` senden und `304 Not Modified` erhalten, wenn die Karte unveraendert ist.

Beworbene Skills:

| Skill ID | Zweck |
|----------|---------|
| `iac_generation` | Alibaba Cloud ROS- und Terraform-Templates aus natuerlicher Sprache generieren |
| `iac_review` | IaC-Templates pruefen und Korrekturen vorschlagen |
| `aliyun_ros_operations` | Bei Alibaba Cloud ROS Stack-Workflows unterstuetzen |
| `terraform_ros_conversion` | Terraform-zu-ROS-Konvertierung mit gebuendelten Skill-Ressourcen unterstuetzen |

Wenn Authentifizierung aktiviert ist, bewirbt die Agent Card die konfigurierten Sicherheitsschemas:

| Schema | Wann beworben |
|--------|-----------------|
| `bearerAuth` | `token` oder `IACCODE_A2A_HTTP_TOKEN` ist gesetzt |
| `basicAuth` | Basic-Benutzername und Passwort sind beide gesetzt |
| `apiKeyAuth` | `api-key` oder `IACCODE_A2A_API_KEY` ist gesetzt |

## Routen

| Route | Methode | Beschreibung |
|-------|--------|-------------|
| `/health` | `GET` | Gibt `{"status":"healthy"}` zurueck |
| `/.well-known/agent-card.json` | `GET` | Gibt die Agent Card zurueck |
| `/` | `POST` | Verarbeitet A2A JSON-RPC-Anfragen |
| REST-Routen | gemischt | Die von `create_rest_routes` registrierten A2A-SDK-REST-Routen |

## Phase-1-Client- und Transporthinweise

Der standardmaessige interoperable Phase-1-Transport ist JSON-RPC ueber HTTP. Der HTTP-Modus bewirbt ausserdem `HTTP+JSON` fuer die SDK-REST-Routen.

Der Server hat auch optionale Transports fuer stdio, Unix-Sockets, WebSocket, offizielles gRPC, gRPC JSON-RPC Envelope und Redis Streams. stdio, Unix-Sockets, WebSocket, gRPC JSON-RPC und Redis Streams sind benutzerdefinierte JSON-RPC-Transports. Offizielles gRPC wird als `grpc` beworben und erfordert optionale gRPC-Abhaengigkeiten.

Der eingebaute Client verwendet Agent-Card-Discovery (`GET /.well-known/agent-card.json`) vor Nachrichtenaufrufen, waehlt die erste beworbene ausfuehrbare `supportedInterfaces[].url` und sendet dann JSON-RPC-Anfragen mit `A2A-Version: 1.0` und A2A 1.0-Methodennamen wie `SendMessage`.

`push-notifications: true` aktiviert A2A-Push-Notification-Konfigurationsmethoden und Zustellung fuer Terminalzustaende.

Agent-Card-Signierung verwendet das A2A-SDK-Signing-Utility und gibt standardmaessige `AgentCardSignature`-JWS-Felder aus. Der symmetrische Schluesselmodus verwendet `HS256`; die Verifikation kann anhand des Protected-Header-`kid` ein konfiguriertes Secret, ein lokales Octet-Key-JWKS oder eine entfernte JWKS-URL auswaehlen. Serverseitige asymmetrische Signierung und automatische Schluesselrotation sind in Phase 1 nicht implementiert.

Die kanonische Liste des in Phase 1 nicht unterstuetzten Verhaltens finden Sie unter [A2A-Protokoll](./overview.md#phase-1-unsupported).

## Push-Notification-Zustell-Backends

`iac-code a2a --config a2a-server.yml` unterstuetzt zwei Push-Zustellqueues:

- `push-queue: local-file` speichert Jobs unterhalb des A2A-Persistenzverzeichnisses und ist fuer lokale Single-Node-Nutzung gedacht.
- `push-queue: redis-streams` speichert Jobs in Redis Streams und koordiniert Worker ueber eine Redis Consumer Group.

Redis-gestuetzte Push-Zustellung erfordert das optionale Extra `a2a-redis` und ist mindestens einmal. Callback-Empfaenger sollten Task-Aktualisierungen idempotent behandeln, da ein Job nach Worker-Crashes, Lease-Ablauf, Reconnects oder Retry-Races erneut zugestellt werden kann.

Haeufige Redis-Optionen:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

Callback-URLs werden vor dem Speichern und erneut vor dem Versand validiert. Der Standardvalidator lehnt Nicht-HTTP(S)-URLs, localhost-Hostnamen und literale private/lokale IP-Adressen ab. Callback-Empfaenger sollten dennoch ihre eigene Authentifizierungs- und Idempotenz-Policy erzwingen.

## JSON-RPC-Methoden

### SendMessage

Fuehrt einen nicht streamenden A2A-Nachrichten-Turn aus. Die Antwort enthaelt einen Task oder eine Nachricht, nachdem der Turn abgeschlossen ist.

**Anfrage**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**Erforderliche Nachrichtenfelder**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `messageId` | string | Ja | Eindeutige Client-Nachrichten-ID |
| `role` | string | Ja | `ROLE_USER` fuer Benutzereingaben verwenden |
| `parts` | array | Ja | Textartige, JSON-Daten-, Rohtext-, lokale File-URL- oder begrenzte multimodale Teile |
| `metadata.iac_code.cwd` | string | Empfohlen | Absoluter Workspace-Pfad; faellt auf das Server-Prozessverzeichnis zurueck, wenn ausgelassen |

`metadata.iac_code.cwd` muss, wenn angegeben, ein vorhandenes absolutes Verzeichnis sein. Es muss innerhalb eines erlaubten Workspace-Roots liegen. Standardmaessig sind die erlaubten Roots das Server-Prozessverzeichnis und das System-Temp-Verzeichnis; `IACCODE_A2A_ALLOWED_CWDS` kann eine OS-pfadgetrennte Allowlist bereitstellen.

Unterstuetzte Eingabekategorien:

| Kategorie | Akzeptierte Form | Grenzen und Verhalten |
|----------|----------------|---------------------|
| Textartige Teile | `text` mit `text/plain`, JSON, Markdown, YAML oder konfigurierten zusaetzlichen Text-MIME-Typen | Direkt an den Prompt angehaengt |
| JSON-Datenteile | `data` mit `application/json` | In kompaktes JSON serialisiert; max. 1 MiB inline |
| Rohtextteile | `raw` mit einem textartigen MIME-Typ | Muss gueltiges UTF-8 sein; max. 1 MiB inline |
| Lokale Textdatei-URLs | `url` mit `file://...` und textartigem MIME-Typ | Datei muss innerhalb von `cwd` und erlaubten Roots existieren; max. 1 MiB |
| Multimodale Raw-/Data-/File-Teile | Bild-, Audio- oder konfigurierte multimodale MIME-Typen | In ein Prompt-Manifest mit Dateiname, Medientyp, Bytegroesse, Hash und Quelle konvertiert; Raw/Data max. 5 MiB, File-URL max. 25 MiB |

Die Aufnahme entfernter HTTP(S)-URLs wird nicht unterstuetzt. File-URL-Teile muessen lokale `file://`-URLs verwenden und innerhalb des erlaubten Workspace bleiben.

### SendStreamingMessage

Fuehrt einen streamenden A2A-Nachrichten-Turn aus. Der Anfragebody hat dieselbe Form wie `SendMessage`, aber der Server streamt JSON-RPC-Antworten als Server-Sent Events.

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

Gibt den gespeicherten A2A-Task per ID zurueck. Verwenden Sie `historyLength`, um die zurueckgegebene Historie zu begrenzen, ohne die gespeicherte Task-Historie zu veraendern. Lassen Sie es aus, um die aktuelle Standardhistorie des Servers zu erhalten.

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

Gibt bekannte Tasks zurueck, die fuer den authentifizierten Aufrufer sichtbar sind. Ergebnisse werden nach Status-Zeitstempel absteigend und dann nach Task-ID absteigend fuer stabile Reihenfolge sortiert. Der Server unterstuetzt `contextId`, `status`, `pageSize`, `pageToken`, `historyLength` und `includeArtifacts`.

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

`nextPageToken` wird zurueckgegeben, wenn eine weitere Seite verfuegbar ist. `includeArtifacts` ist standardmaessig `false`, sodass Listenantworten Task-Artifacts auslassen, sofern sie nicht explizit angefordert werden.

### CancelTask

Fordert den Abbruch eines laufenden Tasks an.

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

Wenn der Task aktiv ist, bricht der Server den laufenden Agent-Turn ab und gibt einen abgebrochenen Task-Zustand aus. Wenn der Task existiert, aber nicht laeuft, gibt der Server den standardmaessigen A2A-`TaskNotCancelableError` zurueck.

### SubscribeToTask

Abonniert einen aktiven Task-Aktualisierungsstream, wenn der Client-Transport dies unterstuetzt.

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

Fuer aktive Tasks beginnt der Stream mit dem aktuellen `Task`, gibt dann nachfolgende Task-Events aus und schliesst, wenn der aktive Turn beendet ist. Das Abonnieren eines abgeschlossenen, fehlgeschlagenen, abgebrochenen oder input-required Tasks gibt einen task-not-found-artigen Fehler zurueck, statt unbegrenzt zu warten. Fuer neue Turns bevorzugen Sie `SendStreamingMessage`; es startet die Ausfuehrung und streamt die Antwort in einer Anfrage.

### Push-Notification-Config-Methoden

Wenn der Server mit `push-notifications: true` startet, unterstuetzt er:

| Methode | Zweck |
|--------|---------|
| `CreateTaskPushNotificationConfig` | Eine Callback-Config fuer einen Task speichern |
| `GetTaskPushNotificationConfig` | Eine Callback-Config abrufen |
| `ListTaskPushNotificationConfigs` | Callback-Configs fuer einen Task auflisten |
| `DeleteTaskPushNotificationConfig` | Eine Callback-Config loeschen |

Beispiel fuer eine Create-Anfrage:

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

Der Server verschluesselt gespeicherte Notification-Tokens und Callback-Authentifizierungszugangsdaten, wenn der lokale Push-Keyring verfuegbar ist.

### GetExtendedAgentCard

Authentifizierte Clients koennen die erweiterte Agent Card anfordern:

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

Die erweiterte Karte enthaelt die oeffentliche Karte plus authentifizierte Laufzeitdetails.

## Task- und Kontextverhalten

iac-code bildet A2A-Kontexte auf interne Agent-Laufzeiten ab:

| Konzept | Verhalten |
|---------|----------|
| `contextId` omitted | Das SDK/der Server generiert eine neue Kontext-ID |
| Same `contextId` | Verwendet dieselbe interne iac-code-Sitzung und denselben Unterhaltungszustand wieder |
| Same `contextId`, different `cwd` | Wird als anderer Workspace abgelehnt |
| Same `contextId`, concurrent message | Wird mit `Task is already working.` abgelehnt |
| Different `contextId` values | Koennen parallel ausgefuehrt werden |
| Idle context | Wird nach dem konfigurierten Idle-Timeout aus dem Speicher entfernt |

Task- und Kontext-IDs muessen nicht leer sein, hoechstens 128 Zeichen haben und duerfen nur Buchstaben, Ziffern, `_`, `.`, `:` oder `-` enthalten.

## Task-Zustaende

| Zustand | Bedeutung |
|-------|---------|
| `TASK_STATE_SUBMITTED` | Der Task wurde akzeptiert |
| `TASK_STATE_WORKING` | iac-code fuehrt den Agent-Turn aus |
| `TASK_STATE_INPUT_REQUIRED` | Der Turn wurde abgeschlossen und der Agent ist bereit fuer Follow-up-Eingaben |
| `TASK_STATE_CANCELED` | Abbruch wurde angefordert und angewendet |
| `TASK_STATE_FAILED` | Der Task ist bei Validierung oder Ausfuehrung fehlgeschlagen |

iac-code verwendet `TASK_STATE_INPUT_REQUIRED` als normalen abgeschlossenen Zustand, da der Kontext fuer Follow-up-Nachrichten verfuegbar bleibt.

## Streaming-Aktualisierungen

Waehrend der Ausfuehrung gibt iac-code `TaskStatusUpdateEvent`-Aktualisierungen aus.

Assistant-Text wird als Statusnachricht geliefert:

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

Tool- und Nutzungsdetails werden ueber `metadata.iac_code` geliefert:

| Metadatenpfad | Beschreibung |
|---------------|-------------|
| `iac_code.tool.status` | `started`, `input_delta`, `input_complete`, `completed` oder `failed` |
| `iac_code.tool.toolUseId` | Stabile Tool-Use-ID zur Korrelation von Tool-Events |
| `iac_code.tool.name` | Tool-Name, wenn verfuegbar |
| `iac_code.tool.inputSummary` | Nur die Form der abgeschlossenen Tool-Eingabe; String-Werte und Feldnamen ausserhalb der Whitelist werden ohne rohe fachliche Payloads dargestellt |
| `iac_code.tool.result` | Tool-Ergebnis, pro Feld auf 4000 Zeichen gekuerzt |
| `iac_code.permission.autoApproved` | Ob A2A die Berechtigungsanfrage automatisch oder ueber einen Resolver genehmigt hat |
| `iac_code.permission.toolName` | Tool, das die Berechtigung angefordert hat |
| `iac_code.permission.safeSummary` | Redigierte, menschenlesbare Berechtigungszusammenfassung |
| `iac_code.permission.inputSummary` | Sichere Operationszusammenfassung fuer `aliyun_api`-Berechtigungsanfragen |
| `iac_code.permission.toolInput` | Nur die Form der Tool-Eingabe fuer Berechtigungsanfragen ausserhalb von `aliyun_api`; String-Werte verwenden Typ, Laenge und Fingerprint, und Feldnamen ausserhalb der Whitelist koennen als Fingerprint erscheinen |
| `iac_code.thinking.type` | `raw_thinking`, wenn `raw-thinking` in `thinking-exposure` aktiviert ist |
| `iac_code.thinking.text` | Roher Provider-Reasoning-Chunk, auf 4000 Zeichen gekuerzt, nur fuer vertrauenswuerdige Konfigurationen ausgegeben, die `raw-thinking` aktivieren |
| `iac_code.usage.inputTokens` | Anzahl der Eingabetoken fuer den Turn |
| `iac_code.usage.outputTokens` | Anzahl der Ausgabetoken fuer den Turn |
| `iac_code.usage.totalTokens` | Gesamtzahl der Token fuer den Turn |

Wenn ein Tool-Ergebnis eine unterstuetzte Text-Artifact-Payload enthaelt, speichert der Server die Payload lokal, gibt ein standardmaessiges `TaskArtifactUpdateEvent` aus und zeichnet das Artifact im Task-Feld `artifacts` auf. Der Artifact-Teil verwendet eine `file://`-URL plus Metadaten wie `mediaType`, `byteSize` und `sha256`; der urspruengliche Artifact-Inhalt wird nicht innerhalb der Tool-Metadaten dupliziert.

## Extensions

Die Agent Card bewirbt die optionale iac-code-Artifact-Metadaten-Extension:

```text
urn:iac-code:a2a:artifact-metadata:v1
```

Diese Extension identifiziert den Namespace `metadata.iac_code`, der fuer Tool-Fortschritt, Berechtigungsentscheidungen, Token-Nutzung und lokale Artifact-Metadaten verwendet wird. Wenn der Server mit einer erforderlichen Extension konfiguriert ist, muessen Clients ihre URI im Header `A2A-Extensions` einschliessen. Fehlende erforderliche Extensions geben den standardmaessigen A2A-`ExtensionSupportRequiredError` zurueck.

Wenn `thinking-exposure` einen oder mehrere Signaltypen aktiviert, kuendigt die Agent Card auch Folgendes an:

```text
urn:iac-code:a2a:thinking-exposure:v1
```

Die Extension-Params enthalten `enabledTypes`, zum Beispiel `["tool_trace", "raw_thinking"]`. Dies ist eine iac-code-Metadatenerweiterung und kein zentraler A2A-Streaming-Event-Typ.

## Fehlerbehandlung

| Szenario | Ergebnis |
|----------|--------|
| Leere Texteingabe | `TASK_STATE_FAILED` mit `A2A server currently accepts text input only.` |
| Nicht unterstuetzter Medientyp | Validierungsfehler oder standardmaessiger A2A-Content-Type-Fehler, je nachdem, wo das SDK die Anfrage ablehnt |
| Remote-URL-Teil | Validierungsfehler, weil URL-Teile lokale `file://`-URLs verwenden muessen |
| File-URL ausserhalb des erlaubten Workspace | Validierungsfehler |
| Fehlende erforderliche A2A-Extension | Standardmaessiger A2A-`ExtensionSupportRequiredError` |
| Ungueltige Workspace-Metadaten | `TASK_STATE_FAILED` mit einer Meldung zu ungueltigem Workspace |
| Fehlende oder ungueltige Authentifizierung | HTTP `401` mit `{"error":"Unauthorized"}` |
| Fehlende A2A-Serverabhaengigkeiten | CLI beendet sich mit einem Installationshinweis fuer das Extra `a2a` |
| Provider-Zugangsdaten fehlen | Bereinigter Authentifizierungsfehler |
| Unerwarteter Laufzeitfehler | Bereinigter interner Fehler |

Der Server vermeidet es, lokale Pfade, Secrets und Provider-Details in unerwarteten Fehlermeldungen zurueckzugeben.
