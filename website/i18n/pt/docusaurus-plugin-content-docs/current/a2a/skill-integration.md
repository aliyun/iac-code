---
sidebar_position: 7
title: Integração de Skill
description: Agentes externos acionam o iac-code por meio do Skill empacotado do iac-code e do Skill Runtime.
---

# Integração de Skill

O iac-code fornece um Skill empacotado para agentes externos. Um agente externo (um agente planejador ou uma plataforma de agentes) não instala o pacote Python do iac-code nem invoca comandos headless; ele aciona um runtime A2A local autenticado por meio de um script ponte de apenas biblioteca padrão para executar trabalho de infraestrutura Alibaba Cloud como geração de templates ROS/Terraform, estimativa de custos, seleção de recursos e deploy.

## Componentes

| Componente | Local | Descrição |
|---|---|---|
| Pacote do Skill | `skills/iac-code/` | Instruções em `SKILL.md`, metadados de agente em `agents/` e `scripts/iac_code.py`, o script ponte |
| Skill Runtime | Publicado por plataforma | Executável nativo CPython 3.12 incorporando o servidor A2A do iac-code |
| Contratos de distribuição | `skill-runtime/skill-package-contract.json`, `skill-runtime/publisher-contract.json` | Restrições de formato e verificação para pacotes de skill e publicadores |

O script ponte é escrito inteiramente com a biblioteca padrão do Python e mantém compatibilidade com Python 3.8+; a CI o compila e executa em smoke tests na matriz completa 3.8–3.14. Não adicione dependências de terceiros nem sintaxe exclusiva de versões novas à ponte.

## Obtenção e cache do Runtime

No primeiro uso, a ponte lê o manifesto, baixa o artefato da plataforma atual, verifica tamanho e SHA-256, instala e armazena em cache sob `<IAC_CODE_CONFIG_DIR ou ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`.

- `python3 scripts/iac_code.py ensure-runtime` — prepara o runtime com antecedência; um runtime em cache é reutilizado.
- `python3 scripts/iac_code.py cache list` — mostra runtimes instalados e pacotes candidatos.
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — limpa caches do runtime ou pacotes candidatos; exige `--confirm` explícito.

## Preflight de configuração

Antes de criar um job, `start` executa uma verificação de prontidão da configuração por meio do runtime. O preflight não lê valores secretos; apenas relata a prontidão:

| Situação | Resultado |
|---|---|
| Provedor LLM ou API key incompletos | Retorna `llm_not_configured` e recusa criar o job |
| Pipeline selling com credenciais Alibaba Cloud incompletas | Retorna `cloud_credentials_not_configured` e recusa criar o job |
| Modo normal com credenciais Alibaba Cloud incompletas | Pode continuar para trabalho que não chama APIs de nuvem, com aviso de preflight |

## Referência de comandos

