---
sidebar_position: 5
title: MCP-Fehlerbehebung
description: Probleme mit MCP-Konfiguration, Verbindung, Authentifizierung und Funktionserkennung diagnostizieren.
---

# MCP-Fehlerbehebung

MCP warnings sind normalerweise nicht fatal; erst wenn alle benoetigten capabilities nicht verfuegbar sind, wird es blockierend. Ein fehlgeschlagener server sollte andere MCP servers oder eingebaute IaC Code tools nicht am Arbeiten hindern.

## Inspect Configuration

Konfigurierte servers ohne Verbindung anzeigen:

```bash
iac-code mcp list
```

Bounded health diagnostics fuer konfigurierte servers ausfuehren:

```bash
iac-code mcp list --check
```

Überprüfen Sie eine geschwärzte Serverkonfiguration, ohne eine Verbindung herzustellen:

```bash
iac-code mcp get my-server --scope local
```

Führen Sie eine begrenzte Integritätsdiagnose für einen Server aus:

```bash
iac-code mcp get my-server --scope local --check
```

Überprüfen Sie die Konfiguration explizit, ohne eine Verbindung herzustellen:

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

Verbinden Sie einen Server oder alle persistenten Server erneut:

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

Symptom:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

Fix:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

Verwenden Sie den in der Konfigurationsliste angezeigten exakten `--scope`. Fuer nicht standardmaessige persistierte
Dateien geben Sie auch den passenden `--source-path` an. Wenn der server entfernt wurde, fuegen Sie ihn neu hinzu,
statt eine fehlende Konfiguration zu authentifizieren.

## Pending Project Server

Status oder warning code: `pending_approval`.

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

Oder starten Sie die interaktive REPL in diesem Projekt und antworten Sie mit „y“, wenn Sie dazu aufgefordert werden. Das Drücken der Eingabetaste bedeutet `N` und lehnt den Server ab.

Wenn die Genehmigung früher funktionierte, aber nicht mehr funktionierte, prüfen Sie, ob sich `.mcp.json` geändert hat. Die Genehmigung ist an die Konfigurationssignatur gebunden.

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

Server mit fehlenden erforderlichen Umgebungsvariablen werden übersprungen.

## Connection Failed

Status oder warning code: `connection_failed`.

For stdio servers:

- Verify `command` exists on `PATH`.
– Verwenden Sie absolute Pfade für Skripte, wenn Sie sie aus verschiedenen Verzeichnissen starten.
- Führen Sie unter Windows knotenbasierte Server über `cmd /c npx` aus.
- Überprüfen Sie, ob alle erforderlichen Umgebungsvariablen konfiguriert sind.

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
– Bestätigen Sie, dass statische Header vorhanden sind und keine Klartextgeheimnisse enthalten.
– Führen Sie `iac-code mcp auth <server>` aus, wenn der Server OAuth erfordert.

## Needs Authentication

Status: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

Wenn der Server OAuth-Aktualisierungstoken verwendet und eine erneute Authentifizierung erforderlich ist, löscht IaC-Code veraltete Token und fordert einen neuen Flow an.

## OAuth Auth Failed

Symptom (`auth-failed`):

```text
MCP auth failed for 'name':
```

Der OAuth flow wurde gestartet, aber nicht sauber beendet: callback URL kann unvollstaendig sein, authorization code
kann abgelaufen sein, oder der authorization server hat einen Fehler zurueckgegeben. Wenn ein neuer flow vor Abschluss
fehlschlaegt, stellt IaC Code den vorherigen auth state wieder her.

Fix:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

Versuchen Sie zuerst erneut `auth`. Fuehren Sie `reset-auth` vor dem erneuten Versuch nur aus, wenn gespeicherte token oder dynamic client state veraltet sind.

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

IaC-Code löscht den gespeicherten OAuth-Client- und Token-Status für diesen Server. Führen Sie die Authentifizierung erneut aus:

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

Der Server hat zusätzliche OAuth-Bereiche angefordert. Öffnen Sie in der aktuellen Sitzung `/mcp` und wählen Sie `Authentifizieren` oder
`Erneut authentifizieren` für diesen Server; IaC-Code enthält die von der Server-Challenge gemeldeten Bereiche in diesem Fluss. Die
Der eigenständige Befehl `iac-code mcp auth name` startet einen normalen Authentifizierungsfluss und überträgt keine Nur-Challenge-Bereiche von a
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

Fuehren Sie den Befehl mit dem exakten `--scope` command aus der Fehlermeldung erneut aus. Das ist scope ambiguity: server name ist gueltig, aber der Befehl braucht einen persistierten scope.

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

Der Server hat eine Verbindung hergestellt, aber eine Funktionsliste ist fehlgeschlagen. Andere Funktionen desselben Servers funktionieren möglicherweise weiterhin. Beheben Sie den serverseitigen Fehler und starten Sie dann den IaC-Code neu oder lösen Sie eine Neuverbindung/Authentifizierungsaktualisierung aus.

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

Überprüfen Sie bei wiederholten Fehlern, ob der Remote-Server die Sitzung abgebrochen oder neu gestartet hat.

## Headers Helper Failed

Zu den Symptomen können Hilfsanalysefehler, Zeitüberschreitungen, ein Exit-Status ungleich Null, ungültiges JSON oder Nicht-String-Headerwerte gehören. Überprüfen Sie, ob der Hilfsbefehl im Konfigurationsquellverzeichnis gültig ist und ein JSON-Objekt wie das Folgende ausgibt:

```json
{"X-Org": "platform"}
```

Geheimnisartiger stderr wird in der Diagnose geschwärzt.

## WebSocket Config Rejected

WebSocket-MCP-Server unterstützen die reine URL-Konfiguration. Entfernen Sie `headers`, `headersHelper` und `oauth` von `type: "ws"`-Servern.

## Resources Are Missing

`list_mcp_resources` wird nur registriert, wenn mindestens ein verbundener Server Ressourcen verfügbar macht. Wenn das Werkzeug fehlt:

- Confirm the server connected.
- Bestätigen Sie, dass der Server `resources/list` unterstützt.
- Überprüfen Sie die Startwarnungen auf Fehler bei der Ressourcenerkennung.

## Prompt or Skill Command Missing

Eingabeaufforderungs- und Fertigkeitsbefehle werden erst nach erfolgreicher Erkennung angezeigt. Überprüfen Sie:

- Die Eingabeaufforderung oder Ressource `skill://` ist auf dem MCP-Server vorhanden.
– Der normalisierte Befehlsname steht nicht in Konflikt mit einem integrierten Befehl.
– Die Remote-Skill-Ressource kann innerhalb des Start-Timeouts gelesen werden.
- Die Fertigkeitsbeschreibung und der Körper entsprechen den Sicherheitsgrenzen des IaC-Codes.

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

MCP-Binärartefakte aus Tool-Ergebnissen werden im sitzungseigenen Verzeichnis für v2-Sitzungen gespeichert:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

Bei älteren Sitzungen ohne unterstützte Layoutmarkierung wird Folgendes verwendet:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Vermeiden Sie es, Konfigurations-, Protokoll- oder Artefaktverzeichnisse freizugeben, ohne sie auf Geheimnisse zu überprüfen.
