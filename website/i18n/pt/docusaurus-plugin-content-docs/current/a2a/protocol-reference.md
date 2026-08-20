---
title: Referência do protocolo
description: Referência completa do protocolo A2A para integração com iac-code.
sidebar_position: 4
---

# Referência do protocolo

Este documento descreve a superfície A2A 1.0 exposta pelo servidor iac-code e o comportamento do cliente Fase 1 usado por `iac-code a2a-client call`. Para opções exatas da CLI, consulte a [Referência de comandos](./command-reference.md).

## Visão geral do ciclo de vida

Uma interação A2A típica segue este fluxo:

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **Descobrir** — Busque `/.well-known/agent-card.json`.
2. **Enviar** — Envie uma mensagem de texto ao endpoint JSON-RPC em `/`.
3. **Transmitir** — Receba payloads `Task`, `Message` e `TaskStatusUpdateEvent`.
4. **Continuar** — Envie uma mensagem de acompanhamento com o mesmo `contextId`.
5. **Cancelar ou consultar** — Use `CancelTask`, `GetTask` ou `ListTasks`.

## Agent Card

O Agent Card está disponível em:

```text
GET /.well-known/agent-card.json
```

Campos importantes:

| Campo | Valor | Significado |
|-------|-------|-------------|
| `name` | `iac-code` | Nome do agente |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | Binding de transport |
| `supportedInterfaces[0].protocolVersion` | `1.0` | Versão do protocolo A2A |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | Endpoint JSON-RPC |
| `capabilities.streaming` | `true` | Suporta atualizações de tarefas em streaming |
| `capabilities.pushNotifications` | `false` ou `true` | `true` quando `push-notifications: true` está configurado |
| `capabilities.extendedAgentCard` | `true` | Chamadores autenticados podem solicitar detalhes estendidos do runtime |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | Namespace opcional de metadados do iac-code para status de ferramentas e metadados de artefatos armazenados |
| `defaultInputModes` | text, JSON, YAML, image, audio, and binary MIME types | Modos MIME de entrada aceitos |
| `defaultOutputModes` | `["text/plain"]` | Apenas saída de texto |

As respostas do Agent Card incluem `Cache-Control: public, max-age=60`, `ETag` e `Last-Modified`. Clientes podem enviar `If-None-Match` e receber `304 Not Modified` quando o card não mudou.

Skills anunciadas:

| Skill ID | Finalidade |
|----------|------------|
| `iac_generation` | Gerar templates Alibaba Cloud ROS e Terraform a partir de linguagem natural |
| `iac_review` | Inspecionar templates IaC e sugerir correções |
| `aliyun_ros_operations` | Auxiliar workflows de stacks Alibaba Cloud ROS |
| `terraform_ros_conversion` | Auxiliar conversão Terraform-para-ROS usando recursos de skill agrupados |

Quando a autenticação está habilitada, o Agent Card anuncia os esquemas de segurança configurados:

| Esquema | Quando anunciado |
|---------|------------------|
| `bearerAuth` | `token` ou `IACCODE_A2A_HTTP_TOKEN` está definido |
| `basicAuth` | Nome de usuário e senha Basic estão ambos definidos |
| `apiKeyAuth` | `api-key` ou `IACCODE_A2A_API_KEY` está definido |

## Rotas

| Rota | Método | Descrição |
|------|--------|-----------|
| `/health` | `GET` | Retorna `{"status":"healthy"}` |
| `/.well-known/agent-card.json` | `GET` | Retorna o Agent Card |
| `/` | `POST` | Trata requisições A2A JSON-RPC |
| Rotas REST | mistas | As rotas REST do SDK A2A registradas por `create_rest_routes` |

## Cliente Fase 1 e observações de transport

O transport interoperável padrão da Fase 1 é JSON-RPC sobre HTTP. O modo HTTP também anuncia `HTTP+JSON` para as rotas REST do SDK.

O servidor também tem transports opcionais para stdio, Unix sockets, WebSocket, gRPC oficial, envelope JSON-RPC gRPC e Redis Streams. stdio, Unix sockets, WebSocket, JSON-RPC gRPC e Redis Streams são transports JSON-RPC customizados. gRPC oficial é anunciado como `grpc` e exige dependências gRPC opcionais.

