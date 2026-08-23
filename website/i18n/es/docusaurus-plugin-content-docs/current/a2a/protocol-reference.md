---
title: Referencia del protocolo
description: Referencia completa del protocolo A2A para la integración con iac-code.
sidebar_position: 4
---

# Referencia del protocolo

Este documento describe la superficie A2A 1.0 expuesta por el servidor iac-code y el comportamiento del cliente de Fase 1 usado por `iac-code a2a-client call`. Para opciones exactas de CLI, consulta la [referencia de comandos](./command-reference.md).

## Resumen del ciclo de vida

Una interacción A2A típica sigue este flujo:

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **Descubrir** — Obtén `/.well-known/agent-card.json`.
2. **Enviar** — Envía un mensaje de texto al endpoint JSON-RPC en `/`.
3. **Transmitir** — Recibe payloads `Task`, `Message` y `TaskStatusUpdateEvent`.
4. **Continuar** — Envía un mensaje de seguimiento con el mismo `contextId`.
5. **Cancelar o consultar** — Usa `CancelTask`, `GetTask` o `ListTasks`.

## Agent Card

La Agent Card está disponible en:

```text
GET /.well-known/agent-card.json
```

Campos importantes:

| Campo | Valor | Significado |
|-------|-------|-------------|
| `name` | `iac-code` | Nombre del agente |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | Enlace de transporte |
| `supportedInterfaces[0].protocolVersion` | `1.0` | Versión del protocolo A2A |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | Endpoint JSON-RPC |
| `capabilities.streaming` | `true` | Soporta actualizaciones de tareas en streaming |
| `capabilities.pushNotifications` | `false` o `true` | `true` cuando `push-notifications: true` está configurado |
| `capabilities.extendedAgentCard` | `true` | Los llamadores autenticados pueden solicitar detalles extendidos del runtime |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | Namespace opcional de metadatos de iac-code para estado de herramientas y metadatos de artefactos almacenados |
| `defaultInputModes` | tipos MIME text, JSON, YAML, image, audio y binary | Modos MIME de entrada aceptados |
| `defaultOutputModes` | `["text/plain"]` | Solo salida de texto |

Las respuestas de Agent Card incluyen `Cache-Control: public, max-age=60`, `ETag` y `Last-Modified`. Los clientes pueden enviar `If-None-Match` y recibir `304 Not Modified` cuando la tarjeta no ha cambiado.

Skills anunciadas:

| Skill ID | Propósito |
|----------|-----------|
| `iac_generation` | Generar plantillas Alibaba Cloud ROS y Terraform a partir de lenguaje natural |
| `iac_review` | Inspeccionar plantillas IaC y sugerir correcciones |
| `aliyun_ros_operations` | Ayudar con flujos de trabajo de stacks de Alibaba Cloud ROS |
| `terraform_ros_conversion` | Ayudar en la conversión de Terraform a ROS usando recursos de skills integrados |

Cuando la autenticación está habilitada, la Agent Card anuncia los esquemas de seguridad configurados:

| Esquema | Cuándo se anuncia |
|---------|-------------------|
| `bearerAuth` | `token` o `IACCODE_A2A_HTTP_TOKEN` está establecido |
| `basicAuth` | El usuario y la contraseña de Basic están establecidos |
| `apiKeyAuth` | `api-key` o `IACCODE_A2A_API_KEY` está establecido |

