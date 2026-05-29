---
title: Referência de comandos
description: Referência completa de comandos da CLI para executar e chamar iac-code sobre A2A.
sidebar_position: 3
---

# Referência de comandos A2A

Esta página documenta todos os comandos `iac-code` relacionados a A2A. Use-a quando precisar dos nomes exatos das opções, padrões comuns de comandos e o significado operacional de cada flag.

## Visão geral dos comandos

| Comando | Finalidade |
|---------|------------|
| `iac-code a2a` | Executar o iac-code como servidor A2A |
| `iac-code a2a-client call` | Descobrir um Agent Card remoto e enviar um prompt |
| `iac-code a2a-client discover` | Buscar e opcionalmente verificar um Agent Card |
| `iac-code a2a-client task-get` | Buscar uma tarefa por ID |
| `iac-code a2a-client task-list` | Listar tarefas com filtros e paginação |
| `iac-code a2a-client task-cancel` | Cancelar uma tarefa ativa |
| `iac-code a2a-client task-subscribe` | Assinar um stream de eventos de uma tarefa ativa |
| `iac-code a2a-client push-config-create` | Criar uma configuração de notificação push de tarefa |
| `iac-code a2a-client push-config-get` | Buscar uma configuração de notificação push de tarefa |
| `iac-code a2a-client push-config-list` | Listar configurações de notificação push de tarefa |
| `iac-code a2a-client push-config-delete` | Excluir uma configuração de notificação push de tarefa |
| `iac-code a2a-client extended-card` | Buscar o Agent Card estendido autenticado |
| `iac-code a2a-route-preview` | Pré-visualizar a seleção local de rota para `a2a-client call` |

Todos os comandos de cliente HTTP aceitam as mesmas opções de autenticação:

| Opção | Descrição |
|-------|-----------|
| `--token` | Bearer token enviado como `Authorization: Bearer <token>` |
| `--basic-username` | Nome de usuário Basic auth |
| `--basic-password` | Senha Basic auth |
| `--api-key` | Valor da API key |
| `--api-key-header` | Nome do cabeçalho da API key; padrão `X-API-Key` |

## Configuração do cliente A2A

Todos os subcomandos `a2a-client` aceitam um arquivo de configuração YAML no nível do grupo:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

Opções da CLI substituem valores de configuração. Use configuração para conexão estável, autenticação, verificação, roteamento e configurações repetidas de tarefas ou push; mantenha texto de prompt pontual na linha de comando.

```yaml
url: http://127.0.0.1:41242/
token: your-bearer-token
basic-username: iac-code
basic-password: your-password
api-key: your-api-key
api-key-header: X-IAC-Code-Key
verify-card-secret: your-card-signing-secret
verify-card-jwks-url: https://a2a.example.com/.well-known/jwks.json
require-card-signature: true
timeout: 30
cwd: /path/to/workspace
context-id: ctx-123
task-id: task-123
config-id: webhook-1
callback-url: https://hooks.example.com/a2a
notification-token: notification-token
auth-scheme: bearer
auth-credentials: callback-token
routes:
  - name: ros
    url: http://127.0.0.1:41242/
    skills:
      - iac_generation
    tags:
      - ros
      - template
```

## `iac-code a2a`

Execute o iac-code como um servidor A2A.

```bash
iac-code a2a
```

Por padrão, o servidor faz bind em `127.0.0.1:41242` e serve JSON-RPC sobre HTTP. A porta `41242` é o padrão do iac-code; ela não é uma porta A2A registrada.

### Opções básicas do servidor

| Opção | Padrão | Descrição |
|-------|--------|-----------|
| `--config` | vazio | Arquivo de configuração YAML contendo opções do servidor A2A |
| `--host` | `127.0.0.1` | Host do servidor HTTP |
| `--port` | `41242` | Porta do servidor HTTP |
| `--transport` | `http` | Transport do servidor: `http`, `stdio`, `unix`, `websocket`, `grpc`, `grpc-jsonrpc` ou `redis-streams` |
| `--thinking-exposure` | `tool-trace` | Expõe um tipo de sinal de thinking A2A; repita para múltiplos. Valores: `raw-thinking`, `tool-trace` |
| `--debug`, `-d` | `false` | Habilitar logs de debug |

Exemplo:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### Configuração YAML

