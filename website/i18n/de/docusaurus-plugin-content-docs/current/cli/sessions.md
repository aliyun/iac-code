---
title: Sitzungen
description: Konversationen über Läufe hinweg speichern und fortsetzen.
---

# Sitzungen

Web, Desktop und Headless verwenden denselben Sitzungsspeicher. Unterhaltungen werden automatisch im Laufzeit-Konfigurationsverzeichnis gespeichert.

## Web und Desktop

### Ein Gespräch fortsetzen

1. Öffnen Sie das Gespräch unter seinem Projekt in der Seitenleiste oder finden Sie es über **Suchen**.

2. Prüfen Sie die gespeicherten Nachrichten und Dateien.

3. Senden Sie eine weitere Anfrage. Für eine unabhängige Aufgabe wählen Sie **Neuer Chat**.

### Ein Gespräch umbenennen oder anheften

1. Öffnen Sie das Gespräch und sein Menü **…**.

2. Wählen Sie **Unterhaltung umbenennen**, geben Sie den Titel ein und klicken Sie auf **Speichern**. Die Seitenleiste zeigt den neuen Titel.

3. Wählen Sie im selben Menü **Gespräch anheften**, um es leichter zu finden. Die Gegenaktion löst die Anheftung.

### Ein Gespräch archivieren und wiederherstellen

1. Wählen Sie im Menü **…** die Aktion **Unterhaltung archivieren**. Es verschwindet aus der aktiven Liste.

2. Öffnen Sie **Einstellungen → Archivierte Unterhaltungen** und suchen oder filtern Sie nach dem Projekt.

3. Klicken Sie beim Gespräch auf **Aus Archiv holen**. Öffnen Sie es danach aus der Seitenleiste.

:::note

Über CLI, A2A oder ACP erstellte Gespräche sind standardmäßig ausgeblendet. Aktivieren Sie zum Anzeigen die entsprechende Nur-Lese-Option unter **Einstellungen → Allgemein → Sichtbarkeit fremder Sitzungen**. Gemeinsamer Speicher bedeutet nicht, dass diese Gespräche in Web/Desktop bearbeitbar sind.

:::

## Headless fortsetzen

Führen Sie den Befehl im Projektverzeichnis der Sitzung aus. `--resume` setzt eine bestimmte Sitzung fort, `--continue` die neueste im Projekt. Die Kurzformen `-r` und `-c` dürfen nicht kombiniert werden.

```bash
iac-code --resume deploy-prod --prompt "Eine VPC-Vorlage erzeugen"
iac-code --continue --prompt "Eine VPC-Vorlage erzeugen"
```

## Andere Projekte

Öffnen Sie in Web oder Desktop das Gespräch unter seinem Projekt in der Seitenleiste. Wechseln Sie für Headless zuerst in das Projektverzeichnis:

```bash
cd /path/to/project
iac-code --resume <session-id> --prompt "Eine VPC-Vorlage erzeugen"
```

## Unterbrochene Unterhaltungen

Öffnen Sie die gespeicherte Unterhaltung, um den Verlauf zu prüfen und fortzufahren. Die Wiederherstellung gleicht unvollständige Werkzeugaufrufe ab. Pipeline-Fortschritt und ausstehende Entscheidungen folgen der [Pipeline-Wiederherstellung](../automation/pipeline-mode.md). Einzelne `--prompt`-Aufrufe führen keine Pipelines aus.