## Rutas

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/health` | `GET` | Devuelve `{"status":"healthy"}` |
| `/.well-known/agent-card.json` | `GET` | Devuelve la Agent Card |
| `/` | `POST` | Maneja solicitudes A2A JSON-RPC |
| Rutas REST | mixto | Las rutas REST del SDK de A2A registradas por `create_rest_routes` |

## Cliente de Fase 1 y notas de transporte

El transporte interoperable predeterminado de Fase 1 es JSON-RPC sobre HTTP. El modo HTTP también anuncia `HTTP+JSON` para las rutas REST del SDK.

El servidor también tiene transportes opcionales para stdio, sockets Unix, WebSocket, gRPC oficial, envoltorio gRPC JSON-RPC y Redis Streams. stdio, sockets Unix, WebSocket, gRPC JSON-RPC y Redis Streams son transportes JSON-RPC personalizados. gRPC oficial se anuncia como `grpc` y requiere dependencias gRPC opcionales.

El cliente integrado usa el descubrimiento de Agent Card (`GET /.well-known/agent-card.json`) antes de las llamadas de mensaje, selecciona el primer `supportedInterfaces[].url` ejecutable anunciado y luego envía solicitudes JSON-RPC con `A2A-Version: 1.0` y nombres de métodos A2A 1.0 como `SendMessage`.

`push-notifications: true` habilita los métodos de configuración de notificaciones push de A2A y la entrega de estados terminales.

La firma de Agent Card usa la utilidad de firma del SDK de A2A y emite campos JWS estándar `AgentCardSignature`. El modo de clave simétrica usa `HS256`; la verificación puede seleccionar un secreto configurado por `kid` del encabezado protegido, un JWKS local de clave octet o una URL JWKS remota. La firma asimétrica del lado del servidor y la rotación automática de claves no están implementadas en la Fase 1.

Para la lista canónica de comportamientos no soportados en Fase 1, consulta [Protocolo A2A](./overview.md#phase-1-unsupported).

## Backends de entrega de notificaciones push

`iac-code a2a --config a2a-server.yml` soporta dos colas de entrega push:

- `push-queue: local-file` almacena trabajos debajo del directorio de persistencia A2A y está pensado para uso local de un solo nodo.
- `push-queue: redis-streams` almacena trabajos en Redis Streams y coordina workers mediante un grupo de consumidores de Redis.

La entrega push respaldada por Redis requiere el extra opcional `a2a-redis` y es al menos una vez. Los receptores de callback deben manejar actualizaciones de tareas de forma idempotente porque un trabajo puede entregarse de nuevo después de fallos de workers, expiración de leases, reconexiones o carreras de reintento.

Opciones comunes de Redis:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

Las URL de callback se validan antes de almacenarlas y nuevamente antes del despacho. El validador predeterminado rechaza URL que no sean HTTP(S), nombres de host localhost y direcciones IP literales privadas/locales. Los receptores de callback aun así deben aplicar su propia política de autenticación e idempotencia.

## Métodos JSON-RPC

### SendMessage

Ejecuta un turno de mensaje A2A sin streaming. La respuesta contiene una tarea o mensaje después de que el turno se haya completado.

**Solicitud**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**Campos de mensaje requeridos**

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `messageId` | string | Sí | ID de mensaje de cliente único |
| `role` | string | Sí | Usa `ROLE_USER` para entrada de usuario |
| `parts` | array | Sí | Partes similares a texto, datos JSON, texto sin procesar, URL de archivo local o partes multimodales acotadas |
| `metadata.iac_code.cwd` | string | Recomendado | Ruta absoluta del espacio de trabajo; si se omite, toma por defecto el directorio del proceso del servidor |
| `metadata.iac_code.channel` | string | Opcional | Canal de telemetría vinculado a este `contextId`; tiene prioridad sobre `IAC_CODE_CHANNEL` |
| `metadata.iac_code.preferredLanguage` | string | Opcional | Idioma de visualización preferido por el llamador para esta tarea; el texto visible para el usuario se localiza por solicitud |
| `metadata.iac_code.candidatePresentation` | string | Opcional | Con `rich-v1`, el paso de confirmación de candidatos del pipeline devuelve cargas estructuradas de presentación enriquecida |

`metadata.iac_code.cwd` debe ser un directorio absoluto existente cuando se proporciona. Debe estar dentro de una raíz de espacio de trabajo permitida. De forma predeterminada, las raíces permitidas son el directorio del proceso del servidor y el directorio temporal del sistema; `IACCODE_A2A_ALLOWED_CWDS` puede proporcionar una lista permitida separada por rutas del sistema operativo.

`metadata.iac_code.channel` vincula `iac_code.channel` al `contextId` de A2A. El valor se recorta, se limita a 128 caracteres y tiene prioridad sobre `IAC_CODE_CHANNEL`; los valores vacíos o que no sean cadenas se ignoran. Los turnos normales, los turnos de pipeline, los seguimientos de input-required y el chat normal después de un handoff de pipeline reutilizan el vínculo con el mismo `contextId`, incluso tras reiniciar el servidor y restaurar el contexto. Un valor válido posterior actualiza el vínculo. Si no existe vínculo, se usa `IAC_CODE_CHANNEL` y después `unknown`.

`metadata.iac_code.preferredLanguage` solo afecta al texto visible para el usuario (progreso, preguntas, avisos de permisos, presentaciones de candidatos, explicaciones de resultados); los nombres de campos del protocolo, los enumerados, los ID y las formas de los comandos nunca se traducen. Los valores aceptados son los idiomas admitidos `en`, `zh`, `es`, `fr`, `de`, `ja`, `pt`; los valores se normalizan recortando espacios, pasando a minúsculas y eliminando el sufijo regional (por ejemplo, `zh-CN` se resuelve a `zh`). Los valores no reconocidos se ignoran y el servidor vuelve a su idioma predeterminado. El campo se aplica solo al turno de mensaje actual; los turnos posteriores que reutilicen el mismo `contextId` deben volver a incluirlo o volverán al idioma predeterminado.

`metadata.iac_code.candidatePresentation` con `rich-v1` hace que el paso de confirmación de candidatos del pipeline selling devuelva una carga estructurada apta para renderizado enriquecido (nombre del candidato, resumen, diagrama de arquitectura, coste mensual total, partidas de coste). Sin el campo, el comportamiento de presentación en texto simple no cambia.

Categorías de entrada soportadas:

| Categoría | Forma aceptada | Límites y comportamiento |
|-----------|----------------|--------------------------|
| Partes similares a texto | `text` con `text/plain`, JSON, Markdown, YAML o tipos MIME de texto extra configurados | Se agregan directamente al prompt |
| Partes de datos JSON | `data` con `application/json` | Serializadas como JSON compacto; máximo 1 MiB inline |
| Partes de texto sin procesar | `raw` con un tipo MIME similar a texto | Deben ser UTF-8 válido; máximo 1 MiB inline |
| URL de archivos de texto locales | `url` con `file://...` y tipo MIME similar a texto | El archivo debe existir dentro de `cwd` y de las raíces permitidas; máximo 1 MiB |
| Partes multimodales raw/data/file | image, audio o tipos MIME multimodales configurados | Convertidas en un manifiesto de prompt con nombre de archivo, tipo de medio, tamaño en bytes, hash y origen; raw/data máximo 5 MiB, URL de archivo máximo 25 MiB |

