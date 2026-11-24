---
title: Instalacao
description: Instale o IaC Code e verifique o comando.
---

# Instalacao

## Instalação do Desktop

Para o aplicativo gráfico, instale o [pacote Desktop](../desktop-app.md) do seu sistema. O Python está incluído; não é necessário instalá-lo manualmente nem executar pip. O guia do Desktop apresenta os requisitos por plataforma.

## Instalar Web e headless

Para Web local ou automação por linha de comando, instale Python 3.10 ou posterior e o pacote abaixo. Essa opção oferece suporte a macOS, Linux e Windows.

```bash
pip install iac-code
```

Verifique o comando:

```bash
iac-code --help
```

Execute `iac-code` em um terminal interativo para iniciar Web e navegador, ou `iac-code web --no-open` para iniciar apenas o servidor. Continue pelo [início rápido](./quick-start.md).

## Recursos opcionais

A instalação padrão inclui as dependências do aplicativo web e do ambiente HTTP. O extra `[http]` permanece como um alias de compatibilidade; não é mais necessário instalá-lo separadamente. No repositório, basta executar `uv sync`.

Os transportes adicionais continuam usando extras opcionais, como `pip install 'iac-code[a2a-grpc]'` para gRPC ou `pip install 'iac-code[a2a-redis]'` para Redis.

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
