---
sidebar_position: 2
title: MCP Schnellstart
description: MCP-Server im Plugin-Bereich von Desktop oder Web hinzufügen und verbinden.
---

# MCP Schnellstart

Verwalten Sie Server im Register **MCP** unter **Plugins** in Desktop oder Web. CLI-Befehle und Konfigurationsdateien unten dienen Automatisierung und erweiterten Einstellungen.

## Einen entfernten HTTP-Server verbinden

1. Öffnen Sie **Plugins → MCP**, wählen Sie das Projekt und klicken Sie auf **Server hinzufügen**.

2. Geben Sie **Name** und **Geltungsbereich** ein und wählen Sie **http (entfernt)** als Transport.

3. Tragen Sie die **URL** und erforderliche Header ein. Verweisen Sie bei Geheimnissen auf Umgebungsvariablen.

4. Klicken Sie auf **Erstellen**. Prüfen und **Genehmigen** Sie gegebenenfalls eine ausstehende Projektkonfiguration.

5. Klicken Sie bei Bedarf auf **Authentifizieren** und schließen Sie die Browserautorisierung ab. Kehren Sie zum Bereich zurück.

6. Klicken Sie auf **Verbindung prüfen** und **Funktionen anzeigen**. Prüfen Sie die erwarteten Werkzeuge, Ressourcen oder Prompts vor der Verwendung.

## Einen lokalen stdio-Server verbinden

1. Öffnen Sie **Plugins → MCP**, wählen Sie das Projekt und klicken Sie auf **Server hinzufügen**.

2. Tragen Sie **Name** und **Geltungsbereich** ein und wählen Sie **stdio (lokaler Befehl)**.

3. Geben Sie den **Befehl**, jeweils ein **Argument** pro Zeile und nötige Umgebungsvariablen ein. Das Programm muss auf dem ausführenden Rechner vorhanden sein.

4. Klicken Sie auf **Erstellen** und genehmigen Sie bei Bedarf die Projektkonfiguration.

5. Prüfen Sie Verbindung und Erkennung über **Verbindung prüfen** und **Funktionen anzeigen**.

:::note

Benutzerkonfiguration gilt projektübergreifend, lokale Konfiguration nur für das gewählte Arbeitsverzeichnis. Projektkonfiguration schreibt eine teilbare `.mcp.json` und erfordert eine lokale Freigabe. Bearbeiten Sie vorhandene Server über deren Bearbeitungsaktion und **Speichern**.

:::

## CLI-Automatisierung

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
iac-code mcp get yuque --check
```

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
iac-code mcp get local-files --config-only
```

## Naechste Schritte

- [MCP-Konfiguration](./configuration.md) beschreibt Geltungsbereiche, Projektdateien, Header, OAuth und erweiterte JSON-Formate.
- [OAuth und Sicherheit](./oauth-and-security.md) beschreibt Tokenspeicherung, Zurücksetzen und Freigaben.
- Die [Fehlerbehebung](./troubleshooting.md) hilft bei ausstehenden Freigaben, Authentifizierungsbedarf und Verbindungsfehlern.
