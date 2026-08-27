---
sidebar_position: 2
title: Erste Schritte
description: Den AG-UI-Adapter von iac-code installieren, starten und aufrufen.
---

# Erste Schritte mit AG-UI

## Voraussetzungen

1. Python 3.10 oder neuer ist installiert.
2. Ein LLM-Anbieter ist für iac-code konfiguriert. Siehe [Authentifizierung](../configuration/authentication.md).
3. Für Zugriffe auf Alibaba Cloud sind Cloud-Anmeldedaten konfiguriert oder temporäre Anmeldedaten werden pro Anfrage übergeben.
4. Ein absoluter Arbeitsbereichspfad steht iac-code zum Lesen und Schreiben zur Verfügung.

Installieren Sie die AG-UI-Abhängigkeiten:

```bash
pip install "iac-code[agui]"
```

Für die Entwicklung aus dem Quellrepository:

```bash
uv sync --extra agui
```

## Option 1: Verwalteten lokalen A2A-Kern starten

Lassen Sie für die einfachste lokale Konfiguration `--a2a-url` weg:

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

Der Adapter wählt einen freien Loopback-Port, startet einen verwalteten Kindprozess `iac-code a2a` und beendet ihn zusammen mit dem Adapter. Der Kindprozess übernimmt die aktuelle iac-code-Konfiguration und Laufzeitumgebung.

Dieser Modus eignet sich für lokale Entwicklung und einen gemeinsamen Prozesslebenszyklus. Verwenden Sie die nächste Option, wenn beide Dienste in der Produktion unabhängig überwacht werden sollen.

## Option 2: Mit einem unabhängigen A2A-Kern verbinden

Starten Sie zuerst den A2A-Server:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

Starten Sie dann den AG-UI-Adapter:

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

Die Dienste behalten getrennte Aufgaben und Ports. A2A kann weiterhin A2A-Clients bedienen, während der Adapter nur über die Loopback-Schnittstelle darauf zugreift.

Mit `--thinking-exposure all` wandelt der Adapter rohe Reasoning-Inhalte in standardisierte `REASONING_*`-Ereignisse um. Aktivieren Sie dies nur für vertrauenswürdige Clients. Verwenden Sie den A2A-Standard `tool-trace`, wenn Reasoning-Inhalte nicht offengelegt werden sollen.

Bei einem Bearer-Token für A2A:

```bash
export IACCODE_A2A_HTTP_TOKEN="lokales-a2a-geheimnis"
iac-code a2a --host 127.0.0.1 --port 41242
```

Übergeben Sie dem Adapter dasselbe Upstream-Token:

```bash
export IAC_CODE_AGUI_A2A_TOKEN="lokales-a2a-geheimnis"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## YAML-Konfiguration

Statische Starteinstellungen können in YAML gespeichert werden:

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
interrupt-ttl: 540
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

Starten Sie den Adapter mit:

```bash
iac-code agui --config agui-server.yml
```

Explizite CLI-Argumente überschreiben YAML. Übergeben Sie sensible Werte wie Tokens über Umgebungsvariablen, statt sie in der Konfigurationsdatei zu speichern.

| CLI / YAML | Standard | Bedeutung |
|------------|----------|-----------|
| `--host` / `host` | `127.0.0.1` | HTTP-Bindeadresse von AG-UI |
| `--port` / `port` | `8000` | AG-UI-HTTP-Port; Bereitstellungsbeispiele verwenden `41243` |
| `--a2a-url` / `a2a-url` | leer | Lokale A2A-URL; leer startet einen verwalteten Kindprozess |
| `--interrupt-ttl` / `interrupt-ttl` | `540` | Sekunden, in denen eine Unterbrechung wiederaufgenommen werden kann |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | Verzeichnis für AG-UI-Threadstatus |
| `--idle-shutdown` / `idle-shutdown` | `0` | Leerlaufabschaltung; `0` deaktiviert sie |
| `--debug` / `debug` | `false` | Debug-Protokollierung |
| `--log-stdout` / `log-stdout` | `false` | Protokolle zusätzlich auf stdout ausgeben |

Zugehörige Umgebungsvariablen:

| Variable | Zweck |
|----------|-------|
| `IAC_CODE_AGUI_HOST` | AG-UI-Bindeadresse |
| `IAC_CODE_AGUI_PORT` | AG-UI-Port |
| `IAC_CODE_AGUI_A2A_URL` | Lokale A2A-Upstream-URL |
| `IAC_CODE_AGUI_A2A_TOKEN` | Bearer-Token für den A2A-Upstream |
| `IAC_CODE_AGUI_AUTH_TOKEN` | Bearer-Token zum Schutz des AG-UI-Endpunkts |
| `IAC_CODE_AGUI_INTERRUPT_TTL` | Lebensdauer einer Unterbrechung |
| `IAC_CODE_AGUI_STATE_DIR` | Verzeichnis für AG-UI-Threadstatus |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | Erlaubte Arbeitsbereichswurzeln, getrennt mit dem Pfadtrenner des Betriebssystems |
| `IAC_CODE_CONFIG_DIR` | iac-code-Konfigurationswurzel und übergeordnetes Standardverzeichnis für AG-UI-Status |

## Zustandsprüfung

```bash
curl http://127.0.0.1:41243/health
```

Beispielantwort:

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "aktuelle iac-code-Version"
}
```

