---
title: Befehlszeilenoptionen
description: Referenz für IaC Code Startoptionen und Einmal-Ausführungsparameter.
---

# Befehlszeilenoptionen

Der Hauptbefehl startet standardmäßig Web. Die folgenden Optionen gelten für Web-Start, einmalige Headless-Aufrufe und stream-json-Unterprozesse. Interaktive Unterhaltungen konfigurieren Sie in Web oder Desktop.

Die Einstiegsspalte zeigt, wo jede Option verwendet werden kann. `headless` steht für `iac-code --prompt ...`; `process` bezeichnet den stream-json-Unterprozessmodus. Modell und Berechtigungen für Web werden in der Oberfläche eingestellt.

| Option | Zweck | Einstieg |
|---|---|---|
| `-h`, `--help` | CLI-Hilfe anzeigen und beenden. Verwenden Sie dies, um die unterstützten Optionen Ihrer installierten Version zu prüfen. | `iac-code`, `web` |
| `-v`, `-V`, `--version` | Installierte IaC Code Version ausgeben und beenden. | `iac-code` |
| `-m <model>`, `--model <model>` | Mit einem bestimmten LLM-Modell starten. Dies überschreibt das gespeicherte Modell für den aktuellen Lauf. | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | Einen einzelnen Prompt ausführen und beenden. Dies aktiviert den nicht-interaktiven Modus. Verwenden Sie `--prompt -`, um den Prompt von der Standardeingabe zu lesen. | `iac-code` |
| `--output-format <format>` | Ausgabeformat für den nicht-interaktiven Modus festlegen. Unterstützte Werte sind `text`, `json` und `stream-json`. Standard ist `text`. | `headless`, `process` |
| `--max-turns <number>` | Maximale Anzahl der Agenten-Runden im nicht-interaktiven Modus begrenzen. Standard ist `100`. | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | Steuert, ob einmalige nicht-interaktive Anfragen Thinking explizit aktivieren. Standard ist `--thinking-enabled`; verwenden Sie `--no-thinking-enabled`, um fuer diesen Lauf `thinking_enabled=false` zu senden, ohne `settings.yml` umzuschreiben. | `headless` |
| `-d`, `--debug` | Debug-Protokollierung für diesen Lauf aktivieren. | `headless`, `process`, `web` |
| `--verbose` | Headless-Fortschritt (nicht-interaktiv) auf der Standardfehlerausgabe anzeigen. | `headless` |
| `-r <session-id-oder-name>`, `--resume <session-id-oder-name>` | Anhand genauer Sitzungs-ID, eindeutigem ID-Präfix oder eindeutigem Namen fortsetzen. Headless-Befehle im Projektverzeichnis der Sitzung ausführen. | `headless` |
| `-c`, `--continue` | Die letzte Sitzung fortsetzen. Kann nicht zusammen mit `--resume` verwendet werden. | `headless` |
| `--allowed-tools <patterns>` | Kommagetrennte Werkzeug-Berechtigungsmuster zum Erlauben, z.B. `'bash(git *),write_file'`. | `headless`, `process` |
| `--disallowed-tools <patterns>` | Kommagetrennte Werkzeug-Berechtigungsmuster zum Verweigern, z.B. `'bash(rm *)'`. | `headless`, `process` |
| `--permission-mode <mode>` | Berechtigungsmodus: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. | `headless`, `process` |

## Berechtigungsmodi

Der Parameter `--permission-mode` steuert, wie der Agent Werkzeug-Berechtigungsprüfungen behandelt:

| Modus | Verhalten |
|---|---|
| `default` | Der Agent fragt nach Bestätigung, wenn eine Werkzeugaktion eine Genehmigung erfordert. |
| `accept_edits` | Dateisystem-Befehle, die als Bearbeitungen gelten (z.B. `mkdir`, `cp`), automatisch genehmigen. Andere Aktionen erfordern weiterhin Bestätigung. |
| `bypass_permissions` | Werkzeugaktionen ausser Sicherheitspruefungen automatisch genehmigen. Jede Allow-Entscheidung, die einen Auditdatensatz erfordert, schlaegt fail-closed fehl, wenn die Auditpersistenz fehlschlaegt. Fuer vertrauenswuerdige Automatisierung vorgesehen. |
| `dont_ask` | Aktionen ablehnen, die eine Genehmigung benötigen, sofern keine ausdrückliche Erlaubnisregel greift. |

Alibaba-Cloud-Schreib-API-Aufrufe ueber `aliyun_api` sind gesondert geschuetzt: Eine blosse `aliyun_api`-Allow-Regel genehmigt sie nicht pauschal, und ausserhalb von `bypass_permissions` muessen Allow-Regeln fuer Schreibzugriffe exakt zum kanonischen `product:action`-Paar passen. `bypass_permissions` genehmigt sie automatisch, aber wie bei anderen auditierten Allow-Entscheidungen wird die Aktion abgelehnt, wenn der Berechtigungsauditdatensatz nicht persistiert werden kann. Verwenden Sie exakte Regeln wie `aliyun_api(ros:CreateStack)`, wenn vertrauenswuerdige Automatisierung nur eine bestimmte Schreib-API erlauben soll.

## Häufige Startbefehle

Die lokale Web-App starten:

```bash
iac-code
```

Für diesen Lauf ein bestimmtes Modell angeben:

```bash
iac-code --prompt "Ein VPC erstellen" --model qwen3.7-max
```

Einen einmaligen Prompt ausführen:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Einen einmaligen Prompt mit fuer diese Anfrage deaktiviertem Thinking ausfuehren:

```bash
iac-code --prompt "Create an OSS Bucket" --no-thinking-enabled
```

Prompt von der Standardeingabe lesen:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Die neueste Sitzung mit einem einzelnen Prompt fortsetzen:

```bash
iac-code --continue --prompt "Eine VPC-Vorlage erzeugen"
```

Bash-Befehle passend zu `git *` vorab genehmigen:

```bash
iac-code --prompt "Den Git-Status anzeigen" --allowed-tools 'bash(git *)'
```

Eine bestimmte Alibaba-Cloud-Schreib-API vorab genehmigen:

```bash
iac-code --prompt "Den Stack bereitstellen" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

In der Automatisierung ohne interaktive Abfragen ausführen:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
