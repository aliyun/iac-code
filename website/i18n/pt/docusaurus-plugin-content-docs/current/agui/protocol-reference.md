---
sidebar_position: 3
title: Referência do protocolo
description: Referência de solicitações, eventos, interrupções, retomada, cancelamento e persistência AG-UI do iac-code.
---

# Referência do protocolo AG-UI

Esta página descreve a interface HTTP/SSE exposta por `iac-code agui` e os campos de extensão do iac-code transportados em envelopes AG-UI padrão. Consulte primeiro a [visão geral](./overview.md) e os [primeiros passos](./getting-started.md).

## Endpoints HTTP

| Método e caminho | Finalidade |
|------------------|------------|
| `GET /health` | Integridade do serviço e versões do protocolo |
| `POST /` | Enviar `RunAgentInput` e receber um fluxo de eventos SSE |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | Extensão de cancelamento com namespace |

O corpo de `POST /` deve usar JSON, e clientes devem solicitar SSE:

```http
Content-Type: application/json
Accept: text/event-stream
```

Quando `IAC_CODE_AGUI_AUTH_TOKEN` estiver configurado, solicitações protegidas também exigem:

```http
Authorization: Bearer <token>
```

Use o cabeçalho padrão `Accept-Language` como alternativa para o idioma das mensagens de erro. `forwardedProps.iacCode.preferredLanguage` tem prioridade e também é encaminhado ao runtime A2A.

## RunAgentInput

