---
title: Befehlsreferenz
description: Vollstaendige CLI-Befehlsreferenz fuer das Ausfuehren und Aufrufen von iac-code ueber A2A.
sidebar_position: 3
---

# A2A-Befehlsreferenz

Diese Seite dokumentiert jeden A2A-bezogenen `iac-code`-Befehl. Verwenden Sie sie, wenn Sie exakte Optionsnamen, gaengige Befehlsmuster und die betriebliche Bedeutung jedes Flags benoetigen.

## Befehlsuebersicht

| Befehl | Zweck |
|---------|---------|
| `iac-code a2a` | iac-code als A2A-Server ausfuehren |
| `iac-code a2a-client call` | Eine entfernte Agent Card entdecken und einen Prompt senden |
| `iac-code a2a-client discover` | Eine Agent Card abrufen und optional verifizieren |
| `iac-code a2a-client task-get` | Einen Task per ID abrufen |
| `iac-code a2a-client task-list` | Tasks mit Filtern und Paginierung auflisten |
| `iac-code a2a-client task-cancel` | Einen aktiven Task abbrechen |
| `iac-code a2a-client task-subscribe` | Einen aktiven Task-Event-Stream abonnieren |
| `iac-code a2a-client push-config-create` | Eine Task-Push-Notification-Config erstellen |
| `iac-code a2a-client push-config-get` | Eine Task-Push-Notification-Config abrufen |
| `iac-code a2a-client push-config-list` | Task-Push-Notification-Configs auflisten |
| `iac-code a2a-client push-config-delete` | Eine Task-Push-Notification-Config loeschen |
| `iac-code a2a-client extended-card` | Die authentifizierte erweiterte Agent Card abrufen |
| `iac-code a2a-client route-preview` | Lokale Routenauswahl fuer `a2a-client call` voranzeigen |

Alle HTTP-Clientbefehle akzeptieren dieselben Authentifizierungsoptionen:

| Option | Beschreibung |
|--------|-------------|
| `--token` | Bearer Token, gesendet als `Authorization: Bearer <token>` |
| `--basic-username` | Benutzername fuer Basic Auth |
| `--basic-password` | Passwort fuer Basic Auth |
| `--api-key` | API-Key-Wert |
| `--api-key-header` | API-Key-Headername; standardmaessig `X-API-Key` |

## A2A-Client-Konfiguration

Alle `a2a-client`-Unterbefehle akzeptieren eine YAML-Konfigurationsdatei auf Gruppenebene:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

CLI-Optionen ueberschreiben Konfigurationswerte. Verwenden Sie Konfiguration fuer stabile Verbindung, Auth, Verifikation, Routing und wiederholte Task- oder Push-Einstellungen; behalten Sie einmaligen Prompt-Text auf der Befehlszeile.

```yaml
url: http://127.0.0.1:41242/
token: your-bearer-token
basic-username: iac-code
basic-password: your-password
api-key: your-api-key
api-key-header: X-IAC-Code-Key
verify-card-secret: your-card-signing-secret
verify-card-jwks-url: https://a2a.example.com/.well-known/jwks.json
require-card-signature: true
timeout: 30
cwd: /path/to/workspace
context-id: ctx-123
task-id: task-123
config-id: webhook-1
callback-url: https://hooks.example.com/a2a
notification-token: notification-token
auth-scheme: bearer
auth-credentials: callback-token
routes:
  - name: ros
    url: http://127.0.0.1:41242/
    skills:
      - iac_generation
    tags:
      - ros
      - template
```

## `iac-code a2a`

Fuehren Sie iac-code als A2A-Server aus.

```bash
iac-code a2a
```

Standardmaessig bindet der Server an `127.0.0.1:41242` und stellt JSON-RPC ueber HTTP bereit. Port `41242` ist der iac-code-Standard; er ist kein registrierter A2A-Port.

### Grundlegende Serveroptionen

| Option | Standard | Beschreibung |
|--------|---------|-------------|
| `--config` | leer | YAML-Konfigurationsdatei mit A2A-Serveroptionen |
| `--host` | `127.0.0.1` | HTTP-Serverhost |
| `--port` | `41242` | HTTP-Serverport |
| `--transport` | `http` | Server-Transport: `http`, `stdio`, `unix`, `websocket`, `grpc`, `grpc-jsonrpc` oder `redis-streams` |
| `--thinking-exposure` | `tool-trace` | Legt einen A2A-Thinking-Signaltyp offen; fuer mehrere Werte wiederholen. Werte: `raw-thinking`, `tool-trace` |
| `--debug`, `-d` | `false` | Debug-Logging aktivieren |

Beispiel:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### YAML-Konfiguration

