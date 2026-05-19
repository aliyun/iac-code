---
title: Configuração
description: Ordem de configuração em tempo de execução e arquivos locais.
---

# Configuração

O IaC Code lê a configuração a partir de argumentos CLI, variáveis de ambiente e arquivos no diretório de configuração em tempo de execução.

Precedência de configuração:

```text
Argumentos CLI > variáveis de ambiente > arquivos de configuração
```

O diretório de tempo de execução padrão é:

```text
~/.iac-code/
```

Você pode realocá-lo definindo a variável de ambiente `IAC_CODE_CONFIG_DIR` (suporta expansão de `~` e `$VAR`). Quando definida, todos os artefatos persistidos — credenciais, configurações, histórico, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — seguem o novo local.

Arquivos comuns:

| Arquivo | Descrição |
|---|---|
| `.credentials.yml` | Credenciais LLM |
| `.cloud-credentials.yml` | Credenciais do provedor de nuvem |
| `settings.yml` | Provedor selecionado, modelo e configurações relacionadas |
| history files | Histórico de entrada para fluxos de trabalho interativos |

Evite fazer commit ou compartilhar arquivos deste diretório porque eles podem conter segredos ou preferências locais.

## Configurações do projeto

Além do `~/.iac-code/settings.yml` no nível do usuário, o IaC Code carrega configurações no nível do projeto a partir do diretório de trabalho atual:

| Arquivo | Escopo |
|---|---|
| `.iac-code/settings.yml` | Configurações compartilhadas do projeto (seguro para commit). |
| `.iac-code/settings.local.yml` | Substituições locais (deve estar no .gitignore). |

Ordem de mesclagem: **configurações do usuário → configurações do projeto → configurações locais do projeto → argumentos CLI** (fontes posteriores substituem as anteriores).

## Configuração de permissões de ferramentas

A seção `permissions` em `settings.yml` configura quais ações de ferramentas são permitidas, negadas ou requerem confirmação:

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| Campo | Descrição |
|---|---|
| `mode` | Modo de permissão: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Lista de padrões de permissão de ferramentas para aprovação automática. |
| `deny` | Lista de padrões de permissão de ferramentas para negação automática. |
| `ask` | Lista de padrões de permissão de ferramentas que sempre requerem confirmação. |
| `additional_directories` | Diretórios adicionais além do cwd nos quais o agente pode escrever. |

### Sintaxe de padrões

Os padrões de permissão de ferramentas seguem o formato `tool_name(rule)`:

| Padrão | Significado |
|---|---|
| `bash` | Corresponder a todos os comandos bash (nome de ferramenta simples). |
| `bash(git *)` | Corresponder a comandos bash que começam com `git`. |
| `bash(curl:*)` | Corresponder a comandos bash que começam com `curl`. |
| `write_file` | Corresponder a todas as chamadas da ferramenta write_file. |

As regras são avaliadas na ordem: **deny → ask → allow → comportamento padrão**. Os argumentos CLI (`--allowed-tools`, `--disallowed-tools`) têm a maior precedência.
