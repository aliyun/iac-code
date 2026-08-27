---
sidebar_position: 1
title: Protocolo AG-UI
description: Arquitetura, recursos e casos de uso da integração AG-UI do iac-code.
---

# Protocolo AG-UI

## O que é AG-UI?

O [Agent-User Interaction Protocol (AG-UI)](https://docs.ag-ui.com/concepts/architecture) é um protocolo de fluxo de eventos entre agentes e aplicações voltadas ao usuário. Um cliente inicia uma execução com `RunAgentInput` e recebe, por HTTP Server-Sent Events (SSE), eventos estruturados de texto, raciocínio, chamadas de ferramentas, etapas, estado e interrupções.

AG-UI é adequado para consoles web, clientes de chat, extensões de IDE e outras aplicações que precisam mostrar a execução do agente em tempo real. Em vez de consumir apenas o texto final, um cliente pode renderizar separadamente a saída do modelo, argumentos e resultados de ferramentas, etapas do Pipeline e operações aguardando confirmação.

## Arquitetura do iac-code

O iac-code usa um **núcleo de execução A2A com um adaptador do protocolo AG-UI**:

```text
Cliente AG-UI
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Loop do agente / Pipeline / LLM / API do Alibaba Cloud
```

`iac-code a2a` é o único núcleo de execução. Ele é responsável por:

- conversas normais e execução de Pipelines;
- sessões do iac-code, contextos e tarefas A2A;
- permissões de ferramentas, perguntas, seleção de opções e retomada;
- ciclo de vida e cancelamento das execuções;
- chamadas a LLMs e APIs do Alibaba Cloud.

`iac-code agui` não cria um segundo runtime do Agent nem executa Pipelines diretamente. Ele apenas:

- converte `RunAgentInput` em solicitações A2A;
- projeta eventos A2A em eventos AG-UI padrão;
- mapeia `threadId/runId` para `contextId/taskId`;
- converte `resume[]` em retomada de entrada A2A;
- persiste os mapeamentos de protocolo e as interrupções pendentes;
- encaminha cancelamentos ao A2A.

Assim, AG-UI e A2A não mantêm semânticas de execução separadas. Seleção de modelo, credenciais de nuvem, regras de permissão e comportamento do Pipeline são tratados pelo mesmo runtime A2A.

## Protocolo padrão e extensões do iac-code

O fluxo externo usa eventos AG-UI padrão, incluindo:

- `RUN_STARTED`, `RUN_FINISHED` e `RUN_ERROR`;
- `TEXT_MESSAGE_*`;
- `REASONING_*`;
- `TOOL_CALL_*`;
- `STEP_STARTED` e `STEP_FINISHED`;
- `ACTIVITY_SNAPSHOT`.

Somente informações úteis do Pipeline que não têm equivalente padrão são emitidas como eventos `CUSTOM` com namespace. Um cliente AG-UI genérico pode ignorá-los sem afetar texto, ferramentas, interrupções ou o ciclo de vida da execução.

As solicitações continuam usando envelopes `RunAgentInput` padrão. O iac-code utiliza o campo padrão `forwardedProps` para o workspace, o modo de execução e outros dados necessários:

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "identidade-da-solicitacao",
      "cwd": "/caminho/absoluto/do/workspace",
      "runMode": "normal"
    }
  }
}
```

Um cliente genérico pode consumir diretamente os eventos padrão do iac-code. Ao chamar `iac-code agui` diretamente, ainda precisa fornecer campos de runtime como `cwd` em `forwardedProps.iacCode`.

## Interações compatíveis

### Conversas normais com vários turnos

Mantenha o mesmo `threadId` durante a conversa e use um novo `runId` para cada turno do usuário. O adaptador vincula o thread a uma sessão do iac-code. Quando um turno termina, a próxima mensagem abre uma nova solicitação HTTP/SSE; ela nunca continua na resposta SSE anterior, já encerrada.

### Pipeline

Defina `forwardedProps.iacCode.runMode` como `pipeline`. O núcleo A2A continua executando o Pipeline. Etapas de nível superior se tornam eventos padrão `STEP_*`, e texto, raciocínio e ferramentas usam seus eventos padrão. Informações de candidatos, progresso de stacks e limpeza sem equivalente padrão são emitidas por `iac-code.pipeline.v1`.

Sub-Pipelines paralelos usam identidades distintas para mensagens e etapas, evitando que o texto de vários loops de agente seja combinado.

### Interrupção e retomada

Quando uma permissão, pergunta ou seleção exige entrada do usuário, a execução atual termina com:

```json
{
  "type": "RUN_FINISHED",
  "outcome": {
    "type": "interrupt",
    "interrupts": []
  }
}
```

A interrupção é persistida antes de ficar visível ao cliente. Depois de coletar as respostas, o cliente inicia uma nova solicitação com o mesmo `threadId`, um novo `runId` e `resume[]`. O fluxo de retomada pertence a essa nova solicitação e não se reconecta ao fluxo antigo.

### Estado do adaptador

O adaptador armazena mapeamentos de protocolo, dados de idempotência e interrupções pendentes em um arquivo por thread. Esse diretório não contém texto de conversa, chaves de LLM ou credenciais de nuvem, e não é um diretório de exportação de conversas.

## Quando usar AG-UI

| Requisito | Modo recomendado |
|-----------|------------------|
| Criar uma interface de chat com texto, raciocínio, ferramentas e etapas em tempo real | **AG-UI** |
| Tratar permissões, perguntas e seleção de opções em uma interface | **AG-UI** |
| Permitir que outro agente ou orquestrador chame o iac-code diretamente | **A2A** |
| Integrar um IDE/editor com sessões ACP e recursos de terminal | **ACP** |
| Operar o iac-code manualmente | **REPL interativo ou Web/Desktop** |

AG-UI e A2A podem ser executados ao mesmo tempo. Eles expõem endpoints HTTP separados e compartilham a mesma implementação de execução do iac-code.

## Limites atuais

- O transporte AG-UI usa HTTP POST e SSE.
- O upstream A2A deve usar um endereço de loopback; o adaptador rejeita URLs A2A remotas arbitrárias.
- `cwd` é obrigatório em cada solicitação e deve estar sob uma raiz de workspace permitida.
- `tools` definidos pelo cliente ainda não são aceitos; o iac-code controla o conjunto de ferramentas.
- Mensagens do usuário aceitam texto e imagens base64 incorporadas, mas não URLs de mídia remota.
- Se o cliente se desconectar de uma execução SSE ativa antes de uma interrupção, o adaptador cancela a tarefa A2A correspondente.
- O fluxo SSE envia um comentário heartbeat a cada 15 segundos. Clientes compatíveis o ignoram.

## Próximos passos

- [Primeiros passos](./getting-started.md) — instale, inicie e conecte o primeiro cliente.
- [Referência do protocolo](./protocol-reference.md) — campos, eventos, interrupção/retomada, persistência e erros.
