---
title: Pipeline-Modus
description: Verwenden Sie den schrittweisen Pipeline-Modus, um komplexe Infrastrukturaufgaben zu führen.
---

# Pipeline-Modus

Der Pipeline-Modus ist ein interaktiver Modus, der Arbeit Schritt für Schritt ausführt. Er eignet sich für Infrastrukturaufgaben, die länger oder fehleranfälliger sind als eine normale Chat-Anfrage: Anforderungen verstehen, einen Ansatz planen, Artefakte erzeugen, den Benutzer bestätigen lassen und dann mit den nächsten Aktionen fortfahren.

Pipeline selbst ist eine allgemeine Fähigkeit. Die heute verfügbare integrierte Implementierung ist die `selling`-Pipeline. `selling` zielt auf Alibaba-Cloud-Infrastrukturszenarien und kann eine Deployment-Anfrage durch Kandidatenarchitekturen, ROS-Templates, Kostenschätzungen und nach Bestätigung bis zum Deployment führen.

Geeignete Anfragen für den Pipeline-Modus sind zum Beispiel:

```text
Einen vorhandenen VPC auswählen und einen VSwitch erstellen
```

```text
Ein kostengünstiges Alibaba-Cloud-Deployment für eine Webanwendung entwerfen und ein Template erzeugen
```

## Pipeline-Modus starten

Der Pipeline-Modus benötigt derzeit die interaktive REPL. Er kann nicht mit `--prompt` kombiniert werden.

Unter macOS oder Linux:

```bash
IAC_CODE_MODE=pipeline iac-code
```

In PowerShell:

```powershell
$env:IAC_CODE_MODE = "pipeline"
iac-code
```

Der Standardname der Pipeline ist `selling`. Um ihn ausdrücklich anzugeben:

```bash
IAC_CODE_MODE=pipeline IAC_CODE_PIPELINE_NAME=selling iac-code
```

## Verhältnis von Pipeline und selling

| Name | Bedeutung |
|---|---|
| Pipeline-Modus | Allgemeiner schrittweiser Ausführungsmodus von IaC Code für lange Abläufe, Bestätigungspunkte, Wiederherstellung und Fortschrittsanzeige. |
| `selling`-Pipeline | Die aktuelle integrierte Pipeline für Alibaba-Cloud-Infrastrukturdesign, Template-Erzeugung, Kostenschätzung und Deployment. |

Wenn später weitere Pipelines bereitgestellt werden, können Sie sie mit `IAC_CODE_PIPELINE_NAME` auswählen. Die aktuelle Version enthält `selling`.

## Umgebungsvariablen

| Variable | Zweck |
|---|---|
| `IAC_CODE_MODE=pipeline` | Aktiviert den Pipeline-Modus. Jeder andere Wert fällt auf den normalen Modus zurück. |
| `IAC_CODE_PIPELINE_NAME` | Wählt die Pipeline-Definition aus. Standard ist `selling`. |
| `IAC_CODE_CWD` | Überschreibt das von der Pipeline verwendete Arbeitsverzeichnis. |
| `IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING` | Aktiviert den optionalen Template-Review-Schritt in der `selling`-Pipeline. |

## Was in der selling-Pipeline passiert

Die `selling`-Pipeline zerlegt eine Infrastrukturanfrage in für Benutzer verständliche Phasen:

| Phase | Was Sie sehen |
|---|---|
| Anforderung verstehen | IaC Code prüft, ob es sich um eine Alibaba-Cloud-Infrastrukturaufgabe handelt. Fehlen wichtige Details, fragt es nach, bevor ein Plan erzeugt wird. |
| Architekturen planen | IaC Code schlägt eine oder mehrere Kandidatenarchitekturen vor, damit Sie Kompromisse vergleichen können. |
| Erzeugen und bewerten | IaC Code erzeugt ROS-Templates für Kandidatenpläne und schätzt Ressourcenkosten. |
| Plan bestätigen | IaC Code zeigt Kandidatendetails an und wartet, bis Sie den Plan auswählen, mit dem fortgefahren werden soll. |
| Deployment | Nach Auswahl eines Plans wechselt IaC Code in die Deployment-Phase und behandelt Tools oder risikoreichere Aktionen gemäß der Berechtigungsrichtlinie. |

Wenn Sie Einschränkungen wie „einen vorhandenen VPC verwenden“ oder „diesen Ressourcentyp nicht erstellen“ erwähnen, versucht die `selling`-Pipeline, diese in späteren Plänen und Templates zu berücksichtigen. Sie müssen keine internen Felder kennen; schreiben Sie die Einschränkungen einfach in die Anfrage.

## Interaktion und Wiederherstellung

Der Pipeline-Modus kann pausieren und auf Benutzereingaben warten, zum Beispiel:

- Die Anforderung ist unklar und IaC Code benötigt Ziel, Größe, Region oder Budget.
- Es gibt mehrere Kandidatenpläne und Sie müssen einen auswählen.
- Eine Tool- oder Deployment-Aktion benötigt eine Berechtigungsfreigabe.
- Der Lauf wurde unterbrochen und muss wiederhergestellt oder fortgesetzt werden.

Wenn der Prozess endet oder die Sitzung unterbrochen wird, speichert IaC Code den Pipeline-Zustand. Wenn Sie später mit `--resume` zu dieser Sitzung zurückkehren, können Sie den bisherigen Fortschritt ansehen und von einem wiederherstellbaren Punkt fortsetzen.

Nachdem die Pipeline abgeschlossen ist, fehlschlägt, frühzeitig beendet oder abgebrochen wird, wechselt IaC Code zurück in den normalen Chat. Danach können Sie Folgefragen stellen, den Plan anpassen oder Probleme nach dem Deployment bearbeiten.

## Automatisierungsintegrationen

Der Pipeline-Modus ist derzeit hauptsächlich für die interaktive REPL gedacht. Der A2A-Servermodus kann Pipeline-Fortschritt, Artefakte, Berechtigungsergebnisse und Wiederherstellungsinformationen nach außen bereitstellen. Das ist nützlich, wenn eine Pipeline an eine externe Konsole oder ein Aufgabensystem angebunden wird.

ACP unterstützt den Pipeline-Modus derzeit nicht. `--prompt` / der [nicht interaktive Modus](./non-interactive-mode.md) führt eine normale einmalige Anfrage aus und führt keine Pipeline-Schritte aus.

## Aktuelle Einschränkungen

- Die aktuelle Version enthält nur die `selling`-Pipeline, hauptsächlich für Alibaba-Cloud-Infrastrukturworkflows.
- Der Pipeline-Modus benötigt die interaktive REPL. `--prompt` wird abgelehnt, wenn `IAC_CODE_MODE=pipeline` gesetzt ist.
- Der Pipeline-Modus unterstützt Texteingaben. In die REPL eingefügte Bilder werden ignoriert, solange die Pipeline aktiv ist.
- Während einer Pipeline sind Shell-Escapes, Skill-Trigger und die meisten Slash-Befehle eingeschränkt, sofern die Pipeline-Definition sie nicht ausdrücklich erlaubt. Grundlegende Befehle wie `/help`, `/status`, `/resume` und `/exit` bleiben verfügbar.
