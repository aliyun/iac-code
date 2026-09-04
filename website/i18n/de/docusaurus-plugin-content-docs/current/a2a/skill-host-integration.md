---
sidebar_position: 3
title: Referenz zur Host-Integration des IaC Code Skills
description: Integrieren Sie die IaC-Code-Skill-Bridge in einen Skill-faehigen Host-Agenten.
---

# Referenz zur Host-Integration des IaC Code Skills

Diese Referenz richtet sich an Entwickler von Agenten und Skill-Verteilungssystemen. Endbenutzer lesen
[IaC Code Skill installieren und verwenden](./skill-integration.md).

## Integrationsmodell und Konfiguration

Das Paket enthaelt `SKILL.md` und die nur auf der Standardbibliothek basierende Bridge `scripts/iac_code.py`. Fuehren Sie
sie mit CPython 3.8 bis 3.14 aus. stdout ist das stabile JSON-Ergebnis, stderr enthaelt Diagnose und Fortschritt. Bewahren
Sie `jobId`, `contextId`, cursor und Korrelationsfelder auf. Bei Fehlern darf nicht auf eine andere Runtime oder direkte
Cloud-API-Aufrufe ausgewichen werden.

Ein Verteiler kann neben `SKILL.md` diese `config.json` ablegen:

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

Die Bridge setzt `skill/` vor `channel`. Standard fuer `pipelineName` ist `selling_solution_first`; `selling` dient nur
einem explizit benoetigten Legacy-Ablauf. `null` bedeutet unbegrenztes Warten. Unbekannte oder ungueltige Werte werden
abgewiesen. Diese Installationsrichtlinie darf nicht aus Benutzerwuenschen abgeleitet, ausgegeben oder waehrend einer
Aufgabe veraendert werden.

## Job starten und verfolgen

Schreiben Sie die vollstaendige Anfrage in eine UTF-8-Datei im Workspace und verwenden Sie einen absoluten Pfad:

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

Verwenden Sie standardmaessig `normal`, `pipeline` nur fuer Vergleich, Bestaetigung und Bereitstellung. Moegliche
Sprachen sind `en`, `zh`, `es`, `fr`, `de`, `ja`, `pt` und `auto`; behalten Sie danach `preferredLanguage` bei.
`llm_not_configured` stoppt vor der Job-Erstellung, `cloud_credentials_not_configured` meldet fehlende Zugangsdaten in
Pipeline.

`--follow` kehrt an der naechsten Darstellungs- oder Interaktionsgrenze, bei `turn_completed` oder einem terminalen
Pipeline-Status zurueck. Bei `boundaryReached: true` zeigen Sie alle `userUpdates` und folgen demselben Job:

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

`boundaryReached` ist kein Abschluss. `presentationRequired` verlangt eine sichtbare Ausgabe vor dem naechsten Aufruf.
Im Normalmodus sind `finalText` und `artifacts` bei `turn_completed` massgeblich; bei einer terminalen Pipeline
`pipelineResult` und `artifacts`. Melden Sie Fehler der Bereinigung. Nur fuer Diagnose oder Wiederaufnahme:

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

Bei `state: input-required` ohne `inputRequired` melden Sie den letzten Text oder Fehler und lassen den Job unveraendert.

## Benutzereingaben behandeln

Jedes `inputRequired` ist eine harte Interaktionsgrenze. Zeigen Sie es in der nativen Host-Oberflaeche und warten Sie
auf eine ausdrueckliche Antwort. Leiten Sie keine Standardantwort ab. Bewahren Sie `kind`, `inputId`, `requestTaskId`,
`contextId` und gegebenenfalls `toolUseId` auf.

| `kind` | Anzuzeigende Informationen | Antwort |
|---|---|---|
| `permission` | Zweck, Wirkung, Ziel, Nur-Lesen, Bereitstellungs- und Sicherheitszusammenfassung, Aktionen | `allow_once` / `deny` |
| `ask_user_question` | Frage, Optionen und erlaubter Freitext | Antwort |
| `candidate_selection` | Alle Zusammenfassungen, Mermaid-Diagramme, Monatssumme und Positionen | ID oder Nummer |
| `deployment_confirmation` | Loesung, URL, Preis, effektive Werte, Ueberschreibungen, Preview, Aktionen | `confirm` / `adjust` / `reselect` / `cancel` |

Schreiben Sie die korrelierte Antwort in eine neue UTF-8-JSON-Datei und setzen Sie denselben Job fort:

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

Lassen Sie `parameterOverrides` ohne Anpassung weg. Leiten Sie die Bestaetigung nicht aus dem urspruenglichen Wunsch
oder einer Host-Freigabe ab.

## Fortsetzen, abbrechen und wiederaufnehmen

Nach einem normalen Turn oder dem Wechsel einer abgeschlossenen Pipeline in den Normalmodus setzen Sie den Job fort:

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

Behalten Sie `jobId` und `contextId`; eine neue `taskId` ist normal. So koennen auch Freigabewartezeiten und
Host-Unterbrechungen wiederaufgenommen werden. Vollstaendiger Abbruch:

```text
python3 scripts/iac_code.py cancel --job-id <job-id>
```

Dies unterscheidet sich von der Ablehnung einer einzelnen Freigabe.

## Fehler und Runtime

Ein Fehler vor Job-Erstellung ist fuer den Aufruf massgeblich. Zeigen Sie bei `incompatible_host` die
Kompatibilitaetsdaten und stoppen Sie, ohne pip, eine andere Runtime oder direkte APIs zu verwenden. Die Runtime liegt
unter `<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`. Aufbau und Integritaet werden durch
`skill-runtime/skill-package-contract.json` und das Release-Manifest festgelegt. Bereinigung erfolgt nur auf
ausdruecklichen Wunsch; aktuelle und aktive Pakete sind geschuetzt.

Die Runtime verwendet einen zufaelligen `127.0.0.1`-Port und einen prozessspezifischen Bearer token. Legen Sie token,
lokalen Zustand, Zugangsdaten, Umgebungswerte und rohe Tool-Ein-/Ausgaben nicht offen.

## Weitere Dokumentation

- [Ueberblick ueber offizielle IaC Code Skills](./skill-overview.md)
- [IaC Code Skill installieren und verwenden](./skill-integration.md)
- [A2A-Uebersicht](./overview.md)
- [A2A-Referenz](./protocol-reference.md)
