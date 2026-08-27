---
sidebar_position: 3
title: Protokollreferenz
description: Referenz zu AG-UI-Anfragen, Ereignissen, Unterbrechungen, Wiederaufnahme, Abbruch und Persistenz in iac-code.
---

# AG-UI-Protokollreferenz

Diese Seite beschreibt die von `iac-code agui` bereitgestellte HTTP/SSE-Schnittstelle und die iac-code-Erweiterungsfelder in standardisierten AG-UI-Umschlägen. Lesen Sie zuerst den [Überblick](./overview.md) und die [Ersten Schritte](./getting-started.md).

## HTTP-Endpunkte

| Methode und Pfad | Zweck |
|------------------|-------|
| `GET /health` | Dienststatus und Protokollversionen |
| `POST /` | `RunAgentInput` senden und SSE-Ereignisstrom empfangen |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | Namensraumgebundene Abbrucherweiterung |

Der Body von `POST /` muss JSON verwenden; Clients sollten SSE anfordern:

```http
Content-Type: application/json
Accept: text/event-stream
```

Bei konfiguriertem `IAC_CODE_AGUI_AUTH_TOKEN` ist außerdem erforderlich:

```http
Authorization: Bearer <token>
```

Der Standardheader `Accept-Language` dient als Rückfall für Fehlermeldungen. `forwardedProps.iacCode.preferredLanguage` hat Vorrang und wird an die A2A-Runtime weitergeleitet.

## RunAgentInput

