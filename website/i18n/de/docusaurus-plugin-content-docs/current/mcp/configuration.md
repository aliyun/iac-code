---
sidebar_position: 2
title: MCP-Konfiguration
description: Konfigurieren Sie MCP-Server über CLI-Commands, Settings-Dateien, Projektdateien und ACP-Sitzungen.
---

# MCP-Konfiguration

MCP-Server werden im Objekt `mcpServers` konfiguriert. IaC Code unterstützt ein mit Claude Code kompatibles Kernschema für `stdio`-, `http`- und `sse`-Server.

## Konfigurationsquellen

IaC Code liest MCP-Server aus diesen Quellen:

| Quelle | Scope | Datei oder Einstiegspunkt | Vertrauensmodell |
|---|---|---|---|
| Benutzer-Settings | `user` | `~/.iac-code/settings.yml` oder `IAC_CODE_CONFIG_DIR/settings.yml` | Vom aktuellen Benutzer vertraut. |
| Lokale Projekt-Settings | `local` | `<workspace>/.iac-code/settings.local.yml` | Privat für den lokalen Checkout. |
| Projekt-MCP-Datei | `project` | `<workspace>/.mcp.json` | Im Projekt geteilt und lokal genehmigungspflichtig. |
| ACP-Sitzungskonfiguration | `session` | `mcp_servers`, die von einem ACP-Client übergeben werden | Gilt nur für diese ACP-Sitzungs-Runtime. |

Die Reihenfolge ist user, project, local, dann session. Spätere Quellen überschreiben frühere nach Servername. Gleichwertige Konfigurationen werden zusätzlich per Inhaltssignatur dedupliziert.

Projektdateien `.mcp.json` werden von der Workspace-Root bis zum aktuellen Verzeichnis gesucht. Kind-Projektdateien überschreiben Eltern-Dateien nach Servername.

## CLI-Commands

Verwenden Sie `iac-code mcp`, um persistente MCP-Konfiguration zu verwalten:

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --type http \
  --url https://mcp.example.com/mcp \
  --header "Authorization=${MCP_REVIEWER_TOKEN}"
```

Verfügbare Commands:

| Command | Zweck |
|---|---|
| `iac-code mcp add` | Fügt einen Server aus strukturierten CLI-Flags hinzu. |
| `iac-code mcp add-json` | Fügt einen Server aus einem JSON-Objekt hinzu. |
| `iac-code mcp list` | Listet konfigurierte Server, Scopes, Transporte und Genehmigungsstatus. |
| `iac-code mcp get` | Gibt eine geschwärzte Serverkonfiguration aus. |
| `iac-code mcp remove` | Entfernt einen Server aus einem persistenten Scope. |
| `iac-code mcp approve` | Genehmigt einen Projektserver aus `.mcp.json`. |
| `iac-code mcp reject` | Lehnt einen Projektserver aus `.mcp.json` ab. |
| `iac-code mcp reset-project-choices` | Löscht gespeicherte Projektgenehmigungen. |
| `iac-code mcp auth` | Startet OAuth-Authentifizierung für einen Server. |
| `iac-code mcp reset-auth` | Löscht gespeicherte OAuth-Tokens und das Client Secret eines Servers. |

Wenn `--scope` fehlt, schreibt IaC Code innerhalb eines Projekts nach `local` und außerhalb eines Projekts nach `user`.

## Stdio-Server

Stdio-Server starten einen lokalen Befehl:

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["./tools/catalog_mcp.py"],
      "env": {
        "CATALOG_ENV": "prod"
      }
    }
  }
}
```

Das Feld `type` kann weggelassen werden, wenn `command` vorhanden ist. IaC Code übergibt eine sichere geerbte Umgebung plus das `env` des Servers. Unter Windows sollten Node-basierte Server mit `cmd /c npx` statt mit nacktem `npx` konfiguriert werden.

## HTTP- und SSE-Server

Entfernte Server benötigen `type` und `url`:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "${MCP_REVIEWER_TOKEN}"
      }
    }
  }
}
```

Verwenden Sie `type: "sse"` für SSE-Server. Statische Headers werden unterstützt. Dynamische `headersHelper`-Commands werden abgelehnt, weil sie ein eigenes Trusted-Execution-Design benötigen.

## Umgebungsvariablen-Erweiterung

Stringwerte unterstützen:

```text
${VAR}
${VAR:-default-value}
```

Fehlende Variablen ohne Default erzeugen eine MCP-Warnung, und der betroffene Server wird übersprungen. Die Erweiterung gilt rekursiv für Strings in Listen und Objekten.

Speichern Sie keine Klartext-Secrets in Headers oder env-Werten. Verwenden Sie Umgebungsvariablen-Referenzen oder OAuth-Secret-Storage.

## Projektgenehmigung

Projektdateien `.mcp.json` können ins Repository committed werden. Deshalb vertraut IaC Code ihnen nicht automatisch.

Beim Start des interaktiven REPL fragt IaC Code:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Enter übernimmt den Default `N` und lehnt genau diese Projektserver-Konfiguration ab. Geben Sie `y` oder `yes` ein, um sie zu genehmigen. Die Genehmigung wird lokal im IaC Code-Konfigurationsverzeichnis gespeichert und enthält Workspace-Pfad, Projektdateipfad, Servername und Konfigurationssignatur. Ändert sich die Serverkonfiguration in `.mcp.json`, wird die Genehmigung ungültig und der Server wird wieder pending.

Headless-, ACP- und A2A-Starts stellen nie interaktive Genehmigungsfragen. Ausstehende Projektserver werden übersprungen und als Warnungen gemeldet.
