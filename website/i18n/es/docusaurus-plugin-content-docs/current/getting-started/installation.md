---
title: Instalacion
description: Instalar IaC Code y verificar el comando.
---

# Instalacion

IaC Code requiere Python 3.10 o posterior. Es compatible con macOS, Linux y Windows.

## Instalar

Instala el paquete desde el indice de paquetes de Python configurado:

```bash
pip install iac-code
```

Verifica el comando:

```bash
iac-code --help
```

## Funciones opcionales

La CLI interactiva funciona con el paquete base. Algunos modos de ejecución dependen de extras opcionales que instalas con la sintaxis `iac-code[<extra>]`:

| Extra | Habilita | Comando |
|---|---|---|
| `http` | La [aplicación web](../web-app.md) local (`iac-code web`) | `pip install 'iac-code[http]'` |
| `a2a` | El [servidor A2A](../a2a/getting-started.md) (`iac-code a2a`) | `pip install 'iac-code[a2a]'` |

Si inicias un modo de ejecución sin su extra, el comando falla con un mensaje que indica el extra que debes instalar. Cuando trabajas desde un clon del repositorio, usa `uv sync --extra <extra>`.

## Requisitos de Windows

En Windows, se debe instalar [Git for Windows](https://gitforwindows.org/) para proporcionar el entorno de shell bash utilizado por la ejecucion de herramientas.

Si Git Bash esta instalado pero no se encuentra en el PATH, configure la variable de entorno `IAC_CODE_GIT_BASH_PATH` con la ruta de `bash.exe`:

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

Puede instalar Git for Windows mediante winget:

```powershell
winget install --id Git.Git -e --source winget
```