Minimales Beispiel für einen normalen Lauf:

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [
    {"id": "message-1", "role": "user", "content": "Erstelle eine VPC-Vorlage"}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1",
      "runMode": "normal"
    }
  }
}
```

### Standardfelder

| Feld | Anforderung | Verhalten von iac-code |
|------|-------------|-------------------------|
| `threadId` | Erforderliche, nicht leere Zeichenfolge | Stabile Gesprächsidentität für einen A2A-Kontext und eine iac-code-Sitzung |
| `runId` | Erforderliche, nicht leere Zeichenfolge | Ein HTTP/SSE-Lauf; darf im Thread nicht wiederverwendet werden |
| `parentRunId` | Optional | Wird nach `RUN_STARTED` kopiert |
| `state` | Erforderlich | Bleibt im Standardumschlag, wird aber nicht als iac-code-Laufzeitstatus genutzt |
| `messages` | Erforderlich | Neuer Lauf verwendet die letzte Benutzernachricht; eine Wiederaufnahme benötigt keine neue |
| `tools` | Erforderlich und leer | Clientdefinierte Werkzeuge werden nicht unterstützt |
| `context` | Erforderlich | Bleibt im Umschlag, wird derzeit nicht in Prompt-Kontext umgewandelt |
| `forwardedProps` | Erforderlich | Muss die Erweiterung `iacCode` enthalten |
| `resume` | Bei Wiederaufnahme | Eine Antwort für jede ausstehende Unterbrechung |

Benutzernachrichten unterstützen Zeichenfolgen sowie `text`- und `image`-Teile mit eingebetteten Base64-`data`-Quellen. Entfernte Bild-URLs, Audio, Video, Dokumente und allgemeine Binärteile werden nicht unterstützt. Ein dekodiertes Bild ist auf 8 MiB, alle Bilder zusammen auf 10 MiB und die gesamte HTTP-Anfrage auf 12 MiB begrenzt.

## `forwardedProps.iacCode`

Das Schema ist strikt; unbekannte Felder werden abgelehnt.

| Feld | Typ | Erforderlich | Bedeutung |
|------|-----|--------------|-----------|
| `schemaVersion` | `1` | Ja | Version der iac-code-Erweiterung |
| `rosInvocationId` | Zeichenfolge | Ja | Aufruferidentität der aktuellen Ausführung, maximal 256 Zeichen |
| `cwd` | Zeichenfolge | Ja | Absoluter Arbeitsbereichspfad |
| `model` | Zeichenfolge | Nein | Modellüberschreibung pro Anfrage |
| `llmApiKey` | Zeichenfolge | Nein | LLM-Anbieterschlüssel pro Anfrage |
| `thinking.enabled` | boolesch | Nein | Reasoning-Ausgabe anfordern |
| `thinking.effort` | Zeichenfolge | Nein | Anbieterspezifischer Reasoning-Aufwand |
| `thinking.budget` | positive Ganzzahl | Nein | Anbieterspezifisches Reasoning-Budget |
| `userId` | Zeichenfolge | Nein | Identität für Telemetrie und Aufruferbindung |
| `channel` | Zeichenfolge | Nein | Metadaten des Aufruferkanals |
| `preferredLanguage` | Zeichenfolge | Nein | Anfragelokale Anzeigesprache, etwa `de` |
| `candidatePresentation` | `standard` oder `rich` | Nein | Darstellung von Pipeline-Kandidaten |
| `runMode` | `normal` oder `pipeline` | Nein | Ausführungsmodus, andernfalls durch A2A gewählt |
| `pipelineName` | Zeichenfolge | Nein | Pipeline-Name, zum Beispiel `selling` |
| `cleanupOnly` | boolesch | Nein | Nur Pipeline-Bereinigung anfordern |
| `alibabaCloud.accessKeyId` | Zeichenfolge | Nein | Anfragebezogene AccessKey-ID |
| `alibabaCloud.accessKeySecret` | Zeichenfolge | Nein | Anfragebezogenes AccessKey-Secret |
| `alibabaCloud.securityToken` | Zeichenfolge | Nein | Anfragebezogenes STS-Token |
| `alibabaCloud.regionId` | Zeichenfolge | Nein | Anfragebezogene Standardregion |

Der erste Lauf und seine Wiederaufnahmen müssen dieselbe `rosInvocationId` behalten. Eine spätere normale Runde darf einen neuen Wert verwenden. Beim Abbruch ist der Wert der aktuellen Ausführung erforderlich.

Eine `threadId` wird an `cwd` und `userId` der ersten Anfrage gebunden; spätere Anfragen können denselben Thread nicht in einen anderen Arbeitsbereich oder zu einem anderen Aufrufer verschieben.

## SSE und Heartbeat

Jedes AG-UI-Ereignis wird als SSE-`data:`-Datensatz gesendet. Nach 15 Sekunden ohne Ereignis sendet der Server:

```text
: heartbeat
```

Dies ist ein SSE-Kommentar, kein AG-UI-`CUSTOM`-Ereignis. Konforme Clients ignorieren ihn; die HTTP-Verbindung bleibt dadurch aktiv.

## Standardereignis-Zuordnung

| A2A/iac-code-Signal | AG-UI-Ausgabe |
|---------------------|---------------|
| Anfrage angenommen | `RUN_STARTED` |
| Agententext | `TEXT_MESSAGE_START/CONTENT/END` |
| Rohes Reasoning | `REASONING_START`, `REASONING_MESSAGE_*`, `REASONING_END` |
| Werkzeugstart und Argumente | `TOOL_CALL_START/ARGS/END` |
| Werkzeugergebnis | `TOOL_CALL_RESULT` |
| Pipeline-Schrittzyklus | `STEP_STARTED/STEP_FINISHED` |
| Pipeline-Wiederaufnahmeabbild | `ACTIVITY_SNAPSHOT` |
| Normaler Abschluss | `RUN_FINISHED` mit `outcome.type = "success"` |
| Benutzereingabe erforderlich | `RUN_FINISHED` mit `outcome.type = "interrupt"` |
| Adapter- oder A2A-Fehler | `RUN_ERROR` |

`RUN_FINISHED` beendet einen AG-UI-Lauf, nicht zwingend die gesamte Pipeline. Eine mehrfach unterbrochene Pipeline besitzt mehrere Läufe mit jeweils eigenem `RUN_STARTED` und `RUN_FINISHED`. Der fachliche Pipeline-Abschluss wird durch `pipeline_completed`, `pipeline_error` und verwandte Ereignisse dargestellt.

Für ausgeglichene AG-UI-Spans schließt der Adapter vor einer Unterbrechung offene Nachrichten-, Reasoning-, Werkzeug- und Schritt-Spans. Der Wiederaufnahmelauf öffnet weiterhin aktive, dauerhafte Pipeline-Schritte erneut. In Rohereignissen kann derselbe fachliche Schritt daher in einem Lauf geschlossen und im nächsten wieder geöffnet werden; die Ausführung läuft nicht rückwärts.

## Benutzerdefinierte iac-code-Ereignisse

### `iac-code.session.v1`

Stellt die aktuelle Adapter-A2A-Zuordnung bereit, einschließlich `threadId`, `aguiRunId`, `executionId`, `contextId`, `taskId`, `rosInvocationId` und `sessionId`. Verwenden Sie `executionId` für die Abbrucherweiterung. Allgemeine Clients dürfen dieses Ereignis ignorieren.

### `iac-code.artifact.v1`

Enthält eine strukturierte Projektion eines A2A-Task-Artefakts für optionale Vorschau, Download oder Diagnose.

### `iac-code.tool-progress.v1`

Enthält Werkzeug-Zwischenfortschritt ohne Standardentsprechung. Start, Argumente und Endergebnis bleiben standardisierte `TOOL_CALL_*`-Ereignisse und werden hier nicht dupliziert.

### `iac-code.pipeline.v1`

Nur nützliche Pipeline-Informationen ohne vollständige Standardentsprechung werden gesendet. Aktuelle `eventType`-Werte:

- Pipeline: `pipeline_started`, `pipeline_resumed`, `pipeline_completed`, `pipeline_error`, `pipeline_warning`, `backup_blocked`;
- Kandidaten: `candidate_started`, `candidate_completed`, `candidate_failed`, `candidate_interrupted`, `candidate_restart_requested`, `candidate_selected`, `candidate_detail_shown`, `candidate_step_failed`;
- Sub-Pipelines und Schrittfehler: `sub_pipeline_started`, `sub_pipeline_completed`, `sub_step_failed`, `step_failed`;
- Stacks und Bereinigung: `stack_progress`, `stack_instances_progress`, `stack_current_changed`, `cleanup_started`, `cleanup_progress`, `cleanup_completed`, `cleanup_failed`;
- Rollback: `rollback_triggered`, `rollback_completed`;
- Kontext: `context_compaction_started`, `context_compacted`, `context_compaction_failed`, `fields_marked_stale`;
- Darstellung und Werkzeuge: `diagram_shown`, `mcp_status`, `tool_progress`.

Signale mit Standardzuordnung werden nicht als `CUSTOM` dupliziert: `text_delta` wird zu `TEXT_MESSAGE_*`, `thinking_delta` zu `REASONING_*`, `tool_started/tool_result` zu `TOOL_CALL_*`, `usage` zu `RUN_FINISHED.usage` und Schrittzyklen zu `STEP_*`.

Clients sollten wiederholte Pipeline-Ereignisse mit `(name, value.eventId)` oder der Pipeline-Sequenz deduplizieren und unbekannte namensraumgebundene Ereignisse tolerieren.

## Unterbrechung

Ein Lauf mit erforderlicher Eingabe endet mit `RUN_FINISHED.outcome.type = "interrupt"`. Jede Unterbrechung enthält:

- `id` und `reason`;
- eine benutzerorientierte `message`;
- eine optionale `toolCallId`;
- ein JSON-`responseSchema`;
- `expiresAt`;
- Metadaten wie `title`, `purpose`, `safeSummary`, `options` und `toolName`.

Für eine Berechtigungsanfrage akzeptiert das Schema normalerweise:

```json
{"decision": "allow_once"}
```

oder:

```json
{"decision": "deny"}
```

Stellen Sie `message`, `responseSchema` und beschreibende Metadaten dar, statt die Oberfläche nur aus `reason` abzuleiten. Fragen und Optionsauswahlen können andere Schemata verwenden.

## Wiederaufnahme

Eine Wiederaufnahme ist ein neues `POST /` mit derselben `threadId`, einer neuen `runId`, derselben `rosInvocationId` und einem Eintrag pro ausstehender Unterbrechung:

```json
{
  "resume": [
    {
      "interruptId": "permission-1",
      "status": "resolved",
      "payload": {"decision": "allow_once"}
    }
  ]
}
```

Regeln:

- jede ausstehende Unterbrechung genau einmal beantworten;
- doppelte und unbekannte IDs werden abgelehnt;
- `resolved` erfordert einen schema-konformen Payload;
- `cancelled` beendet die Unterbrechung und entspricht bei Berechtigungen `deny`;
- dauerhafter Pending-Status wird erst entfernt, nachdem A2A die Antwort akzeptiert hat;
- Schemafehler erzeugen `RUN_ERROR`, die Unterbrechung bleibt erneut beantwortbar;
- eine wiederholte, bereits akzeptierte Antwort führt das Werkzeug nicht erneut aus.

Vor der Wiederaufnahme kann der Adapter A2A zur Wiederherstellung der iac-code-Sitzung auffordern, Task- und Kontextidentität prüfen und fehlende Pipeline-Ereignisse nachholen.

## Dialogrunden und Identitäten

```text
threadId (stabiles Gespräch)
  ├─ runId-1 (Benutzerrunde)
  ├─ runId-2 (Wiederaufnahme)
  ├─ runId-3 (weitere Wiederaufnahme)
  └─ runId-4 (nächste normale Nachricht)
