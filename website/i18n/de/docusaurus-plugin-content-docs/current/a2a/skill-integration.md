---
sidebar_position: 7
title: IaC Code Skill installieren und verwenden
description: Laden Sie den IaC Code Skill herunter und installieren Sie ihn, damit ein externer Agent Alibaba-Cloud-Ressourcen verwalten kann.
---

# IaC Code Skill installieren und verwenden

Der IaC Code Skill richtet sich an externe Agenten, die Skills unterstützen. Nach der Installation kann ein
Host-Agent die Planung von Cloud-Architekturen, das Erstellen und Prüfen von ROS- oder Terraform-Vorlagen,
Kostenschätzungen, die Ressourcenauswahl, Stack-Operationen und Bereitstellungen an IaC Code delegieren. Der Skill
verwendet eine ausschließlich mit der Python-Standardbibliothek erstellte Bridge, um eine lokale, authentifizierte
A2A-Runtime zu starten. IaC Code muss nicht mit pip installiert werden, und der Host darf nicht auf Headless-Befehle
ausweichen.

## Skill herunterladen

### Neueste stabile Version

Laden Sie die neueste stabile Version direkt herunter:

[iac-code-skill.zip herunterladen](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Diese feste URL verweist immer auf das Skill-Paket, das für den stabilen Kanal freigegeben wurde. Sie eignet sich für
Downloads im Browser und für die manuelle Installation und ändert sich bei einer neuen Version nicht.

Installationsprogramme, die Version, Dateigröße, SHA-256-Prüfsumme und die unveränderliche versionsspezifische URL
benötigen, können die Metadaten des stabilen Kanals abrufen:

[latest.json anzeigen](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

Das Dokument enthält:

- `skillVersion`: die aktuelle stabile Version des Skills;
- `skill.url`: die unveränderliche ZIP-URL für diese Version;
- `skill.sha256` und `skill.size`: Werte zur Überprüfung des Downloads;
- `manifest.url`: das unveränderliche Release-Manifest für diese Version.

Für eine strenge Überprüfung oder eine reproduzierbare automatisierte Installation lesen Sie `latest.json`, laden
`skill.url` herunter und überprüfen `skill.sha256`. Erstellen Sie eine versionsspezifische URL nicht selbst.

## Skill installieren

### Voraussetzungen

- Der Host-Agent unterstützt lokale Skills, die durch `SKILL.md` definiert werden.
- CPython 3.8 bis 3.14 ist installiert. Verwenden Sie unter macOS/Linux `python3` und unter Windows vorzugsweise
  `py -3`.
- Die Umgebung kann auf die oben genannten OSS-URLs zugreifen, um das Skill-ZIP und die beim ersten Start benötigte
  Runtime herunterzuladen.
- Eine Modellservice-Konfiguration ist vorhanden. Für Aufgaben, die Cloud-Ressourcen abfragen oder verwalten, ist
  außerdem eine Alibaba-Cloud-Identität mit minimal erforderlichen Berechtigungen nötig.

Offizielle Skill-Runtime-Versionen unterstützen folgende Plattformen:

| Betriebssystem | Architektur |
|---|---|
| macOS | Apple Silicon (arm64) |
| Linux | x86_64 |
| Windows | x86_64 |

Die Mindestversionen des Betriebssystems und der Linux-glibc werden durch das vom Skill festgelegte Runtime-Manifest
bestimmt. Die Bridge prüft die Kompatibilität vor dem Download. Auf einer nicht unterstützten Plattform gibt sie
einen Fehler zurück, statt ein Artefakt für eine andere Plattform oder ABI herunterzuladen.

### In das Skill-Verzeichnis des Host-Agenten entpacken

Entpacken Sie das ZIP direkt in das Skill-Stammverzeichnis des Host-Agenten. Der genaue Pfad ist vom jeweiligen
Produkt abhängig; beachten Sie die Dokumentation des Host-Agenten. Die endgültige Verzeichnisstruktur muss so
aussehen:

```text
<Skill-Stammverzeichnis des Agenten>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

Das ZIP enthält bereits das oberste Verzeichnis `iac-code/`. Legen Sie kein weiteres Verzeichnis mit demselben Namen
an. Starten Sie den Host-Agenten nach der Installation oder Aktualisierung neu oder öffnen Sie eine neue Sitzung,
damit er den Skill erneut erkennt.

### Installation überprüfen

Führen Sie im entpackten Verzeichnis `iac-code` unter macOS oder Linux folgenden Befehl aus:

```bash
python3 scripts/iac_code.py ensure-runtime
```

Führen Sie in Windows PowerShell folgenden Befehl aus:

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

Beim ersten Aufruf lädt der Befehl die Runtime für die aktuelle Plattform herunter, überprüft Größe und
SHA-256-Prüfsumme und gibt JSON mit `skillVersion`, `runtimeTag` und dem Installationspfad aus. Eine überprüfte Runtime
im Cache wird ohne erneuten Download wiederverwendet.

## Modell und Alibaba-Cloud-Identität konfigurieren

Die Skill Runtime verwendet dasselbe Konfigurationsverzeichnis wie die anderen IaC-Code-Modi: standardmäßig
`~/.iac-code/`. Wenn Sie IaC Code bereits über REPL, Web-App oder Desktop-App konfiguriert haben, kann der Skill diese
Einstellungen wiederverwenden. Mit `IAC_CODE_CONFIG_DIR` legen Sie ein anderes Konfigurationsverzeichnis fest.

Stellen Sie in automatisierten Umgebungen die folgenden Variablen über eine Lösung zur Geheimnisverwaltung bereit:

| Kategorie | Umgebungsvariable | Beschreibung |
|---|---|---|
| Modell | `IAC_CODE_PROVIDER` | Modellanbieter |
| Modell | `IAC_CODE_MODEL` | Modellname |
| Modell | `IAC_CODE_API_KEY` | API-Schlüssel des Modellservice |
| Modell | `IAC_CODE_BASE_URL` | Optionale Überschreibung des kompatiblen Endpunkts |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey-ID |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey-Secret |
| Alibaba Cloud | `ALIBABA_CLOUD_SECURITY_TOKEN` | Sicherheitstoken für STS-Anmeldeinformationen |
| Alibaba Cloud | `ALIBABA_CLOUD_REGION_ID` | Standardregion |

Speichern Sie echte Anmeldeinformationen niemals in `SKILL.md`, in Prompts des Host-Agenten, in Projektdateien oder
im Shell-Verlauf. Bevorzugen Sie temporäre Anmeldeinformationen, RAM-Rollen oder OAuth und vergeben Sie nur die für
die Aufgabe erforderlichen Cloud-API-Berechtigungen. Vollständige Anleitungen finden Sie unter
[LLM-Anbieter](../configuration/llm-providers.md) und
[Alibaba-Cloud-Anmeldeinformationen](../configuration/alibaba-cloud-credentials.md).

## Erste Verwendung

Öffnen Sie nach Installation und Konfiguration eine neue Sitzung im Host-Agenten und beschreiben Sie direkt eine
Alibaba-Cloud-Infrastrukturaufgabe. Beispiel:

```text
Verwende iac-code, um die ROS-Vorlage in diesem Projekt zu prüfen. Liste Sicherheitsrisiken und empfohlene Änderungen auf, ohne die Datei zu verändern.
```

Host-Agenten mit einer expliziten Skill-Syntax können den Skill mit `$iac-code` auswählen. Der Host-Agent liest
`SKILL.md`, schreibt die vollständige Anfrage in eine UTF-8-Datei im Arbeitsbereich und verwendet die Bridge, um eine
Aufgabe zu erstellen und zu verfolgen. Der Benutzer muss keinen A2A-Server manuell starten.

Erwarteter Ablauf:

1. Die Bridge prüft, ob die Modell- und Alibaba-Cloud-Konfiguration vollständig ist.
2. Bei der ersten Verwendung lädt sie die vom Skill festgelegte IaC Code Runtime herunter und überprüft sie.
3. Die Runtime lauscht ausschließlich an einem zufälligen Port auf `127.0.0.1` und erzeugt ein prozessspezifisches
   Bearer-Token.
4. Der Host-Agent zeigt Fortschritt, Fragen, Planvorschläge und Berechtigungsanfragen von IaC Code an.
5. Nach Abschluss der Aufgabe gibt der Host-Agent das Endergebnis und die im Arbeitsbereich erzeugten Dateien zurück.

## Aktualisieren und deinstallieren

Laden Sie für eine manuelle Aktualisierung `skill/stable/iac-code-skill.zip` erneut herunter und ersetzen Sie das
gesamte Verzeichnis `iac-code/` im Skill-Stammverzeichnis des Hosts. Ein automatisches Aktualisierungsprogramm kann
`skillVersion` aus `latest.json` vergleichen und anschließend das neue Paket über dessen unveränderliche URL und
SHA-256-Prüfsumme herunterladen und überprüfen. Jeder offizielle Skill ist auf eine überprüfte Runtime festgelegt.
Ersetzen Sie nicht nur `scripts/iac_code.py` und ändern Sie Runtime-URL oder Prüfsumme nicht manuell.

Zum Deinstallieren entfernen Sie `iac-code/` aus dem Skill-Stammverzeichnis des Host-Agenten. Der Runtime-Cache wird
nicht zusammen mit dem Skill-Verzeichnis gelöscht. Führen Sie `cache list` und `cache clean` nur aus, wenn der Benutzer
ausdrücklich das Löschen des Caches verlangt.

## Runtime-Cache

Die bei der ersten Verwendung heruntergeladene Runtime wird unter
`<IAC_CODE_CONFIG_DIR oder ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` zwischengespeichert und automatisch
wiederverwendet. Im normalen Betrieb müssen Sie dieses Verzeichnis nicht verwalten. Mit folgenden Befehlen können Sie
den Speicherbedarf prüfen oder ältere Versionen entfernen:

- `python3 scripts/iac_code.py cache list` — installierte Runtimes und Kandidatenpakete auflisten;
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — Runtime-Caches oder
  Kandidatenpakete entfernen; `--confirm` ist erforderlich.

Die aktuelle Runtime und jede Runtime, die von einem laufenden Prozess verwendet wird, sind vor dem Bereinigen
geschützt. Paketformat und Runtime-Einschränkungen werden im Quelldepot durch
`skill-runtime/skill-package-contract.json` definiert; Benutzer müssen diese Datei nicht ändern.

## Fehlerbehebung

### Konfiguration ist unvollständig

Der Skill prüft die Konfiguration vor dem Erstellen einer Aufgabe, liest oder übermittelt jedoch niemals geheime
Werte:

| Situation | Ergebnis |
|---|---|
| LLM-Anbieter oder API-Schlüssel ist unvollständig | Gibt `llm_not_configured` zurück und erstellt die Aufgabe nicht |
| Alibaba-Cloud-Anmeldeinformationen für die Selling Pipeline sind unvollständig | Gibt `cloud_credentials_not_configured` zurück und erstellt die Aufgabe nicht |
| Alibaba-Cloud-Anmeldeinformationen sind im normalen Modus unvollständig | Aufgaben ohne Cloud-API-Aufrufe können mit einer Vorabwarnung fortgesetzt werden |

### Warum die Ausführung pausiert

IaC Code pausiert, wenn eine Berechtigung, zusätzliche Informationen oder die Auswahl eines Plans erforderlich ist.
Der Host-Agent zeigt die Anfrage unmittelbar an:

- eine Berechtigungsanfrage für ein Tool oder eine Bereitstellung (`permission`);
- eine Multiple-Choice-Frage oder eine Bitte um weitere Informationen (`ask_user_question`);
- die Auswahl eines Planvorschlags aus der Pipeline (`candidate_selection`).

Prüfen Sie vor der Bestätigung Zielressource, Region, erwartete Auswirkungen und Preis. Der Host-Agent kann eine
Ablehnung von IaC Code nicht außer Kraft setzen. Eine einmalige Genehmigung wird im Protokoll als `allow_once`
dargestellt.

> **Hinweis zur Integration des Host-Agenten**
>
> Enthält ein Bridge-Ergebnis `inputRequired`, muss der Host-Agent die aktuelle Anfrage anzeigen und auf eine Antwort
> warten. `boundaryReached` kennzeichnet eine Anzeige- oder Interaktionsgrenze, nicht den Abschluss der Aufgabe; der
> Host muss die Aktualisierung anzeigen und dieselbe Aufgabe weiter verfolgen.

## Sicherheit

- Die Runtime lauscht ausschließlich an einem zufälligen Port auf `127.0.0.1`. Bei jedem Start wird ein neues
  Bearer-Token erzeugt, das jede Anfrage der Bridge mitführt.
- Die Bridge speichert Artefakte und Ergebnisse im Arbeitsbereich der Aufgabe. Ergebnisse werden in
  `.iac-code-skill-results/` geschrieben.
- Anzeigefelder der Vorabprüfung und der Berechtigungsanfragen werden bereinigt; Geheimnisse und Anmeldeinformationen
  erscheinen dort nicht.

## Verwandte Dokumentation

- [Übersicht zum A2A-Protokoll](./overview.md)
- [Referenz zum A2A-Protokoll](./protocol-reference.md)
- [LLM-Anbieter](../configuration/llm-providers.md)
- [Alibaba-Cloud-Anmeldeinformationen](../configuration/alibaba-cloud-credentials.md)
- [Runtime-Konfiguration](../configuration/runtime-configuration.md)
