---
title: Authentifizierung
description: Modell- und Cloud-Zugriff in den Einstellungen von Desktop oder Web einrichten.
---

# Authentifizierung

Öffnen Sie Desktop oder starten Sie Web mit `iac-code` in einem interaktiven Terminal. Richten Sie das Modell und erforderliche Cloud-Zugangsdaten in den Einstellungen ein.

- [Einen Anbieter in Desktop oder Web einrichten](./llm-providers.md)
- [Cloud-Zugriff in Desktop oder Web einrichten](./alibaba-cloud-credentials.md)

Web, Desktop und Headless-Aufrufe verwenden diese gespeicherten Zugangsdaten gemeinsam.

Die Laufzeitkonfiguration wird im Benutzerkonfigurationsverzeichnis gespeichert:

```text
~/.iac-code/
```

Wichtige Dateien umfassen:

| Datei | Zweck |
|---|---|
| `.credentials.yml` | LLM-Anbieter-Anmeldedaten |
| `.cloud-credentials.yml` | Alibaba Cloud-Anmeldedaten |
| `settings.yml` | Laufzeiteinstellungen |