```

Jede HTTP/SSE-Anfrage verwendet eine eindeutige `runId`. Eine Wiederaufnahme ist ein neuer Lauf. Nach einer normalen Runde erzeugt die nächste Nachricht eine neue Ausführung und verwendet die iac-code-Sitzung des Threads weiter. Idempotenz gilt im Bereich `(threadId, runId)`.

## Abbrucherweiterung

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{"threadId": "thread-1", "rosInvocationId": "invocation-1"}
```

Mögliche Ergebnisse sind `cancelled`, `already_terminal` oder HTTP `404` mit `EXECUTION_NOT_FOUND`. Der Abbruch entfernt ausstehende Unterbrechungen und ändert keine standardisierten AG-UI-Ereignisformate.

## Persistenz und Wiederherstellung

Standardverzeichnis:

```text
<config-dir>/agui/threads/<thread-key>.json
```

Jede Datei enthält Thread-/Kontext-/Arbeitsbereichsbindung, Sitzungs-, Task- und Ausführungsidentität, Pipeline-Wiederaufnahmepositionen, ausstehende Unterbrechungen sowie Idempotenzdaten. Der Adapter lädt einen angefragten Thread verzögert und ersetzt atomar nur dessen kleine Datei.

LLM-Schlüssel, AccessKey-Secrets und STS-Tokens werden nie gespeichert. Das Verzeichnis enthält Adapterzuordnungen, keine Gespräche oder Ausführungsartefakte. A2A verwaltet seine eigene Sitzungs- und Taskpersistenz; siehe [A2A-Dokumentation](../a2a/overview.md).

