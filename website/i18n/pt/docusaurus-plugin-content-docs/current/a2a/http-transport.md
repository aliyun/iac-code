---
title: Transport HTTP
description: Execute e chame o servidor A2A do iac-code por HTTP JSON-RPC.
sidebar_position: 5
---

# Transport HTTP

O servidor A2A padrão do iac-code expõe JSON-RPC sobre HTTP, além das rotas REST do SDK A2A. O servidor é construído com Starlette e executa no Uvicorn.

## Iniciando o servidor

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

Instale primeiro as dependências opcionais do servidor:

```bash
uv sync --extra a2a
```

## Resumo dos endpoints

| Rota | Método | Resposta |
|------|--------|----------|
| `/health` | `GET` | Resposta de saúde em JSON simples |
| `/.well-known/agent-card.json` | `GET` | JSON do Agent Card |
| `/` | `POST` | Resposta JSON-RPC ou stream SSE |
| Rotas REST do SDK | misto | Endpoints REST A2A registrados pelo SDK |

## Cabeçalhos

Cabeçalhos recomendados:

```text
Content-Type: application/json
A2A-Version: 1.0
```

Quando a autenticação Bearer está habilitada:

```text
Authorization: Bearer <token>
```

## Autenticação

O servidor suporta autenticação opcional por Bearer token, Basic auth e API key. Se nenhuma opção de autenticação ou variável de ambiente estiver definida, as requisições não precisam de autenticação. Se um ou mais esquemas estiverem configurados, uma requisição poderá se autenticar com qualquer esquema configurado.

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

Você também pode definir `token` no arquivo de configuração YAML A2A.

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

O nome de usuário e a senha devem estar definidos para que Basic auth seja habilitado.

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

O cabeçalho padrão de API key é `X-API-Key`. Você pode alterá-lo em YAML:

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

ou com `IACCODE_A2A_API_KEY_HEADER`.

| Cenário | Comportamento |
|---------|---------------|
| Nenhum esquema de autenticação configurado | Nenhuma autenticação necessária |
| Um ou mais esquemas configurados, qualquer um corresponde | A requisição prossegue |
| Um ou mais esquemas configurados, nenhum esquema corresponde | HTTP `401` com `{"error":"Unauthorized"}` |

## Descoberta do Agent Card

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Autenticado:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

Com autenticação por API key:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

A URL do endpoint JSON-RPC é anunciada em `supportedInterfaces[0].url`. O modo HTTP também anuncia uma interface `HTTP+JSON` para clientes compatíveis com REST.

## Mensagem sem streaming

`SendMessage` retorna uma única resposta JSON-RPC depois que o turno do agente termina.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Create a Terraform VPC module for Alibaba Cloud."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Mensagem em streaming

`SendStreamingMessage` retorna Server-Sent Events. Use `curl -N` para imprimir eventos à medida que chegam.

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "stream-1",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Generate a ROS template for one VPC and two vSwitches."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Cada linha SSE `data:` contém uma resposta JSON-RPC cujo `result` é um `StreamResponse` A2A.

## Mensagem de acompanhamento

Use o `taskId` e o `contextId` retornados pela primeira resposta para continuar a mesma conversa.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-3",
        "taskId": "task-id-from-first-response",
        "contextId": "context-id-from-first-response",
        "role": "ROLE_USER",
        "parts": [{"text": "Now add tags for environment and owner."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

O workspace deve permanecer o mesmo para o `contextId` reutilizado.

## Cancelar uma tarefa em execução

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "CancelTask",
    "params": {
      "id": "task-id"
    }
  }'
```

O cancelamento é cooperativo: o iac-code cancela o turno ativo do agente, emite um estado cancelado e libera o lock do contexto. Cancelar uma tarefa existente que não está mais em execução retorna o `TaskNotCancelableError` A2A padrão.

## Equivalentes na CLI

A maioria dos workflows HTTP tem um comando CLI correspondente:

```yaml
url: http://127.0.0.1:41242/
```

```bash
# Discover the Agent Card
iac-code a2a-client --config a2a-client.yml discover

# Send a non-streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a Terraform VPC module for Alibaba Cloud." \
  --cwd "$PWD"

# Send a streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a ROS template for one VPC and two vSwitches." \
  --cwd "$PWD" \
  --stream

# Inspect task state
iac-code a2a-client --config a2a-client.yml task-get --task-id task-id
iac-code a2a-client --config a2a-client.yml task-list --output table

# Cancel an active task
iac-code a2a-client --config a2a-client.yml task-cancel --task-id task-id
```

Para a lista completa de opções, consulte a [Referência de comandos](./command-reference.md).

## Observações operacionais

- Faça bind em `127.0.0.1` para uso apenas local.
- Use `token` na configuração A2A ou `IACCODE_A2A_HTTP_TOKEN` antes de fazer bind a uma interface de rede compartilhada.
- O modo A2A rejeita solicitações de permissão de ferramentas automaticamente; proteja endpoints não autenticados como serviços de automação local.
- O estado ativo do runtime fica em memória. A persistência espelha metadados de tarefas e contextos, mas reiniciar o processo não retoma trabalho asyncio em andamento.
- Um contexto pode executar apenas uma tarefa por vez; contextos separados podem executar simultaneamente.
