---
title: Installation
description: Installer IaC Code et vérifier la commande.
---

# Installation

## Installation de bureau

Pour une application graphique, installez le [paquet de bureau](../desktop-app.md) de votre système. Le runtime Python est inclus : aucune installation manuelle de Python ou exécution de pip n’est nécessaire. Le guide de bureau détaille les exigences par plateforme.

## Installation web et headless

Pour le web local ou l’automatisation en ligne de commande, installez Python 3.10 ou ultérieur puis le paquet ci-dessous. Cette méthode prend en charge macOS, Linux et Windows.

```bash
pip install iac-code
```

Vérifiez la commande :

```bash
iac-code --help
```

Exécutez `iac-code` dans un terminal interactif pour lancer le web et le navigateur, ou `iac-code web --no-open` pour lancer seulement le serveur. Poursuivez avec le [démarrage rapide](./quick-start.md).

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