Exemplo mínimo de execução normal:

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [
    {"id": "message-1", "role": "user", "content": "Crie um modelo de VPC"}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1",
      "runMode": "normal"
    }
  }
}
```

### Campos padrão

| Campo | Requisito | Comportamento do iac-code |
|-------|-----------|---------------------------|
| `threadId` | String não vazia obrigatória | Identidade estável da conversa, mapeada para um contexto A2A e uma sessão do iac-code |
| `runId` | String não vazia obrigatória | Uma execução HTTP/SSE; não pode ser reutilizada no thread |
| `parentRunId` | Opcional | Copiado para `RUN_STARTED` |
| `state` | Obrigatório | Mantido no envelope padrão, mas não usado como estado de runtime do iac-code |
| `messages` | Obrigatório | Nova execução usa a última mensagem do usuário; uma retomada não precisa adicionar outra |
| `tools` | Obrigatório e vazio | Ferramentas definidas pelo cliente não são compatíveis |
| `context` | Obrigatório | Mantido no envelope; ainda não convertido em contexto do prompt |
| `forwardedProps` | Obrigatório | Deve conter a extensão `iacCode` |
| `resume` | Para retomada | Uma resposta para cada interrupção pendente |

Mensagens do usuário aceitam strings e partes `text` e `image` com fontes `data` base64 incorporadas. URLs de imagem remota, áudio, vídeo, documentos e binários genéricos não são compatíveis. Uma imagem decodificada é limitada a 8 MiB, todas as imagens a 10 MiB e a solicitação HTTP completa a 12 MiB.

## `forwardedProps.iacCode`

O esquema é estrito; campos desconhecidos são rejeitados.

| Campo | Tipo | Obrigatório | Significado |
|-------|------|-------------|-------------|
| `schemaVersion` | `1` | Sim | Versão da extensão do iac-code |
| `rosInvocationId` | string | Sim | Identidade do chamador da execução atual, até 256 caracteres |
| `cwd` | string | Sim | Caminho absoluto do workspace |
| `model` | string | Não | Substituição do modelo para a solicitação |
| `llmApiKey` | string | Não | Chave do provedor LLM para a solicitação |
| `thinking.enabled` | booleano | Não | Solicitar saída de raciocínio |
| `thinking.effort` | string | Não | Esforço de raciocínio específico do provedor |
| `thinking.budget` | inteiro positivo | Não | Orçamento de raciocínio específico do provedor |
| `userId` | string | Não | Identidade de telemetria e vínculo do chamador |
| `channel` | string | Não | Metadados do canal chamador |
| `preferredLanguage` | string | Não | Idioma de exibição local à solicitação, como `pt` |
| `candidatePresentation` | `standard` ou `rich` | Não | Apresentação dos candidatos do Pipeline |
| `runMode` | `normal` ou `pipeline` | Não | Modo de execução; caso contrário, escolhido pelo A2A |
| `pipelineName` | string | Não | Nome do Pipeline, por exemplo `selling` |
| `cleanupOnly` | booleano | Não | Solicitar apenas o caminho de limpeza do Pipeline |
| `alibabaCloud.accessKeyId` | string | Não | AccessKey ID local à solicitação |
| `alibabaCloud.accessKeySecret` | string | Não | Segredo AccessKey local à solicitação |
| `alibabaCloud.securityToken` | string | Não | Token STS local à solicitação |
| `alibabaCloud.regionId` | string | Não | Região padrão local à solicitação |

A execução inicial e suas retomadas devem manter o mesmo `rosInvocationId`. Um turno normal posterior pode usar um novo valor. O cancelamento deve usar o valor da execução atual.

O `threadId` é vinculado aos `cwd` e `userId` da primeira solicitação; solicitações posteriores não podem mover o mesmo thread para outro workspace ou chamador.

## SSE e heartbeat

Cada evento AG-UI é emitido como um registro SSE `data:`. Após 15 segundos sem eventos, o servidor emite:

```text
: heartbeat
```

Esse é um comentário SSE, não um evento AG-UI `CUSTOM`. Clientes compatíveis o ignoram enquanto ele mantém a conexão HTTP ativa.

## Mapeamento de eventos padrão

| Sinal A2A/iac-code | Saída AG-UI |
|--------------------|-------------|
| Solicitação aceita | `RUN_STARTED` |
| Texto do agente | `TEXT_MESSAGE_START/CONTENT/END` |
| Raciocínio bruto | `REASONING_START`, `REASONING_MESSAGE_*`, `REASONING_END` |
| Início e argumentos da ferramenta | `TOOL_CALL_START/ARGS/END` |
| Resultado da ferramenta | `TOOL_CALL_RESULT` |
| Ciclo de vida da etapa do Pipeline | `STEP_STARTED/STEP_FINISHED` |
| Snapshot de recuperação do Pipeline | `ACTIVITY_SNAPSHOT` |
| Conclusão normal | `RUN_FINISHED` com `outcome.type = "success"` |
| Entrada do usuário necessária | `RUN_FINISHED` com `outcome.type = "interrupt"` |
| Erro do adaptador ou A2A | `RUN_ERROR` |

`RUN_FINISHED` encerra uma execução AG-UI, não necessariamente todo o Pipeline. Um Pipeline interrompido várias vezes possui várias execuções, cada uma com seus próprios `RUN_STARTED` e `RUN_FINISHED`. A conclusão funcional do Pipeline é representada por `pipeline_completed`, `pipeline_error` e eventos relacionados.

Para manter os spans AG-UI equilibrados, o adaptador fecha spans abertos de mensagem, raciocínio, ferramenta e etapa antes de uma interrupção encerrar a execução. A retomada reabre etapas duráveis do Pipeline que ainda estão ativas. Por isso, a inspeção de eventos brutos pode mostrar a mesma etapa funcional encerrando em uma execução e reabrindo na seguinte; isso não significa execução invertida.

## Eventos personalizados do iac-code

### `iac-code.session.v1`

Expõe o mapeamento atual entre adaptador e A2A, incluindo `threadId`, `aguiRunId`, `executionId`, `contextId`, `taskId`, `rosInvocationId` e `sessionId`. Use `executionId` na extensão de cancelamento. Clientes genéricos podem ignorar esse evento.

### `iac-code.artifact.v1`

Transporta uma projeção estruturada de um artefato de tarefa A2A para visualização, download ou diagnóstico opcionais.

### `iac-code.tool-progress.v1`

Transporta progresso intermediário de ferramenta sem equivalente padrão. Início, argumentos e resultado final continuam como eventos padrão `TOOL_CALL_*` e não são duplicados aqui.

### `iac-code.pipeline.v1`

Somente informações úteis do Pipeline sem equivalente padrão completo são emitidas. Valores atuais de `eventType`:

- Pipeline: `pipeline_started`, `pipeline_resumed`, `pipeline_completed`, `pipeline_error`, `pipeline_warning`, `backup_blocked`;
- candidatos: `candidate_started`, `candidate_completed`, `candidate_failed`, `candidate_interrupted`, `candidate_restart_requested`, `candidate_selected`, `candidate_detail_shown`, `candidate_step_failed`;
- sub-Pipelines e erros de etapa: `sub_pipeline_started`, `sub_pipeline_completed`, `sub_step_failed`, `step_failed`;
- stacks e limpeza: `stack_progress`, `stack_instances_progress`, `stack_current_changed`, `cleanup_started`, `cleanup_progress`, `cleanup_completed`, `cleanup_failed`;
- rollback: `rollback_triggered`, `rollback_completed`;
- contexto: `context_compaction_started`, `context_compacted`, `context_compaction_failed`, `fields_marked_stale`;
- apresentação e ferramentas: `diagram_shown`, `mcp_status`, `tool_progress`.

Sinais com mapeamentos padrão não são duplicados como `CUSTOM`: `text_delta` se torna `TEXT_MESSAGE_*`, `thinking_delta` se torna `REASONING_*`, `tool_started/tool_result` se tornam `TOOL_CALL_*`, `usage` se torna `RUN_FINISHED.usage` e ciclos de etapas se tornam `STEP_*`.

Clientes devem eliminar eventos de Pipeline repetidos usando `(name, value.eventId)` ou a sequência do Pipeline e tolerar eventos personalizados com namespace desconhecidos.

## Interrupção

Uma execução que requer entrada termina com `RUN_FINISHED.outcome.type = "interrupt"`. Cada interrupção contém:

- `id` e `reason`;
- uma `message` para o usuário;
- um `toolCallId` opcional;
- um `responseSchema` JSON;
- `expiresAt`;
- metadados como `title`, `purpose`, `safeSummary`, `options` e `toolName`.

Para uma solicitação de permissão, o esquema normalmente aceita:

```json
{"decision": "allow_once"}
```

ou:

```json
{"decision": "deny"}
```

Renderize `message`, `responseSchema` e os metadados descritivos em vez de inferir a interface apenas a partir de `reason`. Perguntas e seleção de opções podem usar esquemas diferentes.

## Retomada

Uma retomada é um novo `POST /` com o mesmo `threadId`, um novo `runId`, o mesmo `rosInvocationId` e uma entrada por interrupção pendente:

```json
{
  "resume": [
    {
      "interruptId": "permission-1",
      "status": "resolved",
      "payload": {"decision": "allow_once"}
    }
  ]
}
```

Regras:

- cada interrupção pendente deve ser respondida exatamente uma vez;
- IDs duplicados e desconhecidos são rejeitados;
- `resolved` exige um payload compatível com o esquema;
- `cancelled` encerra a interrupção e corresponde a `deny` para permissões;
- o estado pendente durável só é removido depois que o A2A aceita a resposta;
- erros de esquema produzem `RUN_ERROR`, mantendo a interrupção disponível para nova tentativa;
- repetir uma resposta já aceita não executa a ferramenta novamente.

Antes de aplicar uma retomada, o adaptador pode solicitar ao A2A que restaure a sessão do iac-code, verifica as identidades de tarefa e contexto A2A e recupera eventos de Pipeline ausentes.

## Turnos e identidades

```text
threadId (conversa estável)
  ├─ runId-1 (turno do usuário)
  ├─ runId-2 (retomada de interrupção)
  ├─ runId-3 (outra retomada)
  └─ runId-4 (próxima mensagem normal)
