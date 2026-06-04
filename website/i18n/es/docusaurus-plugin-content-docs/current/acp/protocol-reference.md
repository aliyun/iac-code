---
title: Referencia del protocolo
description: Referencia completa de metodos y eventos del protocolo ACP para la integracion con iac-code.
sidebar_position: 3
---

# Referencia del protocolo

Este documento proporciona una referencia completa de los metodos y eventos de streaming del protocolo ACP (Agent Client Protocol) expuestos por el servidor iac-code.

## Vision general del ciclo de vida

Una sesion ACP tipica sigue este flujo:

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** — Handshake. Negocia la version del protocolo y descubre las capacidades del servidor.
2. **session/new** — Crea una sesion nueva con un runtime de agente independiente.
3. **session/prompt** — Envia entrada del usuario; recibe eventos en streaming hasta una respuesta final.
4. **session/close** — Libera la sesion y sus recursos.

Las sesiones tambien pueden cargarse desde el historial (`session/load`) o reanudarse (`session/resume`) en lugar de crear nuevas.

---

## Metodos

### initialize

Handshake del protocolo. Debe ser la primera llamada en cada conexion.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `protocolVersion` | integer | Si | Version del protocolo solicitada (actualmente `1`) |
| `clientInfo` | object | No | Nombre y version del cliente |
| `clientCapabilities` | object | No | Capacidades que soporta el cliente |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `protocolVersion` | integer | Version del protocolo negociada |
| `agentCapabilities` | object | Capacidades del servidor (ver abajo) |
| `agentInfo` | object | Nombre y version del servidor |
| `authMethods` | array | Metodos de autenticacion disponibles (vacio si se usan credenciales integradas) |

**Capacidades del agente**

| Capacidad | Valor | Significado |
|-----------|-------|---------|
| `loadSession` | `true` | Soporta restaurar sesiones desde el historial |
| `promptCapabilities.embeddedContext` | `true` | Acepta contenido de recursos incrustados en prompts |
| `promptCapabilities.image` | `false` | Entrada de imagen no soportada (degrada a marcador de texto) |
| `promptCapabilities.audio` | `false` | Entrada de audio no soportada (degrada a marcador de texto) |
| `sessionCapabilities.list` | `{}` | Soporta listar sesiones |
| `sessionCapabilities.close` | `{}` | Soporta cerrar sesiones |

---

### session/new

Crea una nueva sesion con un runtime de agente independiente, registro de herramientas y contexto LLM.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `cwd` | string | Si | Ruta absoluta al directorio de trabajo |
| `mcpServers` | object | No | Configuracion de servidores MCP (aceptada pero aun no funcional) |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `sessionId` | string | Identificador unico de sesion para llamadas posteriores |
| `modes` | object | Modos disponibles y modo actual |
| `models` | object | Modelos disponibles y modelo actual |

:::note
Cada sesion crea un AgentLoop independiente. Multiples sesiones pueden ejecutarse de forma concurrente, pero cada una consume una conexion LLM.
:::

---

### session/load

Carga una sesion previamente persistida desde disco, restaurando su historial de mensajes.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `cwd` | string | Si | Ruta del directorio de trabajo |
| `sessionId` | string | Si | ID de la sesion a cargar |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `models` | object | Modelos disponibles y estado del modelo actual |
| `modes` | object | Modos disponibles y estado del modo actual |

:::note
Cargar una sesion lee el historial desde `~/.iac-code/sessions/`, repara automaticamente los mensajes interrumpidos e inyecta el historial en un nuevo AgentLoop.
:::

---

### session/fork

Bifurca una sesion existente para crear una rama independiente con el mismo historial.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `cwd` | string | Si | Ruta del directorio de trabajo |
| `sessionId` | string | Si | ID de la sesion a bifurcar |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `sessionId` | string | Nuevo ID de sesion para la rama bifurcada |
| `models` | object | Modelos disponibles y estado del modelo actual |
| `modes` | object | Modos disponibles y estado del modo actual |

---

### session/resume

Reanuda o reconecta a una sesion existente. Carga automaticamente el historial si es necesario.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `cwd` | string | Si | Ruta del directorio de trabajo |
| `sessionId` | string | Si | ID de la sesion a reanudar |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `models` | object | Modelos disponibles y estado del modelo actual (opcional) |
| `modes` | object | Modos disponibles y estado del modo actual (opcional) |

:::note
A diferencia de `session/new`, la respuesta no incluye un campo `sessionId` ya que el cliente ya conoce el ID de sesion de la solicitud.
:::

---

### session/prompt

Envia entrada del usuario y activa respuestas de streaming del agente.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `sessionId` | string | Si | ID de la sesion objetivo |
| `prompt` | array | Si | Array de bloques de contenido (ver Tipos de bloque de contenido abajo) |

**Tipos de bloque de contenido**

| Tipo | Descripcion |
|------|-------------|
| `TextContentBlock` | Entrada de texto plano del usuario |
| `EmbeddedResourceContentBlock` | Contenido de archivo incrustado en linea |
| `ResourceContentBlock` | Referencia de enlace a recurso |
| `ImageContentBlock` | Imagen (degrada a marcador de texto `[image: mime/type]`) |
| `AudioContentBlock` | Audio (degrada a marcador de texto `[audio: mime/type]`) |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `stopReason` | string | Por que se completo el prompt (ver Razones de parada) |
| `usage` | object | Uso de tokens: `inputTokens`, `outputTokens`, `totalTokens` |

