---
sidebar_position: 1
title: AG-UI-Protokoll
description: Architektur, Funktionen und Einsatzbereiche der AG-UI-Integration von iac-code.
---

# AG-UI-Protokoll

## Was ist AG-UI?

Das [Agent-User Interaction Protocol (AG-UI)](https://docs.ag-ui.com/concepts/architecture) ist ein Ereignisstrom-Protokoll zwischen Agenten und Benutzeranwendungen. Ein Client startet mit `RunAgentInput` einen Lauf und empfängt über HTTP Server-Sent Events (SSE) strukturierte Ereignisse für Text, Reasoning, Werkzeugaufrufe, Schritte, Status und Unterbrechungen.

AG-UI eignet sich für Webkonsolen, Chat-Clients, IDE-Erweiterungen und andere Anwendungen, die eine Agentenausführung in Echtzeit darstellen. Statt nur den Abschlusstext zu verarbeiten, kann ein Client Modellausgaben, Werkzeugargumente und -ergebnisse, Pipeline-Schritte sowie ausstehende Bestätigungen getrennt anzeigen.

## Architektur von iac-code

iac-code verwendet einen **A2A-Ausführungskern mit einem AG-UI-Protokolladapter**:

```text
AG-UI-Client
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Agentenschleife / Pipeline / LLM / Alibaba-Cloud-API
```

`iac-code a2a` ist der einzige Ausführungskern. Er verwaltet:

- normale Unterhaltungen und Pipeline-Ausführungen;
- iac-code-Sitzungen sowie A2A-Kontexte und -Tasks;
- Werkzeugberechtigungen, Fragen, Optionsauswahl und Wiederaufnahme;
- Lebenszyklus und Abbruch von Ausführungen;
- Aufrufe von LLMs und Alibaba-Cloud-APIs.

`iac-code agui` erzeugt keine zweite Agent-Runtime und führt Pipelines nicht selbst aus. Der Adapter:

- wandelt AG-UI-`RunAgentInput` in A2A-Anfragen um;
- bildet A2A-Ereignisse auf standardisierte AG-UI-Ereignisse ab;
- ordnet `threadId/runId` den A2A-Werten `contextId/taskId` zu;
- wandelt AG-UI-`resume[]` in eine A2A-Eingabewiederaufnahme um;
- persistiert Protokollzuordnungen und ausstehende Unterbrechungen;
- leitet Abbrüche an A2A weiter.

AG-UI und A2A besitzen daher keine getrennte Ausführungssemantik. Modellauswahl, Cloud-Anmeldedaten, Berechtigungsregeln und Pipeline-Verhalten werden von derselben A2A-Runtime umgesetzt.

## Standardprotokoll und iac-code-Erweiterungen

Der externe Strom verwendet standardisierte AG-UI-Ereignisse:

- `RUN_STARTED`, `RUN_FINISHED` und `RUN_ERROR`;
- `TEXT_MESSAGE_*`;
- `REASONING_*`;
- `TOOL_CALL_*`;
- `STEP_STARTED` und `STEP_FINISHED`;
- `ACTIVITY_SNAPSHOT`.

Nur nützliche Pipeline-Informationen ohne standardisierte Entsprechung erscheinen als namensraumgebundene `CUSTOM`-Ereignisse. Ein allgemeiner AG-UI-Client darf sie ignorieren, ohne Text, Werkzeugaufrufe, Unterbrechungen oder den Laufzyklus zu beeinträchtigen.

Anfragen bleiben standardisierte `RunAgentInput`-Umschläge. iac-code nutzt `forwardedProps` für Arbeitsbereich, Laufmodus und weitere erforderliche Laufzeitdaten:

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "anfrage-identitaet",
      "cwd": "/absoluter/arbeitsbereich/pfad",
      "runMode": "normal"
    }
  }
}
```

Ein allgemeiner Client kann die standardisierten Ereignisse von iac-code direkt verarbeiten. Bei einem direkten Aufruf von `iac-code agui` muss er jedoch Laufzeitfelder wie `cwd` unter `forwardedProps.iacCode` bereitstellen.

## Unterstützte Interaktionen

### Normale Unterhaltungen mit mehreren Dialogrunden

Verwenden Sie für die Unterhaltung dieselbe `threadId` und für jede Benutzerrunde eine neue `runId`. Der Adapter bindet den Thread an eine iac-code-Sitzung. Nach Abschluss einer Runde startet die nächste Nachricht eine neue HTTP/SSE-Anfrage; sie setzt niemals eine bereits abgeschlossene SSE-Antwort fort.

### Pipeline

Setzen Sie `forwardedProps.iacCode.runMode` auf `pipeline`. Der A2A-Kern führt die Pipeline weiterhin aus. Hauptschritte werden zu `STEP_*`-Ereignissen; Text, Reasoning und Werkzeuge verwenden ihre jeweiligen Standardereignisse. Kandidateninformationen, Stack- und Bereinigungsfortschritt ohne Standardentsprechung werden über `iac-code.pipeline.v1` gesendet.

Parallele Sub-Pipelines verwenden getrennte Nachrichten- und Schrittidentitäten, sodass Texte mehrerer Agentenschleifen nicht zusammengeführt werden.

### Unterbrechung und Wiederaufnahme

Wenn eine Berechtigung, Frage oder Auswahl eine Benutzereingabe benötigt, endet der aktuelle Lauf mit:

```json
{
  "type": "RUN_FINISHED",
  "outcome": {
    "type": "interrupt",
    "interrupts": []
  }
}
```

Die Unterbrechung wird persistiert, bevor sie für den Client sichtbar wird. Anschließend startet der Client eine neue Anfrage mit derselben `threadId`, einer neuen `runId` und `resume[]`. Der Wiederaufnahmestrom gehört zu dieser neuen Anfrage und verbindet sich nicht erneut mit dem alten Strom.

### Adapterstatus

Der Adapter speichert Protokollzuordnungen, Idempotenzdaten und ausstehende Unterbrechungen in einer Datei pro Thread. Das Verzeichnis enthält weder Gesprächstexte noch LLM-Schlüssel oder Cloud-Anmeldedaten und ist kein Exportverzeichnis für Unterhaltungen.

## Wann sollte AG-UI verwendet werden?

| Anforderung | Empfohlener Modus |
|-------------|-------------------|
| Chat-Oberfläche mit Live-Text, Reasoning, Werkzeugen und Schritten | **AG-UI** |
| Berechtigungen, Fragen und Optionsauswahl in einer Oberfläche | **AG-UI** |
| Direkter Aufruf von iac-code durch einen Agenten oder Orchestrator | **A2A** |
| IDE-/Editor-Integration mit ACP-Sitzungen und Terminalfunktionen | **ACP** |
| Manuelle Bedienung von iac-code | **Interaktive REPL oder Web/Desktop** |

AG-UI und A2A können gleichzeitig laufen. Sie stellen getrennte HTTP-Endpunkte bereit, verwenden aber dieselbe Ausführungsimplementierung von iac-code.

## Aktuelle Grenzen

- Der AG-UI-Transport verwendet HTTP POST und SSE.
- Der vorgelagerte A2A-Dienst muss eine Loopback-Adresse verwenden; beliebige entfernte A2A-URLs werden abgelehnt.
- `cwd` ist pro Anfrage erforderlich und muss unterhalb eines erlaubten Arbeitsbereichsstamms liegen.
- Vom Client definierte `tools` werden derzeit nicht akzeptiert; iac-code verwaltet den Werkzeugsatz.
- Benutzernachrichten unterstützen Text und eingebettete Base64-Bilder, aber keine entfernten Medien-URLs.
- Trennt sich ein Client vor einer Unterbrechung von einem aktiven SSE-Lauf, bricht der Adapter den passenden A2A-Task ab.
- Der SSE-Strom sendet alle 15 Sekunden einen Heartbeat-Kommentar. Konforme Clients ignorieren ihn.

## Nächste Schritte

- [Erste Schritte](./getting-started.md) — Installation, Start und Verbindung des ersten Clients.
- [Protokollreferenz](./protocol-reference.md) — Anfragefelder, Ereignisse, Unterbrechung/Wiederaufnahme, Persistenz und Fehler.
