---
title: Installation
description: Installieren Sie IaC Code und ueberpruefen Sie den Befehl.
---

# Installation

## Desktop installieren

Installieren Sie für die grafische Anwendung das [Desktop-Paket](../desktop-app.md) Ihres Betriebssystems. Python ist enthalten; eine manuelle Python-Installation oder pip ist nicht erforderlich. Die Desktop-Anleitung beschreibt Plattformvoraussetzungen.

## Web und Headless installieren

Installieren Sie für lokales Web oder Kommandozeilenautomatisierung Python 3.10 oder neuer und danach das folgende Paket. Dieser Weg unterstützt macOS, Linux und Windows.

```bash
pip install iac-code
```

Ueberpruefen Sie den Befehl:

```bash
iac-code --help
```

`iac-code` startet im interaktiven Terminal Web und Browser. `iac-code web --no-open` startet nur den Server. Weiter geht es unter [Schnellstart](./quick-start.md).

## Optionale Funktionen

Die Standardinstallation enthält die Abhängigkeiten der Web-App und der HTTP-Laufzeit. Das Extra `[http]` bleibt als Kompatibilitätsalias erhalten und muss nicht mehr separat installiert werden. Im ausgecheckten Repository genügt `uv sync`.

Zusätzliche Transportarten verwenden weiterhin optionale Extras, etwa `pip install 'iac-code[a2a-grpc]'` für gRPC oder `pip install 'iac-code[a2a-redis]'` für Redis.

## Windows-Anforderungen

Unter Windows muss [Git for Windows](https://gitforwindows.org/) installiert sein, um die bash-Shell-Umgebung für die Werkzeugausführung bereitzustellen.

Wenn Git Bash installiert, aber nicht im PATH ist, setzen Sie die Umgebungsvariable `IAC_CODE_GIT_BASH_PATH` auf den Pfad von `bash.exe`:

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

Sie können Git for Windows über winget installieren:

```powershell
winget install --id Git.Git -e --source winget
```
