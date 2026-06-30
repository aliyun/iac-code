---
sidebar_position: 3
title: Tools, Ressourcen, Prompts und Skills
description: Verstehen Sie, wie MCP-Funktionen in IaC Code erscheinen.
---

# Tools, Ressourcen, Prompts und Skills

Verbundene MCP-Server können vier Arten von Funktionen für IaC Code bereitstellen.

## Tools

Jedes MCP-Tool wird zu einem IaC Code-Tool:

```text
mcp__<server>__<tool>
```

Tool-Beschreibungen und JSON-Eingabeschemas kommen vom MCP-Server. IaC Code leitet die Tool-Eingabe des Modells an den MCP-Server weiter und konvertiert anschließend MCP-Content-Blöcke in ein normales Tool-Ergebnis.

MCP-Tool-Annotationen werden soweit möglich berücksichtigt:

| MCP-Annotation | Verhalten in IaC Code |
|---|---|
| `readOnlyHint: true` | Das Tool gilt als read-only und parallelisierungssicher. |
| `destructiveHint: true` | Das Tool gilt für Berechtigungsentscheidungen als destruktiv. |

MCP-Tools laufen weiterhin durch das bestehende Berechtigungssystem von IaC Code. Konfigurieren Sie die Policy mit normalen `permissions`-Settings oder CLI-Flags wie `--allowed-tools`, `--disallowed-tools` und `--permission-mode`.

MCP-Fortschrittsbenachrichtigungen werden im interaktiven Rendering, in Headless-Fortschrittsausgaben, in ACP-Tool-Fortschrittsupdates und in A2A-Tool-Metadaten sichtbar.

## Tool-Ergebnisse und Artefakte

IaC Code konvertiert MCP-Content-Blöcke in modell-sichtbaren Text:

| MCP-Inhalt | IaC Code-Ergebnis |
|---|---|
| Textinhalt | Direkt im Tool-Ergebnis enthalten. |
| `structuredContent` | Als formatiertes JSON in einem Structured-Content-Abschnitt gerendert. |
| Textressourcen | Mit Server- und URI-Herkunft gerendert. |
| `resource_link` | Als Ressourcenlink mit URI und MIME-Type gerendert. |
| Bild-, Audio- und Blob-Daten | Als private Artefaktdateien gespeichert und per Artefakt-ID referenziert. |

Binärartefakte werden hier gespeichert:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

Das Modell sieht Artefakt-ID und Metadaten, nicht rohe base64-Daten.

## Ressourcen

Sobald ein verbundener Server Ressourcen bereitstellt, registriert IaC Code zwei globale Tools:

| Tool | Zweck |
|---|---|
| `list_mcp_resources` | Listet Ressourcen verbundener MCP-Server. Optional nach Servername filterbar. |
| `read_mcp_resource` | Liest eine Ressource per `server` und `uri`. |

Ressourcenzeilen enthalten Servername, URI, optionalen Ressourcennamen und optionalen MIME-Type.

## Prompts

MCP-Prompts werden zu Slash-Commands:

```text
/mcp__<server>__<prompt> key=value
```

Beim Aufruf ruft IaC Code MCP `prompts/get` auf, rendert die zurückgegebenen Prompt-Nachrichten, injiziert den gerenderten Prompt in die Konversation und lässt das Modell weiterarbeiten. Prompt-Argumente können so übergeben werden:

```text
template_name=prod-vpc region=cn-hangzhou
```

oder als JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Pflichtargumente werden vor dem MCP-Aufruf validiert. Quoted values werden unterstützt, einschließlich Windows-Pfade mit Backslashes.

## Skills

MCP-Ressourcen mit `skill://`-URIs werden zu Skill-Commands:

```text
$mcp__<server>__<skill>
```

IaC Code liest die Remote-Skill-Ressource, parst das Frontmatter und registriert sie als normalen Skill-Command. Remote-MCP-Skills sind aus Sicherheitsgründen begrenzt:

- Remote `allowed_tools` werden gelöscht.
- Remote Auto-Trigger-Pfadregeln werden gelöscht.
- Remote Skill-Body und Beschreibung sind längenbegrenzt.
- Bei Konflikt mit einem bestehenden Command wird der Remote-Skill mit einer MCP-Warnung übersprungen.

MCP-Skill-Ressourcen können beim Start gelesen werden, damit der Command vor dem Benutzeraufruf registriert ist.

## Dynamische Updates

Wenn ein MCP-Server `tools/list_changed`, `resources/list_changed` oder `prompts/list_changed` sendet, aktualisiert IaC Code die betroffene Capability-Liste und die Tool- oder Command-Registry. Refresh-Fehler werden als MCP-Warnungen gemeldet und stoppen die aktive Sitzung nicht.
