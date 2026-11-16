---
title: CLI-Uebersicht
description: Starten Sie IaC Code vom Terminal und waehlen Sie den richtigen Workflow.
---

# CLI-Uebersicht

Starten Sie `iac-code` in einem Terminal, um die lokale Web-App und den Browser zu öffnen. Konfigurieren Sie bei der ersten Verwendung den LLM-Anbieter und die Alibaba-Cloud-Zugangsdaten in den Einstellungen.

```bash
iac-code
iac-code web --no-open
```

Die Terminaloberfläche bleibt über `iac-code tui` erreichbar, erhält jedoch keine Updates und wird nicht mehr gewartet. Verwenden Sie künftig die Web-App oder die Desktop-App.

```bash
iac-code tui
iac-code tui --help
```

`iac-code` öffnet standardmäßig die Web-App. Terminaloptionen stehen hinter `iac-code tui`, zum Beispiel `iac-code tui --resume <id>`. Die Headless-Optionen `--prompt` und stream-json bleiben beim Hauptbefehl. Ohne interaktives Terminal starten Sie Web ausdrücklich mit `iac-code web --no-open`.

```bash
iac-code --prompt "Create an OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
