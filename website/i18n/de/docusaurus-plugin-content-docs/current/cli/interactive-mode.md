---
title: Bisherige Terminaloberfläche
description: Kompatibler Einstieg für bestehende Terminalabläufe.
---

# Bisherige Terminaloberfläche

TUI ist die bisherige Oberfläche für Gespräche und Werkzeugaktionen direkt im Terminal. Diese Seite dient vorhandenen Terminalabläufen.

:::note

TUI wird nicht mehr aktualisiert oder gewartet. Neue Oberflächenfunktionen richten sich an [Web](../web-app.md) und [Desktop](../desktop-app.md); dieselben Bedienelemente und Darstellungen sind im Terminal nicht zugesichert.

:::

## TUI starten und verwenden

1. Installieren Sie das [Python-Paket](../getting-started/installation.md). Richten Sie einen [Modellanbieter](../configuration/llm-providers.md) und bei Cloud-Aufgaben [Alibaba-Cloud-Zugangsdaten](../configuration/alibaba-cloud-credentials.md) ein. TUI kann die gespeicherte Konfiguration wiederverwenden.

2. Öffnen Sie ein interaktives Terminal, wechseln Sie ins Projekt und starten Sie TUI:

   ```bash
   cd /path/to/project
   iac-code tui
   ```

3. Nach dem Wartungshinweis erscheint die Eingabe. Geben Sie Ihre Aufgabe ein und senden Sie sie. Antworten und Werkzeugaktivitäten erscheinen im Terminal. Beantworten Sie dort erforderliche Fragen und Freigabeanfragen.

4. `/help` zeigt die Befehle Ihrer installierten Version, `/status` die aktuelle Sitzung und `/exit` beendet die Oberfläche.

TUI benötigt ein interaktives Terminal. Für Skripte oder CI ohne Terminal verwenden Sie [Headless-Automatisierung](../automation/non-interactive-mode.md). Ohne Unterbefehl öffnet `iac-code` standardmäßig Web.

## Ein Terminalgespräch fortsetzen

Starten Sie aus dem Projektverzeichnis des Gesprächs. Eine Sitzung anhand ihrer ID oder ihres eindeutigen Namens fortsetzen:

```bash
iac-code tui --resume <session-id-or-name>
```

Die neueste Sitzung im aktuellen Projekt fortsetzen:

```bash
iac-code tui --continue
```

Kombinieren Sie `--resume` und `--continue` nicht. Die Startoptionen Ihrer Version finden Sie unter `iac-code tui --help`.

## Zu Web oder Desktop wechseln

1. Starten Sie `iac-code web` oder öffnen Sie Desktop und wählen Sie dasselbe Projektverzeichnis.

2. Mit demselben Betriebssystembenutzer und `IAC_CODE_CONFIG_DIR` lassen sich gespeicherte Modelleinstellungen, Cloud-Zugangsdaten und Anweisungsgedächtnis wiederverwenden. Verwalten Sie diese anschließend in den Einstellungen; siehe [Häufige Aktionen](./commands.md).

3. Beginnen Sie ein neues Gespräch. Aktivieren Sie zum Lesen eines alten Terminalgesprächs die entsprechende Nur-Lese-Option unter **Einstellungen → Allgemein → Sichtbarkeit fremder Sitzungen**. Der gemeinsame Speicher macht diese Gespräche nicht in Web/Desktop bearbeitbar; siehe [Sitzungen](./sessions.md).
