---
sidebar_position: 1
title: MCP-Integration
description: Erweitern Sie IaC Code mit Model Context Protocol-Servern um externe Tools, Ressourcen, Prompts und Skills.
---

# MCP-Integration

IaC Code kann als Host für das Model Context Protocol (MCP) arbeiten. MCP-Server erweitern den Agenten um externe Tools, Ressourcen, Prompts und wiederverwendbare Skills, laufen dabei aber weiter durch die Berechtigungs-, Sitzungs-, Logging- und Ausgabewege von IaC Code.

Verwenden Sie MCP, wenn IaC Code lokale oder entfernte Funktionen aufrufen soll, die nicht eingebaut sind, zum Beispiel einen privaten Template-Katalog, einen internen Deployment-Reviewer, einen Inventar-Abfragedienst oder ein spezialisiertes Cloud-Operations-Tool.

## Unterstützte Oberflächen

| Oberfläche | MCP-Unterstützung |
|---|---|
| Interaktiver REPL | Lädt Benutzer-, lokale und genehmigte Projektserver. Fragt nach, bevor neue Projektserver aus `.mcp.json` vertraut werden. |
| Nicht-interaktiver Modus | Lädt Benutzer-, lokale und genehmigte Projektserver. Fragt nie interaktiv; ausstehende Projektserver werden mit Warnungen übersprungen. |
| ACP-Server | Nimmt MCP-Serverkonfigurationen von ACP-Clients pro Sitzung an und stellt die entdeckten MCP-Funktionen in dieser Sitzung bereit. |
| A2A-Server | Lädt MCP über den normalen Runtime-Pfad und kann MCP-Warnungen und Tool-Fortschritt in A2A-Task-Metadaten veröffentlichen. |
| Pipeline-Modus | Nutzt dieselben Runtime-Integrationen wie der Normalmodus, einschließlich MCP-Tool-Fortschritt und Warnungsweitergabe. |

## Unterstützte Funktionen

| Funktion | Status |
|---|---|
| `stdio`-Transport | Unterstützt lokale MCP-Serverprozesse. |
| Streamable HTTP-Transport | Unterstützt entfernte MCP-Server. |
| SSE-Transport | Unterstützt entfernte MCP-Server. |
| MCP-Tools | Werden als Agent-Tools mit Namen `mcp__<server>__<tool>` bereitgestellt. |
| MCP-Ressourcen | Werden über `list_mcp_resources` und `read_mcp_resource` bereitgestellt. |
| MCP-Prompts | Werden als Slash-Commands mit Namen `mcp__<server>__<prompt>` bereitgestellt. |
| MCP-`skill://`-Ressourcen | Werden als Skill-Commands mit Namen `mcp__<server>__<skill>` bereitgestellt. |
| OAuth-Loopback-Authentifizierung | Unterstützt entfernte Server mit OAuth-Metadaten. |
| `roots/list` | Unterstützt. IaC Code gibt die aktive Workspace-Root als file-URI zurück. |
| `list_changed`-Benachrichtigungen | Unterstützt für Tools, Ressourcen und Prompts. Registrierungen werden dynamisch aktualisiert. |
| MCP-Elicitation | Noch nicht unterstützt. Server, die Elicitation anfordern, erhalten einen klaren Fehler. |
| WebSocket-, SDK- und IDE-Transporte | Nicht unterstützt. |
| Dynamische `headersHelper`-Commands | Nicht unterstützt. Verwenden Sie statische Headers oder Umgebungsvariablen-Referenzen. |
| IaC Code als MCP-Server | Nicht unterstützt. IaC Code arbeitet derzeit nur als MCP-Host. |

## Ablauf

Zur Laufzeit führt IaC Code diese Schritte aus:

1. Lädt MCP-Konfiguration aus Benutzer-, lokalen, Projekt- und Sitzungsquellen.
2. Erweitert `${VAR}`- und `${VAR:-default}`-Referenzen.
3. Überspringt unsichere oder ungültige Server mit sichtbaren Warnungen.
4. Verbindet genehmigte Server mit begrenzter Parallelität.
5. Entdeckt Tools, Ressourcen, Prompts und `skill://`-Ressourcen.
6. Registriert diese Funktionen in den vorhandenen Tool- und Command-Registries.
7. Konvertiert MCP-Tool-Ergebnisse in normale IaC Code-Tool-Ergebnisse und speichert Binärartefakte im Runtime-Konfigurationsverzeichnis.
8. Trennt MCP-Clients, wenn REPL, Headless-Ausführung, ACP-Sitzung oder A2A-Runtime geschlossen werden.

Ein fehlgeschlagener MCP-Server blockiert andere konfigurierte Server nicht. Verbindungs- und Discovery-Fehler bleiben als MCP-Warnungen sichtbar.

## Namensgebung

MCP-Tools und Commands werden zu öffentlichen Namen normalisiert:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Zeichen außerhalb von Buchstaben, Zahlen und Unterstrichen werden zu Unterstrichen. Wenn entdeckte Funktionen nach der Normalisierung kollidieren, hängt IaC Code einen kurzen Digest an, damit Namen eindeutig bleiben.

## Zugehörige Seiten

- [MCP-Konfiguration](./configuration.md)
- [Tools, Ressourcen, Prompts und Skills](./capabilities.md)
- [OAuth und Sicherheit](./oauth-and-security.md)
- [Fehlerbehebung](./troubleshooting.md)