La ingesta de URL HTTP(S) remotas no está soportada. Las partes de URL de archivo deben usar URL locales `file://` y permanecer dentro del espacio de trabajo permitido.

### SendStreamingMessage

Ejecuta un turno de mensaje A2A en streaming. El cuerpo de la solicitud tiene la misma forma que `SendMessage`, pero el servidor transmite respuestas JSON-RPC como Server-Sent Events.

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

Devuelve la tarea A2A guardada por ID. Usa `historyLength` para limitar el historial devuelto sin mutar el historial de tarea almacenado. Omítelo para recibir el historial predeterminado actual del servidor.

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

Devuelve las tareas conocidas visibles para el llamador autenticado. Los resultados se ordenan por marca de tiempo de estado descendente y luego por ID de tarea descendente para un orden estable. El servidor soporta `contextId`, `status`, `pageSize`, `pageToken`, `historyLength` e `includeArtifacts`.

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

`nextPageToken` se devuelve cuando hay otra página disponible. `includeArtifacts` toma por defecto `false`, por lo que las respuestas de listado omiten los artefactos de tarea salvo que se soliciten explícitamente.

### CancelTask

Solicita la cancelación de una tarea en ejecución.

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

Si la tarea está activa, el servidor cancela el turno de agente en ejecución y emite un estado de tarea cancelado. Si la tarea existe pero no está en ejecución, el servidor devuelve el `TaskNotCancelableError` estándar de A2A.

### SubscribeToTask

Se suscribe a un stream de actualizaciones de una tarea activa cuando el transporte del cliente lo soporta.

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

Para tareas activas, el stream comienza con el `Task` actual, luego emite eventos de tarea posteriores y se cierra cuando termina el turno activo. Suscribirse a una tarea completada, fallida, cancelada o que requiere entrada devuelve un error de estilo tarea no encontrada en lugar de esperar indefinidamente. Para turnos nuevos, prefiere `SendStreamingMessage`; inicia la ejecución y transmite la respuesta en una solicitud.

### Métodos de configuración de notificaciones push

Cuando el servidor inicia con `push-notifications: true`, soporta:

| Método | Propósito |
|--------|-----------|
| `CreateTaskPushNotificationConfig` | Almacenar una configuración de callback para una tarea |
| `GetTaskPushNotificationConfig` | Obtener una configuración de callback |
| `ListTaskPushNotificationConfigs` | Listar configuraciones de callback de una tarea |
| `DeleteTaskPushNotificationConfig` | Eliminar una configuración de callback |

