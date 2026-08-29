---
title: Solution-first-Pipeline
description: Wählen Sie eine Architektur, bevor die zugehörige ROS-Vorlage erzeugt und bereitgestellt wird.
---

# Solution-first-Pipeline

`selling_solution_first` ist eine Alibaba-Cloud-Kaufpipeline, in der Sie Architekturen vergleichen, bevor IaC Code eine ROS-Vorlage erzeugt. Nur die ausgewählte Lösung wird umgesetzt und bepreist; dadurch entfällt Arbeit für Kandidaten, die nicht bereitgestellt werden.

Die bestehende Pipeline `selling` bleibt verfügbar und ist weiterhin die Voreinstellung. Die neue Pipeline ist eine ausdrücklich zu wählende Alternative und verändert bestehende `selling`-Sitzungen nicht.

## Geeignete Einsatzfälle

Verwenden Sie `selling_solution_first`, wenn Sie:

- mehrere Architekturen, Produkte, Kosten, Vorteile und Risiken vor der Umsetzung vergleichen möchten;
- Region, Größenordnung, Netzwerk, Verfügbarkeit oder Budget klären möchten, bevor eine Vorlage festgelegt wird;
- nur die gewählte Architektur erzeugen, prüfen und bepreisen möchten;
- die endgültigen ROS-Parameter und das genaue Angebot vor dem Erstellen von Cloud-Ressourcen prüfen möchten.

| Pipeline | Arbeitsreihenfolge |
|---|---|
| `selling` | Kandidatenvorlagen erzeugen und bewerten, eine auswählen und anschließend bereitstellen. |
| `selling_solution_first` | Eine Architektur planen und auswählen, nur diese Wahl umsetzen und anschließend bereitstellen. |

## Pipeline starten

Im interaktiven Terminal:

```bash
IAC_CODE_MODE=pipeline \
IAC_CODE_PIPELINE_NAME=selling_solution_first \
iac-code
```

Wählen Sie in der lokalen Web-App beim Erstellen einer Unterhaltung den Pipeline-Modus und starten Sie den Server mit dem Pipeline-Namen:

```bash
IAC_CODE_PIPELINE_NAME=selling_solution_first iac-code web
```

Über A2A kann der Aufrufer Modus und Pipeline pro Nachricht wählen, ohne die Servervoreinstellung zu ändern:

```json
{
  "metadata": {
    "iac_code": {
      "run_mode": "pipeline",
      "pipeline_name": "selling_solution_first",
      "preferredLanguage": "de",
      "candidatePresentation": "rich-v1"
    }
  }
}
```

`pipeline_name` akzeptiert `selling` und `selling_solution_first`. Ein nicht unterstützter, nicht leerer Wert wird abgelehnt, statt unbemerkt eine andere Pipeline auszuführen. Verwenden Sie zum Fortsetzen einer gespeicherten Pipeline dieselbe A2A-`contextId`; die Pipeline-Identität im dauerhaften Snapshot ist maßgeblich.

## Die drei Phasen

### 1. Lösung planen und auswählen

IaC Code prüft zunächst, ob die Anfrage eine unterstützte Alibaba-Cloud-Infrastrukturaufgabe ist. Wenn fehlende Angaben die Produktauswahl, Topologie oder den Preis wesentlich ändern würden, stellt es gezielte Rückfragen.

Danach werden ein bis drei vergleichbare Lösungen angezeigt. Eine Lösung kann Folgendes enthalten:

- Architekturdiagramm und Topologie;
- Alibaba-Cloud-Produkte und Ressourcenbestand;
- empfohlene Spezifikationen und feste Einschränkungen;
- geeignete Szenarien und gelöste Probleme;
- grob geschätzte monatliche Kosten zum Vergleich;
- Vor- und Nachteile, Risiken und Begründung der Empfehlung.

Sie können eine Lösung wählen, die Anforderung ändern und einen neuen Satz erzeugen lassen oder abbrechen. In dieser Phase werden weder eine ROS-Vorlage noch Cloud-Ressourcen erstellt.

### 2. Ausgewählte Lösung umsetzen

IaC Code bearbeitet ausschließlich die ausgewählte Lösung. Es erzeugt und schreibt die ROS-Vorlage, validiert sie, löst Pflichtparameter auf, führt `PreviewStack` aus und fordert eine genaue ROS-Kostenschätzung an.

Vor der Bereitstellung zeigt die Oberfläche die endgültige Architektur, die Vorlagenparameter und das Angebot. Sie können:

- die Bereitstellung bestätigen;
- zulässige Parameter ändern und neu berechnen;
- zur ersten Phase zurückkehren und eine andere Lösung auswählen oder planen;
- abbrechen, ohne Cloud-Ressourcen zu erstellen.

