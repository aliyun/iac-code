---
sidebar_position: 3
title: Referência de integração do Skill do IaC Code para hosts
description: Integre a ponte do Skill do IaC Code a um agente host compatível.
---

# Referência de integração do Skill do IaC Code para hosts

Este documento é destinado a desenvolvedores de agentes e sistemas de distribuição de Skills. Usuários devem ler
[Instalar e usar o Skill do IaC Code](./skill-integration.md).

## Modelo de integração e configuração

O pacote contém `SKILL.md` e a ponte `scripts/iac_code.py`, que usa apenas a biblioteca padrão. Execute-a com CPython
3.8 a 3.14. Trate stdout como o resultado JSON estável e stderr como diagnóstico e progresso. Preserve `jobId`,
`contextId`, cursor e os campos de correlação. Em caso de erro, não use outro Runtime nem chamadas diretas às APIs de
nuvem.

O distribuidor pode colocar este `config.json` ao lado de `SKILL.md`:

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

A ponte adiciona `skill/` antes de `channel`. O padrão de `pipelineName` é `selling_solution_first`; `selling` serve
apenas para um fluxo legado solicitado explicitamente. `null` significa espera ilimitada. Campos desconhecidos ou
inválidos são recusados. Essa política de instalação não deve ser derivada de uma solicitação, exposta ou alterada
durante uma tarefa.

## Iniciar e acompanhar um job

Grave a solicitação completa em um arquivo UTF-8 no workspace e use um caminho absoluto:

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

Use `normal` por padrão e `pipeline` apenas para comparação, confirmação e implantação. O idioma pode ser `en`, `zh`,
`es`, `fr`, `de`, `ja`, `pt` ou `auto`; preserve depois `preferredLanguage`. `llm_not_configured` interrompe antes da
criação, e `cloud_credentials_not_configured` indica credenciais ausentes no Pipeline.

`--follow` retorna no próximo limite de apresentação ou interação, em `turn_completed` ou no estado terminal do
Pipeline. Com `boundaryReached: true`, mostre todos os `userUpdates` e acompanhe o mesmo job:

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

`boundaryReached` não significa conclusão. `presentationRequired` exige exibir a atualização antes da próxima chamada.
No modo normal, use `finalText` e `artifacts` em `turn_completed`; no Pipeline terminal, use `pipelineResult` e
`artifacts` e informe falhas de limpeza. Apenas para diagnóstico ou recuperação:

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

Se o estado for `input-required` sem `inputRequired`, informe o texto ou erro mais recente e não altere o job.

## Tratar a entrada do usuário

Cada `inputRequired` é um limite rígido: apresente-o na interface nativa do host e aguarde uma resposta explícita. Não
deduza padrões. Preserve `kind`, `inputId`, `requestTaskId`, `contextId` e, quando houver, `toolUseId`.

| `kind` | Informações que o host deve mostrar | Resposta |
|---|---|---|
| `permission` | Objetivo, efeito, alvo, somente leitura, resumos de implantação e segurança, ações | `allow_once` / `deny` |
| `ask_user_question` | Pergunta, opções e texto livre permitido | Resposta |
| `candidate_selection` | Todos os resumos, diagramas Mermaid, total mensal e itens | ID ou número |
| `deployment_confirmation` | Solução, URL, preço, parâmetros efetivos e alterados, Preview, ações | `confirm` / `adjust` / `reselect` / `cancel` |

Grave a resposta correlacionada em um novo arquivo JSON UTF-8 e retome o mesmo job:

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

Omita `parameterOverrides` sem ajustes. Não deduza a confirmação da solicitação inicial nem de uma aprovação do host.

## Continuar, cancelar e recuperar

Após um turno normal ou a passagem de um Pipeline concluído para o modo normal, continue o job existente:

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

Preserve `jobId` e `contextId`; um novo `taskId` é esperado. Isso também permite recuperar esperas de permissão e
interrupções do host. Para cancelar toda a operação:

```text
python3 scripts/iac_code.py cancel --job-id <job-id>
```

O cancelamento completo é diferente de negar uma permissão.

## Erros e Runtime

Um erro anterior à criação é definitivo para a chamada. Em `incompatible_host`, mostre as informações de compatibilidade
e pare, sem usar pip, outro Runtime ou APIs diretas. O Runtime fica em
`<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`. Sua estrutura e integridade são definidas
por `skill-runtime/skill-package-contract.json` e pelo manifesto da versão. A limpeza exige solicitação explícita;
pacotes atuais ou ativos são protegidos.

O Runtime usa uma porta aleatória de `127.0.0.1` e um Bearer token por processo. Não exponha token, estado local,
credenciais, valores de ambiente nem entradas ou saídas brutas das ferramentas.

## Documentação relacionada

- [Visão geral dos Skills oficiais do IaC Code](./skill-overview.md)
- [Instalar e usar o Skill do IaC Code](./skill-integration.md)
- [Visão geral do A2A](./overview.md)
- [Referência do A2A](./protocol-reference.md)