Eine abgelaufene Unterbrechung wird beim nächsten Zugriff abgelehnt, ihr Pending-Status gelöscht und der passende A2A-Task nach Möglichkeit abgebrochen.

## Verbindungsabbrüche

- Ein Lauf, der sicher mit einer Unterbrechung endete, hängt nicht mehr von seiner SSE-Verbindung ab.
- Eine Wiederaufnahme erzeugt eine neue SSE-Verbindung.
- Bei Trennung eines gewöhnlichen aktiven Laufs bricht der Adapter den A2A-Task ab.
- Eine Trennung nach einer Unterbrechung löscht deren persistenten Wiederaufnahmestatus nicht.

## Fehler

Fehler vor Beginn von SSE verwenden einen HTTP-JSON-Umschlag. Fehler während der Ausführung verwenden standardisierte `RUN_ERROR`-Ereignisse.

| Code | Bedeutung |
|------|-----------|
| `INVALID_INPUT` | Ungültiger Umschlag, Erweiterungswert, Nachrichteninhalt oder Arbeitsbereich |
| `DUPLICATE_RUN_ID` | Derselbe Anfrage-Digest verwendet eine bestehende Run-ID |
| `RUN_ID_CONFLICT` | Eine andere Anfrage verwendet eine bestehende Run-ID erneut |
| `THREAD_BUSY` | Der Thread besitzt bereits einen aktiven Lauf |
| `THREAD_BINDING_CONFLICT` | Arbeitsbereich oder Aufrufer widerspricht der Threadbindung |
| `RESUME_REQUIRED` | Der Thread wartet auf Unterbrechungsantworten |
| `INCOMPLETE_RESUME` | Fehlende Unterbrechungen oder doppelte IDs |
| `UNKNOWN_INTERRUPT` | Unbekannte Unterbrechung in der Wiederaufnahme |
| `RESUME_PAYLOAD_INVALID` | Fehlender Payload oder Schemaverstoß |
| `RESUME_ALREADY_APPLIED` | Antwort wurde bereits angewendet oder steht im Konflikt |
| `EXECUTION_EXPIRED` | Unterbrechung ist abgelaufen |
| `EXECUTION_LOST` | Adapter, A2A-Task oder iac-code-Sitzung konnte nicht wiederhergestellt werden |
| `STATE_PERSISTENCE_FAILED` | Wiederherstellungskritischer Status konnte nicht gespeichert werden |
| `A2A_UNAVAILABLE` | Lokaler A2A-Ausführungsdienst ist nicht verfügbar |
| `A2A_PROTOCOL_ERROR` | Task-/Kontext-/Sitzungsidentität widerspricht der Zuordnung |
| `A2A_EXECUTION_FAILED` | A2A-Task ist fehlgeschlagen |
| `CANCELLED` | Ausführung wurde abgebrochen |

Wiederherstellungskritische Schreibfehler werden sicher behandelt. Der Adapter meldet keinen wiederherstellbaren Task, keine Sitzung und keine Unterbrechung, bevor die Zuordnung dauerhaft gespeichert ist, und bricht nötigenfalls den passenden A2A-Task ab.
