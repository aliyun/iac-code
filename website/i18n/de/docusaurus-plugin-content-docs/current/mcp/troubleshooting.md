---
sidebar_position: 5
title: MCP-Fehlerbehebung
description: Diagnostizieren Sie MCP-Konfigurations-, Verbindungs-, Authentifizierungs- und Discovery-Probleme.
---

# MCP-Fehlerbehebung

MCP-Warnungen sind nicht fatal, sofern nicht jede benötigte Funktion fehlt. Ein fehlgeschlagener Server sollte andere MCP-Server oder eingebaute IaC Code-Tools nicht am Arbeiten hindern.

## Konfiguration prüfen

Konfigurierte Server auflisten:

```bash
iac-code mcp list
```

Eine geschwärzte Serverkonfiguration anzeigen:

```bash
iac-code mcp get my-server --scope local
```

Einen fehlerhaften Server entfernen:

```bash
iac-code mcp remove my-server --scope local
```

Projektgenehmigungen löschen:

```bash
iac-code mcp reset-project-choices
```

## Ausstehender Projektserver

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Lösung:

```bash
iac-code mcp approve name
```

oder starten Sie den interaktiven REPL im Projekt und antworten Sie bei der Frage mit `y`. Enter bedeutet `N` und lehnt den Server ab.

Wenn die Genehmigung früher funktionierte, prüfen Sie, ob `.mcp.json` geändert wurde. Genehmigung ist an die Konfigurationssignatur gebunden.

## Fehlende Umgebungsvariable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Eine dieser Lösungen verwenden:

```bash
export TOKEN=...
```

oder einen Default setzen:

```json
"Authorization": "${TOKEN:-}"
```

Server mit fehlenden erforderlichen Umgebungsvariablen werden übersprungen.

## Verbindung fehlgeschlagen

Für stdio-Server:

- Prüfen Sie, dass `command` auf dem `PATH` existiert.
- Verwenden Sie absolute Skriptpfade, wenn aus verschiedenen Verzeichnissen gestartet wird.
- Unter Windows Node-basierte Server über `cmd /c npx` starten.
- Prüfen Sie erforderliche Umgebungsvariablen.

Für HTTP- oder SSE-Server:

- URL und Transporttyp prüfen.
- TLS- und Proxy-Einstellungen prüfen.
- Sicherstellen, dass statische Headers vorhanden sind und keine Klartext-Secrets enthalten.
- `iac-code mcp auth <server>` ausführen, wenn der Server OAuth verlangt.

## Authentifizierung erforderlich

Symptom:

```text
MCP server 'name' requires authentication.
```

Lösung:

```bash
iac-code mcp auth name --scope user
```

Wenn der Server OAuth-Refresh-Tokens nutzt und erneute Authentifizierung erforderlich ist, löscht IaC Code veraltete Tokens und fordert einen neuen Flow an.

## Capability Discovery fehlgeschlagen

Mögliche Symptome:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

Der Server ist verbunden, aber eine Capability-Liste ist fehlgeschlagen. Andere Funktionen desselben Servers können weiter funktionieren. Beheben Sie den serverseitigen Fehler und starten Sie IaC Code neu oder lösen Sie reconnect/auth refresh aus.

## Ressourcen fehlen

`list_mcp_resources` wird nur registriert, wenn mindestens ein verbundener Server Ressourcen bereitstellt. Wenn das Tool fehlt:

- Prüfen Sie, dass der Server verbunden ist.
- Prüfen Sie, dass der Server `resources/list` unterstützt.
- Prüfen Sie Startwarnungen auf Resource-Discovery-Fehler.

## Prompt- oder Skill-Command fehlt

Prompt- und Skill-Commands erscheinen erst nach erfolgreicher Discovery. Prüfen Sie:

- Prompt oder `skill://`-Ressource existiert auf dem MCP-Server.
- Der normalisierte Command-Name kollidiert nicht mit einem built-in Command.
- Die Remote-Skill-Ressource kann innerhalb des Start-Timeouts gelesen werden.
- Skill-Beschreibung und Body passen in die Sicherheitslimits von IaC Code.

## Logs und Artefakte

Runtime-Logs liegen standardmäßig unter:

```text
<config-dir>/logs/
```

oder unter `IAC_CODE_LOG_DIR`, wenn gesetzt.

Binärartefakte aus MCP-Tool-Ergebnissen werden gespeichert unter:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Teilen Sie config-, log- oder artifact-Verzeichnisse nicht, ohne sie vorher auf Secrets zu prüfen.
