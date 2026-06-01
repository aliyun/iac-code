---
title: Contribuir
description: Como configurar um ambiente local e contribuir para o IaC Code.
---

# Contribuir

## Pré-requisitos

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Configuração

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` instala todas as dependências e configura os hooks de pre-commit (verificações de lint e formato em cada commit).

## Fluxo de desenvolvimento

Executar em modo de depuração:

```bash
make dev
```

Executar testes:

```bash
make test           # versão padrão do Python
make test PY=3.12   # versão específica
make test PY=all    # todas as versões suportadas (3.10–3.14)
```

Qualidade do código:

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

Cobertura:

```bash
make coverage
```

## Estrutura do projeto

```
src/iac_code/       # código-fonte
tests/              # testes
website/            # site de documentação (Docusaurus)
```

## Enviar alterações

1. Faça fork do repositório e crie uma branch de funcionalidade.
2. Faça suas alterações com testes.
3. Execute `make format`, depois certifique-se de que `make lint` e `make test` passem.
4. Abra um pull request para `main`.
