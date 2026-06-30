---
title: Nicht-interaktiver Modus
description: Einmalige Prompts aus Argumenten oder stdin ausführen.
---

# Nicht-interaktiver Modus

Der nicht-interaktive Modus führt einen einzelnen Prompt aus und beendet sich. Verwenden Sie ihn, wenn IaC Code eine Ausgabe für eine wiederholbare Aufgabe erzeugen soll, ohne in der REPL zu bleiben.

Verwenden Sie `--prompt`, um den Prompt direkt zu übergeben:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Verwenden Sie `--prompt -`, um den Prompt von der Standardeingabe zu lesen:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Verwenden Sie `--output-format`, wenn der Aufrufer strukturierte Ausgabe benötigt:

```bash
iac-code --prompt "Create an OSS Bucket" --output-format json
```

Verwenden Sie `--max-turns`, um die maximale Arbeitszeit des Agenten zu begrenzen:

```bash
iac-code --prompt "Create a VPC" --max-turns 20
```

Unterstützte Ausgabeformate sind:

| Format | Zweck |
|---|---|
| `text` | Für Menschen lesbare Ausgabe. Dies ist die Standardeinstellung. |
| `json` | Ein einzelnes JSON-Ergebnis für Aufrufer, die die endgültige Antwort parsen. |
| `stream-json` | Streaming-JSON-Ereignisse für Aufrufer, die inkrementellen Fortschritt verarbeiten. |

## Berechtigungssteuerung in der Automatisierung

Verwenden Sie beim nicht-interaktiven Ausführen `--permission-mode`, um zu steuern, wie der Agent Werkzeuggenehmigungen behandelt:

```bash
iac-code --prompt "Deploy the stack" --permission-mode bypass_permissions
```

In `bypass_permissions` werden Werkzeugaktionen ausser Sicherheitspruefungen automatisch genehmigt, aber jede Allow-Entscheidung, die einen Auditdatensatz erfordert, schlaegt weiterhin fail-closed fehl, wenn die Auditpersistenz fehlschlaegt. Alibaba-Cloud-Schreib-APIs bleiben ausserhalb von `bypass_permissions` gesondert geschuetzt; fuer enger gefasste vertrauenswuerdige Automatisierung bleiben Sie ausserhalb von `bypass_permissions` und erlauben jede benoetigte Schreib-API explizit:

```bash
iac-code --prompt "Deploy the stack" \
  --allowed-tools 'aliyun_api(ros:CreateStack)' \
  --permission-mode dont_ask
```

Um einzuschränken, was der Agent tun kann, kombinieren Sie `--allowed-tools` und `--disallowed-tools`:

```bash
iac-code --prompt "Check the stack status" \
  --allowed-tools 'bash(git *),bash(ls:*)' \
  --disallowed-tools 'bash(rm *)' \
  --permission-mode dont_ask
```

Alle Startparameter finden Sie unter [Befehlszeilenoptionen](../cli/command-line-options.md).
