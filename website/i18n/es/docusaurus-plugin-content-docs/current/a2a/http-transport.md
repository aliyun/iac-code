---
title: Transporte HTTP
description: Ejecuta y llama al servidor A2A de iac-code sobre HTTP JSON-RPC.
sidebar_position: 5
---

# Transporte HTTP

El servidor A2A predeterminado de iac-code expone JSON-RPC sobre HTTP, además de las rutas REST del SDK de A2A. El servidor está construido con Starlette y se ejecuta en Uvicorn.

## Iniciar el servidor

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

Instala primero las dependencias opcionales del servidor:

```bash
uv sync --extra a2a
```

## Resumen de endpoints

| Ruta | Método | Respuesta |
|------|--------|-----------|
| `/health` | `GET` | Respuesta de salud JSON simple |
| `/.well-known/agent-card.json` | `GET` | JSON de Agent Card |
| `/` | `POST` | Respuesta JSON-RPC o stream SSE |
| Rutas REST del SDK | mixto | Endpoints REST de A2A registrados por el SDK |

## Encabezados

Encabezados recomendados:

```text
Content-Type: application/json
A2A-Version: 1.0
```

Cuando la autenticación Bearer está habilitada:

```text
Authorization: Bearer <token>
```

## Autenticación

El servidor soporta autenticación opcional mediante token Bearer, Basic auth y clave de API. Si no se establecen opciones de autenticación ni variables de entorno, las solicitudes no necesitan autenticación. Si se configuran uno o más esquemas, una solicitud puede autenticarse con cualquier esquema configurado.

### Token Bearer

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

También puedes establecer `token` en el archivo de configuración YAML de A2A.

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Tanto el nombre de usuario como la contraseña deben establecerse para que Basic auth se habilite.

### Clave de API

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

El encabezado predeterminado de clave de API es `X-API-Key`. Puedes cambiarlo en YAML:

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

o con `IACCODE_A2A_API_KEY_HEADER`.

| Escenario | Comportamiento |
|-----------|----------------|
| Ningún esquema de autenticación configurado | No se requiere autenticación |
| Uno o más esquemas configurados, cualquiera coincide | La solicitud continúa |
| Uno o más esquemas configurados, ningún esquema coincide | HTTP `401` con `{"error":"Unauthorized"}` |

## Descubrimiento de Agent Card

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Autenticado:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

Con autenticación por clave de API:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

La URL del endpoint JSON-RPC se anuncia en `supportedInterfaces[0].url`. El modo HTTP también anuncia una interfaz `HTTP+JSON` para clientes compatibles con REST.

## Mensaje sin streaming

`SendMessage` devuelve una única respuesta JSON-RPC después de que termina el turno del agente.

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

## Mensaje en streaming

`SendStreamingMessage` devuelve Server-Sent Events. Usa `curl -N` para imprimir los eventos a medida que llegan.

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

Cada línea SSE `data:` contiene una respuesta JSON-RPC cuyo `result` es una `StreamResponse` de A2A.

## Mensaje de seguimiento

Usa el `taskId` y el `contextId` devueltos por la primera respuesta para continuar la misma conversación.

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

El espacio de trabajo debe seguir siendo el mismo para el `contextId` reutilizado.

## Cancelar una tarea en ejecución

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

La cancelación es cooperativa: iac-code cancela el turno activo del agente, emite un estado cancelado y libera el bloqueo del contexto. Cancelar una tarea existente que ya no está en ejecución devuelve el `TaskNotCancelableError` estándar de A2A.

## Equivalentes de CLI

La mayoría de los flujos de trabajo HTTP tienen un comando CLI equivalente:

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

Para la lista completa de opciones, consulta la [referencia de comandos](./command-reference.md).

## Notas operativas

- Enlaza a `127.0.0.1` para uso solo local.
- Usa `token` en la configuración A2A o `IACCODE_A2A_HTTP_TOKEN` antes de enlazar a una interfaz de red compartida.
- El modo A2A rechaza automáticamente las solicitudes de permisos de herramientas salvo que `auto-approve-permissions` o una regla de permisos explícita las permita. Las decisiones de permisos se auditan localmente; toda decisión allow que requiere un registro de auditoría falla en modo cerrado si ese registro no se puede persistir. Las API protegidas de escritura de Alibaba Cloud requieren autorización exacta por API fuera de los modos de bypass global.
- El estado activo del runtime está en memoria. La persistencia refleja metadatos de tareas y contextos, pero reiniciar el proceso no reanuda trabajo asyncio en curso.
- Un contexto solo puede ejecutar una tarea a la vez; los contextos separados pueden ejecutarse de forma concurrente.
