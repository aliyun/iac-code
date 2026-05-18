---
sidebar_position: 2
title: Primeros pasos
description: Inicia el servidor A2A y envía tu primer mensaje.
---

# Primeros pasos con A2A

## Requisitos previos

1. **iac-code instalado** — Consulta la guía de [instalación](/docs/getting-started/installation).

2. **Credenciales de LLM configuradas** — Consulta la guía de [autenticación](/docs/configuration/authentication) para configurar las credenciales de tu proveedor de modelo.

3. **Dependencias del servidor A2A** — Instala iac-code con el extra `a2a`:

```bash
uv sync --extra a2a
```

## Iniciar el servidor A2A

Inicia el servidor en la interfaz local predeterminada:

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

Usa un archivo de configuración YAML cuando necesites estado local, almacenamiento de artefactos, entrega de notificaciones push o Agent Cards firmadas:

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
```

Ejecútalo con:

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` habilita los métodos de configuración de notificaciones push de tareas A2A y la entrega de estados terminales. Usa `push-queue: redis-streams` con `push-redis-url` cuando varios workers necesiten coordinar la entrega push.

El servidor expone:

| Ruta | Propósito |
|------|-----------|
| `GET /health` | Comprobación de salud |
| `GET /.well-known/agent-card.json` | Descubrimiento de Agent Card |
| `POST /` | Endpoint A2A JSON-RPC |

El servidor HTTP también registra las rutas REST del SDK de A2A y anuncia interfaces `JSONRPC` y `HTTP+JSON` en la Agent Card.

## Verificar el descubrimiento

Obtén la Agent Card:

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Deberías ver `name: "iac-code"`, interfaces `JSONRPC` y `HTTP+JSON`, encabezados de caché como `ETag`, la extensión opcional `urn:iac-code:a2a:artifact-metadata:v1`, modos de entrada soportados y skills como `iac_generation`, `iac_review`, `aliyun_ros_operations` y `terraform_ros_conversion`.

Comprueba el endpoint de salud:

```bash
curl http://127.0.0.1:41242/health
```

Respuesta esperada:

```json
{"status":"healthy"}
```

## Requerir autenticación

La autenticación es opcional. Si no se establecen opciones de autenticación A2A ni variables de entorno, las solicitudes no necesitan autenticación. Cuando se configura cualquier esquema de autenticación, cada solicitud, incluido el descubrimiento de Agent Card, debe satisfacer uno de los esquemas configurados.

### Token Bearer

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

La clave equivalente de configuración YAML es `token`.

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

El nombre de usuario y la contraseña deben estar presentes. Las claves equivalentes de configuración YAML son `basic-username` y `basic-password`.

### Clave de API

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

El encabezado predeterminado de clave de API es:

```text
X-API-Key: <api-key>
```

Sobrescríbelo con la clave de configuración YAML `api-key-header` o `IACCODE_A2A_API_KEY_HEADER`:

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## Llamar a un agente A2A remoto

Pon los ajustes estables de conexión del cliente y autenticación en un archivo YAML:

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
```

Usa `a2a-client call` para una llamada directa de cliente de Fase 1:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

Usa `--stream` cuando quieras eventos incrementales en lugar de una única respuesta final:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

Las opciones de línea de comandos sobrescriben los valores de configuración cuando necesitas un destino o token puntual:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

Para enrutamiento multiagente, previsualiza la selección de ruta antes de llamar:

```bash
iac-code a2a-route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

Consulta la [referencia de comandos](./command-reference.md) para todos los comandos A2A, incluida la gestión de tareas, CRUD de configuración push, Agent Cards extendidas y opciones de transporte.

## Enviar un primer mensaje con curl

Pasa el directorio del espacio de trabajo mediante `message.metadata.iac_code.cwd`; la ruta debe ser absoluta, ya debe existir y debe estar dentro de una raíz de espacio de trabajo permitida. De forma predeterminada, las raíces permitidas son el directorio del proceso del servidor y el directorio temporal del sistema. Sobrescríbelas con `IACCODE_A2A_ALLOWED_CWDS`.

El servidor acepta partes similares a texto, partes de datos JSON, texto UTF-8 sin procesar, archivos de texto locales `file://` del espacio de trabajo y adjuntos multimodales acotados. La ingesta de URL remotas no está soportada; las partes `url` deben ser URL locales `file://` dentro del espacio de trabajo permitido.

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

Para salida en streaming, usa `SendStreamingMessage`:

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

## Ejemplo mínimo con el SDK de Python

El ejemplo siguiente usa `a2a-sdk>=1.0.2,<2`, que es el rango de versiones utilizado por el extra `a2a`.

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
Para servidores autenticados, construye el `httpx.AsyncClient` con `headers={"Authorization": "Bearer <token>"}` para que tanto el descubrimiento de Agent Card como las llamadas JSON-RPC incluyan el token.
:::

## Próximos pasos

- [Referencia de comandos](./command-reference.md) — Referencia completa de comandos y opciones de CLI.
- [Referencia del protocolo](./protocol-reference.md) — Detalles de métodos, rutas, estados y metadatos.
- [Transporte HTTP](./http-transport.md) — Comportamiento HTTP JSON-RPC, autenticación bearer y flujos de trabajo con curl.
- [Ejemplos](./examples.md) — Ejemplos de SDK, HTTP directo, seguimiento, cancelación y manejo de metadatos.