O cliente integrado usa descoberta de Agent Card (`GET /.well-known/agent-card.json`) antes das chamadas de mensagem, seleciona o primeiro `supportedInterfaces[].url` executável anunciado e então envia requisições JSON-RPC com `A2A-Version: 1.0` e nomes de métodos A2A 1.0 como `SendMessage`.

`push-notifications: true` habilita os métodos de configuração de notificações push A2A e a entrega de estados terminais.

A assinatura de Agent Card usa o utilitário de assinatura do SDK A2A e emite campos JWS `AgentCardSignature` padrão. O modo de chave simétrica usa `HS256`; a verificação pode selecionar um segredo configurado pelo `kid` do cabeçalho protegido, um JWKS local de chave octet ou uma URL JWKS remota. Assinatura assimétrica no lado do servidor e rotação automática de chaves não estão implementadas na Fase 1.

Para a lista canônica de comportamentos sem suporte na Fase 1, consulte [Protocolo A2A](./overview.md#phase-1-unsupported).

## Backends de entrega de notificações push

`iac-code a2a --config a2a-server.yml` suporta duas filas de entrega push:

- `push-queue: local-file` armazena jobs abaixo do diretório de persistência A2A e é destinado a uso local de nó único.
- `push-queue: redis-streams` armazena jobs em Redis Streams e coordena workers por meio de um consumer group Redis.

A entrega push baseada em Redis exige o extra opcional `a2a-redis` e é at-least-once. Receptores de callback devem tratar atualizações de tarefas de forma idempotente, porque um job pode ser entregue novamente após falhas de worker, expiração de lease, reconexões ou disputas de retry.

Opções comuns de Redis:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

URLs de callback são validadas antes do armazenamento e novamente antes do envio. O validador padrão rejeita URLs que não sejam HTTP(S), hostnames localhost e endereços IP literais privados/locais. Receptores de callback ainda devem aplicar sua própria política de autenticação e idempotência.

## Métodos JSON-RPC

### SendMessage

Executa um turno de mensagem A2A sem streaming. A resposta contém uma tarefa ou mensagem depois que o turno foi concluído.

**Requisição**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**Campos obrigatórios da mensagem**

| Campo | Tipo | Obrigatório | Descrição |
|-------|------|-------------|-----------|
| `messageId` | string | Sim | ID único da mensagem do cliente |
| `role` | string | Sim | Use `ROLE_USER` para entrada do usuário |
| `parts` | array | Sim | Partes semelhantes a texto, dados JSON, texto bruto, URL de arquivo local ou partes multimodais limitadas |
| `metadata.iac_code.cwd` | string | Recomendado | Caminho absoluto do workspace; usa como padrão o diretório do processo do servidor se omitido |
| `metadata.iac_code.channel` | string | Opcional | Canal de telemetria vinculado a este `contextId`; tem prioridade sobre `IAC_CODE_CHANNEL` |
| `metadata.iac_code.preferredLanguage` | string | Opcional | Idioma de exibição preferido pelo chamador para esta tarefa; o texto visível ao usuário é localizado por requisição |
| `metadata.iac_code.candidatePresentation` | string | Opcional | Com `rich-v1`, a etapa de confirmação de candidatos do pipeline retorna payloads estruturados de apresentação rica |

`metadata.iac_code.cwd` deve ser um diretório absoluto existente quando fornecido. Ele deve estar dentro de uma raiz de workspace permitida. Por padrão, as raízes permitidas são o diretório do processo do servidor e o diretório temporário do sistema; `IACCODE_A2A_ALLOWED_CWDS` pode fornecer uma allowlist separada pelo separador de caminhos do sistema operacional.

`metadata.iac_code.channel` vincula `iac_code.channel` ao `contextId` A2A. O valor é aparado, limitado a 128 caracteres e tem prioridade sobre `IAC_CODE_CHANNEL`; valores vazios ou que não sejam strings são ignorados. Turnos normais, turnos de pipeline, acompanhamentos input-required e o chat normal após um handoff de pipeline reutilizam o vínculo com o mesmo `contextId`, inclusive após reinício do servidor e restauração do contexto. Um valor válido enviado depois atualiza o vínculo. Sem vínculo, a telemetria usa `IAC_CODE_CHANNEL` e depois `unknown`.

`metadata.iac_code.preferredLanguage` afeta apenas o texto visível ao usuário (progresso, perguntas, prompts de permissão, apresentações de candidatos, explicações de resultados); nomes de campos do protocolo, enums, IDs e formatos de comando nunca são traduzidos. Os valores aceitos são os idiomas suportados `en`, `zh`, `es`, `fr`, `de`, `ja`, `pt`; os valores são normalizados removendo espaços, convertendo para minúsculas e retirando o sufixo regional (por exemplo, `zh-CN` vira `zh`). Valores não reconhecidos são ignorados e o servidor volta ao idioma padrão. O campo vale apenas para o turno de mensagem atual; turnos seguintes que reutilizem o mesmo `contextId` precisam enviá-lo de novo, senão voltam ao idioma padrão.

`metadata.iac_code.candidatePresentation` com `rich-v1` faz a etapa de confirmação de candidatos do pipeline selling retornar um payload estruturado próprio para renderização rica (nome do candidato, resumo, diagrama de arquitetura, custo mensal total, itens de custo). Sem o campo, o comportamento de apresentação em texto simples permanece inalterado.

Categorias de entrada suportadas:

| Categoria | Formato aceito | Limites e comportamento |
|-----------|----------------|-------------------------|
| Partes semelhantes a texto | `text` com `text/plain`, JSON, Markdown, YAML ou tipos MIME de texto extras configurados | Anexadas diretamente ao prompt |
| Partes de dados JSON | `data` com `application/json` | Serializadas em JSON compacto; máximo de 1 MiB inline |
| Partes de texto bruto | `raw` com um tipo MIME semelhante a texto | Devem ser UTF-8 válido; máximo de 1 MiB inline |
| URLs de arquivos de texto locais | `url` com `file://...` e tipo MIME semelhante a texto | O arquivo deve existir dentro de `cwd` e das raízes permitidas; máximo de 1 MiB |
| Partes multimodais raw/data/file | image, audio ou tipos MIME multimodais configurados | Convertidas em um manifesto de prompt com nome de arquivo, tipo de mídia, tamanho em bytes, hash e fonte; raw/data máx. 5 MiB, URL de arquivo máx. 25 MiB |

Ingestão de URLs HTTP(S) remotas não é suportada. Partes de URL de arquivo devem usar URLs locais `file://` e permanecer dentro do workspace permitido.

### SendStreamingMessage

Executa um turno de mensagem A2A em streaming. O corpo da requisição tem o mesmo formato de `SendMessage`, mas o servidor transmite respostas JSON-RPC como Server-Sent Events.

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

Retorna a tarefa A2A salva pelo ID. Use `historyLength` para limitar o histórico retornado sem alterar o histórico de tarefa armazenado. Omita-o para receber o histórico padrão atual do servidor.

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

Retorna tarefas conhecidas visíveis ao chamador autenticado. Os resultados são ordenados pelo timestamp de status em ordem decrescente e, em seguida, pelo ID da tarefa em ordem decrescente para manter ordenação estável. O servidor suporta `contextId`, `status`, `pageSize`, `pageToken`, `historyLength` e `includeArtifacts`.

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

`nextPageToken` é retornado quando outra página está disponível. `includeArtifacts` tem padrão `false`, portanto respostas de listagem omitem artefatos de tarefas a menos que solicitados explicitamente.

### CancelTask

Solicita cancelamento para uma tarefa em execução.

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

Se a tarefa estiver ativa, o servidor cancela o turno do agente em execução e emite um estado de tarefa cancelado. Se a tarefa existir, mas não estiver em execução, o servidor retorna o `TaskNotCancelableError` A2A padrão.

### SubscribeToTask

Assina um stream de atualizações de tarefa ativa quando suportado pelo transport do cliente.

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

Para tarefas ativas, o stream começa com a `Task` atual, depois emite eventos de tarefa subsequentes e fecha quando o turno ativo termina. Assinar uma tarefa concluída, falha, cancelada ou que exige entrada retorna um erro no estilo task-not-found em vez de esperar indefinidamente. Para novos turnos, prefira `SendStreamingMessage`; ele inicia a execução e transmite a resposta em uma requisição.

### Métodos de configuração de notificações push

Quando o servidor inicia com `push-notifications: true`, ele suporta:

| Método | Finalidade |
|--------|------------|
| `CreateTaskPushNotificationConfig` | Armazenar uma configuração de callback para uma tarefa |
| `GetTaskPushNotificationConfig` | Buscar uma configuração de callback |
| `ListTaskPushNotificationConfigs` | Listar configurações de callback para uma tarefa |
| `DeleteTaskPushNotificationConfig` | Excluir uma configuração de callback |

Exemplo de requisição de criação:

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

O servidor criptografa tokens de notificação armazenados e credenciais de autenticação de callback quando o keyring push local está disponível.

### GetExtendedAgentCard

Clientes autenticados podem solicitar o Agent Card estendido:

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

O card estendido inclui o card público mais detalhes autenticados do runtime.

## Comportamento de tarefas e contextos

O iac-code mapeia contextos A2A para runtimes internos do agente:

| Conceito | Comportamento |
|----------|---------------|
| `contextId` omitido | O SDK/servidor gera um novo ID de contexto |
| Mesmo `contextId` | Reutiliza a mesma sessão interna do iac-code e o estado da conversa |
| Mesmo `contextId`, `cwd` diferente | Rejeitado como um workspace diferente |
| Mesmo `contextId`, mensagem concorrente | Rejeitado com `Task is already working.` |
| Valores diferentes de `contextId` | Podem executar simultaneamente |
| Contexto ocioso | Removido da memória após o timeout de ociosidade configurado |

IDs de tarefas e contextos devem ser não vazios, ter no máximo 128 caracteres e conter apenas letras, dígitos, `_`, `.`, `:` ou `-`.

## Estados de tarefa

| Estado | Significado |
|--------|-------------|
| `TASK_STATE_SUBMITTED` | A tarefa foi aceita |
| `TASK_STATE_WORKING` | O iac-code está executando o turno do agente |
| `TASK_STATE_INPUT_REQUIRED` | O turno foi concluído e o agente está pronto para entrada de acompanhamento |
| `TASK_STATE_CANCELED` | O cancelamento foi solicitado e aplicado |
| `TASK_STATE_FAILED` | A tarefa falhou na validação ou execução |

O iac-code usa `TASK_STATE_INPUT_REQUIRED` como o estado normal de conclusão porque o contexto permanece disponível para mensagens de acompanhamento. Turnos A2A comuns tratam `TASK_STATE_INPUT_REQUIRED` como esse estado normal de conclusão; o backup bem-sucedido de fim de turno é o checkpoint não bloqueante `normal_turn_end`. Se uma publicação terminal ou de cancelamento A2A comum for bloqueada por uma barreira crítica de backup, os metadados da task expõem `metadata.iac_code.backupBlocked` com `reason`, `error` e `recoverable`; clientes devem aguardar a recuperação antes de enviar o próximo turno. O modo Pipeline publica falhas de backup como eventos de pipeline, portanto os clientes devem inspecionar `metadata.iac_code.pipeline.eventType == "backup_blocked"` e os dados do evento. O modo Pipeline não cria mais backups ao concluir steps; `pipeline_step_completed` permanece apenas para recuperação legada. Com `IAC_CODE_CONFIG_BACKUP_DIR`, `input_required` e `waiting_input` são publicados antes de seu único backup crítico. `terminal` continua protegendo publicações terminais, e `handoff_ready` continua protegendo `pipeline_handoff_ready`. Para publicações committed terminais e `pipeline_handoff_ready`, o iac-code persiste um evento de pipeline `backup_committed` com `committedEventId`, `committedEventType` e `committedSequence`; clientes podem pareá-lo com o evento protegido antes de tratar a publicação como recuperável após reiniciar.

Quando o servidor A2A usa `IAC_CODE_CONFIG_BACKUP_TMP_DIR`, `backup_committed` confirma o snapshot local imutável e permite recuperação no mesmo sandbox; não confirma que o diretório final foi atualizado. A recuperação em outro sandbox fica disponível depois que o processo em segundo plano copia o snapshot para `IAC_CODE_CONFIG_BACKUP_DIR`. Sem preparação local, `backup_committed` continua confirmando o diretório final.

## Atualizações em streaming

Durante a execução, o iac-code emite atualizações `TaskStatusUpdateEvent`.

Texto do assistente é entregue como uma mensagem de status:

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

Detalhes de ferramentas e uso são entregues por `metadata.iac_code`:

| Caminho de metadados | Descrição |
|----------------------|-----------|
| `iac_code.tool.status` | `started`, `input_delta`, `input_complete`, `completed` ou `failed` |
| `iac_code.tool.toolUseId` | ID estável de uso de ferramenta para correlacionar eventos de ferramenta |
| `iac_code.tool.name` | Nome da ferramenta quando disponível |
| `iac_code.tool.inputSummary` | Resumo apenas em forma estrutural da entrada completa da ferramenta; valores de string e nomes de campo fora da lista permitida são representados sem payloads de negócio brutos |
| `iac_code.tool.result` | Resultado da ferramenta, truncado para 4000 caracteres por campo |
| `iac_code.permission.autoApproved` | Se o A2A aprovou a solicitação de permissão automaticamente ou por meio de um resolver |
| `iac_code.permission.toolName` | Ferramenta que solicitou permissão |
| `iac_code.permission.safeSummary` | Resumo de permissão redigido e legível por humanos |
| `iac_code.permission.inputSummary` | Resumo seguro da operação para solicitações de permissão de `aliyun_api` |
| `iac_code.permission.toolInput` | Entrada de ferramenta apenas em forma estrutural para solicitações de permissão que não são de `aliyun_api`; valores de string usam tipo, tamanho e fingerprint, e nomes de campo fora da lista permitida podem ser representados por fingerprint |
| `iac_code.mcpWarning` | Warning MCP de configuração ou execução redigido, incluindo server name, warning code, mensagem pública, scope e source path quando disponível |
| `iac_code.mcpStatus` | Snapshot redigido do status de servidores MCP com connection state, authentication state, approval state, capability counts, capability refresh errors e warnings de configuração |
| `iac_code.mcpProgress` | MCP tool progress metadata com public tool name, original server/tool names, tool-use ID, progress, total e mensagem pública opcional |
| `iac_code.thinking.type` | `raw_thinking` quando `raw-thinking` está habilitado em `thinking-exposure` |
| `iac_code.thinking.text` | Chunk bruto de raciocínio do provider, truncado para 4000 caracteres, emitido apenas em configurações confiáveis que habilitam `raw-thinking` |
| `iac_code.usage.inputTokens` | Contagem de tokens de entrada do turno |
| `iac_code.usage.outputTokens` | Contagem de tokens de saída do turno |
| `iac_code.usage.totalTokens` | Contagem total de tokens do turno |

O modo pipeline também pode publicar status MCP como metadata de pipeline com `metadata.iac_code.pipeline.eventType == "mcp_status"`. Warnings MCP, snapshots de status e metadata de progresso são sanitizados antes do streaming; secrets em headers, variáveis de ambiente, tokens OAuth e caminhos locais são redigidos ou resumidos.

Quando um resultado de ferramenta inclui um payload de artefato de texto suportado, o servidor armazena o payload localmente, emite um `TaskArtifactUpdateEvent` padrão e registra o artefato no campo `artifacts` da tarefa. A parte do artefato usa uma URL `file://` mais metadados como `mediaType`, `byteSize` e `sha256`; o conteúdo original do artefato não é duplicado dentro dos metadados da ferramenta.

### Decisões interativas de permissão

Com a configuração padrão, uma solicitação de permissão de ferramenta levantada durante um turno A2A vira uma espera de entrada: o iac-code pausa o turno, emite uma atualização de estado `TASK_STATE_INPUT_REQUIRED` com um envelope de permissão e aguarda a decisão estruturada do chamador.

Os metadados da atualização de estado contêm:

- `metadata.iac_code.input` — o envelope de permissão (`schemaVersion` 1), com os campos:
  - Campos de correlação: `kind: "permission"`, `requestTaskId`, `contextId`, `inputId`, `toolUseId`, `toolName`
  - Campos de exibição: `title`, `purpose`, `effect`, `target`, `isReadOnly`, `safeSummary`, além de `deploymentSummary` em solicitações de deploy
  - `prompt` e `options` (`allow_once` / `deny`), localizados no idioma preferido do chamador
- `metadata.iac_code.permission` — contém `autoApproved: false`, `pending: true`, `toolName`, `toolUseId`

O chamador envia a decisão por uma mensagem de banda lateral: uma única mensagem A2A `ROLE_USER` contendo exatamente um DataPart `application/json` cujo payload deve conter exatamente estes campos:

```json
{
  "schemaVersion": 1,
  "kind": "permission",
  "requestTaskId": "<taskId que levantou a solicitação de permissão>",
  "inputId": "<inputId do envelope>",
  "toolUseId": "<toolUseId do envelope>",
  "decision": "allow_once"
}
```

- `decision` aceita apenas `allow_once` ou `deny`.
- O `taskId` externo da mensagem deve ser igual a `requestTaskId`; `contextId` vem do envelope externo da mensagem. Todos os campos de correlação devem ser preservados literalmente; não devem ser reutilizados entre requisições, e uma resposta de uma entrada nunca deve ser reinterpretada como outra.
- Quando o servidor casa a decisão, devolve um DataPart `permission_ack` (`schemaVersion: 1`, `kind: "permission_ack"`, com `inputId`, `toolUseId`, `decision`, `accepted: true`) e emite uma atualização de estado `TASK_STATE_WORKING` com `metadata.iac_code.inputReceived`; o turno então retoma.

No modo pipeline, solicitações de permissão são publicadas como eventos de pipeline (o envelope também carrega `scope` e coordenadas de etapa/candidato); o formato de resposta de banda lateral é idêntico.

Com `auto-approve-permissions` habilitado ou regras de permissão explícitas configuradas, solicitações de permissão não viram entrada interativa; são aprovadas automaticamente (com auditoria) ou resolvidas pelas regras. APIs protegidas de escrita Alibaba Cloud não são liberadas por regras allow comuns e ainda exigem autorização exata por API. Decisões de permissão são auditadas localmente; toda decisão allow que exige um registro de auditoria falha de forma fechada quando o registro não pode ser persistido.

## Extensões

O Agent Card anuncia a extensão opcional de metadados de artefato do iac-code:

```text
urn:iac-code:a2a:artifact-metadata:v1
```

Esta extensão identifica o namespace `metadata.iac_code` usado para progresso de ferramentas, decisões de permissão, uso de tokens e metadados de artefatos locais. Se o servidor estiver configurado com qualquer extensão obrigatória, os clientes devem incluir seu URI no cabeçalho `A2A-Extensions`. Extensões obrigatórias ausentes retornam o `ExtensionSupportRequiredError` A2A padrão.

Quando `thinking-exposure` habilita um ou mais tipos de sinal, o Agent Card também anuncia:

```text
urn:iac-code:a2a:thinking-exposure:v1
```

Os params da extensão incluem `enabledTypes`, como `["tool_trace", "raw_thinking"]`. Esta é uma extensão de metadados do iac-code, não um tipo de evento de streaming central do A2A.

## Tratamento de erros

| Cenário | Resultado |
|---------|-----------|
| Entrada de texto vazia | `TASK_STATE_FAILED` com `A2A server currently accepts text input only.` |
| Tipo de mídia sem suporte | Erro de validação ou erro padrão A2A de content-type, dependendo de onde o SDK rejeita a requisição |
| Parte de URL remota | Erro de validação porque partes de URL devem usar URLs locais `file://` |
| URL de arquivo fora do workspace permitido | Erro de validação |
| Extensão A2A obrigatória ausente | `ExtensionSupportRequiredError` A2A padrão |
| Metadados de workspace inválidos | `TASK_STATE_FAILED` com uma mensagem de workspace inválido |
| Autenticação ausente ou inválida | HTTP `401` com `{"error":"Unauthorized"}` |
| Dependências do servidor A2A ausentes | CLI sai com uma dica de instalação para o extra `a2a` |
| Credenciais do provedor ausentes | Erro de autenticação sanitizado |
| Erro inesperado de runtime | Erro interno sanitizado |

O servidor evita retornar caminhos locais, segredos e detalhes do provedor em mensagens de erro inesperadas.
