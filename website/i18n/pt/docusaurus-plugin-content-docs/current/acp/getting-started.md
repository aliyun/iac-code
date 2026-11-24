---
sidebar_position: 2
title: Primeiros passos
description: Inicie o servidor ACP e conecte seu primeiro cliente.
---

# Primeiros passos com ACP

## Pre-requisitos

1. **iac-code instalado** — Consulte o guia de [Instalacao](../getting-started/installation.md).

2. **Credenciais LLM configuradas** — Configure **Configurações → Modelos** no Desktop ou Web, ou forneça os parâmetros para automação. Veja [Autenticação](../configuration/authentication.md).

3. **Python ACP SDK** (opcional, para clientes programaticos)

   O SDK oficial em Python esta publicado no PyPI como **`agent-client-protocol`** (importado como `acp`). Os exemplos nesta pagina foram verificados com a versao `0.9.0`:

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## Iniciando o servidor ACP

### Modo Stdio (padrao)

```bash
iac-code acp
```

O servidor comunica-se via stdin/stdout usando JSON-RPC. Este e o modo usado quando um IDE lanca o iac-code como subprocesso.

### Modo HTTP+SSE

```bash
iac-code acp --transport http --port 8765
```

Escuta na porta especificada. Os clientes conectam-se via HTTP para requisicoes e recebem atualizacoes em streaming atraves de Server-Sent Events. Adequado para cenarios remotos ou multi-cliente.

Pode proteger o endpoint HTTP definindo a variavel de ambiente `IACCODE_ACP_HTTP_TOKEN` — o servidor exigira um cabecalho `Authorization: Bearer <token>` correspondente.

### Verifique se funciona

```bash
# Stdio: o processo deve iniciar e aguardar entrada JSON-RPC no stdin
iac-code acp

# HTTP: verifique o endpoint de saude
curl http://127.0.0.1:8765/health
```

## Exemplo minimo

Um exemplo minimo em Python usando o SDK oficial `agent-client-protocol`. Para um guia mais completo (renderizacao de chamadas de ferramentas, chunks de raciocinio, transporte HTTP+SSE), consulte [Exemplos](./examples.md).

```python
"""Minimal iac-code ACP client using agent-client-protocol==0.9.0."""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        # Stream assistant text to stdout; ignore other update kinds in this minimal demo.
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        # Auto-approve for demonstration — use interactive approval in production.
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main() -> None:
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. Initialize — negotiate capabilities
        init_result = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Protocol version: {init_result.protocol_version}")

        # 2. Create a session tied to your project directory
        session = await conn.new_session(cwd="/path/to/project")
        print(f"Session ID: {session.session_id}")

        # 3. Send a prompt; streaming output is delivered via MyClient.session_update
        result = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Generate a VPC template with 2 VSwitches",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. Clean up
        await conn.close_session(session_id=session.session_id)


asyncio.run(main())
```

Pontos-chave:

- `acp.spawn_agent_process` lanca `iac-code acp` como subprocesso e gerencia o ciclo de vida do stdio.
- `new_session(cwd=...)` limita as operacoes de arquivo ao diretorio especificado.
- As atualizacoes em streaming (chunks de texto, pensamentos, chamadas de ferramentas) chegam atraves do callback `session_update` na sua subclasse `acp.Client` — `prompt()` em si retorna um unico `PromptResponse` quando o turno termina, com o `stop_reason` final.
- Quando uma solicitacao de permissao chega, `request_permission` deve retornar um `AllowedOutcome(outcome="selected", option_id=...)` ou um `DeniedOutcome(outcome="cancelled")` — qualquer outro valor dispara um `pydantic.ValidationError`.

## Configuracao do cliente

O iac-code funciona com qualquer editor ou cliente compativel com ACP. A configuracao abaixo aplica-se ao **Zed** e **VSCode**:

```json
{
  "agent_servers": {
    "iac-code": {
      "type": "custom",
      "command": "iac-code",
      "args": ["acp"]
    }
  }
}
```

- **Zed** — Adicione o trecho ao seu `settings.json` do Zed. O Zed suporta nativamente servidores de agentes ACP.
- **VSCode** — Precisa instalar primeiro uma extensao de cliente ACP (qualquer extensao que suporte o Agent Client Protocol) e depois aplicar a mesma configuracao nas definicoes da extensao.

## Proximos passos

- [Referencia do protocolo](./protocol-reference.md) — Documentacao completa de metodos e eventos
- [Transporte HTTP+SSE](./http-transport.md) — Implantacao remota e autenticacao por token
