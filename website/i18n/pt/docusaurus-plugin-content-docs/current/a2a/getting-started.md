---
sidebar_position: 2
title: Primeiros passos
description: Inicie o servidor A2A e envie sua primeira mensagem.
---

# Primeiros passos com A2A

## Pré-requisitos

1. **iac-code instalado** — Consulte o guia de [Installation](/docs/getting-started/installation).

2. **Credenciais de LLM configuradas** — Consulte o guia de [Authentication](/docs/configuration/authentication) para configurar as credenciais do seu provedor de modelo.

3. **Dependências do servidor A2A** — Instale o iac-code com o extra `a2a`:

```bash
uv sync --extra a2a
```

## Iniciando o servidor A2A

Inicie o servidor na interface local padrão:

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

Use um arquivo de configuração YAML quando precisar de estado local, armazenamento de artefatos, entrega de notificações push ou Agent Cards assinados:

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
thinking-exposure:
  - tool-trace
  - raw-thinking
```

Execute com:

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` habilita os métodos de configuração de notificações push de tarefas A2A e a entrega de estados terminais. Use `push-queue: redis-streams` com `push-redis-url` quando vários workers precisarem coordenar a entrega push.

`thinking-exposure` controla quais sinais de runtime que não fazem parte da resposta são expostos via `metadata.iac_code` durante streaming. O padrão é `tool-trace`; adicione `raw-thinking` apenas para clientes confiáveis que podem ver conteúdo de raciocínio do provider.

O servidor expõe:

| Rota | Finalidade |
|------|------------|
| `GET /health` | Verificação de saúde |
| `GET /.well-known/agent-card.json` | Descoberta do Agent Card |
| `POST /` | Endpoint A2A JSON-RPC |

O servidor HTTP também registra as rotas REST do SDK A2A e anuncia as interfaces `JSONRPC` e `HTTP+JSON` no Agent Card.

## Verificar descoberta

Busque o Agent Card:

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Você deve ver `name: "iac-code"`, interfaces `JSONRPC` e `HTTP+JSON`, cabeçalhos de cache como `ETag`, a extensão opcional `urn:iac-code:a2a:artifact-metadata:v1`, modos de entrada suportados e skills como `iac_generation`, `iac_review`, `aliyun_ros_operations` e `terraform_ros_conversion`.

Verifique o endpoint de saúde:

```bash
curl http://127.0.0.1:41242/health
```

Resposta esperada:

```json
{"status":"healthy"}
```

## Exigir autenticação

A autenticação é opcional. Se nenhuma opção de autenticação A2A ou variável de ambiente estiver definida, as requisições não precisam de autenticação. Quando qualquer esquema de autenticação estiver configurado, todas as requisições, incluindo a descoberta do Agent Card, devem satisfazer um dos esquemas configurados.

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

A chave equivalente de configuração YAML é `token`.

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

O nome de usuário e a senha devem estar presentes. As chaves equivalentes de configuração YAML são `basic-username` e `basic-password`.

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

O cabeçalho padrão de API key é:

```text
X-API-Key: <api-key>
```

Substitua-o com a chave de configuração YAML `api-key-header` ou `IACCODE_A2A_API_KEY_HEADER`:

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## Chamar um agente A2A remoto

Coloque as configurações estáveis de conexão e autenticação do cliente em um arquivo YAML:

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
```

Use `a2a-client call` para uma chamada direta de cliente Fase 1:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

Use `--stream` quando quiser eventos incrementais em vez de uma resposta final única:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

As opções de linha de comando substituem os valores de configuração quando você precisa de um alvo ou token pontual:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

Para roteamento multiagente, pré-visualize a seleção de rota antes de chamar:

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

Consulte a [Referência de comandos](./command-reference.md) para todos os comandos A2A, incluindo gerenciamento de tarefas, CRUD de configuração push, Agent Cards estendidos e opções de transport.

## Enviar uma primeira mensagem com curl

Passe o diretório do workspace por `message.metadata.iac_code.cwd`; o caminho deve ser absoluto, já deve existir e deve estar dentro de uma raiz de workspace permitida. Por padrão, as raízes permitidas são o diretório do processo do servidor e o diretório temporário do sistema. Substitua-as com `IACCODE_A2A_ALLOWED_CWDS`.

O servidor aceita partes semelhantes a texto, partes de dados JSON, texto UTF-8 bruto, arquivos de texto locais `file://` do workspace e anexos multimodais limitados. Ingestão de URLs remotas não é suportada; partes `url` devem ser URLs locais `file://` dentro do workspace permitido.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Generate a ROS VPC template with two vSwitches."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Para saída em streaming, use `SendStreamingMessage`:

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Review my Terraform files and suggest ROS equivalents."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Exemplo mínimo com o SDK Python

O exemplo abaixo usa `a2a-sdk>=1.0.2,<2`, que é o intervalo de versões usado pelo extra `a2a`.

```python
"""Minimal iac-code A2A client using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Generate a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("status_update"):
                status = event.status_update.status
                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

:::tip
Para servidores autenticados, construa o `httpx.AsyncClient` com `headers={"Authorization": "Bearer <token>"}` para que tanto a descoberta do Agent Card quanto as chamadas JSON-RPC incluam o token.
:::

## Próximos passos

- [Referência de comandos](./command-reference.md) — Referência completa de comandos e opções da CLI.
- [Referência do protocolo](./protocol-reference.md) — Detalhes de método, rota, estado e metadados.
- [Transport HTTP](./http-transport.md) — Comportamento HTTP JSON-RPC, autenticação Bearer e workflows com curl.
- [Exemplos](./examples.md) — Exemplos de SDK, HTTP direto, acompanhamento, cancelamento e tratamento de metadados.
