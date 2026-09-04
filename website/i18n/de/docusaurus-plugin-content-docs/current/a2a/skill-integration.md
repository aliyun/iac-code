---
sidebar_position: 2
title: IaC Code Skill installieren und verwenden
description: Fuegen Sie IaC Code einem Skill-faehigen Agenten hinzu und verwalten Sie Alibaba-Cloud-Infrastruktur.
---

# IaC Code Skill installieren und verwenden

Mit dem IaC Code Skill kann ein kompatibler Agent Aufgaben an IaC Code delegieren: Cloud-Architekturen planen,
ROS- oder Terraform-Vorlagen erstellen und pruefen, Kosten schaetzen, vorhandene Ressourcen auswaehlen, ROS-Stacks
verwalten und Ressourcen bereitstellen. Das Paket enthaelt eine gepruefte IaC Code Runtime; eine separate
IaC-Code-Installation ist nicht erforderlich.

## Download

[Aktuelle stabile iac-code-skill.zip herunterladen](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Diese feste URL verweist immer auf die aktuelle stabile Version. Automatische Installer koennen
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)
lesen, um Version, unveraenderliche URL, Groesse und SHA-256 zu erhalten und `skill.url` gegen `skill.sha256` zu pruefen.

## Installation

Der Agent muss lokale, mit `SKILL.md` definierte Skills unterstuetzen. Benoetigt werden CPython 3.8 bis 3.14 und beim
ersten Einsatz Netzwerkzugriff auf die Downloadadresse. Verwenden Sie unter macOS/Linux `python3`, unter Windows
`py -3`. Offizielle Runtimes gibt es fuer macOS auf Apple Silicon, Linux x86_64 und Windows x86_64. System und ABI
werden vor dem Download geprueft.

Entpacken Sie die ZIP-Datei in das vom Agenten dokumentierte Skill-Verzeichnis. Das Archiv enthaelt bereits
`iac-code/`:

```text
<Skill-Stammverzeichnis des Agenten>/
└── iac-code/
    ├── SKILL.md
    ├── agents/openai.yaml
    └── scripts/iac_code.py
```

Uebliche Speicherorte:

