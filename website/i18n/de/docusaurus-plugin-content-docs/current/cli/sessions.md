---
title: Sitzungen
description: Konversationen über Läufe hinweg speichern und fortsetzen.
---

# Sitzungen

IaC Code speichert jede Konversation automatisch auf der Festplatte. Sie können frühere Sitzungen fortsetzen und dort weiterarbeiten, wo Sie aufgehört haben.

## Sitzungen fortsetzen

### Interaktiv: `/resume`

Verwenden Sie im REPL den Befehl `/resume`:

```text
/resume
```

Dies öffnet eine interaktive Auswahl mit den letzten Sitzungen des aktuellen Projekts. Wenn ein Sitzungsname gesetzt ist, wird er als Titel angezeigt; sonst wird die letzte Eingabe oder ersatzweise die erste Eingabe verwendet.

Eine bestimmte Sitzung können Sie über die exakte Sitzungs-ID, ein eindeutiges ID-Präfix oder einen eindeutigen Sitzungsnamen fortsetzen:

```text
/resume abc123
```

### Sitzungen benennen

Verwenden Sie `/rename`, um der aktiven Sitzung einen stabilen, gut lesbaren Namen zu geben:

```text
/rename deploy-prod
```

Der Name wird in den Sitzungsmetadaten gespeichert. Er erscheint beim Fortsetzen im Willkommensbanner, im Exit-Hinweis und in der `/resume`-Auswahl.

Wenn der Name eine Sitzung eindeutig identifiziert, können Sie damit fortsetzen:

```text
/resume deploy-prod
iac-code --resume deploy-prod
```

### CLI: `--resume` und `--continue`

Eine bestimmte Sitzung können Sie über die Befehlszeile per exakter Sitzungs-ID, eindeutigem ID-Präfix oder eindeutigem Sitzungsnamen fortsetzen:

```bash
iac-code --resume <session-id-oder-name>
```

Die zuletzt verwendete Sitzung fortsetzen:

```bash
iac-code --continue
```

Die Kurzoptionen `-r` und `-c` sind ebenfalls verfügbar:

```bash
iac-code -r <session-id-oder-name>
iac-code -c
```

### Projektübergreifende Sitzungen

Wenn eine Sitzung zu einem anderen Projektverzeichnis gehört, wechselt IaC Code das Arbeitsverzeichnis nicht direkt. Stattdessen wird ein Befehl ausgegeben, der die Sitzung im richtigen Kontext fortsetzt:

```text
cd /path/to/other/project && iac-code --resume <session-id>
```

Dieser Befehl wird nach Möglichkeit auch in die Zwischenablage kopiert.

## Wiederherstellung nach Unterbrechungen

Wenn eine Sitzung mitten in der Ausführung unterbrochen wurde, etwa weil der Prozess während eines Tool-Laufs beendet wurde, erkennt IaC Code beim Fortsetzen verwaiste Tool-Aufrufe und ergänzt synthetische Fehlerergebnisse. So kann das Modell sauber weiterarbeiten, statt dauerhaft auf Tool-Ausgabe zu warten, die nie eintreffen wird.

## Sitzungsauswahl

Die `/resume`-Auswahl zeigt:

| Spalte | Beschreibung |
|--------|--------------|
| Titel | Sitzungsname, falls gesetzt; sonst letzte oder erste Benutzereingabe |
| Branch | Git-Branch zum Zeitpunkt der Sitzung |
| Zeit | Letzte Änderungszeit |

Sitzungen werden absteigend nach Aktualität sortiert. Sie können Text eingeben, um nach dem Titelinhalt zu filtern.
