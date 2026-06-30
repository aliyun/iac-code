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

Sie können es verlegen, indem Sie die Umgebungsvariable `IAC_CODE_CONFIG_DIR` setzen (unterstützt `~`- und `$VAR`-Erweiterung). Sobald gesetzt, folgen alle persistierten Artefakte — Anmeldedaten, Einstellungen, Verlauf, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — dem neuen Speicherort. Start-/Debug-Logs liegen standardmaessig unter `<config-dir>/logs/` und koennen separat mit `IAC_CODE_LOG_DIR` verschoben werden; Berechtigungsauditdatensaetze bleiben unter `<config-dir>/logs/`.

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
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| Feld | Beschreibung |
|---|---|
| `mode` | Berechtigungsmodus: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Liste der automatisch genehmigten Werkzeug-Berechtigungsmuster. |
| `deny` | Liste der automatisch verweigerten Werkzeug-Berechtigungsmuster. |
| `ask` | Liste der Werkzeug-Berechtigungsmuster, die immer eine Bestätigung erfordern. |
| `additional_directories` | Zusätzliche Verzeichnisse über cwd hinaus, in die der Agent schreiben darf. |
| `audit` | Lokale Einstellungen fuer das Berechtigungsauditprotokoll. |

### Mustersyntax

Werkzeug-Berechtigungsmuster folgen dem Format `tool_name(rule)`:

| Muster | Bedeutung |
|---|---|
| `bash` | Alle Bash-Befehle abgleichen (bloßer Werkzeugname). |
| `bash(git *)` | Bash-Befehle abgleichen, die mit `git` beginnen. |
| `bash(curl:*)` | Bash-Befehle abgleichen, die mit `curl` beginnen. |
| `write_file` | Alle write_file-Werkzeugaufrufe abgleichen. |
| `aliyun_api(ros:CreateStack)` | Ein Alibaba-Cloud-API-Produkt/Aktion-Paar abgleichen. |

Regeln werden in folgender Reihenfolge ausgewertet: **deny → ask → allow → Standardverhalten**. CLI-Argumente (`--allowed-tools`, `--disallowed-tools`) haben die höchste Priorität.

### Alibaba-Cloud-API-Berechtigungen

`aliyun_api` unterscheidet reine Lese-API-Aufrufe von Aufrufen, die Cloud-Ressourcen veraendern koennen. Nur-Lese-API-Aktionen werden automatisch erlaubt. Nicht nur lesende API-Aufrufe erfordern eine Bestaetigung oder eine exakte Allow-Regel fuer das jeweilige Produkt und die jeweilige Aktion, zum Beispiel:

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

Eine blosse `aliyun_api`-Allow-Regel genehmigt Alibaba-Cloud-Schreib-APIs nicht pauschal. Ausserhalb von `bypass_permissions` muessen Allow-Regeln fuer Schreibzugriffe exakt zum kanonischen `product:action`-Paar passen. Im Modus `bypass_permissions` werden geschuetzte Alibaba-Cloud-Schreib-APIs automatisch genehmigt, aber jede Allow-Entscheidung, die einen Auditdatensatz erfordert, schlaegt weiterhin fail-closed fehl, wenn die Auditpersistenz fehlschlaegt. Wildcards koennen weiterhin fuer deny- oder ask-Regeln sowie fuer Nur-Lese-Regelabgleiche nuetzlich sein.

ROA-artige Requests gelten nur dann als nur lesend, wenn die Methode `GET` ist und der Request keinen Body hat. Nicht nur lesende ROA-Requests folgen derselben Anforderung an eine exakte kanonische `product:action`-Allow-Regel wie RPC-artige Schreib-APIs: Eine exakte Regel wie `aliyun_api(cs:CreateCluster)` kann den Schreibzugriff genehmigen, waehrend Wildcard-Allow-Regeln nicht nur lesende Aufrufe weiterhin nicht genehmigen.

### Berechtigungsauditprotokoll

Berechtigungsentscheidungen, die Benutzerabfragen, Tool-Cache-Grenzen, Automatisierungsgenehmigungen oder Resolver-Genehmigungen ueberschreiten, werden angehaengt an:

```text
<config-dir>/logs/permission-audit.jsonl
```

Standardmaessig ist dies `~/.iac-code/logs/permission-audit.jsonl`. Das Berechtigungsauditlog folgt `IAC_CODE_CONFIG_DIR`; `IAC_CODE_LOG_DIR` verschiebt nur Start-/Debug-Logs. Der Audit-Writer haengt JSONL-Datensaetze mit Dateisperren an, rotiert die Datei und schraenkt lokale Dateiberechtigungen ein, soweit das Betriebssystem dies unterstuetzt. Routinemaessige automatische Nur-Lese-Genehmigungen koennen ausgelassen werden, aber Ablehnungen, Prompts, gecachte Entscheidungen, Automatisierungsgenehmigungen, Resolver-Genehmigungen und andere auditierte Berechtigungsgrenzen werden aufgezeichnet.

Audit-Einstellungen werden unter `permissions.audit` konfiguriert:

| Feld | Standard | Beschreibung |
|---|---:|---|
| `include_tool_input` | `false` | Nur die Form der Tool-Eingabe in JSONL-Auditdatensaetze aufnehmen. String-Werte werden als Typ, Laenge und Fingerprint gespeichert; geheimnisverdaechtige Schluessel werden redigiert; Feldnamen ausserhalb der Whitelist koennen als Fingerprint erscheinen; rohe fachliche Payload-Strings werden nicht geschrieben. Alibaba-Cloud-API-Eintraege behalten zusaetzlich eine sichere Operationszusammenfassung. |
| `max_file_bytes` | `10485760` | `permission-audit.jsonl` rotieren, wenn diese Groesse ueberschritten wird. |
| `max_files` | `5` | Anzahl der aufzubewahrenden rotierten Auditdateien. Werte ueber dem eingebauten Maximum werden begrenzt. |

Wenn eine Allow-Entscheidung, die einen Auditdatensatz erfordert, nicht im Auditprotokoll persistiert werden kann, verhaelt sich IaC Code fail-closed und lehnt die Aktion ab, statt sie ohne Auditspur auszufuehren.