Verwenden Sie `--config` fuer Authentifizierung, Speicherung, Signierung, transportspezifische Einstellungen, Push-Zustellung und andere Deployment-Details. Schluessel koennen Bindestriche oder Unterstriche verwenden. Die gaengigen CLI-Flags `--host`, `--port` und `--transport` ueberschreiben Werte aus der Konfigurationsdatei.

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
```

Fuehren Sie ihn aus mit:

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### HTTP-Authentifizierung

Authentifizierung ist optional. Konfigurieren Sie Serverauthentifizierung in YAML oder mit Umgebungsvariablen. Wenn keine Auth-Einstellung konfiguriert ist, sind Anfragen unauthentifiziert. Wenn ein oder mehrere Schemas konfiguriert sind, kann eine Anfrage jedes konfigurierte Schema erfuellen.

| Konfigurationsschluessel | Umgebungsvariable | Beschreibung |
|--------|----------------------|-------------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Bearer Token |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Benutzername fuer Basic Auth |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Passwort fuer Basic Auth |
| `api-key` | `IACCODE_A2A_API_KEY` | API-Key-Wert |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | API-Key-Headername |

Bearer Token:

```yaml
token: local-dev-token
```

Basic Auth:

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

API Key:

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### Persistenz und Artifacts

| Konfigurationsschluessel | Standard | Beschreibung |
|--------|---------|-------------|
| `persistence-dir` | `~/.iac-code/a2a` | Lokale JSON-Metadaten fuer Tasks, Kontexte, Routen und Push-Configs |
| `artifact-dir` | `<persistence-dir>/artifacts` | Lokaler Artifact-Payload-Speicher |

Persistenz spiegelt Task- und Kontext-Snapshots fuer Wiederherstellungsmetadaten. Sie startet einen laufenden asyncio-Task nach einem Prozessabsturz nicht neu.

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Agent-Card-Signierung

| Konfigurationsschluessel | Beschreibung |
|--------|-------------|
| `signing-secret` | HMAC-Secret zum Signieren der oeffentlichen Agent Card |

Der Server gibt A2A-SDK-`AgentCardSignature`-JWS-Felder aus. Der symmetrische Modus verwendet `HS256`.

```yaml
signing-secret: local-card-signing-secret
```

### Push-Notification-Zustellung

| Konfigurationsschluessel | Standard | Beschreibung |
|--------|---------|-------------|
| `push-notifications` | `false` | A2A-Task-Push-Notification-Config-Methoden und Terminalzustands-Zustellung aktivieren |
| `push-queue` | `local-file` | Push-Queue-Backend: `local-file` oder `redis-streams` |
| `push-redis-url` | leer | Redis-URL fuer die Redis-gestuetzte Push-Queue |
| `push-stream` | `iac-code:a2a:push` | Redis Stream fuer Push-Jobs |
| `push-retry-key` | `iac-code:a2a:push:retry` | Redis Sorted Set fuer verzoegerte Wiederholungen |
| `push-dead-stream` | `iac-code:a2a:push:dead` | Redis Stream fuer Dead-Letter-Jobs |
| `push-consumer-group` | `iac-code-push` | Redis Consumer Group fuer Push-Worker |
| `push-consumer-name` | leer | Redis Consumer-Name fuer diesen Worker |
| `push-lease-timeout-ms` | `300000` | Redis Pending-Lease-Timeout |

Lokale Datei-Queue:

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

Redis-Streams-Queue:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
```

Redis-gestuetzte Push-Zustellung erfordert das Extra `a2a-redis`.

### Transportoptionen

| Transport | Befehl | Hinweise |
|-----------|---------|-------|
| HTTP JSON-RPC und REST | `iac-code a2a --transport http` | Standard. Bewirbt `JSONRPC`- und `HTTP+JSON`-Schnittstellen. |
| stdio | `iac-code a2a --transport stdio` | Experimentelle benutzerdefinierte JSON-RPC-Frames ueber Standardeingabe/-ausgabe. |
| Unix socket | `iac-code a2a --config a2a-server.yml --transport unix` | Erfordert `socket-path` in der Konfiguration. |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | Verwendet `ws-path` aus der Konfiguration, standardmaessig `/a2a`. |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | Verwendet `grpc-host` und `grpc-port` aus der Konfiguration. |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | Benutzerdefinierter JSON-RPC Envelope ueber gRPC. |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | Erfordert `redis-url` in der Konfiguration. |

Redis-Streams-Transportoptionen:

| Konfigurationsschluessel | Standard | Beschreibung |
|--------|---------|-------------|
| `redis-url` | leer | Redis-Verbindungs-URL; erforderlich fuer `--transport redis-streams` |
| `request-stream` | `iac-code:a2a:requests` | Name des Request Streams |
| `response-stream` | `iac-code:a2a:responses` | Name des Response Streams |
| `consumer-group` | `iac-code` | Consumer Group des Request Streams |

### Thinking-Offenlegung

