---
title: Opções de linha de comando
description: Referência para as opções de inicialização e execução única do IaC Code.
---

# Opções de linha de comando

O comando principal inicia a web por padrão. As opções abaixo cobrem a inicialização web, a execução headless pontual e os subprocessos stream-json. Configure conversas interativas no Web ou Desktop.

A coluna de entrada mostra onde cada opção é aceita. `headless` significa `iac-code --prompt ...`; `process` indica o modo de subprocesso stream-json. O modelo e as permissões de Web são configurados na interface.

| Opção | Finalidade | Entrada |
|---|---|---|
| `-h`, `--help` | Mostrar a ajuda do CLI e sair. Use para verificar as opções suportadas pela versão instalada. | `iac-code`, `web` |
| `-v`, `-V`, `--version` | Exibir a versão instalada do IaC Code e sair. | `iac-code` |
| `-m <model>`, `--model <model>` | Iniciar com um modelo LLM específico. Substitui o modelo salvo para a execução atual. | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | Executar um único prompt e sair. Ativa o modo não interativo. Use `--prompt -` para ler o prompt da entrada padrão. | `iac-code` |
| `--output-format <format>` | Definir o formato de saída para o modo não interativo. Os valores suportados são `text`, `json` e `stream-json`. O padrão é `text`. | `headless`, `process` |
| `--max-turns <number>` | Limitar o número máximo de turnos do agente no modo não interativo. O padrão é `100`. | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | Controla se solicitações nao interativas de execucao unica habilitam thinking explicitamente. O padrão é `--thinking-enabled`; use `--no-thinking-enabled` para enviar `thinking_enabled=false` nesta execução sem reescrever `settings.yml`. | `headless` |
| `-d`, `--debug` | Ativar os logs de depuração nesta execução. | `headless`, `process`, `web` |
| `--verbose` | Mostrar o progresso do modo headless (não interativo) na saída de erro padrão. | `headless` |
| `-r <id-ou-nome-da-sessao>`, `--resume <id-ou-nome-da-sessao>` | Retomar por ID exato, prefixo único ou nome único. Execute os comandos headless no diretório do projeto da sessão. | `headless` |
| `-c`, `--continue` | Retomar a sessão mais recente. Não pode ser usado junto com `--resume`. | `headless` |
| `--allowed-tools <patterns>` | Padrões de permissão de ferramentas separados por vírgulas para permitir, ex. `'bash(git *),write_file'`. | `headless`, `process` |
| `--disallowed-tools <patterns>` | Padrões de permissão de ferramentas separados por vírgulas para negar, ex. `'bash(rm *)'`. | `headless`, `process` |
| `--permission-mode <mode>` | Modo de permissão: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. | `headless`, `process` |

## Modos de permissão

O parâmetro `--permission-mode` controla como o agente lida com as verificações de permissão de ferramentas:

| Modo | Comportamento |
|---|---|
| `default` | O agente solicita confirmação quando uma ação de ferramenta requer aprovação. |
| `accept_edits` | Aprovar automaticamente comandos do sistema de arquivos considerados como edições (ex. `mkdir`, `cp`). Outras ações ainda solicitam confirmação. |
| `bypass_permissions` | Aprovar automaticamente ações de ferramentas exceto verificações de segurança. Toda decisão allow que exige um registro de auditoria falha de forma fechada se a persistência de auditoria falhar. Destinado para automação confiável. |
| `dont_ask` | Negar ações que exigem aprovação, salvo quando se aplicar uma regra de permissão explícita. |

Chamadas de API de escrita Alibaba Cloud feitas por `aliyun_api` são protegidas separadamente: uma regra allow simples `aliyun_api` não as aprova globalmente, e fora de `bypass_permissions` as regras allow de escrita devem corresponder exatamente ao par canônico `product:action`. `bypass_permissions` as aprova automaticamente, mas, como outras decisões allow auditadas, a ação é negada se o registro de auditoria de permissões não puder ser persistido. Use regras exatas como `aliyun_api(ros:CreateStack)` quando a automação confiável deve permitir apenas uma API de escrita específica.

## Comandos de inicialização comuns

Iniciar o aplicativo web local:

```bash
iac-code
```

Iniciar com um modelo específico para esta execução:

```bash
iac-code --prompt "Criar uma VPC" --model qwen3.7-max
```

Executar um prompt único:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Executar um prompt único com thinking desabilitado para esta solicitação:

```bash
iac-code --prompt "Create an OSS Bucket" --no-thinking-enabled
```

Ler o prompt da entrada padrão:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Continuar a sessão mais recente com um prompt único:

```bash
iac-code --continue --prompt "Gerar um modelo de VPC"
```

Pré-aprovar comandos bash que correspondam a `git *`:

```bash
iac-code --prompt "Mostrar o estado do git" --allowed-tools 'bash(git *)'
```

Pré-aprovar uma API de escrita específica do Alibaba Cloud:

```bash
iac-code --prompt "Implantar a pilha" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

Executar em automação sem prompts interativos:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
