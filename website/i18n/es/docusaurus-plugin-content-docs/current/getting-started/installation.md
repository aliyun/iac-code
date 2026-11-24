---
title: Instalacion
description: Instalar IaC Code y verificar el comando.
---

# Instalacion

## Instalar escritorio

Para una aplicación gráfica, instale el [paquete de escritorio](../desktop-app.md) de su sistema. Incluye Python: no necesita instalarlo manualmente ni ejecutar pip. La guía de escritorio detalla los requisitos por plataforma.

## Instalar Web y headless

Para Web local o automatización por línea de comandos, instale Python 3.10 o posterior y después el paquete siguiente. Este método admite macOS, Linux y Windows.

```bash
pip install iac-code
```

Verifica el comando:

```bash
iac-code --help
```

Ejecute `iac-code` en un terminal interactivo para abrir Web y el navegador, o `iac-code web --no-open` para iniciar solo el servidor. Continúe con [Inicio rápido](./quick-start.md).

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
