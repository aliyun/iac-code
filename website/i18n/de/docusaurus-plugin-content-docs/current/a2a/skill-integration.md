---
sidebar_position: 7
title: Skill-Integration
description: Externe Agenten steuern iac-code ueber das paketierte iac-code-Skill und die Skill-Runtime an.
---

# Skill-Integration

iac-code liefert ein paketiertes Skill fuer externe Agenten. Ein externer Agent (ein Planner-Agent oder eine Agentenplattform) installiert weder das Python-Paket von iac-code noch ruft er Headless-Befehle auf; er steuert eine lokale authentifizierte A2A-Runtime ueber ein Bridge-Skript mit reiner Standardbibliothek an, um Alibaba-Cloud-Infrastrukturaufgaben wie ROS-/Terraform-Vorlagenerzeugung, Kostenschaetzung, Ressourcenauswahl und Deployment auszufuehren.

## Bestandteile

| Bestandteil | Ort | Beschreibung |
|---|---|---|
| Skill-Paket | `skills/iac-code/` | `SKILL.md`-Anleitung, Agenten-Metadaten in `agents/` und `scripts/iac_code.py`, das Bridge-Skript |
| Skill-Runtime | Pro Plattform veroeffentlicht | Native CPython-3.12-Executable mit eingebettetem iac-code-A2A-Server |
| Verteilungsvertraege | `skill-runtime/skill-package-contract.json`, `skill-runtime/publisher-contract.json` | Format- und Verifizierungsbeschraenkungen fuer Skill-Pakete und Herausgeber |

Das Bridge-Skript ist vollstaendig mit der Python-Standardbibliothek geschrieben und bleibt mit Python 3.8+ kompatibel; die CI kompiliert und startet es ueber die gesamte 3.8–3.14-Matrix als Smoke-Run. Fuegen Sie der Bridge keine Drittanbieter-Abhaengigkeiten und keine Syntax neuerer Versionen hinzu.

## Runtime-Beschaffung und Cache

Beim ersten Gebrauch liest die Bridge das Manifest, laedt das Artefakt fuer die aktuelle Plattform herunter, verifiziert Groesse und SHA-256, installiert es und legt es unter `<IAC_CODE_CONFIG_DIR oder ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` in den Cache.

- `python3 scripts/iac_code.py ensure-runtime` - bereitet die Runtime vor; eine gecachte Runtime wird wiederverwendet.
- `python3 scripts/iac_code.py cache list` - zeigt installierte Runtimes und Kandidatenpakete an.
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` - bereinigt Runtime-Caches oder Kandidatenpakete; erfordert explizites `--confirm`.

## Konfigurations-Preflight

Vor dem Anlegen eines Jobs fuehrt `start` ueber die Runtime einen Konfigurationsbereitschafts-Check aus. Der Preflight liest keine Geheimwerte; er meldet nur die Bereitschaft:

| Situation | Ergebnis |
|---|---|
| LLM-Provider oder API-Key unvollstaendig | Liefert `llm_not_configured` und verweigert das Anlegen des Jobs |
| Selling-Pipeline mit unvollstaendigen Alibaba-Cloud-Zugangsdaten | Liefert `cloud_credentials_not_configured` und verweigert das Anlegen des Jobs |
| Normalmodus mit unvollstaendigen Alibaba-Cloud-Zugangsdaten | Kann fuer Arbeiten ohne Cloud-API-Aufrufe fortgesetzt werden, mit Preflight-Warnung |

## Befehlsreferenz

| Befehl | Zweck |
|---|---|
| `start` | Job anlegen: `--mode normal|pipeline`, `--pipeline-name`, `--cwd` absoluter Workspace, `--prompt-file` UTF-8-Promptdatei, `--language auto|en|zh|es|fr|de|ja|pt`, optional `--follow` |
| `follow` | Konsumiert den Ereignisstrom bis zur naechsten Interaktionsgrenze: `--job-id`, `--cursor`, `--wait-seconds` (Standard 60 s, maximal 120 s) |
| `continue` | Setzt eine Normalmodus-Konversation im selben Job fort: `--job-id`, `--prompt-file`, optional `--follow` |
| `respond` | Beantwortet eine ausstehende Eingabe, siehe [Benutzereingabe](#input-required) |
| `poll` | Einmaliges Pollen nur fuer Diagnose und Wiederherstellung; nicht als `follow`-Ersatz verwenden |
| `cancel` | Bricht den Job ab |
| `ensure-runtime` / `cache list` / `cache clean` | Runtime- und Cache-Verwaltung |

`start --follow` und `follow` schreiben Step-Grenzen und niederfrequente Heartbeats nach stderr; stdout liefert genau ein begrenztes JSON-Ergebnis.

## Interaktionsgrenzen {#boundaries}

`--follow` konsumiert den Ereignisstrom bis zur naechsten Step-Grenze, Berechtigungsanfrage, Benutzerfrage, Kandidatenauswahl, `turn_completed` oder zum Endzustand. Ein Grenzergebnis traegt:

- `boundaryReached: true` - eine Grenze wurde erreicht; das bedeutet **nicht**, dass der Job abgeschlossen ist;
- `presentationRequired: true` und `userUpdates` - lokalisierte, unmittelbar anzeigbare Zeichenketten;
- den `cursor` zum Fortsetzen.

Der externe Agent muss zunaechst jede empfangene `userUpdates`-Zeichenkette in einer fuer den Benutzer sichtbaren Antwort praesentieren und danach sofort erneut `follow` mit dem zurueckgegebenen `cursor` aufrufen. Beantworten Sie die Infrastrukturaufgabe nicht parallel und stellen Sie keine unzusammenhaengenden Fragen, waehrend ein Follow laeuft.

## Benutzereingabe {#input-required}

Ein Ergebnis enthaelt `inputRequired`, wenn Benutzereingabe noetig ist. Es gibt drei Arten:

- `permission` - eine Tool- oder Deployment-Berechtigungsanfrage. Der Umschlag enthaelt `inputId`, `toolUseId`, Titel, Zweck, Wirkung, Ziel, Nur-Lese-Markierung, `safeSummary` und bei Deploy-Anfragen `deploymentSummary`. Der externe Agent sollte gemaess seiner eigenen Berechtigungsrichtlinie entscheiden: Wuerde dieselbe Operation bei direkter Ausfuehrung ohne Rueckfrage fortgesetzt, antworten Sie `allow_once`; wuerde die Richtlinie sie ablehnen, antworten Sie `deny`; andernfalls fragen Sie den Benutzer. Ablehnungen von iac-code selbst duerfen nicht uebersteuert werden.
- `ask_user_question` - eine Auswahl- oder Freitextfrage. Praesentieren Sie Prompt und Optionen unveraendert; Freitext wird nur akzeptiert, wenn `allowFreeText` `true` ist.
- `candidate_selection` - Planauswahl der Pipeline. Praesentieren Sie zuerst Zusammenfassung, Architekturdiagramm (Mermaid), monatliche Gesamtkosten und Kostenpositionen jedes Kandidaten und liefern Sie dann den gewaehlten Kandidaten zurueck. Ersetzen Sie die gelieferten Preise nie durch grobe Schaetzungen.

`respond` hat zwei Formen:

```bash
# Inline-Entscheidung fuer Berechtigungen
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# Fragen und Kandidatenauswahlen verwenden eine Antwortdatei
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

