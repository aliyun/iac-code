---
title: Transporte HTTP+SSE
description: Execute o servidor ACP via HTTP com Server-Sent Events para cenarios remotos e multi-cliente.
sidebar_position: 5
---

# Transporte HTTP+SSE

O servidor ACP do iac-code suporta dois modos de transporte. O transporte **Stdio** padrao comunica-se via entrada/saida padrao e e ideal para integracoes locais com IDEs. O transporte **HTTP+SSE** expoe um endpoint de rede e transmite respostas via Server-Sent Events, tornando-o adequado para implantacoes remotas, ambientes com balanceamento de carga e acesso multi-cliente.

## Por que HTTP+SSE

O Stdio tem limitacoes inerentes:

- Requer que o processo servidor seja filho direto do cliente — sem acesso remoto.
- O gerenciamento de processos bloqueante dificulta o atendimento de multiplos clientes simultaneamente.
- Incompativel com proxies de rede, balanceadores de carga ou implantacoes em containers.

O HTTP+SSE resolve essas restricoes:

- **Amigavel a redes** — acessivel de qualquer maquina que possa alcancar o endpoint.
- **Multi-cliente** — cada cliente recebe uma conexao isolada com seu proprio fluxo de eventos.
- **Pronto para infraestrutura** — funciona atras de proxies reversos, em containers e com ferramentas padrao de monitoramento HTTP.
- **Integracao facil** — qualquer cliente HTTP (curl, fetch, SDK) pode interagir com o servidor.

## Iniciando o servidor HTTP

```bash
# Default port 8765
iac-code acp --transport http

# Custom port
iac-code acp --transport http --port 9090
```

O servidor usa [Starlette](https://www.starlette.io/) como framework ASGI e executa no Uvicorn.

## Rotas

Todas as rotas sao servidas no caminho `/acp`. O metodo HTTP determina a operacao.

### `POST /acp`

Envia uma requisicao JSON-RPC ao servidor.

- **`initialize`** — Cria uma nova conexao e retorna a resposta JSON-RPC completa diretamente. A resposta inclui um cabecalho `Acp-Connection-Id`.
- **Todos os outros metodos** — Requer um cabecalho `Acp-Connection-Id` valido. Retorna `202 Accepted` imediatamente; o resultado real e entregue de forma assincrona atraves do stream SSE.

### `GET /acp`

Abre um stream de Server-Sent Events para receber respostas e notificacoes.

- Requer o cabecalho `Acp-Connection-Id`.
- Os eventos tem o tipo `message` com a resposta/notificacao JSON-RPC como campo `data`.
- O stream inclui campos `id` e `retry` para reconexao automatica.

### `DELETE /acp`

Fecha a conexao e libera todos os recursos associados.

- Requer o cabecalho `Acp-Connection-Id`.
- Retorna `200 OK`.

## ID de conexao

O ID de conexao vincula as requisicoes de um cliente e seu stream de eventos SSE.

1. O cliente envia um `POST /acp` com o metodo `initialize`.
2. O servidor responde com o resultado de inicializacao e um cabecalho de resposta `Acp-Connection-Id` contendo um UUID.
3. Todas as requisicoes subsequentes (`POST`, `GET`, `DELETE`) devem incluir o cabecalho de requisicao `Acp-Connection-Id` com este valor.
4. Cada ID de conexao mapeia para uma sessao de agente ACP independente com sua propria fila de eventos.

Se uma requisicao referencia um ID de conexao ausente ou invalido, o servidor retorna `400 Bad Request`.

## Autenticacao

O servidor suporta autenticacao opcional via token Bearer atraves da variavel de ambiente `IACCODE_ACP_HTTP_TOKEN`.

```bash
# Set the token before starting the server
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

Quando definido, cada requisicao deve incluir:

```
Authorization: Bearer your-secret-token
```

| Cenario | Comportamento |
|----------|----------|
| Token nao definido | Sem autenticacao necessaria (adequado para desenvolvimento local) |
| Token definido, cabecalho corresponde | Requisicao prossegue normalmente |
| Token definido, cabecalho ausente/incorreto | `401 Unauthorized` retornado |

## Fluxo de trabalho completo

Abaixo esta uma interacao completa usando `curl`:

```bash
# Step 1: Initialize — creates a connection and returns the Connection ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# Step 2: Open the SSE stream (run in background)
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# Step 3: Create a session
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# Step 4: Send a prompt
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# Step 5: Close the connection
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# Clean up background SSE process
kill $SSE_PID 2>/dev/null
```

:::tip
A resposta `initialize` e retornada de forma sincrona (dentro de um timeout de 30 segundos). Todas as respostas subsequentes chegam exclusivamente atraves do stream SSE aberto no Passo 2.
:::