## Offiziellen JavaScript-Client verwenden

Installieren Sie die geprüfte Clientversion:

```bash
pnpm add @ag-ui/client@0.0.58
```

Das Beispiel verbindet sich mit `iac-code agui`, verwendet den standardisierten `HttpAgent` und übergibt iac-code-Laufzeitwerte in `forwardedProps`:

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const threadId = randomUUID();
const rosInvocationId = randomUUID();
const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId,
  // Wenn IAC_CODE_AGUI_AUTH_TOKEN konfiguriert ist:
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId,
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "de",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "Erstelle eine VPC-Vorlage mit zwei vSwitches.",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[Werkzeug] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[Schritt] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

Bei einem Bearer-Token übergeben Sie `Authorization` über `HttpAgent.headers`. Browseranwendungen verbinden sich normalerweise über ein Same-Origin-Backend oder einen Reverse Proxy; der Adapter fügt keine CORS-Richtlinie hinzu.

## Unterbrechungen verarbeiten

Der offizielle Client speichert `RUN_FINISHED.outcome.interrupts` in `agent.pendingInterrupts`. Erstellen Sie jede Antwort anhand ihres `responseSchema` und senden Sie sie in einem neuen Lauf:

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent({ forwardedProps, resume: responses }, subscriber);
```

Dieser Payload gilt nur für Berechtigungsunterbrechungen, deren Schema `decision` verlangt. Fragen und Optionsauswahlen besitzen eigene Schemata.

Eine Wiederaufnahme muss die ursprüngliche `threadId`, eine neue `runId` und die `rosInvocationId` der unterbrochenen Ausführung verwenden. Sie muss alle aktuell ausstehenden Unterbrechungen in einer Anfrage beantworten und für `resolved` das jeweilige `responseSchema` erfüllen. Verwenden Sie `status: "cancelled"`, wenn der Benutzer nicht fortfahren möchte.

## Pipeline starten

Setzen Sie `runMode` auf `pipeline` und wählen Sie optional eine Pipeline:

```javascript
const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "pipeline",
    pipelineName: "selling",
    candidatePresentation: "rich",
  },
};
```

Clients sollten `STEP_*`, `TOOL_CALL_*`, `ACTIVITY_SNAPSHOT` und `CUSTOM` verarbeiten. Ein allgemeiner Client kann unbekannte iac-code-Custom-Ereignisse ignorieren und trotzdem alle Standardereignisse normal verarbeiten.

## Arbeitsbereich und temporäre Anmeldedaten

`cwd` wird nicht beim Serverstart festgelegt. Jede Anfrage muss einen absoluten Pfad unter einer durch `IAC_CODE_AGUI_ALLOWED_CWDS` oder `IACCODE_A2A_ALLOWED_CWDS` erlaubten Wurzel enthalten.

Der Aufrufer kann Modell, LLM-Schlüssel und temporäre Alibaba-Cloud-Anmeldedaten über `forwardedProps.iacCode` pro Anfrage übergeben. Der Adapter schreibt diese Geheimnisse nicht in seine Threadstatusdatei, sondern leitet sie nach den üblichen A2A-Regeln an den Ausführungskern weiter.

## Statusverzeichnis

Standardaufbau:

```text
<IAC_CODE_CONFIG_DIR>/agui/
  threads/
    <threadId>.json
```

Jeder Thread wird unabhängig geschrieben; beim Start werden historische Threads nicht durchsucht. Normale UUIDs bleiben lesbar. Unsichere IDs werden kodiert, besonders lange IDs verwenden einen Dateischlüssel fester Länge. Das JSON-Dokument speichert und prüft stets die ursprüngliche `threadId`.

Das Verzeichnis enthält nur Adapterzuordnungen, Unterbrechungen und Idempotenzstatus, aber keine Gesprächsinhalte oder Anmeldedaten. Bearbeiten Sie die JSON-Dateien nicht manuell.

## Nächste Schritte

- [AG-UI-Überblick](./overview.md)
- [Protokollreferenz](./protocol-reference.md)
