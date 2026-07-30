---
title: Instalacao
description: Instale o IaC Code e verifique o comando.
---

# Instalacao

O IaC Code requer Python 3.10 ou posterior. É compatível com macOS, Linux e Windows.

## Instalar

Instale o pacote a partir do indice de pacotes Python configurado:

```bash
pip install iac-code
```

Verifique o comando:

```bash
iac-code --help
```

## Recursos opcionais

A CLI interativa funciona com o pacote base. Alguns modos de execução dependem de extras opcionais que você instala com a sintaxe `iac-code[<extra>]`:

| Extra | Habilita | Comando |
|---|---|---|
| `http` | O [aplicativo web](../web-app.md) local (`iac-code web`) | `pip install 'iac-code[http]'` |
| `a2a` | O [servidor A2A](../a2a/getting-started.md) (`iac-code a2a`) | `pip install 'iac-code[a2a]'` |

Se você iniciar um modo de execução sem o extra correspondente, o comando falha com uma mensagem indicando o extra a instalar. Ao trabalhar a partir de um clone do repositório, use `uv sync --extra <extra>`.

## Requisitos do Windows

No Windows, o [Git for Windows](https://gitforwindows.org/) deve estar instalado para fornecer o ambiente de shell bash utilizado pela execução de ferramentas.

Se o Git Bash estiver instalado mas não estiver no PATH, defina a variável de ambiente `IAC_CODE_GIT_BASH_PATH` com o caminho do `bash.exe`:

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

Você pode instalar o Git for Windows via winget:

```powershell
winget install --id Git.Git -e --source winget
```