Ejemplo de solicitud de creación:

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

El servidor cifra los tokens de notificación almacenados y las credenciales de autenticación de callback cuando el keyring push local está disponible.

### GetExtendedAgentCard

Los clientes autenticados pueden solicitar la Agent Card extendida:

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

La tarjeta extendida incluye la tarjeta pública más detalles autenticados del runtime.

## Comportamiento de tareas y contextos

iac-code asigna contextos A2A a runtimes internos de agente:

| Concepto | Comportamiento |
|----------|----------------|
| `contextId` omitido | El SDK/servidor genera un nuevo ID de contexto |
| Mismo `contextId` | Reutiliza la misma sesión interna de iac-code y el estado de conversación |
| Mismo `contextId`, distinto `cwd` | Rechazado como un espacio de trabajo diferente |
| Mismo `contextId`, mensaje concurrente | Rechazado con `Task is already working.` |
| Valores de `contextId` diferentes | Pueden ejecutarse concurrentemente |
| Contexto inactivo | Expulsado de memoria después del timeout de inactividad configurado |

Los IDs de tarea y contexto no deben estar vacíos, pueden tener como máximo 128 caracteres y solo pueden contener letras, dígitos, `_`, `.`, `:` o `-`.

## Estados de tarea

| Estado | Significado |
|--------|-------------|
| `TASK_STATE_SUBMITTED` | La tarea fue aceptada |
| `TASK_STATE_WORKING` | iac-code está ejecutando el turno del agente |
| `TASK_STATE_INPUT_REQUIRED` | El turno se completó y el agente está listo para entrada de seguimiento |
| `TASK_STATE_CANCELED` | Se solicitó y aplicó la cancelación |
| `TASK_STATE_FAILED` | La tarea falló en validación o ejecución |

iac-code usa `TASK_STATE_INPUT_REQUIRED` como estado completado normal porque el contexto queda disponible para mensajes de seguimiento. Los turnos A2A ordinarios tratan `TASK_STATE_INPUT_REQUIRED` como ese estado normal de finalización; su backup correcto de fin de turno es el checkpoint no bloqueante `normal_turn_end`. Si una publicación terminal o de cancelación A2A ordinaria queda bloqueada por una barrera crítica de respaldo, los metadatos de tarea exponen `metadata.iac_code.backupBlocked` con `reason`, `error` y `recoverable`; los clientes deben esperar la recuperación antes de enviar el siguiente turno. El modo Pipeline publica los fallos de backup como eventos de pipeline, por lo que los clientes deben inspeccionar `metadata.iac_code.pipeline.eventType == "backup_blocked"` y los datos del evento. El modo Pipeline ya no crea backups al completar steps; `pipeline_step_completed` se conserva solo para recuperación heredada. Con `IAC_CODE_CONFIG_BACKUP_DIR` habilitado, `input_required` y `waiting_input` se publican antes de ejecutar su único backup crítico. La razón `terminal` sigue protegiendo las publicaciones terminales, y `handoff_ready` sigue protegiendo el evento `pipeline_handoff_ready`. Para publicaciones committed terminales y `pipeline_handoff_ready`, iac-code persiste un evento de pipeline `backup_committed` con `committedEventId`, `committedEventType` y `committedSequence`; los clientes pueden emparejarlo con el evento protegido antes de tratar la publicación como recuperable tras reiniciar.

Como un mismo estado terminal lógico puede aparecer varias veces en el flujo de eventos, cada publicación protegida por backup incluye un booleano explícito `authoritative` junto a `visibility`. La preescritura `pending_backup` y el `pipeline_handoff_ready` degradado (`visibility: "unavailable"`, `data.action: "switch_to_normal_unavailable"`) son `authoritative: false`; solo la publicación `committed` es `authoritative: true`. Los clientes que cuentan eventos terminales para auditoría o monitoreo deben fusionar por `(eventType, pipelineRunId)` y conservar solo los eventos autoritativos; de lo contrario, una única cancelación se cuenta como varios `pipeline_canceled` y `pipeline_handoff_ready`. Los sobres sin ese campo son anteriores y deben recurrir a `visibility`.