```

Cada solicitação HTTP/SSE usa um `runId` exclusivo. A retomada é uma nova execução. Após um turno normal, a próxima mensagem cria uma nova execução e reutiliza a sessão do iac-code do thread. A idempotência está no escopo de `(threadId, runId)`.

## Extensão de cancelamento

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{"threadId": "thread-1", "rosInvocationId": "invocation-1"}
```

Os resultados possíveis são `cancelled`, `already_terminal` ou HTTP `404` com `EXECUTION_NOT_FOUND`. O cancelamento limpa interrupções pendentes e não altera os formatos padrão dos eventos AG-UI.

## Persistência e recuperação

O estado do adaptador usa por padrão:

```text
<config-dir>/agui/threads/<thread-key>.json
```

Cada arquivo contém o vínculo entre thread, contexto e workspace, identidades de sessão, tarefa e execução, posições de recuperação do Pipeline, interrupções pendentes e dados de idempotência. O adaptador carrega sob demanda apenas o thread solicitado e substitui atomicamente somente o pequeno arquivo desse thread.

Chaves de LLM, segredos AccessKey e tokens STS nunca são armazenados. Esse é um diretório de mapeamentos do adaptador, não de conversas ou artefatos de execução. O A2A gerencia sua própria persistência de sessões e tarefas; consulte a [documentação do A2A](../a2a/overview.md).

