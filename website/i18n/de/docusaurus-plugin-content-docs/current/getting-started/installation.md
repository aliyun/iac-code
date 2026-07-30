---
title: Installation
description: Installieren Sie IaC Code und ueberpruefen Sie den Befehl.
---

# Installation

IaC Code erfordert Python 3.10 oder neuer. Es unterstützt macOS, Linux und Windows.

## Installieren

Installieren Sie das Paket aus dem konfigurierten Python-Paketindex:

```bash
pip install iac-code
```

Ueberpruefen Sie den Befehl:

```bash
iac-code --help
```

## Optionale Funktionen

Die interaktive CLI funktioniert mit dem Basispaket. Einige Ausführungsmodi hängen von optionalen Extras ab, die Sie mit der Syntax `iac-code[<extra>]` installieren:

| Extra | Aktiviert | Befehl |
|---|---|---|
| `http` | Die lokale [Web-App](../web-app.md) (`iac-code web`) | `pip install 'iac-code[http]'` |
| `a2a` | Den [A2A-Server](../a2a/getting-started.md) (`iac-code a2a`) | `pip install 'iac-code[a2a]'` |

Wenn Sie einen Ausführungsmodus ohne das zugehörige Extra starten, schlägt der Befehl mit einer Meldung fehl, die das zu installierende Extra nennt. Bei der Arbeit mit einem Checkout des Repositorys verwenden Sie stattdessen `uv sync --extra <extra>`.

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
