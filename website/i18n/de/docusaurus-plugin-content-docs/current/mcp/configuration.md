---
sidebar_position: 2
title: MCP-Konfiguration
description: MCP-Server über CLI-Befehle, Einstellungsdateien, Projektdateien und ACP-Sitzungen konfigurieren.
---

# MCP-Konfiguration

MCP servers werden unter dem `mcpServers` object konfiguriert. IaC Code unterstuetzt ein mit Claude Code kompatibles core schema fuer `stdio`, `http`, `sse`, and URL-only `ws` servers.

## Schnellstart

Fuer einen entfernten HTTP-MCP-Server wie Yuque fuegen Sie den Server mit der positionalen URL-Form hinzu und starten dann OAuth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Fuer stdio-Wrapper wie `mcp-remote` setzen Sie den subprocess-Befehl hinter `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

## Configuration Sources

IaC-Code liest MCP-Server aus diesen Quellen:

| Quelle | Geltungsbereich | Datei oder Einstiegspunkt | Vertrauensmodell |
|---|---|---|---|
| Benutzereinstellungen | `user` | `~/.iac-code/settings.yml` oder `IAC_CODE_CONFIG_DIR/settings.yml` | Vom aktuellen Benutzer als vertrauenswürdig eingestuft. |
| Lokale Projekteinstellungen | `local` | `<workspace>/.iac-code/settings.local.yml` | Privat an der örtlichen Kasse. |
| Projekt-MCP-Datei | `project` | `<workspace>/.mcp.json` | Wird mit dem Projekt geteilt und erfordert eine örtliche Genehmigung. |
| ACP-Sitzungskonfiguration | `session` | `mcpServers` von einem ACP-Client übergeben | Gilt nur für diese ACP-Sitzungslaufzeit. |

Die Priorität ist Benutzer, Projekt, lokal, dann Sitzung. Spätere Quellen überschreiben frühere Quellen anhand des Servernamens. Äquivalente Konfigurationen werden auch durch die Inhaltssignatur dedupliziert.

Projektdateien `.mcp.json` werden vom Stammverzeichnis des Arbeitsbereichs bis zum aktuellen Verzeichnis erkannt. Untergeordnete Projektdateien überschreiben übergeordnete Dateien nach Servername.

## CLI Commands

Verwenden Sie `iac-code mcp`, um die persistente MCP-Konfiguration zu verwalten:

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

Remote-HTTP-Server können mit der positionellen URL-Form im Claude-Stil hinzugefügt werden:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

SSE- und WebSocket-Server verwenden dieselbe positionelle URL-Form mit ihrem jeweiligen Transport:

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

Für stdio-Wrapper wie `mcp-remote` fügen Sie den Unterprozessbefehl nach `--` ein:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Verfügbare Befehle:

| Befehl | Zweck |
|---|---|
| `iac-code mcp add` | Fügen Sie einen Server aus strukturierten CLI-Flags hinzu. |
| `iac-code mcp add-json` | Fügen Sie einen Server aus einem JSON-Objekt hinzu. |
| `iac-code mcp list` | Listet konfigurierte server, scopes, transports und approval status ohne Verbindung auf. |
| `iac-code mcp list --config-only` | Alias für die Standard-Konfigurationsliste. |
| `iac-code mcp list --check` | Verbindet kurz und zeigt begrenzte health diagnostics. |
| `iac-code mcp get` | Drucken Sie eine redigierte Serverkonfiguration, ohne eine Verbindung herzustellen. |
| `iac-code mcp get --config-only` | Drucken Sie eine redigierte Serverkonfiguration, ohne eine Verbindung herzustellen. |
| `iac-code mcp get --check` | Stellen Sie kurz eine Verbindung her und zeigen Sie begrenzte Zustandsdiagnosen für einen Server an. |
| `iac-code mcp remove` | Entfernen Sie einen Server aus einem dauerhaften Bereich. |
| `iac-code mcp approve` | Genehmigen Sie einen Projektserver `.mcp.json`. |
| `iac-code mcp reject` | Lehnen Sie einen Projekt-`.mcp.json`-Server ab. |
| `iac-code mcp reset-project-choices` | Löschen Sie gespeicherte Projektgenehmigungsoptionen. |
| `iac-code mcp auth` | Starten Sie die OAuth-Authentifizierung für einen Server. |
| `iac-code mcp reset-auth` | Löschen Sie gespeicherte OAuth-Tokens und Client-Geheimnisse für einen Server. |
| `iac-code mcp reconnect` | Verbinden Sie einen Server oder alle persistenten Server erneut mit `--all`. |
| `iac-code mcp disable` | Deaktivieren Sie einen persistenten Server, ohne die freigegebene Projektkonfiguration zu bearbeiten. |
| `iac-code mcp enable` | Aktivieren Sie einen persistenten Server erneut. |

## Befehlsoptionen

Der folgende option set entspricht `iac-code mcp <command> --help`:

| Befehl | Optionen |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options; nur `--help`. |
| `iac-code mcp reject` | No command-specific options; nur `--help`. |
| `iac-code mcp reset-project-choices` | No command-specific options; nur `--help`. |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

Wenn `--scope` weggelassen wird, schreibt IaC-Code innerhalb eines Projekts in `local` und außerhalb eines Projekts in `user`.

Für Befehle, die auf einem vorhandenen persistenten Server ausgeführt werden, kann IaC-Code einen eindeutigen Server über persistente Bereiche hinweg finden, wenn `--scope` weggelassen wird. Wenn derselbe Name in mehreren Bereichen vorhanden ist, schlägt der Befehl mit genauen `--scope`-Befehlen zur eindeutigen Unterscheidung fehl.

## Interaktiver MCP-Manager

Im interaktiven REPL öffnet `/mcp` einen Vollbild-MCP-Manager. Er gruppiert Server nach Quelle und zeigt Verbindungsstatus, Authentifizierungsstatus, Konfigurationsdiagnosen, Fehlerdetails und den konfigurierten Ort.

Im Manager können Sie die Tools, Ressourcen und Prompts eines verbundenen Servers prüfen; Remote-Server authentifizieren, erneut authentifizieren oder die Authentifizierung löschen; Server neu verbinden; persistente Server aktivieren oder deaktivieren; Projektserver aus `.mcp.json` genehmigen oder ablehnen; und persistente Einträge entfernen. OAuth-Flows zeigen die Autorisierungs-URL, unterstützen das Kopieren und akzeptieren eine eingefügte Callback-URL oder einen Autorisierungscode, wenn die Browser-Weiterleitung den lokalen Callback-Listener nicht erreichen kann.

`/mcp enable <name>`, `/mcp disable <name>` und `/mcp reconnect <name>` führen Schnellaktionen aus, ohne den Manager zu öffnen. Wenn `/mcp` über piped stdin oder eine andere Nicht-TTY-Eingabe eingeht, gibt IaC Code eine Meldung aus, dass ein Terminal erforderlich ist; verwenden Sie `iac-code mcp <command>` für nicht interaktive Automatisierung.

## Stdio Servers

Stdio servers launch a local command:

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

Das Feld `type` kann weggelassen werden, wenn `command` vorhanden ist. IaC-Code übergibt eine sichere geerbte Umgebung plus den Server `env`. Bevorzugen Sie unter Windows `cmd /c npx` anstelle von bloßem `npx` für knotenbasierte Server.

## HTTP and SSE Servers

Remote-Server erfordern `type` und `url`:

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

Verwenden Sie `type: "sse"` für SSE-Server. Statische Header werden entweder mit der CLI-Syntax `KEY=VALUE` oder `Name: Value` unterstützt.

Dynamische Header können mit `headersHelper` bereitgestellt werden:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "X-Org": "platform"
      },
      "headersHelper": "python ./scripts/mcp_headers.py"
    }
  }
}
```

