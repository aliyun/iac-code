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
| `AGENTS.md` | Benutzerspeicher, der als dauerhafte Anweisungen geladen wird |
| Verlaufsdateien | Eingabeverlauf für interaktive Workflows |

Vermeiden Sie es, Dateien aus diesem Verzeichnis zu committen oder zu teilen, da sie Geheimnisse oder lokale Einstellungen enthalten können.

## Speicherdateien

IaC Code hat zwei öffentliche Speicherorte:

| Speicherort | Zweck |
|---|---|
| `<project-root>/AGENTS.md` | Projektspeicher. Er kann committet werden, wenn die Anweisungen für alle Personen nützlich sind, die am Projekt arbeiten. |
| `<config-dir>/AGENTS.md` | Benutzerspeicher. Er folgt `IAC_CODE_CONFIG_DIR` und ist privat für den lokalen Benutzer. |

Setzen Sie `IAC_CODE_INSTRUCTION_MEMORY_FILE`, um einen anderen Dateinamen für den Anweisungsspeicher zu verwenden, zum Beispiel `IAC-CODE.md`.

Projektbezogene auto-memory-Themendateien werden hier gespeichert:

```text
<config-dir>/projects/<project-key>/memory/
```

`MEMORY.md` in diesem Ordner ist der Themenindex, den auto-memory-Side-Calls verwenden. Er wird nicht als dauerhafter Kontext geladen. Wenn auto-memory aktiviert ist, kann IaC Code relevante Themendateien auswählen und sie als versteckten Unterhaltungskontext hinzufügen.

## Projekteinstellungen

Zusätzlich zur benutzerbezogenen `~/.iac-code/settings.yml` lädt IaC Code projektbezogene Einstellungen aus dem aktuellen Arbeitsverzeichnis:

| Datei | Geltungsbereich |
|---|---|
| `.iac-code/settings.yml` | Gemeinsame Projekteinstellungen (sicher zu committen). |
| `.iac-code/settings.local.yml` | Lokale Überschreibungen (sollte in .gitignore stehen). |

Zusammenführungsreihenfolge: **Benutzereinstellungen → Projekteinstellungen → Lokale Projekteinstellungen → CLI-Argumente** (spätere Quellen überschreiben frühere).

## Anfragerichtlinie für Provider

Provider-Einträge in `settings.yml` können Felder für Anfragerichtlinien bei OpenAI-kompatiblen Providern enthalten. Diese Einstellungen sind nützlich, wenn ein Modell sichtbare Antwort-Tokens getrennt von Reasoning-/Thinking-Tokens behandelt.

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Feld | Geltungsbereich | Beschreibung |
|---|---|---|
| `thinkingBudget` | Provider oder Modell | Positives ganzzahliges Budget für Reasoning/Thinking, das an Provider übergeben wird, die es unterstützen. |
| `maxCompletionTokens` | Provider oder Modell | Positiver ganzzahliger Überschreibungswert für `max_completion_tokens` bei Providern/Modellen, die dieses Anfragefeld verwenden. |
| `effort` | Provider oder Modell | Optionale Überschreibung des Thinking-Aufwands; nur wirksam bei Modellen, die Effort-Steuerung unterstützen. |

Gültige modellbezogene Werte unter `providers.<provider>.models.<model>` überschreiben providerbezogene Werte. Ungültige numerische Werte werden ignoriert, sodass IaC Code auf den providerbezogenen Wert oder die eingebaute Modellrichtlinie zurückfällt.

Für Alibaba Cloud DashScope und DashScope Token Plan hat IaC Code ein eingebautes `thinkingBudget=8192` für `glm-5.2` und `kimi-k2.7-code`. Wenn `maxCompletionTokens` nicht gesetzt ist, wird das Anfrage-Limit aus dem normalen Antwort-Token-Limit plus dem wirksamen Thinking Budget berechnet.

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