Use `--config` para autenticação, armazenamento, assinatura, configurações específicas de transport, entrega push e outros detalhes de implantação. Chaves podem usar hífens ou underscores. As flags comuns da CLI `--host`, `--port` e `--transport` substituem valores do arquivo de configuração.

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
```

Execute com:

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### Autenticação HTTP

A autenticação é opcional. Configure a autenticação do servidor em YAML ou com variáveis de ambiente. Se nenhuma configuração de autenticação estiver definida, as requisições não são autenticadas. Quando um ou mais esquemas estiverem configurados, uma requisição pode satisfazer qualquer esquema configurado.

| Chave de configuração | Variável de ambiente | Descrição |
|--------|----------------------|-----------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Bearer token |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Nome de usuário Basic auth |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Senha Basic auth |
| `api-key` | `IACCODE_A2A_API_KEY` | Valor da API key |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | Nome do cabeçalho da API key |

Bearer token:

```yaml
token: local-dev-token
```

Basic auth:

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

API key:

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### Persistência e artefatos

| Chave de configuração | Padrão | Descrição |
|--------|--------|-----------|
| `persistence-dir` | `~/.iac-code/a2a` | Metadados JSON locais para tarefas, contextos, rotas e configurações push |
| `artifact-dir` | `<persistence-dir>/artifacts` | Armazenamento local de payloads de artefatos |

A persistência espelha snapshots de tarefas e contextos para metadados de restauração. Ela não reinicia uma tarefa asyncio em andamento após uma falha do processo.

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Assinatura de Agent Card

| Chave de configuração | Descrição |
|--------|-----------|
| `signing-secret` | Segredo HMAC usado para assinar o Agent Card público |

O servidor emite campos JWS `AgentCardSignature` do SDK A2A. O modo simétrico usa `HS256`.

```yaml
signing-secret: local-card-signing-secret
```

### Entrega de notificações push

| Chave de configuração | Padrão | Descrição |
|--------|--------|-----------|
| `push-notifications` | `false` | Habilitar métodos de configuração de notificação push de tarefas A2A e entrega de estados terminais |
| `push-queue` | `local-file` | Backend de fila push: `local-file` ou `redis-streams` |
| `push-redis-url` | vazio | URL Redis para a fila push baseada em Redis |
| `push-stream` | `iac-code:a2a:push` | Stream Redis para jobs push |
| `push-retry-key` | `iac-code:a2a:push:retry` | Sorted set Redis para retries atrasados |
| `push-dead-stream` | `iac-code:a2a:push:dead` | Stream Redis para jobs dead-letter |
| `push-consumer-group` | `iac-code-push` | Consumer group Redis para workers push |
| `push-consumer-name` | vazio | Nome do consumidor Redis para este worker |
| `push-lease-timeout-ms` | `300000` | Timeout de lease pendente no Redis |

Fila de arquivo local:

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

Fila Redis Streams:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
```

A entrega push baseada em Redis exige o extra `a2a-redis`.

### Opções de transport

| Transport | Comando | Observações |
|-----------|---------|-------------|
| HTTP JSON-RPC e REST | `iac-code a2a --transport http` | Padrão. Anuncia interfaces `JSONRPC` e `HTTP+JSON`. |
| stdio | `iac-code a2a --transport stdio` | Frames JSON-RPC customizados experimentais sobre entrada/saída padrão. |
| Unix socket | `iac-code a2a --config a2a-server.yml --transport unix` | Exige `socket-path` na configuração. |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | Usa `ws-path` da configuração, com padrão `/a2a`. |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | Usa `grpc-host` e `grpc-port` da configuração. |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | Envelope JSON-RPC customizado sobre gRPC. |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | Exige `redis-url` na configuração. |

Opções de transport Redis Streams:

| Chave de configuração | Padrão | Descrição |
|--------|--------|-----------|
| `redis-url` | vazio | URL de conexão Redis; obrigatória para `--transport redis-streams` |
| `request-stream` | `iac-code:a2a:requests` | Nome do stream de requisições |
| `response-stream` | `iac-code:a2a:responses` | Nome do stream de respostas |
| `consumer-group` | `iac-code` | Consumer group do stream de requisições |

### Exposição de thinking