Cuando el servidor A2A usa `IAC_CODE_CONFIG_BACKUP_TMP_DIR`, `backup_committed` confirma el snapshot local inmutable y permite recuperar dentro del mismo sandbox; no confirma que el directorio final se haya actualizado. La recuperación en otro sandbox queda disponible después de que el proceso en segundo plano copie el snapshot a `IAC_CODE_CONFIG_BACKUP_DIR`. Sin preparación local, `backup_committed` sigue confirmando el directorio final.

## Actualizaciones en streaming

Durante la ejecución, iac-code emite actualizaciones `TaskStatusUpdateEvent`.

El texto del asistente se entrega como un mensaje de estado:

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

Los detalles de herramientas y uso se entregan mediante `metadata.iac_code`:

| Ruta de metadatos | Descripción |
|-------------------|-------------|
| `iac_code.tool.status` | `started`, `input_delta`, `input_complete`, `completed` o `failed` |
| `iac_code.tool.toolUseId` | ID estable de uso de herramienta para correlacionar eventos de herramienta |
| `iac_code.tool.name` | Nombre de la herramienta cuando está disponible |
| `iac_code.tool.inputSummary` | Resumen solo con forma de la entrada completada de la herramienta; los valores de cadena y los nombres de campo fuera de la lista permitida se representan sin payloads de negocio sin procesar |
| `iac_code.tool.result` | Resultado de la herramienta, truncado a 4000 caracteres por campo |
| `iac_code.permission.autoApproved` | Si A2A aprobó la solicitud de permiso automáticamente o mediante un resolver |
| `iac_code.permission.toolName` | Herramienta que solicitó el permiso |
| `iac_code.permission.safeSummary` | Resumen de permisos redactado y legible por humanos |
| `iac_code.permission.inputSummary` | Resumen seguro de operación para solicitudes de permisos de `aliyun_api` |
| `iac_code.permission.toolInput` | Entrada de herramienta solo con forma para solicitudes de permisos que no son de `aliyun_api`; los valores de cadena usan tipo, longitud y huella, y los nombres de campo fuera de la lista permitida pueden representarse con huella |
| `iac_code.mcpWarning` | Advertencia MCP de configuración o ejecución redactada, con server name, warning code, mensaje público, scope y source path cuando está disponible |
| `iac_code.mcpStatus` | Instantánea redactada del estado de servidores MCP con connection state, authentication state, approval state, capability counts, capability refresh errors y advertencias de configuración |
| `iac_code.mcpProgress` | MCP tool progress metadata con public tool name, original server/tool names, tool-use ID, progress, total y mensaje público opcional |
| `iac_code.thinking.type` | `raw_thinking` cuando `raw-thinking` está habilitado en `thinking-exposure` |
| `iac_code.thinking.text` | Chunk bruto de razonamiento del provider, truncado a 4000 caracteres, emitido solo para configuraciones de confianza que habilitan `raw-thinking` |
| `iac_code.usage.inputTokens` | Recuento de tokens de entrada del turno |
| `iac_code.usage.outputTokens` | Recuento de tokens de salida del turno |
| `iac_code.usage.totalTokens` | Recuento total de tokens del turno |

El modo pipeline también puede publicar el estado MCP como metadata de pipeline con `metadata.iac_code.pipeline.eventType == "mcp_status"`. Las advertencias MCP, las instantáneas de estado y la metadata de progreso se sanitizan antes del streaming; los secretos de headers, variables de entorno, tokens OAuth y rutas locales se redactan o resumen.

Cuando un resultado de herramienta incluye un payload de artefacto de texto soportado, el servidor almacena el payload localmente, emite un `TaskArtifactUpdateEvent` estándar y registra el artefacto en el campo `artifacts` de la tarea. La parte de artefacto usa una URL `file://` más metadatos como `mediaType`, `byteSize` y `sha256`; el contenido original del artefacto no se duplica dentro de los metadatos de herramienta.

### Decisiones interactivas de permisos

Con la configuración predeterminada, una solicitud de permiso de herramienta durante un turno A2A se convierte en una espera de entrada: iac-code pausa el turno, emite una actualización de estado `TASK_STATE_INPUT_REQUIRED` con un sobre de permiso y espera la decisión estructurada del llamador.

Los metadatos de la actualización de estado contienen:

