---
title: Konfiguration
description: Laufzeitkonfigurationsreihenfolge und lokale Dateien.
---

# Konfiguration

IaC Code liest die Konfiguration aus CLI-Argumenten, Umgebungsvariablen und Dateien im Laufzeitkonfigurationsverzeichnis.

Konfigurationspriorität:

```text
CLI-Argumente > Umgebungsvariablen > Konfigurationsdateien
```

Das Laufzeitverzeichnis ist standardmäßig:

```text
~/.iac-code/
```

Sie können es verlegen, indem Sie die Umgebungsvariable `IAC_CODE_CONFIG_DIR` setzen (unterstützt `~`- und `$VAR`-Erweiterung). Sobald gesetzt, folgen alle persistierten Artefakte — Anmeldedaten, Einstellungen, Verlauf, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — dem neuen Speicherort.

Häufige Dateien:

| Datei | Beschreibung |
|---|---|
| `.credentials.yml` | LLM-Anmeldedaten |
| `.cloud-credentials.yml` | Cloud-Anbieter-Anmeldedaten |
| `settings.yml` | Ausgewählter Anbieter, Modell und zugehörige Einstellungen |
| Verlaufsdateien | Eingabeverlauf für interaktive Workflows |

Vermeiden Sie es, Dateien aus diesem Verzeichnis zu committen oder zu teilen, da sie Geheimnisse oder lokale Einstellungen enthalten können.

## Projekteinstellungen

Zusätzlich zur benutzerbezogenen `~/.iac-code/settings.yml` lädt IaC Code projektbezogene Einstellungen aus dem aktuellen Arbeitsverzeichnis:

| Datei | Geltungsbereich |
|---|---|
| `.iac-code/settings.yml` | Gemeinsame Projekteinstellungen (sicher zu committen). |
| `.iac-code/settings.local.yml` | Lokale Überschreibungen (sollte in .gitignore stehen). |

Zusammenführungsreihenfolge: **Benutzereinstellungen → Projekteinstellungen → Lokale Projekteinstellungen → CLI-Argumente** (spätere Quellen überschreiben frühere).

## Werkzeug-Berechtigungskonfiguration

Der Abschnitt `permissions` in `settings.yml` konfiguriert, welche Werkzeugaktionen erlaubt, verweigert oder bestätigt werden müssen:

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| Feld | Beschreibung |
|---|---|
| `mode` | Berechtigungsmodus: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Liste der automatisch genehmigten Werkzeug-Berechtigungsmuster. |
| `deny` | Liste der automatisch verweigerten Werkzeug-Berechtigungsmuster. |
| `ask` | Liste der Werkzeug-Berechtigungsmuster, die immer eine Bestätigung erfordern. |
| `additional_directories` | Zusätzliche Verzeichnisse über cwd hinaus, in die der Agent schreiben darf. |

### Mustersyntax

Werkzeug-Berechtigungsmuster folgen dem Format `tool_name(rule)`:

| Muster | Bedeutung |
|---|---|
| `bash` | Alle Bash-Befehle abgleichen (bloßer Werkzeugname). |
| `bash(git *)` | Bash-Befehle abgleichen, die mit `git` beginnen. |
| `bash(curl:*)` | Bash-Befehle abgleichen, die mit `curl` beginnen. |
| `write_file` | Alle write_file-Werkzeugaufrufe abgleichen. |

Regeln werden in folgender Reihenfolge ausgewertet: **deny → ask → allow → Standardverhalten**. CLI-Argumente (`--allowed-tools`, `--disallowed-tools`) haben die höchste Priorität.