| Konfigurationsschluessel | Standard | Beschreibung |
|--------|---------|-------------|
| `thinking-exposure` | `tool-trace` | Nicht zur Antwort gehoerende Laufzeitsignaltypen, die ueber A2A `metadata.iac_code` offengelegt werden. Verwenden Sie eine YAML-Liste, eine kommaseparierte Zeichenkette oder wiederholte `--thinking-exposure`-Flags. Unterstuetzte Werte sind `tool-trace` und `raw-thinking`. |

`tool-trace` behaelt die vorhandenen Metadaten fuer Tool-Fortschritt, Berechtigungen und Ergebnisse bei. `raw-thinking` gibt Provider-Reasoning-Chunks als `metadata.iac_code.thinking`-Updates mit `type: raw_thinking` und `text` aus. iac-code erzeugt derzeit keine separaten thought-summary- oder progress-summary-Events, daher sind diese keine gueltigen Offenlegungstypen.

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### Berechtigungsverhalten

| Konfigurationsschluessel | Standard | Beschreibung |
|--------|---------|-------------|
| `auto-approve-permissions` | `false` | Tool-Berechtigungsanfragen, die waehrend A2A-Turns entstehen, automatisch genehmigen |

Ohne `auto-approve-permissions: true` lehnt der A2A-Modus Berechtigungsabfragen ab und gibt Berechtigungsmetadaten aus. Verwenden Sie es nur fuer vertrauenswuerdige Automatisierungsumgebungen.

## `iac-code a2a-client call`

Entdeckt eine Agent Card, waehlt den beworbenen Endpunkt und sendet einen Prompt.

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD"
```

| Option | Standard | Beschreibung |
|--------|---------|-------------|
| `--url` | leer | A2A-Agent-Basis-URL oder JSON-RPC-Endpunkt-URL; kann aus der Konfiguration kommen |
| `--route` | wiederholbar | Route-Spec, die verwendet wird, wenn `--url` ausgelassen ist |
| `--route-name` | leer | Auszuwaehlende benannte Route |
| `--prompt`, `-p` | erforderlich | Prompt-Text |
| `--cwd` | `.` | Workspace-Pfad, gesendet als `message.metadata.iac_code.cwd` |
| `--context-id` | leer | Vorhandene A2A-Kontext-ID fuer eine Follow-up-Nachricht |
| `--verify-card-secret`, `--signing-secret` | leer | HMAC-Secret fuer Agent-Card-Verifikation |
| `--verify-card-jwks-url` | leer | Entfernte JWKS-URL fuer Agent-Card-Verifikation |
| `--require-card-signature`, `--require-signature` | `false` | Unsignierte oder ungueltige Agent Cards ablehnen |
| `--timeout` | `30.0` | Aufruf-Timeout in Sekunden |
| `--stream` | `false` | `SendStreamingMessage` verwenden und Stream-Events ausgeben |

Follow-up im selben Kontext:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --context-id ctx-123 \
  --prompt "Now add outputs for the VPC and vSwitch IDs." \
  --cwd "$PWD"
```

Streaming:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this Terraform module." \
  --cwd "$PWD" \
  --stream
```

Signierte Agent Card verlangen:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

Mit einer entfernten JWKS-URL verifizieren:

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

Eine entfernte Agent Card abrufen und ausgeben.

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| Option | Beschreibung |
|--------|-------------|
| `--url` | A2A-Agent-Basis-URL; kann aus der Konfiguration kommen |
| `--verify-card-secret`, `--signing-secret` | HMAC-Secret fuer Verifikation |
| `--verify-card-jwks-url` | Entfernte JWKS-URL fuer Verifikation |
| `--require-card-signature`, `--require-signature` | Eine gueltige Signatur verlangen |

Authentifizierte Discovery:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## Task-Befehle

Task-Befehle rufen JSON-RPC-Task-Methoden direkt auf. Sie sind fuer Betriebswerkzeuge, Dashboards und Debugging nuetzlich.

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| Option | Beschreibung |
|--------|-------------|
| `--url` | A2A-JSON-RPC-Endpunkt-URL; kann aus der Konfiguration kommen |
| `--task-id` | Task-ID; kann aus der Konfiguration kommen |
| `--history-length` | Maximale Anzahl zurueckzugebender Task-Historieneintraege |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| Option | Standard | Beschreibung |
|--------|---------|-------------|
| `--url` | leer | A2A-JSON-RPC-Endpunkt-URL; kann aus der Konfiguration kommen |
| `--context-id` | leer | Nach Kontext-ID filtern |
| `--status` | leer | Nach Task-Zustand filtern |
| `--page-size` | leer | Maximale Anzahl zurueckzugebender Tasks |
| `--page-token` | leer | Paginierungstoken |
| `--include-artifacts` | `false` | Task-Artifacts in die Antwort einschliessen |
| `--output` | `table` | `table` oder `json` |

JSON-Ausgabe:

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --include-artifacts \
  --output json
```

