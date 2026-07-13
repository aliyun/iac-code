---
sidebar_position: 3
title: Tools, Ressourcen, Prompts und Skills
description: Verstehen, wie MCP-Funktionen in IaC Code erscheinen.
---

# Tools, Ressourcen, Prompts und Skills

Verbundene MCP servers stellen IaC Code vier Arten von capabilities bereit.

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

Werkzeugbeschreibungen und JSON-Eingabeschemata stammen vom MCP-Server. IaC-Code leitet die Werkzeugeingabe des Modells an den MCP-Server weiter und wandelt dann MCP-Inhaltsblöcke in ein normales Werkzeugergebnis um.

Zu den Berechtigungsaufforderungen und Prüfmetadaten gehören der MCP-Servername, der ursprüngliche Toolname, der öffentliche normalisierte Toolname und schreibgeschützte/destruktive Anmerkungen.

Anmerkungen zum MCP-Tool werden nach Möglichkeit berücksichtigt:

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` | Das Tool wird als schreibgeschützt und nebenläufigkeitssicher behandelt. |
| `destructiveHint: true` | Das Tool wird bei Berechtigungsentscheidungen als destruktiv behandelt. |

MCP-Tools durchlaufen weiterhin das bestehende Berechtigungssystem von IaC Code. Konfigurieren Sie die Berechtigungsrichtlinie mit normalen `permissions`-Einstellungen oder CLI-Flags wie `--allowed-tools`, `--disallowed-tools` und `--permission-mode`.

MCP-Fortschrittsbenachrichtigungen werden in interaktivem Rendering, Headless-Fortschrittsausgabe, ACP-Tool-Fortschrittsaktualisierungen und A2A-Tool-Metadaten angezeigt.

## Tool Results and Artifacts

IaC-Code konvertiert MCP-Inhaltsblöcke in für das Modell sichtbaren Text:

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result when small; großer Text wird als privates `.txt`, `.json` oder `.md` artifact gespeichert. |
| `structuredContent` | Wird als formatiertes JSON unter einem Abschnitt mit strukturiertem Inhalt gerendert. |
| Textressourcen | Mit Server- und URI-Herkunft gerendert. |
| `resource_link` | Wird als Ressourcenlink mit URI und MIME-Typ gerendert. |
| Bild-, Audio- und Blobdaten | Als private Artefaktdateien gespeichert und durch die Artefakt-ID referenziert. |

Binäre Artefakte werden im sitzungseigenen MCP-Tool-Ergebnisverzeichnis für v2-Sitzungen gespeichert:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/<server>/<tool>/
```

Bei älteren Sitzungen ohne unterstützte Layoutmarkierung wird weiterhin Folgendes verwendet:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data. Große Text-artifacts enthalten einen path so the full output can be read without flooding the conversation.

## Resources

Wenn ein verbundener Server Ressourcen verfügbar macht, registriert IaC Code zwei globale Tools:

| Tool | Purpose |
|---|---|
| `list_mcp_resources` | Listet Ressourcen von verbundenen MCP-Servern auf. Optional nach Servername filtern. |
| `read_mcp_resource` | Liest eine Ressource nach `server` und `uri`. |

Zu den Ressourcenzeilen gehören der Servername, der URI, der optionale Ressourcenname und der optionale MIME-Typ.

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

Beim Aufruf ruft IaC-Code MCP `prompts/get` auf, rendert die zurückgegebenen Prompt-Nachrichten, fügt den gerenderten Prompt in die Konversation ein und lässt das Modell fortfahren. Prompt-Argumente können wie folgt übergeben werden:

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Erforderliche Eingabeaufforderungsargumente werden vor dem MCP-Aufruf validiert. Werte in Anführungszeichen werden unterstützt, einschließlich Windows-Pfaden mit Backslashes.

## Skills

MCP-Ressourcen mit `skill://`-URIs werden zu Skill-Befehlen:

```text
$mcp__<server>__<skill>
```

IaC-Code liest die Remote-Skill-Ressource, analysiert Frontmatter und registriert sie als normalen Skill-Befehl. Remote-MCP-Fähigkeiten sind sicherheitsbeschränkt:

- Remote `allowed_tools` are cleared.
– Die Regeln für den Remote-Auto-Trigger-Pfad wurden gelöscht.
- Die Länge des Remote-Skill-Körpers und der Beschreibung ist begrenzt.
– Wenn der Remote-Skill mit einem vorhandenen Befehl in Konflikt steht, wird er mit einer MCP-Warnung übersprungen.

MCP-Skill-Ressourcen können während des Startvorgangs gelesen werden, sodass der Befehl registriert werden kann, bevor der Benutzer ihn aufruft.

Wenn kein Befehlskonflikt vorliegt, erhalten MCP-Skills auch einen Kompatibilitätsalias:

```text
$<server>:<skill>
```

Beispielsweise können `$mcp__yuque__search` und `$yuque:search` in denselben Remote-Skill aufgelöst werden.

## Server Instructions (Server-Anweisungen)

Wenn ein verbundener Server „Anweisungen“ von der Initialisierung zurückgibt, fügt IaC-Code diese als dedizierten MCP-Server-Anweisungsabschnitt in die Agent-Eingabeaufforderung ein. Diese Anweisungen werden als serverbezogene Anleitung behandelt und haben keine Vorrang vor lokalen Projektanweisungen.

## Elicitation (interaktive Anfragen)

Interaktive sessions können MCP elicitation requests an den Benutzer weiterleiten. URL-mode elicitation kann den Benutzer bitten, einen externen URL flow abzuschließen, und danach den ursprünglichen MCP tool call bis zu einem begrenzten retry limit erneut versuchen. Nicht interaktive Kontexte brechen elicitation sicher ab.

## Dynamic Updates

Wenn ein MCP-Server `tools/list_changed`, `resources/list_changed` oder `prompts/list_changed` sendet, aktualisiert IaC-Code die betroffene Funktionsliste und aktualisiert die Tool- oder Befehlsregistrierung. Aktualisierungsfehler werden als MCP-Warnungen gemeldet und führen nicht zum Beenden der aktiven Sitzung.
