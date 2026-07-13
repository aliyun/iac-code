---
sidebar_position: 1
title: MCP-Integration
description: Model-Context-Protocol-Server verwenden, um IaC Code mit externen Tools, Ressourcen, Prompts und Skills zu erweitern.
---

# MCP-Integration

IaC Code kann als Model Context Protocol (MCP) host arbeiten. MCP servers erweitern den agent um externe tools, resources, prompts und reusable skills und laufen weiter durch IaC Codes permission-, session-, logging- und output-handling-Pfade.

Verwenden Sie MCP, wenn Sie möchten, dass IaC-Code eine lokale oder Remote-Funktion aufruft, die nicht in das Produkt integriert ist, z. B. einen privaten Vorlagenkatalog, einen internen Bereitstellungsprüfer, einen Inventarabfragedienst oder ein spezielles Cloud-Betriebstool.

## Supported Surfaces

| Surface | MCP support |
|---|---|
| Interaktive REPL | Lädt Benutzer-, lokale und genehmigte Projektserver. Eingabeaufforderungen, bevor neuen Projekt-`.mcp.json`-Servern vertraut wird. |
| Nicht interaktiver Modus | Lädt Benutzer-, lokale und genehmigte Projektserver. Nie Aufforderungen; Ausstehende Projektserver werden mit Warnungen übersprungen. |
| ACP-Server | Akzeptiert Sitzungs-MCP-Serverkonfigurationen von ACP-Clients und stellt erkannte MCP-Funktionen innerhalb dieser Sitzung bereit. |
| A2A-Server | Lädt MCP über die normale Laufzeit und kann MCP-Warnungen und Tool-Fortschritt in A2A-Aufgabenmetadaten veröffentlichen. |
| Pipeline-Modus | Verwendet die gleichen Laufzeitintegrationen wie im Normalmodus, einschließlich MCP-Tool-Fortschritt und Warnungsweitergabe. |

## Supported Capabilities

| Capability | Status |
|---|---|
| `stdio`-Transport | Unterstützt für lokale MCP-Serverprozesse. |
| Streambarer HTTP-Transport | Unterstützt für Remote-MCP-Server. |
| SSE-Transport | Unterstützt für Remote-MCP-Server. |
| MCP-Tools | Wird als Agent-Tools mit dem Namen `mcp__<server>__<tool>` bereitgestellt. |
| MCP-Ressourcen | Verfügbar gemacht durch `list_mcp_resources` und `read_mcp_resource`. |
| MCP-Eingabeaufforderungen | Wird als Slash-Befehl mit dem Namen `mcp__<server>__<prompt>` angezeigt. |
| MCP `skill://`-Ressourcen | Wird als Skill-Befehle mit dem Namen `mcp__<server>__<skill>` bereitgestellt. |
| OAuth-Loopback-Authentifizierung | Unterstützt für Remote-Server mit OAuth-Metadaten. |
| `roots/list` | Unterstützt. IaC-Code gibt das Stammverzeichnis des aktiven Arbeitsbereichs als Datei-URI zurück. |
| `list_changed`-Benachrichtigungen | Unterstützt für Tools, Ressourcen und Eingabeaufforderungen. Registrierungen werden dynamisch aktualisiert. |
| MCP elicitation | In interaktiven sessions unterstützt. Nicht interaktive runs brechen sicher ab. URL elicitation kann nach Benutzerbestätigung den ursprünglichen tool call erneut versuchen. |
| WebSocket transport | Für server mit ausschließlich `ws://` oder `wss://` URL unterstützt. WebSocket lehnt headers, `headersHelper` und OAuth ab, da der installierte SDK transport nur eine URL akzeptiert. |
| Dynamische `headersHelper` commands | Für vertrauenswürdige `http` und `sse` server unterstützt. Helpers laufen ohne shell, mit begrenztem timeout, minimaler Umgebung und redigierten diagnostics. |
| SDK- und IDE-Transporte | Nicht unterstützt. |
| IaC-Code als MCP-Server | Nicht unterstützt. IaC-Code fungiert derzeit nur als MCP-Host. |

## How It Works

At runtime IaC Code:

1. Lädt die MCP-Konfiguration aus Benutzer-, Projekt-, lokalen und Sitzungsquellen.
2. Erweitert die Referenzen `${VAR}` und `${VAR:-default}`.
3. Überspringt unsichere oder ungültige Server mit für den Benutzer sichtbaren Warnungen.
4. Verbindet genehmigte Server mit begrenzter Parallelität.
5. Entdeckt Tools, Ressourcen, Prompts und `skill://`-Ressourcen.
6. Registriert diese Funktionen in den vorhandenen Tool- und Befehlsregistern.
7. Fügt Anweisungen für verbundene Server als serverbezogene Anleitung in die Agent-Eingabeaufforderung ein.
8. Konvertiert MCP-Tool-Ergebnisse in normale IaC-Code-Tool-Ergebnisse und speichert binäre Artefakte und große Textartefakte im Runtime-Konfigurationsverzeichnis.
9. Trennt MCP-Clients, wenn REPL, Headless-Ausführung, ACP-Sitzung oder A2A-Runtime geschlossen werden.

Ein ausgefallener MCP-Server blockiert andere konfigurierte Server nicht. Verbindungs- und Erkennungsfehler bleiben als MCP-Warnungen sichtbar.

## Naming

MCP-Tools und -Befehle werden in öffentliche Namen normalisiert:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Zeichen außerhalb von Buchstaben, Zahlen und Unterstrichen werden zu Unterstrichen. Wenn zwei erkannte Funktionen nach der Normalisierung kollidieren, hängt IaC-Code einen kurzen Digest an, um die Namen eindeutig zu halten.

Für MCP-Skills registriert IaC Code auch einen Kompatibilitätsalias wie `<server>:<skill>`, wenn dieser Alias nicht mit einem vorhandenen Befehl in Konflikt steht. Die Diagnose behält die ursprünglichen Server-, Tool-, Eingabeaufforderungs- oder Skillnamen bei, selbst wenn öffentliche Namen normalisiert werden.

## Related Pages

- [MCP Schnellstart](./quick-start.md)
- [MCP-Konfiguration](./configuration.md)
- [Tools, Ressourcen, Eingabeaufforderungen und Fähigkeiten](./capabilities.md)
- [OAuth und Sicherheit](./oauth-and-security.md)
- [Fehlerbehebung](./troubleshooting.md)