Eine Antwort muss alle Korrelationsfelder der ausstehenden Eingabe unveraendert lassen und bleibt an die aktuellen `kind`, `inputId`, `requestTaskId` und `contextId` gebunden; verwenden Sie nie eine Antwort aus einer anderen Anfrage erneut und interpretieren Sie eine Ressourcenauswahl nie als Deployment-Bestaetigung um.

## Sprachsteuerung

`start --language` setzt die bevorzugte Sprache des Jobs (bei Unbekanntheit `auto`). Jedes Ergebnis dieses Jobs wiederholt `preferredLanguage`; behandeln Sie es als dauerhaften Steuerungszustand: Fortschritt, Fragen, Berechtigungs-Prompts, Kandidatenplaene und Endergebnisse werden in dieser Sprache praesentiert, waehrend Protokollfeldnamen, Aufzaehlungen, IDs und Befehle unveraendert bleiben. Wenn massgeblicher Text bereits diese Sprache verwendet, praesentieren Sie ihn direkt oder fassen Sie ihn in derselben Sprache zusammen; uebersetzen Sie chinesische, fuer Benutzer sichtbare Inhalte nie ins Englische.

## Verhaeltnis zum A2A-Protokoll

Die Bridge spricht mit der lokalen Runtime ueber HTTP A2A JSON-RPC; Task-Zustaende, Artefakte und Berechtigungsinteraktionen nutzen das A2A-Protokoll von iac-code wieder:

- Sideband-Antworten fuer Berechtigungen verwenden das `schemaVersion 1`-Nachrichtenformat; Felder und Einschraenkungen stehen in der [Protokollreferenz](./protocol-reference.md).
- Im Pipeline-Modus liefert `candidatePresentation: rich-v1` strukturierte Kandidatenpraesentations-Payloads.
- Job-Ergebniszustaende entsprechen A2A-Task-Zustaenden: `turn_completed` beendet einen normalen Turn; Pipeline-Endzustaende sind `completed`, `failed`, `canceled` und `rejected`, wobei `pipelineResult` und `artifacts` das massgebliche Ergebnis sind.

## Sicherheitsgrenze

- Die Runtime lauscht nur auf einem zufaelligen Port auf `127.0.0.1`; jeder Start erzeugt einen frischen zufaelligen Bearer-Token, und jede Bridge-Anfrage traegt ihn.
- Die Bridge haelt Artefakte und Ergebnisse innerhalb des Job-Workspaces; Ergebnisse werden nach `.iac-code-skill-results/` im Workspace geschrieben.
- Preflight-Berichte und Berechtigungs-Anzeigefelder sind bereinigt; Geheimwerte und Zugangsdaten erscheinen nie in Anzeigefeldern.