**Razones de parada**

| Valor | Significado |
|-------|---------|
| `end_turn` | El modelo completo normalmente |
| `max_turn_requests` | Alcanzo el limite maximo del bucle de llamadas a herramientas |
| `max_tokens` | Alcanzo el limite de tokens de salida |
| `cancelled` | El cliente cancelo el prompt |
| `refusal` | El modelo se nego a responder |

:::note
Durante la ejecucion, el servidor envia notificaciones `session/update` con eventos de streaming antes de devolver la respuesta final.
:::

---

### session/cancel

Cancela una tarea de prompt en ejecucion.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `sessionId` | string | Si | Sesion con el prompt en ejecucion |

**Comportamiento**

- Deja de consumir eventos del flujo
- Las herramientas en ejecucion no se terminan forzosamente, pero sus resultados se descartan
- La llamada `prompt` pendiente devuelve con `stopReason: "cancelled"`

---

### session/close

Cierra una sesion y libera sus recursos.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `sessionId` | string | Si | Sesion a cerrar |

**Comportamiento**

- La sesion se elimina de la memoria
- El historial persistido permanece en disco
- Las llamadas `prompt` posteriores a esta sesion devuelven un error

---

### sessions/list

Lista todas las sesiones persistidas para un directorio de trabajo dado.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `cwd` | string | Si | Directorio de trabajo para delimitar el listado |

**Campos de respuesta**

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `sessions` | array | Lista de objetos de sesion con `sessionId` y metadatos |

---

### config/set

Establece dinamicamente una opcion de configuracion para una sesion.

**Parametros de solicitud**

| Campo | Tipo | Requerido | Descripcion |
|-------|------|----------|-------------|
| `sessionId` | string | Si | Sesion objetivo |
| `configId` | string | Si | Clave de configuracion a establecer |
| `value` | any | Si | Nuevo valor |

---

## Eventos de streaming

Durante la ejecucion de `session/prompt`, el servidor envia notificaciones `session/update` que contienen datos de eventos de streaming.

### Formato del evento

Cada notificacion `session/update` lleva un objeto de actualizacion con un tipo especifico:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### Mapeo de tipos de evento

| Evento interno | Tipo de actualizacion ACP | Descripcion |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | Salida incremental de texto del agente |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | Contenido de razonamiento/pensamiento del modelo |
| `ToolUseStartEvent` | `ToolCallStart` | Comienza la invocacion de herramienta |
| `ToolResultEvent` | `ToolCallProgress` | Resultado de herramienta (completado o fallido) |
| `CompactionEvent` | `AgentMessageChunk` | Notificacion de compactacion de contexto |
| `ErrorEvent` | `AgentMessageChunk` | Informacion de error |

### Ciclo de vida de llamada a herramienta

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, raw_input=tool input)
    │
    ├── ToolCallProgress (status=completed, raw_output=result)   ← success
    │
    └── ToolCallProgress (status=failed, raw_output=error)       ← failure
```

**Mapeo de tipo de herramienta**

| Herramienta | ACP ToolKind |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| Otras | `other` |

---

## Solicitudes de permisos

Antes de ejecutar herramientas de alto riesgo, iac-code envia un callback `request_permission` al cliente.

### Categorias de permisos de herramientas

| Categoria | Herramientas | Auto-permitidas |
|----------|-------|-------------|
| Solo lectura | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | Si |
| Escritura | `write_file`, `edit_file` | No — requiere aprobacion |
| Ejecucion | `bash`, `agent` | No — requiere aprobacion |

### Evento request_permission

El servidor envia un callback `request_permission` con:

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| `options` | array | Opciones de permisos disponibles |
| `sessionId` | string | Sesion que solicita el permiso |
| `toolCall` | object | Detalles de la llamada a herramienta (titulo, tipo, entrada) |

### Opciones de permisos

| ID de opcion | Significado |
|-----------|---------|
| `allow_once` | Permitir esta invocacion especifica |
| `allow_always` | Permitir todas las futuras llamadas de esta herramienta en esta sesion cuando la herramienta admite permiso global; no se ofrece para `bash` por defecto |
| `allow_rule:<rules>` | Permitir futuras llamadas que coincidan con las reglas sugeridas en esta sesion |
| `deny_rule:<rules>` | Denegar futuras llamadas que coincidan con las reglas sugeridas en esta sesion |
| `reject_once` | Denegar esta invocacion especifica |
| `reject_always` | Denegar todas las futuras llamadas de esta herramienta en esta sesion |

### Formato de respuesta

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

O para denegar:

```json
{
  "outcome": "denied"
}
```

| Respuesta del cliente | Comportamiento de la herramienta |
|----------------|---------------|
| `AllowedOutcome` | La herramienta se ejecuta normalmente |
| `DeniedOutcome` | La herramienta se omite; el modelo recibe un error "Permission denied." |

---

## Manejo de errores

### Formato de RequestError

Los errores siguen el formato de error JSON-RPC 2.0:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### Codigos de error comunes

| Codigo | Nombre | Descripcion |
|------|------|-------------|
| `-32700` | Error de analisis | JSON invalido |
| `-32600` | Solicitud invalida | JSON-RPC malformado |
| `-32601` | Metodo no encontrado | Metodo desconocido |
| `-32602` | Parametros invalidos | Parametros faltantes o invalidos (por ejemplo, ID de sesion desconocido) |
| `-32603` | Error interno | Fallo del lado del servidor |
