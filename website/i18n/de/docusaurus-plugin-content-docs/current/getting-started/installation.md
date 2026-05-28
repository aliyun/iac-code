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