### `iac-code a2a-client task-cancel`

```bash
iac-code a2a-client --config a2a-client.yml task-cancel \
  --task-id task-123
```

Der Abbruch ist kooperativ. Ein abgeschlossener, fehlgeschlagener, abgebrochener oder input-required Task gibt den standardmaessigen A2A-Task-not-cancelable-Fehler zurueck.

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

Der Befehl streamt Events fuer aktive Tasks. Fuer einen neuen Turn bevorzugen Sie `a2a-client call --stream`; dies startet den Task und streamt Aktualisierungen in einem Befehl.

## Push-Notification-Config-Befehle

Diese Befehle erfordern einen Server, der mit `push-notifications: true` gestartet wurde. Sie verwalten standardmaessige A2A-Task-Push-Notification-Configs.

### `iac-code a2a-client push-config-create`

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

| Option | Beschreibung |
|--------|-------------|
| `--url` | A2A-JSON-RPC-Endpunkt-URL; kann aus der Konfiguration kommen |
| `--task-id` | Task-ID; kann aus der Konfiguration kommen |
| `--config-id` | Push-Config-ID; kann aus der Konfiguration kommen |
| `--callback-url` | HTTP(S)-Callback-URL; kann aus der Konfiguration kommen |
| `--notification-token` | Token, gesendet als `X-A2A-Notification-Token` |
| `--auth-scheme` | Callback-Auth-Schema, zum Beispiel `bearer` oder `basic` |
| `--auth-credentials` | Callback-Auth-Zugangsdaten |

Callback-URLs werden vor Speicherung und Versand validiert. Der Standardvalidator lehnt Nicht-HTTP(S)-URLs, localhost-Namen und literale private/lokale IP-Adressen ab.

### `iac-code a2a-client push-config-get`

```bash
iac-code a2a-client --config a2a-client.yml push-config-get \
  --task-id task-123 \
  --config-id webhook-1
```

### `iac-code a2a-client push-config-list`

```bash
iac-code a2a-client --config a2a-client.yml push-config-list \
  --task-id task-123 \
  --page-size 10
```

### `iac-code a2a-client push-config-delete`

```bash
iac-code a2a-client --config a2a-client.yml push-config-delete \
  --task-id task-123 \
  --config-id webhook-1
```

## `iac-code a2a-client extended-card`

Die authentifizierte erweiterte Agent Card abrufen.

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

Die oeffentliche Agent Card bewirbt `capabilities.extendedAgentCard=true`. Die erweiterte Karte fuegt authentifizierte Laufzeitdetails hinzu, einschliesslich Task-Verwaltung und Push-Konfigurationsfaehigkeitsmetadaten.

## `iac-code a2a-client route-preview`

Vorschau, wie `a2a-client call` konfigurierte Routen aufloest, wenn `--url` ausgelassen ist.

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| Option | Beschreibung |
|--------|-------------|
| `--route` | Wiederholbare Route-Spec im Format `name=url;skills=a,b;tags=x,y` |
| `--name` | Aufzuloesender Routenname |
| `--skill` | Aufzuloesende Skill-ID |
| `--prompt` | Prompt-Text fuer Name-/Tag-Abgleich |
| `--route-state-dir`, `--persistence-dir` | Verzeichnis zum Persistieren von Routen-Snapshots |
| `--save-routes` | Angegebene Routen im Routen-Zustandsverzeichnis speichern |

Routen-Snapshots speichern:

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

Aufruf ueber Routen:

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## Umgebungsvariablen

| Variable | Beschreibung |
|----------|-------------|
| `IACCODE_A2A_HTTP_TOKEN` | Standard fuer Server-/Client-Bearer-Token |
| `IACCODE_A2A_BASIC_USERNAME` | Standard fuer Server-/Client-Basic-Auth-Benutzername |
| `IACCODE_A2A_BASIC_PASSWORD` | Standard fuer Server-/Client-Basic-Auth-Passwort |
| `IACCODE_A2A_API_KEY` | Standard fuer Server-/Client-API-Key |
| `IACCODE_A2A_API_KEY_HEADER` | Standard fuer API-Key-Headername |
| `IACCODE_A2A_ALLOWED_CWDS` | OS-pfadgetrennte Liste erlaubter Workspace-Roots fuer eingehende Nachrichtenmetadaten und File-URLs |
| `IACCODE_A2A_TEXT_MIME_TYPES` | Zusaetzliche komma- oder semikolongetrennte textartige MIME-Typen |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | Zusaetzliche komma- oder semikolongetrennte multimodale MIME-Typen |
| `IAC_CODE_A2A_PUSH_KEYRING` | Umgebungsgesteuerter verschluesselter Push-Secret-Keyring |