| Chave de configuração | Padrão | Descrição |
|--------|--------|-----------|
| `thinking-exposure` | `tool-trace` | Tipos de sinais de runtime fora da resposta expostos via A2A `metadata.iac_code`. Use uma lista YAML, uma string separada por vírgulas ou flags `--thinking-exposure` repetidas. Os valores suportados são `tool-trace` e `raw-thinking`. |

`tool-trace` preserva os metadados existentes de progresso de ferramenta, permissões e resultados. `raw-thinking` emite chunks de raciocínio do provider como atualizações `metadata.iac_code.thinking` com `type: raw_thinking` e `text`. O iac-code atualmente não produz eventos separados de thought-summary ou progress-summary, portanto eles não são tipos de exposição válidos.

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### Comportamento de permissões

| Chave de configuração | Padrão | Descrição |
|--------|--------|-----------|
| `auto-approve-permissions` | `false` | Aprovar automaticamente solicitações de permissão de ferramentas levantadas durante turnos A2A |

Sem `auto-approve-permissions: true`, o modo A2A rejeita prompts de permissão e emite metadados de permissão. Use-o apenas em ambientes de automação confiáveis.

## `iac-code a2a-client call`

Descobre um Agent Card, escolhe o endpoint anunciado e envia um prompt.

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD"
```

| Opção | Padrão | Descrição |
|-------|--------|-----------|
| `--url` | vazio | URL base do agente A2A ou URL do endpoint JSON-RPC; pode vir da configuração |
| `--route` | repetível | Especificação de rota usada quando `--url` é omitido |
| `--route-name` | vazio | Rota nomeada a selecionar |
| `--prompt`, `-p` | obrigatório | Texto do prompt |
| `--cwd` | `.` | Caminho do workspace enviado como `message.metadata.iac_code.cwd` |
| `--context-id` | vazio | ID de contexto A2A existente para uma mensagem de acompanhamento |
| `--verify-card-secret`, `--signing-secret` | vazio | Segredo HMAC para verificação do Agent Card |
| `--verify-card-jwks-url` | vazio | URL JWKS remota usada para verificação do Agent Card |
| `--require-card-signature`, `--require-signature` | `false` | Rejeitar Agent Cards não assinados ou inválidos |
| `--timeout` | `30.0` | Timeout da chamada em segundos |
| `--stream` | `false` | Usar `SendStreamingMessage` e imprimir eventos do stream |

Acompanhamento no mesmo contexto:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --context-id ctx-123 \
  --prompt "Now add outputs for the VPC and vSwitch IDs." \
  --cwd "$PWD"
```

Streaming:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this Terraform module." \
  --cwd "$PWD" \
  --stream
```

Exigir um Agent Card assinado:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

Verificar usando uma URL JWKS remota:

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

Busca e imprime um Agent Card remoto.

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| Opção | Descrição |
|-------|-----------|
| `--url` | URL base do agente A2A; pode vir da configuração |
| `--verify-card-secret`, `--signing-secret` | Segredo HMAC para verificação |
| `--verify-card-jwks-url` | URL JWKS remota para verificação |
| `--require-card-signature`, `--require-signature` | Exigir uma assinatura válida |

Descoberta autenticada:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## Comandos de tarefa

Comandos de tarefa chamam métodos JSON-RPC de tarefa diretamente. Eles são úteis para ferramentas operacionais, dashboards e depuração.

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| Opção | Descrição |
|-------|-----------|
| `--url` | URL do endpoint A2A JSON-RPC; pode vir da configuração |
| `--task-id` | ID da tarefa; pode vir da configuração |
| `--history-length` | Máximo de entradas de histórico de tarefa a retornar |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| Opção | Padrão | Descrição |
|-------|--------|-----------|
| `--url` | vazio | URL do endpoint A2A JSON-RPC; pode vir da configuração |
| `--context-id` | vazio | Filtrar por ID de contexto |
| `--status` | vazio | Filtrar por estado da tarefa |
| `--page-size` | vazio | Máximo de tarefas a retornar |
| `--page-token` | vazio | Token de paginação |
| `--include-artifacts` | `false` | Incluir artefatos de tarefas na resposta |
| `--output` | `table` | `table` ou `json` |

Saída JSON:

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --include-artifacts \
  --output json
```

### `iac-code a2a-client task-cancel`

