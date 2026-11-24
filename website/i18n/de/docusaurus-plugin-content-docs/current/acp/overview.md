---
sidebar_position: 1
title: ACP-Protokoll
description: Uebersicht ueber die Agent Client Protocol-Unterstuetzung in iac-code.
---

# ACP-Protokoll

## Was ist ACP

[Agent Client Protocol (ACP)](https://agentclientprotocol.com/get-started/introduction) ist ein standardisiertes Kommunikationsprotokoll zwischen KI-Agenten und ihren Clients. Es definiert, wie Clients (IDEs, Editoren, Automatisierungstools) Agentensitzungen ueber strukturierte JSON-RPC-Nachrichten starten, mit ihnen interagieren und sie verwalten.

## iac-code als ACP-Server

iac-code stellt seine Infrastructure-as-Code-Faehigkeiten ueber einen ACP-Server bereit. Jeder ACP-kompatible Client kann `iac-code acp` als Unterprozess starten (oder ueber HTTP+SSE verbinden) und programmatisch:

- Sitzungen erstellen, die auf ein Projektverzeichnis beschraenkt sind
- Eingaben in natuerlicher Sprache senden und Streaming-Antworten empfangen
- Dateischreib- und destruktive Operationen genehmigen oder ablehnen
- Mehrere gleichzeitige Sitzungen verwalten

Damit lässt sich iac-code als **kombinierbares Backend** in Entwicklungsumgebungen einbinden.

## Anwendungsfaelle

- **IDE- / Editor-Integration** -- Zed, VS Code oder benutzerdefinierte Editoren koennen iac-code als Kontextserver einbetten, um IaC-Generierung inline bereitzustellen.
- **Agent-zu-Agent-Orchestrierung** -- Andere KI-Agenten koennen die IaC-Faehigkeiten von iac-code ueber das Protokoll aufrufen und so Multi-Agenten-Workflows ermoeglichen.
- **Automatisierungspipelines** -- CI/CD-Skripte oder ChatOps-Bots koennen iac-code kopflos aufrufen, um Vorlagen zu generieren und zu validieren.

## Vergleich der Interaktionsmodi

| Modus | Befehl | Geeignet fuer |
|------|---------|----------|
| **Web** | `iac-code` | Praktische Erkundung, iteratives Erstellen von Vorlagen |
| **Nicht-interaktive CLI** | `iac-code --prompt "..."` | Skripting, einmalige Generierung, CI-Pipelines |
| **ACP-Server** | `iac-code acp` | IDE-Integration, Multi-Sitzungs-Verwaltung, programmatischer Zugriff |

ACP bietet sitzungsorientierte Client-Integration mit strukturierten Streaming-Ereignissen. Web, A2A und der SDK-Prozessmodus haben eigene Sitzungs- und Streaming-Schnittstellen.

## Kernfaehigkeiten

- **Multi-Sitzungs-Verwaltung** -- Erstellen, auflisten, verzweigen, fortsetzen und schliessen Sie unabhaengige Sitzungen, jede mit eigenem Gespraechsverlauf und Arbeitsverzeichnis.
- **Streaming-Antworten** -- Echtzeit-Ereignisse fuer Agententext, Denkprozesse, Tool-Aufrufe, Tool-Fortschritt und Abschluss.
- **Berechtigungsframework** -- Nur-Lese-Tools werden automatisch zugelassen; Schreib- und destruktive Tools erfordern eine explizite Client-Genehmigung vor der Ausfuehrung.
- **Dualer Transport** -- Stdio fuer lokale/Unterprozess-Nutzung, HTTP+SSE fuer entfernte und Netzwerk-Szenarien.
- **MCP-Serverkonfigurations-Durchleitung** -- Clients koennen bei der Sitzungserstellung MCP-Server zur Tool-Erweiterung deklarieren.
- **Slash-Befehle-Unterstuetzung** -- Leiten Sie Slash-Befehle (`/compact`, `/clear`, `/debug` usw.) ueber das Protokoll weiter.
- **Laufzeitmetriken** -- Token-Nutzung, Latenz und Tool-Aufruf-Statistiken auf Sitzungsebene.
