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

Você pode realocá-lo definindo a variável de ambiente `IAC_CODE_CONFIG_DIR` (suporta expansão de `~` e `$VAR`). Quando definida, todos os artefatos persistidos — credenciais, configurações, histórico, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — seguem o novo local. Logs de inicialização/depuração ficam por padrão em `<config-dir>/logs/` e podem ser movidos separadamente com `IAC_CODE_LOG_DIR`; registros de auditoria de permissões permanecem em `<config-dir>/logs/`.

Arquivos comuns:

| Arquivo | Descrição |
|---|---|
| `.credentials.yml` | Credenciais LLM |
| `.cloud-credentials.yml` | Credenciais do provedor de nuvem |
| `settings.yml` | Provedor selecionado, modelo e configurações relacionadas |
| `AGENTS.md` | Memória do usuário carregada como instruções persistentes |
| history files | Histórico de entrada para fluxos de trabalho interativos |

Evite fazer commit ou compartilhar arquivos deste diretório porque eles podem conter segredos ou preferências locais.

## Arquivos de memória

O IaC Code tem dois locais públicos de memória:

| Local | Finalidade |
|---|---|
| `<project-root>/AGENTS.md` | Memória do projeto. Pode ser commitada quando as instruções forem úteis para todas as pessoas que trabalham no projeto. |
| `<config-dir>/AGENTS.md` | Memória do usuário. Segue `IAC_CODE_CONFIG_DIR` e é privada para o usuário local. |

Defina `IAC_CODE_INSTRUCTION_MEMORY_FILE` para usar outro nome de arquivo de memória de instruções, por exemplo `IAC-CODE.md`.

Os arquivos de tópicos de auto-memory do projeto são armazenados em:

```text
<config-dir>/projects/<project-key>/memory/
```

`MEMORY.md` nessa pasta é o índice de tópicos usado pelas side calls de auto-memory. Ele não é carregado como contexto permanente. Quando auto-memory está ativada, o IaC Code pode selecionar arquivos de tópicos relevantes e adicioná-los como contexto oculto da conversa.

## Configurações do projeto

Além do `~/.iac-code/settings.yml` no nível do usuário, o IaC Code carrega configurações no nível do projeto a partir do diretório de trabalho atual:

| Arquivo | Escopo |
|---|---|
| `.iac-code/settings.yml` | Configurações compartilhadas do projeto (seguro para commit). |
| `.iac-code/settings.local.yml` | Substituições locais (deve estar no .gitignore). |

Ordem de mesclagem: **configurações do usuário → configurações do projeto → configurações locais do projeto → argumentos CLI** (fontes posteriores substituem as anteriores).

## Política de requisição do provedor

As entradas de provedor em `settings.yml` podem incluir campos de política de requisição para provedores compatíveis com OpenAI. Essas configurações são úteis quando um modelo separa os tokens da resposta visível dos tokens de reasoning/thinking.

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Campo | Escopo | Descrição |
|---|---|---|
| `thinkingBudget` | Provedor ou modelo | Orçamento de reasoning/thinking como inteiro positivo, enviado aos provedores que oferecem suporte a ele. |
| `maxCompletionTokens` | Provedor ou modelo | Valor inteiro positivo que substitui `max_completion_tokens` para provedores/modelos que usam esse campo de requisição. |
| `effort` | Provedor ou modelo | Substituição opcional do effort de thinking, válida apenas para modelos que oferecem controle de effort. |

Valores válidos no nível do modelo em `providers.<provider>.models.<model>` substituem os valores no nível do provedor. Valores numéricos inválidos são ignorados, então o IaC Code volta ao valor do provedor ou à política integrada do modelo.

Para Alibaba Cloud DashScope e DashScope Token Plan, o IaC Code tem um `thinkingBudget=8192` integrado para `glm-5.2` e `kimi-k2.7-code`. Quando `maxCompletionTokens` não é definido, o limite da requisição é calculado como o limite normal de tokens de resposta mais o thinking budget efetivo.

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
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| Campo | Descrição |
|---|---|
| `mode` | Modo de permissão: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Lista de padrões de permissão de ferramentas para aprovação automática. |
| `deny` | Lista de padrões de permissão de ferramentas para negação automática. |
| `ask` | Lista de padrões de permissão de ferramentas que sempre requerem confirmação. |
| `additional_directories` | Diretórios adicionais além do cwd nos quais o agente pode escrever. |
| `audit` | Configurações locais do log de auditoria de permissões. |