```bash
iac-code a2a-client --config a2a-client.yml task-cancel \
  --task-id task-123
```

O cancelamento é cooperativo. Uma tarefa concluída, falha, cancelada ou que exige entrada retorna o erro A2A padrão task-not-cancelable.

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

O comando transmite eventos para tarefas ativas. Para um novo turno, prefira `a2a-client call --stream`; ele inicia a tarefa e transmite atualizações em um único comando.

## Comandos de configuração de notificações push

Estes comandos exigem um servidor iniciado com `push-notifications: true`. Eles gerenciam configurações padrão de notificação push de tarefas A2A.

### `iac-code a2a-client push-config-create`

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

| Opção | Descrição |
|-------|-----------|
| `--url` | URL do endpoint A2A JSON-RPC; pode vir da configuração |
| `--task-id` | ID da tarefa; pode vir da configuração |
| `--config-id` | ID da configuração push; pode vir da configuração |
| `--callback-url` | URL de callback HTTP(S); pode vir da configuração |
| `--notification-token` | Token enviado como `X-A2A-Notification-Token` |
| `--auth-scheme` | Esquema de autenticação do callback, como `bearer` ou `basic` |
| `--auth-credentials` | Credenciais de autenticação do callback |

URLs de callback são validadas antes do armazenamento e envio. O validador padrão rejeita URLs que não sejam HTTP(S), nomes localhost e endereços IP literais privados/locais.

### `iac-code a2a-client push-config-get`

```bash
iac-code a2a-client --config a2a-client.yml push-config-get \
  --task-id task-123 \
  --config-id webhook-1
```

### `iac-code a2a-client push-config-list`

```bash
iac-code a2a-client --config a2a-client.yml push-config-list \
  --task-id task-123 \
  --page-size 10
```

### `iac-code a2a-client push-config-delete`

```bash
iac-code a2a-client --config a2a-client.yml push-config-delete \
  --task-id task-123 \
  --config-id webhook-1
```

## `iac-code a2a-client extended-card`

Busca o Agent Card estendido autenticado.

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

O Agent Card público anuncia `capabilities.extendedAgentCard=true`. O card estendido adiciona detalhes autenticados do runtime, incluindo gerenciamento de tarefas e metadados de capacidade de configuração push.

## `iac-code a2a-route-preview`

Pré-visualize como `a2a-client call` resolve rotas configuradas quando `--url` é omitido.

```bash
iac-code a2a-route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| Opção | Descrição |
|-------|-----------|
| `--route` | Especificação de rota repetível no formato `name=url;skills=a,b;tags=x,y` |
| `--name` | Nome da rota a resolver |
| `--skill` | ID da skill a resolver |
| `--prompt` | Texto de prompt usado para correspondência de nome/tag |
| `--route-state-dir`, `--persistence-dir` | Diretório usado para persistir snapshots de rotas |
| `--save-routes` | Salvar rotas fornecidas no diretório de estado de rotas |

Salvar snapshots de rotas:

```bash
iac-code a2a-route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

Chamar por rotas:

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## Variáveis de ambiente

| Variável | Descrição |
|----------|-----------|
| `IACCODE_A2A_HTTP_TOKEN` | Padrão de Bearer token do servidor/cliente |
| `IACCODE_A2A_BASIC_USERNAME` | Padrão de nome de usuário Basic auth do servidor/cliente |
| `IACCODE_A2A_BASIC_PASSWORD` | Padrão de senha Basic auth do servidor/cliente |
| `IACCODE_A2A_API_KEY` | Padrão de API key do servidor/cliente |
| `IACCODE_A2A_API_KEY_HEADER` | Padrão de nome do cabeçalho da API key |
| `IACCODE_A2A_ALLOWED_CWDS` | Lista separada pelo separador de caminhos do sistema operacional de raízes de workspace permitidas para metadados de mensagens recebidas e URLs de arquivos |
| `IACCODE_A2A_TEXT_MIME_TYPES` | Tipos MIME extras semelhantes a texto, separados por vírgula ou ponto e vírgula |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | Tipos MIME multimodais extras, separados por vírgula ou ponto e vírgula |
| `IAC_CODE_A2A_PUSH_KEYRING` | Keyring criptografado de segredos push gerenciado pelo ambiente |
