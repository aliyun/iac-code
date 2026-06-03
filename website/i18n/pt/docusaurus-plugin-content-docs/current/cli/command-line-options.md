---
title: Opções de linha de comando
description: Referência para as opções de inicialização e execução única do IaC Code.
---

# Opções de linha de comando

As opções de linha de comando alteram como o IaC Code é iniciado. Use-as antes de entrar no REPL interativo, ou combine-as com `--prompt` para automação única.

| Opção | Finalidade |
|---|---|
| `-h`, `--help` | Mostrar a ajuda do CLI e sair. Use para verificar as opções suportadas pela versão instalada. |
| `-v`, `-V`, `--version` | Exibir a versão instalada do IaC Code e sair. |
| `-m <model>`, `--model <model>` | Iniciar com um modelo LLM específico. Substitui o modelo salvo para a execução atual. |
| `-p <prompt>`, `--prompt <prompt>` | Executar um único prompt e sair. Ativa o modo não interativo. Use `--prompt -` para ler o prompt da entrada padrão. |
| `--output-format <format>` | Definir o formato de saída para o modo não interativo. Os valores suportados são `text`, `json` e `stream-json`. O padrão é `text`. |
| `--max-turns <number>` | Limitar o número máximo de turnos do agente no modo não interativo. O padrão é `100`. |
| `-d`, `--debug` | Ativar o registro de depuração para a execução atual. No modo interativo, use `/debug` para inspecionar ou alterar o registro de depuração após a inicialização. |
| `-r <id-ou-nome-da-sessao>`, `--resume <id-ou-nome-da-sessao>` | Retomar uma sessão anterior por ID exato, prefixo único de ID ou nome único de sessão. Sessões resolvidas em outro projeto imprimem um comando `cd ... && iac-code --resume <id>` em vez de trocar o projeto atual em tempo real. |
| `-c`, `--continue` | Retomar a sessão mais recente. Não pode ser usado junto com `--resume`. |
| `--allowed-tools <patterns>` | Padrões de permissão de ferramentas separados por vírgulas para permitir, ex. `'bash(git *),write_file'`. |
| `--disallowed-tools <patterns>` | Padrões de permissão de ferramentas separados por vírgulas para negar, ex. `'bash(rm *)'`. |
| `--permission-mode <mode>` | Modo de permissão: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |

## Modos de permissão

O parâmetro `--permission-mode` controla como o agente lida com as verificações de permissão de ferramentas:

| Modo | Comportamento |
|---|---|
| `default` | O agente solicita confirmação quando uma ação de ferramenta requer aprovação. |
| `accept_edits` | Aprovar automaticamente comandos do sistema de arquivos considerados como edições (ex. `mkdir`, `cp`). Outras ações ainda solicitam confirmação. |
| `bypass_permissions` | Aprovar automaticamente todas as ações de ferramentas exceto verificações de segurança. Destinado para automação confiável. |
| `dont_ask` | Negar silenciosamente qualquer ação que normalmente solicitaria confirmação. Útil para execuções estritamente somente leitura. |

## Comandos de inicialização comuns

Iniciar o REPL interativo com o modelo salvo:

```bash
iac-code
```

Iniciar com um modelo específico para esta execução:

```bash
iac-code --model qwen3.6-plus
```

Executar um prompt único:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Ler o prompt da entrada padrão:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Retomar a sessão mais recente:

```bash
iac-code --continue
```

Permitir apenas comandos git e bash somente leitura:

```bash
iac-code --allowed-tools 'bash(git *)'
```

Executar em automação sem prompts interativos:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
