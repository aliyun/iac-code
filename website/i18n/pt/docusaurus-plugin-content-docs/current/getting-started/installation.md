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
