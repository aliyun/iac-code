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

La instalación predeterminada incluye las dependencias de la aplicación web y del entorno HTTP. El extra `[http]` se conserva como alias de compatibilidad; ya no hace falta instalarlo por separado. Desde el repositorio basta con ejecutar `uv sync`.

Los transportes adicionales siguen usando extras opcionales, como `pip install 'iac-code[a2a-grpc]'` para gRPC o `pip install 'iac-code[a2a-redis]'` para Redis.

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