Die grobe Schätzung aus Phase 1 und das genaue ROS-Angebot aus Phase 2 sind unterschiedliche Werte. Die Bereitstellungsbestätigung verwendet das genaue Angebot und die aktuellen Vorlagenparameter.

### 3. Bereitstellen

Nach der Bestätigung erstellt IaC Code den ROS-Stack, überträgt den maßgeblichen Stack-Fortschritt, wartet auf den Endstatus und zeichnet Stack-ID und Ausgaben auf. Bereitstellungsfehler bleiben für Diagnose und Wiederherstellung verfügbar.

## Bereitstellungsbestätigung und Werkzeugberechtigung

Bereitstellungsbestätigung und Werkzeugberechtigung sind zwei getrennte Sicherheitsgrenzen:

1. **Bereitstellungsbestätigung** bedeutet, dass Sie Lösung, Parameter und angebotene Kosten akzeptieren.
2. **Werkzeugberechtigung** autorisiert einen konkreten Cloud-Änderungsaufruf wie `ros:CreateStack` oder `vpc:CreateVpc` für diese Ausführung.

Die erste Bestätigung genehmigt die zweite nicht automatisch. Benötigt ein Werkzeug eine Berechtigung, hält IaC Code genau dort an und zeigt eine sichere Anfrage. Lese-, Änderungs- und Löschvorgänge werden unterschieden. API-Details können Produkt, API, Region, Aufrufreihenfolge und geschwärzte Parameter enthalten; Zugangsdaten, Token, Signaturen und andere vertrauliche Werte erscheinen nie in Anzeigefeldern.

Der Benutzer kann **Einmal zulassen** oder **Ablehnen** wählen. Die Entscheidung wird exakt der Anfrage zugeordnet und im Berechtigungs-Auditprotokoll gespeichert. Kann der erforderliche Auditdatensatz nicht dauerhaft geschrieben werden, wird eine Erlaubnis sicher verweigert.

## Pause, Wiederherstellung und Übergabe

Lösungsauswahl, Fragen, Bereitstellungsbestätigung und Berechtigungsanfragen sind wiederherstellbare Wartepunkte. IaC Code speichert einen Pipeline-Snapshot, bevor es auf die Fortsetzung durch den Aufrufer angewiesen ist. Nach einem Neustart oder dem erneuten Laden der Unterhaltung rekonstruiert die Oberfläche abgeschlossene Schritte und stellt offene Eingaben an ihrer ursprünglichen Position wieder her.

Für A2A-Integrationen gilt:

- `permission_requested` und `permission_resolved` behalten den zugehörigen Schritt und die Kandidatenkoordinaten;
- `pendingPermissions` zeigt ungelöste Anfragen in einem wiederhergestellten Task-Snapshot;
- eine Berechtigungsantwort über den Seitenkanal setzt den ursprünglichen Task und Kontext fort;
- die wiederholte Übermittlung derselben Entscheidung ist idempotent, eine widersprüchliche Entscheidung wird abgelehnt.

Wenn die Pipeline abgeschlossen wird, fehlschlägt, vorzeitig endet oder abgebrochen wird, übergibt sie denselben Kontext an den normalen Chat. Folgeanfragen können die gewählte Lösung, die erzeugte Vorlage, das Bereitstellungsergebnis und den Bereinigungsstatus weiterverwenden, ohne eine neue Unterhaltung zu beginnen.

## Oberflächen und Sprachen

Die Pipeline funktioniert im interaktiven Terminal, in der lokalen Web-App, in der Desktop-Web-Hülle, im SDK-Prozessmodus und im A2A-Servermodus. Die Darstellungsfähigkeiten unterscheiden sich — A2A kann beispielsweise strukturierte `rich-v1`-Kandidaten anfordern —, Pipeline-Zustand und Sicherheitsgrenzen sind jedoch gemeinsam.

Sichtbare Pipeline-Texte werden auf Englisch, vereinfachtem Chinesisch, Spanisch, Französisch, Deutsch, Japanisch und Portugiesisch unterstützt. A2A-Aufrufer wählen die Sprache einer Anfrage mit `metadata.iac_code.preferredLanguage`; Protokollfeldnamen, Enum-Werte, IDs und JSON-Strukturen werden nicht übersetzt.

## Verwandte Dokumentation

- [Pipeline-Modus](./pipeline-mode.md)
- [Web-App](../web-app.md)
- [A2A-Protokollreferenz](../a2a/protocol-reference.md)
- [Alibaba-Cloud-Anmeldedaten](../configuration/alibaba-cloud-credentials.md)