| Comando | Finalidade |
|---|---|
| `start` | Criar um job: `--mode normal|pipeline`, `--pipeline-name`, `--cwd` workspace absoluto, `--prompt-file` arquivo de prompt UTF-8, `--language auto|en|zh|es|fr|de|ja|pt`, opcional `--follow` |
| `follow` | Consome o fluxo de eventos até o próximo limite de interação: `--job-id`, `--cursor`, `--wait-seconds` (padrão 60 s, máximo 120 s) |
| `continue` | Continua uma conversa em modo normal no mesmo job: `--job-id`, `--prompt-file`, opcional `--follow` |
| `respond` | Responde a uma entrada pendente, veja [Entrada do usuário](#input-required) |
| `poll` | Polling de uso único apenas para diagnóstico e recuperação; não use como substituto do `follow` |
| `cancel` | Cancela o job |
| `ensure-runtime` / `cache list` / `cache clean` | Gerenciamento do runtime e do cache |

`start --follow` e `follow` escrevem limites de etapa e heartbeats de baixa frequência no stderr; o stdout emite exatamente um resultado JSON limitado.

## Limites de interação {#boundaries}

`--follow` consome o fluxo de eventos até o próximo limite de etapa, solicitação de permissão, pergunta do usuário, seleção de candidato, `turn_completed` ou estado terminal. Um resultado de limite carrega:

- `boundaryReached: true` — um limite foi alcançado; isso **não** significa que o job terminou;
- `presentationRequired: true` e `userUpdates` — strings localizadas prontas para exibir ao usuário;
- o `cursor` necessário para continuar.

O agente externo deve primeiro apresentar cada string `userUpdates` recebida em uma resposta visível ao usuário e então chamar `follow` novamente com o `cursor` retornado. Não responda à tarefa de infraestrutura em paralelo nem faça perguntas sem relação enquanto um follow está em execução.

## Entrada do usuário {#input-required}

Um resultado contém `inputRequired` quando é necessária entrada do usuário. Há três tipos:

- `permission` — uma solicitação de permissão de ferramenta ou deploy. O envelope contém `inputId`, `toolUseId`, título, propósito, efeito, alvo, marcador de somente leitura, `safeSummary` e, em solicitações de deploy, `deploymentSummary`. O agente externo deve decidir conforme sua própria política de permissões: se a mesma operação prosseguiria sem perguntar quando o agente a executa diretamente, responda `allow_once`; se a política a negaria, responda `deny`; caso contrário, pergunte ao usuário. Negações do próprio iac-code não devem ser sobrepostas.
- `ask_user_question` — uma pergunta de múltipla escolha ou texto livre. Apresente o prompt e as opções como estão; aceite texto livre somente quando `allowFreeText` for `true`.
- `candidate_selection` — seleção de plano do pipeline. Apresente primeiro o resumo, o diagrama de arquitetura (Mermaid), o custo mensal total e os itens de custo de cada candidato, e então retorne o candidato escolhido. Nunca substitua os preços fornecidos por estimativas aproximadas.

`respond` tem duas formas:

```bash
# Decisão inline para permissões
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# Perguntas e seleções de candidato usam arquivo de resposta
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

Uma resposta deve preservar todos os campos de correlação da entrada pendente e fica vinculada aos atuais `kind`, `inputId`, `requestTaskId` e `contextId`; nunca reutilize uma resposta de outra requisição nem reinterprete uma seleção de recurso como confirmação de deploy.

## Controle de idioma

`start --language` define o idioma preferido do job (use `auto` quando desconhecido). Todo resultado desse job repete `preferredLanguage`; trate-o como estado de controle durável: progresso, perguntas, prompts de permissão, planos candidatos e resultados finais são apresentados nesse idioma, enquanto nomes de campos do protocolo, enums, IDs e comandos permanecem inalterados. Quando o texto autorizado já usa esse idioma, apresente-o diretamente ou resuma-o no mesmo idioma; nunca traduza conteúdo chinês visível ao usuário para o inglês.

## Relação com o protocolo A2A

A ponte se comunica com o runtime local por HTTP A2A JSON-RPC; estados de tarefa, artefatos e interações de permissão reutilizam o protocolo A2A do iac-code:

- Respostas de banda lateral de permissão usam o formato de mensagem `schemaVersion 1`; veja a [Referência do protocolo](./protocol-reference.md) para campos e restrições.
- No modo pipeline, passar `candidatePresentation: rich-v1` retorna payloads estruturados de apresentação de candidatos.
- Os estados de resultado do job correspondem a estados de tarefa A2A: `turn_completed` encerra um turno normal; os estados terminais do pipeline são `completed`, `failed`, `canceled` e `rejected`, com `pipelineResult` e `artifacts` como resultado autorizado.

## Limite de segurança

- O runtime escuta apenas em uma porta aleatória de `127.0.0.1`; cada inicialização gera um novo Bearer token aleatório, e toda requisição da ponte o carrega.
- A ponte mantém artefatos e resultados dentro do workspace do job; os resultados são escritos em `.iac-code-skill-results/` do workspace.
- Relatórios de preflight e campos de exibição de permissão são sanitizados; segredos e credenciais nunca aparecem nos campos de exibição.
