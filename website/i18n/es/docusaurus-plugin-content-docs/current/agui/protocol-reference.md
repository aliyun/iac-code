---
sidebar_position: 3
title: Referencia del protocolo
description: Solicitudes, eventos, Interrupt, Resume, cancelación y persistencia de AG-UI en iac-code.
---

# Referencia del protocolo AG-UI

Esta página describe la interfaz HTTP/SSE de `iac-code agui` y las extensiones de iac-code dentro del envelope AG-UI estándar. Consulte antes la [descripción general](./overview.md) y los [primeros pasos](./getting-started.md).

## Endpoints HTTP

| Método y ruta | Uso |
|---------------|-----|
| `GET /health` | Salud y versiones |
| `POST /` | Enviar `RunAgentInput` y recibir SSE |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | Extensión de cancelación |

Use JSON y solicite SSE:

```http
Content-Type: application/json
Accept: text/event-stream
```

Con `IAC_CODE_AGUI_AUTH_TOKEN`, añada `Authorization: Bearer <token>`. `Accept-Language` actúa como idioma alternativo; `forwardedProps.iacCode.preferredLanguage` tiene prioridad y se reenvía a A2A.

## RunAgentInput

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [{"id": "message-1", "role": "user", "content": "Crea una plantilla de VPC"}],
  "tools": [],
  "context": [],
  "forwardedProps": {"iacCode": {
    "schemaVersion": 1,
    "rosInvocationId": "invocation-1",
    "cwd": "/workspace/session-1",
    "runMode": "normal"
  }}
}
```

| Campo estándar | Requisito y comportamiento |
|----------------|---------------------------|
| `threadId` | Obligatorio y estable durante la conversación |
| `runId` | Obligatorio y único por solicitud HTTP/SSE |
| `parentRunId` | Opcional; se copia a `RUN_STARTED` |
| `state` | Obligatorio; no es el estado del runtime de iac-code |
| `messages` | Obligatorio; un run nuevo usa el último mensaje de usuario |
| `tools` | Obligatorio y vacío; no admite herramientas del cliente |
| `context` | Obligatorio; actualmente no se convierte en contexto del prompt |
| `forwardedProps` | Obligatorio con la extensión `iacCode` |
| `resume` | Respuestas a todos los Interrupt pendientes |

Los mensajes admiten texto e imágenes base64 en línea. No se admiten URL remotas, audio, vídeo, documentos ni binarios genéricos. Límites: 8 MiB por imagen, 10 MiB en total y 12 MiB por solicitud.

## `forwardedProps.iacCode`

El schema es estricto y rechaza campos desconocidos.

| Campo | Tipo | Obligatorio | Uso |
|-------|------|-------------|-----|
| `schemaVersion` | `1` | Sí | Versión de extensión |
| `rosInvocationId` | string | Sí | Identidad de la ejecución, máximo 256 caracteres |
| `cwd` | string | Sí | Workspace absoluto |
| `model` / `llmApiKey` | string | No | Modelo y clave LLM por solicitud |
| `thinking.enabled/effort/budget` | boolean/string/entero positivo | No | Opciones de thinking |
| `userId` / `channel` | string | No | Identidad y canal del llamante |
| `preferredLanguage` | string | No | Idioma visible, por ejemplo `es` |
| `candidatePresentation` | `standard` / `rich` | No | Presentación de candidatos |
| `runMode` | `normal` / `pipeline` | No | Modo de ejecución |
| `pipelineName` | string | No | Nombre del Pipeline |
| `cleanupOnly` | boolean | No | Ejecutar solo limpieza |
| `alibabaCloud.accessKeyId` | string | No | AccessKey ID temporal |
| `alibabaCloud.accessKeySecret` | string | No | AccessKey Secret temporal |
| `alibabaCloud.securityToken` | string | No | Token STS temporal |
| `alibabaCloud.regionId` | string | No | Región predeterminada |

El run inicial y sus Resume conservan el mismo `rosInvocationId`. Un turno normal posterior puede usar otro. El mismo `threadId` queda vinculado al primer `cwd` y `userId`.

## SSE y eventos estándar

Tras 15 segundos sin eventos, el servidor envía `: heartbeat`. Es un comentario SSE, no un evento `CUSTOM`.

| Señal | Evento AG-UI |
|-------|-------------|
| Solicitud aceptada | `RUN_STARTED` |
| Texto | `TEXT_MESSAGE_*` |
| Razonamiento | `REASONING_*` |
| Herramienta y argumentos | `TOOL_CALL_START/ARGS/END` |
| Resultado | `TOOL_CALL_RESULT` |
| Paso de Pipeline | `STEP_STARTED/STEP_FINISHED` |
| Snapshot de recuperación | `ACTIVITY_SNAPSHOT` |
| Éxito o espera de entrada | `RUN_FINISHED` con outcome `success` o `interrupt` |
| Error | `RUN_ERROR` |

`RUN_FINISHED` finaliza un run, no necesariamente el Pipeline. Los Interrupt producen runs nuevos. Antes de terminar por Interrupt se cierran los spans abiertos y el nuevo run reabre los pasos duraderos activos; no indica ejecución en orden inverso.

## Eventos personalizados

- `iac-code.session.v1`: relaciones de thread, execution, context, task y session; `executionId` permite cancelar.
- `iac-code.artifact.v1`: proyección de artifacts A2A.
- `iac-code.tool-progress.v1`: progreso intermedio sin equivalente estándar.
- `iac-code.pipeline.v1`: datos útiles del Pipeline sin equivalente estándar.

Tipos de Pipeline admitidos:

- `pipeline_started`, `pipeline_resumed`, `pipeline_completed`, `pipeline_error`, `pipeline_warning`, `backup_blocked`;
- `candidate_started`, `candidate_completed`, `candidate_failed`, `candidate_interrupted`, `candidate_restart_requested`, `candidate_selected`, `candidate_detail_shown`, `candidate_step_failed`;
- `sub_pipeline_started`, `sub_pipeline_completed`, `sub_step_failed`, `step_failed`;
- `stack_progress`, `stack_instances_progress`, `stack_current_changed`, `cleanup_started`, `cleanup_progress`, `cleanup_completed`, `cleanup_failed`;
- `rollback_triggered`, `rollback_completed`;
- `context_compaction_started`, `context_compacted`, `context_compaction_failed`, `fields_marked_stale`;
- `diagram_shown`, `mcp_status`, `tool_progress`.

`text_delta`, `thinking_delta`, `tool_started/tool_result`, `usage` y el ciclo de pasos ya tienen eventos estándar y no se duplican como `CUSTOM`. Deduzca repeticiones por `eventId` o sequence.

## Interrupt y Resume

Cada Interrupt contiene `id`, `reason`, `message`, `responseSchema`, un `toolCallId` opcional y metadata descriptiva. La autorización suele aceptar `{"decision":"allow_once"}` o `{"decision":"deny"}`. La UI debe respetar el schema en vez de deducir la respuesta solo a partir de `reason`.

El adaptador no impone un plazo al Interrupt. Un Interrupt pendiente puede reanudarse hasta que A2A resuelva, cancele o termine la tarea; A2A es el único responsable del ciclo de vida de ejecución y recuperación.

Resume es otra solicitud con el mismo `threadId`, un `runId` nuevo y el mismo `rosInvocationId`:

```json
{"resume": [{
  "interruptId": "permission-1",
  "status": "resolved",
  "payload": {"decision": "allow_once"}
}]}
```

Debe responder exactamente una vez a todos los Interrupt pendientes. `resolved` exige un payload válido; `cancelled` cancela y equivale a `deny` para permisos. Un error de schema genera `RUN_ERROR` y mantiene el Interrupt disponible. Repetir una respuesta aceptada no vuelve a ejecutar la herramienta.

## Identidades y cancelación

Cada solicitud usa un `runId` único dentro del `threadId`; un Resume también es un run nuevo. La idempotencia se limita a `(threadId, runId)`.

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{"threadId": "thread-1", "rosInvocationId": "invocation-1"}
```

