---
title: Installation
description: Installer IaC Code et vérifier la commande.
---

# Installation

IaC Code nécessite Python 3.10 ou une version ultérieure. Il est compatible avec macOS, Linux et Windows.

## Installer

Installez le paquet depuis l'index de paquets Python configuré :

```bash
pip install iac-code
```

Vérifiez la commande :

```bash
iac-code --help
```

## Fonctionnalités optionnelles

L’installation par défaut comprend les dépendances de l’application web et de l’environnement HTTP. L’extra `[http]` reste un alias de compatibilité ; il n’est plus nécessaire de l’installer séparément. Depuis le dépôt, `uv sync` suffit.

Les transports supplémentaires utilisent toujours des extras facultatifs, par exemple `pip install 'iac-code[a2a-grpc]'` pour gRPC ou `pip install 'iac-code[a2a-redis]'` pour Redis.

## Configuration requise pour Windows

Sous Windows, [Git for Windows](https://gitforwindows.org/) doit être installé pour fournir l'environnement shell bash utilisé par l'exécution des outils.

Si Git Bash est installé mais n'est pas dans le PATH, définissez la variable d'environnement `IAC_CODE_GIT_BASH_PATH` avec le chemin de `bash.exe` :

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

Vous pouvez installer Git for Windows via winget :

```powershell
winget install --id Git.Git -e --source winget
```
