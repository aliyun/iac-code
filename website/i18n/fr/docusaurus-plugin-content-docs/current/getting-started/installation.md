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

La CLI interactive fonctionne avec le paquet de base. Certains modes d'exécution dépendent d'extras optionnels que vous installez avec la syntaxe `iac-code[<extra>]` :

| Extra | Active | Commande |
|---|---|---|
| `http` | L'[application web](../web-app.md) locale (`iac-code web`) | `pip install 'iac-code[http]'` |
| `a2a` | Le [serveur A2A](../a2a/getting-started.md) (`iac-code a2a`) | `pip install 'iac-code[a2a]'` |

Si vous lancez un mode d'exécution sans son extra, la commande échoue avec un message indiquant l'extra à installer. Lorsque vous travaillez à partir d'un clone du dépôt, utilisez plutôt `uv sync --extra <extra>`.

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
