---
title: Authentifizierung
description: Konfigurieren Sie LLM- und Cloud-Anmeldedaten mit dem Authentifizierungsablauf.
---

# Authentifizierung

Starten Sie `iac-code` in einem Terminal, um die lokale Web-App und den Browser zu öffnen. Konfigurieren Sie bei der ersten Verwendung den LLM-Anbieter und die Alibaba-Cloud-Zugangsdaten in den Einstellungen.

- **Einstellungen → Modelle**: Anbieter auswählen, API-Schlüssel eingeben und Modell auswählen.
- **Einstellungen → Cloud-Anmeldedaten**: Alibaba Cloud, Authentifizierungsmethode und Region auswählen.

Web, Desktop und Headless-Aufrufe verwenden diese gespeicherten Zugangsdaten gemeinsam.

Für die Einrichtung im Terminal starten Sie `iac-code tui` und führen `/auth` aus.

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