Responde `cancelled`, `already_terminal` o `EXECUTION_NOT_FOUND`. La cancelación borra los Interrupt pendientes.

## Persistencia, desconexión y errores

El estado se guarda en `<config-dir>/agui/threads/<thread-key>.json`. Contiene relaciones, identidades, posiciones del Pipeline, Interrupt e idempotencia; carga solo el thread solicitado y sustituye atómicamente un archivo pequeño. No almacena claves LLM, secretos de AccessKey, STS token, texto de conversación ni artifacts. A2A gestiona su propia persistencia; consulte su [documentación](../a2a/overview.md).

Un run terminado con Interrupt ya no depende de su SSE; desconectar un run normal activo cancela la tarea A2A.

Antes de SSE, los errores usan JSON HTTP; durante la ejecución usan `RUN_ERROR`. Los códigos principales son `INVALID_INPUT`, `DUPLICATE_RUN_ID`, `RUN_ID_CONFLICT`, `THREAD_BUSY`, `THREAD_BINDING_CONFLICT`, `RESUME_REQUIRED`, `INCOMPLETE_RESUME`, `UNKNOWN_INTERRUPT`, `RESUME_PAYLOAD_INVALID`, `RESUME_ALREADY_APPLIED`, `EXECUTION_LOST`, `STATE_PERSISTENCE_FAILED`, `A2A_UNAVAILABLE`, `A2A_PROTOCOL_ERROR`, `A2A_EXECUTION_FAILED` y `CANCELLED`.

Las escrituras necesarias para la recuperación fallan de forma cerrada: el adaptador no anuncia un estado recuperable antes de guardarlo y cancela la tarea A2A cuando sea necesario.
