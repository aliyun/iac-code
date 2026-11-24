---
sidebar_position: 2
title: Primeros pasos
description: Lanzar el servidor ACP y conectar tu primer cliente.
---

# Primeros pasos con ACP

## Requisitos previos

1. **iac-code instalado** — Consulta la guia de [Instalacion](../getting-started/installation.md).

2. **Credenciales LLM configuradas** — Configure **Ajustes → Modelos** en escritorio o Web, o proporcione los parámetros para automatización. Consulte [Autenticación](../configuration/authentication.md).

3. **SDK de Python para ACP** (opcional, para clientes programaticos)

   El SDK oficial de Python se publica en PyPI como **`agent-client-protocol`** (importado como `acp`). Los ejemplos en esta pagina estan verificados con la version `0.9.0`:

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## Iniciar el servidor ACP

### Modo Stdio (predeterminado)

```bash
iac-code acp
```

El servidor se comunica a traves de stdin/stdout usando JSON-RPC. Este es el modo utilizado cuando un IDE lanza iac-code como subproceso.

### Modo HTTP+SSE

```bash
iac-code acp --transport http --port 8765
```

Escucha en el puerto especificado. Los clientes se conectan via HTTP para las solicitudes y reciben actualizaciones en streaming a traves de Server-Sent Events. Adecuado para escenarios remotos o multi-cliente.

Puedes asegurar el endpoint HTTP configurando la variable de entorno `IACCODE_ACP_HTTP_TOKEN` — el servidor requerira un encabezado `Authorization: Bearer <token>` coincidente.

### Verificar que funciona

```bash
# Stdio: el proceso deberia iniciarse y esperar entrada JSON-RPC en stdin
iac-code acp

# HTTP: verificar el endpoint de salud
curl http://127.0.0.1:8765/health
```

## Ejemplo minimo

Un ejemplo minimo en Python usando el SDK oficial `agent-client-protocol`. Para un recorrido mas detallado (renderizado de llamadas a herramientas, fragmentos de razonamiento, transporte HTTP+SSE), consulta [Ejemplos](./examples.md).

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

Puntos clave:

- `acp.spawn_agent_process` lanza `iac-code acp` como un subproceso y gestiona su ciclo de vida de stdio.
- `new_session(cwd=...)` limita las operaciones de archivos al directorio indicado.
- Las actualizaciones en streaming (fragmentos de texto, razonamientos, llamadas a herramientas) llegan a traves del callback `session_update` en tu subclase de `acp.Client` — `prompt()` en si devuelve un unico `PromptResponse` una vez que el turno termina, con el `stop_reason` final.
- Cuando llega una solicitud de permiso, `request_permission` debe devolver un `AllowedOutcome(outcome="selected", option_id=...)` o un `DeniedOutcome(outcome="cancelled")` — cualquier otro valor genera un `pydantic.ValidationError`.

## Configuracion del cliente

iac-code funciona con cualquier editor o cliente compatible con ACP. La configuracion siguiente se aplica a **Zed** y **VSCode**:

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

- **Zed** — Agrega el fragmento a tu `settings.json` de Zed. Zed soporta nativamente servidores de agentes ACP.
- **VSCode** — Necesitas instalar primero una extension de cliente ACP (cualquier extension que soporte el Agent Client Protocol), y luego aplicar la misma configuracion en los ajustes de la extension.

## Siguientes pasos

- [Referencia del protocolo](./protocol-reference.md) — Documentacion completa de metodos y eventos
- [Transporte HTTP+SSE](./http-transport.md) — Despliegue remoto y autenticacion con token
