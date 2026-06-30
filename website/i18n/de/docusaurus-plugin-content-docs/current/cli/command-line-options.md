---
title: Befehlszeilenoptionen
description: Referenz für IaC Code Startoptionen und Einmal-Ausführungsparameter.
---

# Befehlszeilenoptionen

Befehlszeilenoptionen steuern, wie IaC Code gestartet wird. Sie können vor dem Betreten der interaktiven REPL verwendet oder mit `--prompt` für einmalige Automatisierung kombiniert werden.

| Option | Zweck |
|---|---|
| `-h`, `--help` | CLI-Hilfe anzeigen und beenden. Verwenden Sie dies, um die unterstützten Optionen Ihrer installierten Version zu prüfen. |
| `-v`, `-V`, `--version` | Installierte IaC Code Version ausgeben und beenden. |
| `-m <model>`, `--model <model>` | Mit einem bestimmten LLM-Modell starten. Dies überschreibt das gespeicherte Modell für den aktuellen Lauf. |
| `-p <prompt>`, `--prompt <prompt>` | Einen einzelnen Prompt ausführen und beenden. Dies aktiviert den nicht-interaktiven Modus. Verwenden Sie `--prompt -`, um den Prompt von der Standardeingabe zu lesen. |
| `--output-format <format>` | Ausgabeformat für den nicht-interaktiven Modus festlegen. Unterstützte Werte sind `text`, `json` und `stream-json`. Standard ist `text`. |
| `--max-turns <number>` | Maximale Anzahl der Agenten-Runden im nicht-interaktiven Modus begrenzen. Standard ist `100`. |
| `-d`, `--debug` | Debug-Protokollierung für den aktuellen Lauf aktivieren. Im interaktiven Modus verwenden Sie `/debug`, um die Debug-Protokollierung nach dem Start zu prüfen oder zu ändern. |
| `-r <session-id-oder-name>`, `--resume <session-id-oder-name>` | Eine vorherige Sitzung über die exakte Sitzungs-ID, ein eindeutiges ID-Präfix oder einen eindeutigen Sitzungsnamen fortsetzen. Projektübergreifend aufgelöste Sitzungen geben einen `cd ... && iac-code --resume <id>`-Befehl aus, statt das aktuelle Projekt direkt zu wechseln. |
| `-c`, `--continue` | Die letzte Sitzung fortsetzen. Kann nicht zusammen mit `--resume` verwendet werden. |
| `--allowed-tools <patterns>` | Kommagetrennte Werkzeug-Berechtigungsmuster zum Erlauben, z.B. `'bash(git *),write_file'`. |
| `--disallowed-tools <patterns>` | Kommagetrennte Werkzeug-Berechtigungsmuster zum Verweigern, z.B. `'bash(rm *)'`. |
| `--permission-mode <mode>` | Berechtigungsmodus: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |

## Berechtigungsmodi

Der Parameter `--permission-mode` steuert, wie der Agent Werkzeug-Berechtigungsprüfungen behandelt:

| Modus | Verhalten |
|---|---|
| `default` | Der Agent fragt nach Bestätigung, wenn eine Werkzeugaktion eine Genehmigung erfordert. |
| `accept_edits` | Dateisystem-Befehle, die als Bearbeitungen gelten (z.B. `mkdir`, `cp`), automatisch genehmigen. Andere Aktionen erfordern weiterhin Bestätigung. |
| `bypass_permissions` | Werkzeugaktionen ausser Sicherheitspruefungen automatisch genehmigen. Jede Allow-Entscheidung, die einen Auditdatensatz erfordert, schlaegt fail-closed fehl, wenn die Auditpersistenz fehlschlaegt. Fuer vertrauenswuerdige Automatisierung vorgesehen. |
| `dont_ask` | Jede Aktion, die normalerweise eine Bestätigung erfordern würde, stillschweigend ablehnen. Nützlich für strenge Nur-Lese-Läufe. |

Alibaba-Cloud-Schreib-API-Aufrufe ueber `aliyun_api` sind gesondert geschuetzt: Eine blosse `aliyun_api`-Allow-Regel genehmigt sie nicht pauschal, und ausserhalb von `bypass_permissions` muessen Allow-Regeln fuer Schreibzugriffe exakt zum kanonischen `product:action`-Paar passen. `bypass_permissions` genehmigt sie automatisch, aber wie bei anderen auditierten Allow-Entscheidungen wird die Aktion abgelehnt, wenn der Berechtigungsauditdatensatz nicht persistiert werden kann. Verwenden Sie exakte Regeln wie `aliyun_api(ros:CreateStack)`, wenn vertrauenswuerdige Automatisierung nur eine bestimmte Schreib-API erlauben soll.

## Häufige Startbefehle

Die interaktive REPL mit dem gespeicherten Modell starten:

```bash
iac-code
```

Für diesen Lauf ein bestimmtes Modell angeben:

```bash
iac-code --model qwen3.6-plus
```

Einen einmaligen Prompt ausführen:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Prompt von der Standardeingabe lesen:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Die letzte Sitzung fortsetzen:

```bash
iac-code --continue
```

Nur git und schreibgeschützte Bash-Befehle erlauben:

```bash
iac-code --allowed-tools 'bash(git *)'
```

Eine bestimmte Alibaba-Cloud-Schreib-API erlauben:

```bash
iac-code --allowed-tools 'aliyun_api(ros:CreateStack)'
```

In der Automatisierung ohne interaktive Abfragen ausführen:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
