---
title: Authentifizierung
description: Konfigurieren Sie LLM- und Cloud-Anmeldedaten mit dem Authentifizierungsablauf.
---

# Authentifizierung

Starten Sie `iac-code` in einem Terminal, um die lokale Web-App und den Browser zu öffnen. Konfigurieren Sie bei der ersten Verwendung den LLM-Anbieter und die Alibaba-Cloud-Zugangsdaten in den Einstellungen.

Die Terminaloberfläche bleibt über `iac-code tui` erreichbar, erhält jedoch keine Updates und wird nicht mehr gewartet. Verwenden Sie künftig die Web-App oder die Desktop-App.

Verwenden Sie `/auth` im interaktiven Modus, um sowohl den Zugang zum Modellanbieter als auch den Alibaba Cloud-Zugang zu konfigurieren.

```bash
iac-code tui
```

```text
/auth
```

Der Authentifizierungsablauf fuehrt Sie durch die Anbieterauswahl, API-Schluessel-Eingabe, Modellauswahl und die Einrichtung der Alibaba Cloud-Anmeldedaten.

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