Uma interrupção expirada é rejeitada no próximo acesso, seu estado pendente é removido e o adaptador tenta cancelar a tarefa A2A correspondente.

## Desconexões

- Uma execução concluída com segurança por uma interrupção deixa de depender da conexão SSE.
- Uma retomada cria uma nova conexão SSE.
- Desconectar uma execução normal ativa faz o adaptador cancelar a tarefa A2A.
- Desconectar após uma interrupção não apaga seu estado persistente de recuperação.

## Erros

Erros anteriores ao início do SSE usam um envelope JSON HTTP. Erros durante a execução usam eventos padrão `RUN_ERROR`.

| Código | Significado |
|--------|-------------|
| `INVALID_INPUT` | Envelope, extensão, conteúdo de mensagem ou workspace inválido |
| `DUPLICATE_RUN_ID` | O mesmo digest de solicitação usou um run ID existente |
| `RUN_ID_CONFLICT` | Uma solicitação diferente reutilizou um run ID existente |
| `THREAD_BUSY` | O thread já tem uma execução ativa |
| `THREAD_BINDING_CONFLICT` | Workspace ou chamador conflita com o vínculo do thread |
| `RESUME_REQUIRED` | O thread aguarda respostas de interrupção |
| `INCOMPLETE_RESUME` | Interrupções pendentes ausentes ou IDs duplicados |
| `UNKNOWN_INTERRUPT` | A retomada referencia uma interrupção desconhecida |
| `RESUME_PAYLOAD_INVALID` | Payload ausente ou incompatível com o esquema |
| `RESUME_ALREADY_APPLIED` | A resposta já foi aplicada ou conflita com ela |
| `EXECUTION_EXPIRED` | A interrupção expirou |
| `EXECUTION_LOST` | Não foi possível recuperar o adaptador, a tarefa A2A ou a sessão do iac-code |
| `STATE_PERSISTENCE_FAILED` | O estado crítico para recuperação não pôde ser persistido |
| `A2A_UNAVAILABLE` | O serviço local de execução A2A está indisponível |
| `A2A_PROTOCOL_ERROR` | Identidade de tarefa, contexto ou sessão conflita com o mapeamento |
| `A2A_EXECUTION_FAILED` | A tarefa A2A terminou com falha |
| `CANCELLED` | A execução foi cancelada |

Gravações críticas para recuperação falham de forma segura. O adaptador não anuncia uma tarefa, sessão ou interrupção recuperável antes que seu mapeamento esteja persistido, e cancela a tarefa A2A correspondente quando necessário.
