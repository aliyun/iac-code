---
title: CLI-Uebersicht
description: Starten Sie IaC Code vom Terminal und waehlen Sie den richtigen Workflow.
---

# CLI-Uebersicht

Starten Sie `iac-code` in einem Terminal, um die lokale Web-App und den Browser zu öffnen. Richten Sie beim ersten Start den LLM-Anbieter und die Alibaba-Cloud-Zugangsdaten in den Einstellungen ein.

```bash
iac-code
iac-code web --no-open
```

Wählen Sie beim Erstellen einer Unterhaltung in Web oder Desktop normalen Chat oder Pipeline. Für Automatisierung verwenden Sie einen einzelnen Prompt oder einen stream-json-Unterprozess:

```bash
iac-code --prompt "Eine VPC-Vorlage erzeugen"
iac-code --input-format stream-json --output-format stream-json
```

Siehe [Kommandozeilenoptionen](./command-line-options.md) und [Headless-Automatisierung](../automation/non-interactive-mode.md). Ohne interaktives Terminal starten Sie Web ausdrücklich mit `iac-code web --no-open`.

Für bestehende Terminalabläufe siehe [bisherige Terminaloberfläche](./interactive-mode.md).