Der helper muss ein JSON object ausgeben, dessen Schlüssel und Werte Strings sind. Dynamische Header überschreiben statische Header mit demselben Namen. IaC Code führt helpers ohne shell, ohne stdin, mit minimal geerbter Umgebung, dem Konfigurationsquellverzeichnis als cwd, 5 Sekunden timeout und redigierten stderr diagnostics aus. Der `headersHelper` command string wird nicht per Umgebungsvariable expandiert; referenzierte Variablen werden in die helper Umgebung übergeben, und der helper muss sie selbst lesen. Helpers in project `.mcp.json` benötigen project approval, bevor sie ausgeführt werden.

## WebSocket Servers

WebSocket servers use `type: "ws"`:

```json
{
  "mcpServers": {
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/mcp"
    }
  }
}
```

Der installierte MCP SDK WebSocket-Transport akzeptiert nur eine URL. IaC-Code lehnt WebSocket-Konfigurationen ab, die auch `headers`, `headersHelper` oder `oauth` festlegen.

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

Fehlende Variablen ohne default erzeugen eine MCP warning, und der betroffene server wird übersprungen. Umgebungsexpansion gilt rekursiv für Strings in Listen und Objekten, außer für den `headersHelper` command string; dieser bleibt literal und erhält referenzierte Variablen über die helper Umgebung.

Speichern Sie keine Klartextgeheimnisse in Headern oder Umgebungswerten. Verwenden Sie Umgebungsvariablenreferenzen oder OAuth-Geheimnisspeicher.

## Project Approval

Das Projekt `.mcp.json` kann in ein Repository übernommen werden, sodass IaC-Code ihm nicht automatisch vertraut.

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Durch Drücken der Eingabetaste bleibt die Standardeinstellung `N` erhalten und die exakte Projektserverkonfiguration wird abgelehnt. Geben Sie „y“ oder „yes“ ein, um es zu genehmigen. Die Genehmigung wird lokal im IaC-Code-Konfigurationsverzeichnis gespeichert und umfasst den Arbeitsbereichspfad, den Projektdateipfad, den Servernamen und die Konfigurationssignatur. Wenn sich die Serverkonfiguration `.mcp.json` ändert, wird die Genehmigung ungültig und der Server wird wieder ausstehend.

Headless-, ACP- und A2A-Startups stellen niemals interaktive Genehmigungsfragen. Ausstehende Projektserver werden übersprungen und als Warnungen gemeldet.

## Disabled Servers

`iac-code mcp disable <name>` speichert einen privaten Eintrag für den deaktivierten Status im IaC-Code-Konfigurationsverzeichnis. Bei projektbezogenen Servern wird die freigegebene Datei `.mcp.json` dadurch nicht verändert. Deaktivierte Einträge sind nach Bereich, Quelldatei, Servername und Konfigurationssignatur kodiert, sodass eine Änderung der Serverkonfiguration den veralteten deaktivierten Status ungültig macht.