- `metadata.iac_code.input` — el sobre de permiso (`schemaVersion` 1), con los campos:
  - Campos de correlación: `kind: "permission"`, `requestTaskId`, `contextId`, `inputId`, `toolUseId`, `toolName`
  - Campos de visualización: `title`, `purpose`, `effect`, `target`, `isReadOnly`, `safeSummary`, más `deploymentSummary` en solicitudes de despliegue
  - `prompt` y `options` (`allow_once` / `deny`), localizados al idioma preferido del llamador
- `metadata.iac_code.permission` — contiene `autoApproved: false`, `pending: true`, `toolName`, `toolUseId`

El llamador envía su decisión mediante un mensaje de banda lateral: un único mensaje A2A `ROLE_USER` que contiene exactamente un DataPart `application/json` cuyo payload debe contener exactamente estos campos:

```json
{
  "schemaVersion": 1,
  "kind": "permission",
  "requestTaskId": "<taskId que emitió la solicitud de permiso>",
  "inputId": "<inputId del sobre>",
  "toolUseId": "<toolUseId del sobre>",
  "decision": "allow_once"
}
```

- `decision` solo acepta `allow_once` o `deny`.
- El `taskId` externo del mensaje debe ser igual a `requestTaskId`; `contextId` procede del sobre externo del mensaje. Todos los campos de correlación deben conservarse literalmente; no deben reutilizarse entre solicitudes ni reinterpretarse una respuesta de una entrada como otra.
- Cuando el servidor empareja la decisión, devuelve un DataPart `permission_ack` (`schemaVersion: 1`, `kind: "permission_ack"`, con `inputId`, `toolUseId`, `decision`, `accepted: true`) y emite una actualización de estado `TASK_STATE_WORKING` con `metadata.iac_code.inputReceived`; el turno se reanuda.

En modo pipeline, las solicitudes de permiso se publican como eventos de pipeline (el sobre incluye además `scope` y coordenadas de paso/candidato); el formato de respuesta de banda lateral es idéntico.

Con `auto-approve-permissions` habilitado o reglas de permisos explícitas configuradas, las solicitudes de permiso no se convierten en entrada interactiva; se aprueban automáticamente (con auditoría) o se resuelven según las reglas. Las API protegidas de escritura de Alibaba Cloud no quedan liberadas por reglas allow ordinarias y siguen requiriendo autorización exacta por API. Las decisiones de permisos se auditan localmente; toda decisión allow que requiere un registro de auditoría falla en modo cerrado si el registro no puede persistirse.

## Extensiones

La Agent Card anuncia la extensión opcional de metadatos de artefactos de iac-code:

```text
urn:iac-code:a2a:artifact-metadata:v1
```

Esta extensión identifica el namespace `metadata.iac_code` usado para progreso de herramientas, decisiones de permisos, uso de tokens y metadatos de artefactos locales. Si el servidor está configurado con alguna extensión requerida, los clientes deben incluir su URI en el encabezado `A2A-Extensions`. Las extensiones requeridas ausentes devuelven el `ExtensionSupportRequiredError` estándar de A2A.

Cuando `thinking-exposure` habilita uno o más tipos de señal, la Agent Card también anuncia:

```text
urn:iac-code:a2a:thinking-exposure:v1
```

Los params de la extensión incluyen `enabledTypes`, como `["tool_trace", "raw_thinking"]`. Esta es una extensión de metadatos de iac-code, no un tipo de evento de streaming central de A2A.

## Manejo de errores

| Escenario | Resultado |
|-----------|-----------|
| Entrada de texto vacía | `TASK_STATE_FAILED` con `A2A server currently accepts text input only.` |
| Tipo de medio no soportado | Error de validación o error estándar de tipo de contenido de A2A, según dónde el SDK rechace la solicitud |
| Parte de URL remota | Error de validación porque las partes de URL deben usar URL locales `file://` |
| URL de archivo fuera del espacio de trabajo permitido | Error de validación |
| Extensión A2A requerida ausente | `ExtensionSupportRequiredError` estándar de A2A |
| Metadatos de espacio de trabajo no válidos | `TASK_STATE_FAILED` con un mensaje de espacio de trabajo no válido |
| Autenticación ausente o no válida | HTTP `401` con `{"error":"Unauthorized"}` |
| Dependencias del servidor A2A ausentes | La CLI sale con una pista de instalación para el extra `a2a` |
| Credenciales de proveedor ausentes | Error de autenticación saneado |
| Error inesperado de runtime | Error interno saneado |

El servidor evita devolver rutas locales, secretos y detalles del proveedor en mensajes de error inesperados.