- **Codex**: `~/.agents/skills/iac-code/` fuer alle Projekte oder
  `<Repository>/.agents/skills/iac-code/` fuer ein Repository. Siehe
  [Codex-Skills-Dokumentation](https://developers.openai.com/codex/skills#where-codex-loads-local-skills).
- **Claude Code**: `~/.claude/skills/iac-code/` fuer alle Projekte oder
  `<Repository>/.claude/skills/iac-code/` fuer ein Repository. Siehe
  [Claude-Code-Skills-Dokumentation](https://code.claude.com/docs/en/skills#where-skills-live).

Starten Sie den Agenten neu oder oeffnen Sie eine neue Sitzung. Pruefen Sie die Runtime im entpackten Verzeichnis mit:

```bash
python3 scripts/iac_code.py ensure-runtime
```

In Windows PowerShell verwenden Sie `py -3 scripts\iac_code.py ensure-runtime`. Beim ersten Aufruf werden Groesse und
SHA-256 der passenden Runtime geprueft; spaetere Aufgaben nutzen die verifizierte lokale Kopie.

## Modell und Alibaba-Cloud-Identitaet konfigurieren

Der Skill verwendet standardmaessig `~/.iac-code/` und uebernimmt vorhandene Einstellungen aus REPL, Web- oder
Desktop-App. Ein anderes Verzeichnis wird mit `IAC_CODE_CONFIG_DIR` gewaehlt. Automatisierte Umgebungen sollten Modell-
und Cloud-Zugangsdaten aus einer Geheimnisverwaltung beziehen. Schreiben Sie sie nicht in `SKILL.md`, Prompts,
Projektdateien oder die Shell-Historie. Bevorzugen Sie temporaere Zugangsdaten, RAM-Rollen oder OAuth mit minimalen
Rechten. Details: [LLM-Anbieter](../configuration/llm-providers.md) und
[Alibaba-Cloud-Zugangsdaten](../configuration/alibaba-cloud-credentials.md).

## Arbeitsmodus waehlen

- Der **Normalmodus** ist der Standard fuer Abfragen und Aenderungen, Vorlagenarbeit, Fehleranalyse und die
  Bereitstellung eines klaren Ziels.
- Der **Pipeline-Modus** wird auf ausdruecklichen Wunsch oder fuer einen gefuehrten Ablauf mit Architekturvorschlaegen,
  Kostenvergleich, Bestaetigung und Bereitstellung verwendet.

Beschreiben Sie normalerweise nur das gewuenschte Ergebnis. Nennen Sie Pipeline, wenn Sie Loesungen vergleichen wollen.

## Erste Verwendung

Oeffnen Sie eine neue Sitzung im Host-Agenten und geben Sie beispielsweise ein:

```text
Pruefe mit iac-code die ROS-Vorlage in diesem Projekt. Liste Sicherheitsrisiken und Verbesserungen auf, ohne die Datei zu aendern.
```

Waehlen Sie den Skill in Codex mit `$iac-code` oder in Claude Code mit `/iac-code` explizit aus. Konfigurationspruefung und Runtime-Start erfolgen
automatisch; ein A2A Server muss nicht manuell gestartet werden. IaC Code kann pausieren, um Folgendes anzufordern:

- Freigabe oder Ablehnung einer Aktion (`permission`)
- Antwort auf eine Frage (`ask_user_question`)
- Auswahl einer Architektur (`candidate_selection`)
- Pruefung von Loesung, Preis und Parametern sowie Bestaetigen, Anpassen, Neuauswaehlen oder Abbrechen
  (`deployment_confirmation`)

Pruefen Sie Zielressourcen, Region, Auswirkungen und Preis. Ein urspruenglicher Bereitstellungswunsch genehmigt nicht
automatisch die spaetere Bestaetigung. Nach Abschluss koennen Sie in derselben Sitzung weiterarbeiten; der Kontext bleibt
erhalten. Fortschritt und Fragen werden auf Englisch, vereinfachtem Chinesisch, Spanisch, Franzoesisch, Deutsch,
Japanisch oder Portugiesisch ausgegeben.

## Aktualisieren und deinstallieren

Laden Sie fuer ein Update die stabile ZIP erneut herunter, ersetzen Sie `iac-code/` vollstaendig und starten Sie den
Agenten neu. Ersetzen Sie nicht nur das Bridge-Skript und aendern Sie weder Runtime-URL noch Digest. Zum Deinstallieren
loeschen Sie `iac-code/`. Die Runtime bleibt im Cache; pruefen Sie vor einer zusaetzlichen Bereinigung `cache list` und
verwenden Sie danach `cache clean ... --confirm`.

## Fehlerbehebung

- `llm_not_configured`: Vervollstaendigen Sie die Modellkonfiguration.
- `cloud_credentials_not_configured`: Hinterlegen Sie die fuer Pipeline erforderlichen Cloud-Zugangsdaten. Der
  Normalmodus kann Aufgaben ohne Cloud-API mit einer Warnung fortsetzen.
- `incompatible_host`: Fuehren Sie `ensure-runtime` aus und pruefen Sie Python, System, Architektur, Netzwerk und Proxy.
  Aktualisieren oder wechseln Sie den Host, statt die Pruefung zu umgehen.
- Pausierte Aufgabe: Sie wartet auf eine Antwort, Freigabe, Auswahl oder Bereitstellungsbestaetigung. Ist die
  Host-Sitzung nach einer Unterbrechung noch vorhanden, setzen Sie dieselbe Aufgabe fort.

Mit `python3 scripts/iac_code.py cache list` pruefen Sie den Cache. Alte Runtimes entfernen Sie mit
`cache clean --runtime-tag <tag> --confirm`, Kandidaten mit `cache clean --candidates --confirm`. Aktuelle und aktive
Runtimes sind geschuetzt.

## Sicherheit

- Die Runtime lauscht nur auf einem zufaelligen `127.0.0.1`-Port und nutzt pro Prozess einen neuen Bearer token.
- Ergebnisse bleiben im Workspace, gegebenenfalls unter `.iac-code-skill-results/`.
- Bereitschafts- und Freigabeanzeigen enthalten keine Werte von Zugangsdaten.

## Weitere Dokumentation

- [Ueberblick ueber offizielle IaC Code Skills](./skill-overview.md)
- [Referenz zur Host-Integration des IaC Code Skills](./skill-host-integration.md)
- [A2A-Protokolluebersicht](./overview.md)
- [Runtime-Konfiguration](../configuration/runtime-configuration.md)
