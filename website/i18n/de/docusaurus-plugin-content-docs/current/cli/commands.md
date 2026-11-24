---
title: Häufige Aktionen
description: Einstellungen, Unterhaltungsmenüs und Eingabefeld in Desktop und Web verwenden.
---

# Häufige Aktionen

Diese Schritte gelten für Desktop und Web; beide verwenden dieselbe Gesprächsoberfläche.

## Ein Gespräch beginnen

1. Öffnen Sie Desktop oder starten Sie Web mit `iac-code` in einem interaktiven Terminal.

2. Klicken Sie in der Seitenleiste auf **Neuer Chat**. Wählen Sie unter der Eingabe das Projektverzeichnis für vorhandene Dateien oder neue Vorlagen.

3. Wählen Sie vor der ersten Nachricht **Normaler Modus** oder **Pipeline-Modus**. Wählen Sie bei Pipeline auch die gewünschte Pipeline aus.

4. Geben Sie die Aufgabe ein und senden Sie sie. Das Gespräch erscheint unter seinem Projekt in der Seitenleiste und kann dort fortgesetzt werden.

## Dateien oder Bilder hinzufügen

1. Öffnen Sie ein Gespräch im Projekt der Datei.

2. Geben Sie `@` und einen Teil des Dateinamens ein, und wählen Sie den Treffer. Ein Dateiverweis erscheint an der Eingabe. Bilder wählen Sie über die Anhangsfunktion aus.

3. Ergänzen Sie die Aufgabe, etwa „Prüfe die ROS-Vorlage und erkläre ihre Parameter“, kontrollieren Sie die Verweise und senden Sie die Nachricht.

## Modell und Denkoptionen wählen

1. Richten Sie den Anbieter gemäß [Modellkonfiguration](../configuration/llm-providers.md) ein.

2. Öffnen Sie im Gespräch die Modellauswahl unter der Eingabe und wählen Sie Anbieter und Modell.

3. Wählen Sie, falls unterstützt, den Denkaufwand und schalten Sie **Nachdenken** für dieses Gespräch ein oder aus.

4. Prüfen Sie das angezeigte Modell vor der nächsten Nachricht. Die verfügbaren Denkoptionen hängen vom Modell ab.

## Eine Werkzeugfreigabe bearbeiten

1. Öffnen Sie vor dem Senden die Berechtigungssteuerung in der Eingabe und wählen Sie den gewünschten Modus.

2. Prüfen Sie auf einer Freigabekarte den Werkzeugnamen und die vorgesehene Aktion, einschließlich Zieldatei oder Cloud-API.

3. Genehmigen Sie die Aktion oder lehnen Sie sie ab. Eine Genehmigung setzt die Aktion fort; eine Ablehnung wird dem Assistenten übermittelt. Fragekarten beantworten Sie gesondert.

## Anweisungen und Speicher verwalten

1. Öffnen Sie **Einstellungen → Gedächtnis** und wählen Sie oben das Projekt.

2. Tragen Sie unter **Projektgedächtnis** die projektbezogenen Anweisungen ein und klicken Sie dort auf **Speichern**.

3. Tragen Sie unter **Benutzergedächtnis** projektübergreifende Wünsche ein und klicken Sie auf dessen **Speichern**. Prüfen Sie jeweils die Bestätigung.

4. Schalten Sie **Automatisches Gedächtnis → Automatisch aufzeichnen** um, damit der Assistent hilfreiche Fakten während der Arbeit speichern kann. Die Änderung wird automatisch gespeichert.

5. Suchen und lesen Sie Einträge in der **Gedächtnisbibliothek**. Entfernen Sie veraltete Einträge über deren Löschfunktion und bestätigen Sie die Löschung.

## Sprache und Darstellung ändern

1. Öffnen Sie **Einstellungen → Allgemein**.

2. Wählen Sie die **Sprache**. Die Auswahl wird automatisch gespeichert und die Seite neu geladen.

3. Klicken Sie unter **Farbschema** auf ein Design. Darstellung und gespeicherte Auswahl werden automatisch aktualisiert.

## Weitere Aufgaben

Weitere Schrittfolgen finden Sie unter [Sitzungen](./sessions.md), [Skills](./skills.md) und [MCP-Schnellstart](../mcp/quick-start.md).

## Optionale Eingabekürzel

In einer bestehenden normalen Unterhaltung zeigt `/` wenige Kürzel an:

| Kürzel | Zweck |
|---|---|
| `/status` | Aktuellen Sitzungsstatus anzeigen. |
| `/compact` | Unterhaltung zusammenfassen, um Kontext zu reduzieren. |
| `/mcp` | MCP-Status anzeigen; Verbindungen unter **Plugins → MCP** verwalten. |

Pipeline blendet `/compact` aus; im Entwurf einer neuen Unterhaltung fehlen diese Sitzungskürzel. Wählen Sie Skills aus den Vorschlägen, ergänzen Sie die Anfrage und senden Sie sie. Siehe [Skills](./skills.md).