### Sintaxe de padrões

Os padrões de permissão de ferramentas seguem o formato `tool_name(rule)`:

| Padrão | Significado |
|---|---|
| `bash` | Corresponder a todos os comandos bash (nome de ferramenta simples). |
| `bash(git *)` | Corresponder a comandos bash que começam com `git`. |
| `bash(curl:*)` | Corresponder a comandos bash que começam com `curl`. |
| `write_file` | Corresponder a todas as chamadas da ferramenta write_file. |
| `aliyun_api(ros:CreateStack)` | Corresponder a um par produto/ação de API Alibaba Cloud. |

As regras são avaliadas na ordem: **deny → ask → allow → comportamento padrão**. Os argumentos CLI (`--allowed-tools`, `--disallowed-tools`) têm a maior precedência.

### Permissões de API Alibaba Cloud

`aliyun_api` distingue chamadas de API somente leitura de chamadas que podem modificar recursos de nuvem. Ações de API somente leitura são permitidas automaticamente. Chamadas de API que não são somente leitura exigem confirmação ou uma regra allow exata para esse produto/ação, por exemplo:

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

Uma regra allow simples `aliyun_api` não aprova globalmente APIs de escrita Alibaba Cloud. Fora de `bypass_permissions`, regras allow de escrita devem corresponder exatamente ao par canônico `product:action`. No modo `bypass_permissions`, APIs protegidas de escrita Alibaba Cloud são aprovadas automaticamente, mas toda decisão allow que exige um registro de auditoria ainda falha de forma fechada se a persistência de auditoria falhar. Wildcards ainda podem ser úteis para regras deny ou ask e para correspondência de regras somente leitura.

Requisições no estilo ROA são tratadas como somente leitura apenas quando o método é `GET` e a requisição não tem body. Requisições ROA que não são somente leitura seguem o mesmo requisito de regra allow canônica exata `product:action` das APIs de escrita no estilo RPC: uma regra exata como `aliyun_api(cs:CreateCluster)` pode aprovar a escrita, enquanto regras allow com wildcards continuam não aprovando chamadas que não são somente leitura.

### Log de auditoria de permissões

Decisões de permissão que cruzam prompts do usuário, limites de cache de ferramentas, aprovação de automação ou aprovação de resolver são anexadas a:

```text
<config-dir>/logs/permission-audit.jsonl
```

Por padrão, este caminho é `~/.iac-code/logs/permission-audit.jsonl`. O log de auditoria de permissões segue `IAC_CODE_CONFIG_DIR`; `IAC_CODE_LOG_DIR` move apenas logs de inicialização/depuração. O gravador de auditoria anexa registros JSONL com bloqueio de arquivo, rotaciona o arquivo e restringe permissões locais do arquivo quando o sistema operacional oferece suporte. Aprovações automáticas rotineiras de somente leitura podem ser omitidas, mas negações, prompts, decisões em cache, aprovações de automação, aprovações de resolver e outros limites de permissão auditados são registrados.

As configurações de auditoria são definidas em `permissions.audit`:

| Campo | Padrão | Descrição |
|---|---:|---|
| `include_tool_input` | `false` | Incluir entrada de ferramenta apenas em forma estrutural nos registros JSONL de auditoria. Valores de string são gravados como tipo, tamanho e fingerprint; chaves com aparência de segredo são redigidas; nomes de campo fora da lista permitida podem ser representados por fingerprint; strings brutas de payload de negócio não são gravadas. Entradas de API Alibaba Cloud também mantêm um resumo seguro da operação. |
| `max_file_bytes` | `10485760` | Rotacionar `permission-audit.jsonl` quando ultrapassar este tamanho. |
| `max_files` | `5` | Número de arquivos de auditoria rotacionados a manter. Valores acima do máximo integrado são limitados. |

Se uma decisão allow que exige um registro de auditoria não puder ser persistida no log de auditoria, o IaC Code falha de forma fechada e nega a ação em vez de executá-la sem trilha de auditoria.
