---
title: Referencia de comandos
description: Referencia completa de comandos CLI para ejecutar y llamar a iac-code sobre A2A.
sidebar_position: 3
---

# Referencia de comandos A2A

Esta página documenta todos los comandos de `iac-code` relacionados con A2A. Úsala cuando necesites nombres exactos de opciones, patrones comunes de comandos y el significado operativo de cada flag.

## Resumen de comandos

| Comando | Propósito |
|---------|-----------|
| `iac-code a2a` | Ejecutar iac-code como servidor A2A |
| `iac-code a2a-client call` | Descubrir una Agent Card remota y enviar un prompt |
| `iac-code a2a-client discover` | Obtener y opcionalmente verificar una Agent Card |
| `iac-code a2a-client task-get` | Obtener una tarea por ID |
| `iac-code a2a-client task-list` | Listar tareas con filtros y paginación |
| `iac-code a2a-client task-cancel` | Cancelar una tarea activa |
| `iac-code a2a-client task-subscribe` | Suscribirse a un stream de eventos de una tarea activa |
| `iac-code a2a-client push-config-create` | Crear una configuración de notificación push de tarea |
| `iac-code a2a-client push-config-get` | Obtener una configuración de notificación push de tarea |
| `iac-code a2a-client push-config-list` | Listar configuraciones de notificación push de tarea |
| `iac-code a2a-client push-config-delete` | Eliminar una configuración de notificación push de tarea |
| `iac-code a2a-client extended-card` | Obtener la Agent Card extendida autenticada |
| `iac-code a2a-client route-preview` | Previsualizar la selección local de ruta para `a2a-client call` |

Todos los comandos de cliente HTTP aceptan las mismas opciones de autenticación:

| Opción | Descripción |
|--------|-------------|
| `--token` | Token Bearer enviado como `Authorization: Bearer <token>` |
| `--basic-username` | Nombre de usuario de Basic auth |
| `--basic-password` | Contraseña de Basic auth |
| `--api-key` | Valor de clave de API |
| `--api-key-header` | Nombre del encabezado de clave de API; por defecto es `X-API-Key` |

## Configuración del cliente A2A

Todos los subcomandos `a2a-client` aceptan un archivo de configuración YAML a nivel de grupo:

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

Las opciones de CLI sobrescriben los valores de configuración. Usa la configuración para conexión estable, autenticación, verificación, enrutamiento y ajustes repetidos de tareas o push; mantén el texto de prompts puntuales en la línea de comandos.

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

Ejecuta iac-code como servidor A2A.

```bash
iac-code a2a
```

De forma predeterminada, el servidor se enlaza a `127.0.0.1:41242` y sirve JSON-RPC sobre HTTP. El puerto `41242` es el predeterminado de iac-code; no es un puerto A2A registrado.

### Opciones básicas del servidor

| Opción | Predeterminado | Descripción |
|--------|----------------|-------------|
| `--config` | vacío | Archivo de configuración YAML que contiene opciones del servidor A2A |
| `--host` | `127.0.0.1` | Host del servidor HTTP |
| `--port` | `41242` | Puerto del servidor HTTP |
| `--transport` | `http` | Transporte del servidor: `http`, `stdio`, `unix`, `websocket`, `grpc`, `grpc-jsonrpc` o `redis-streams` |
| `--thinking-exposure` | `tool-trace` | Expone un tipo de señal de thinking A2A; repite para varios. Valores: `raw-thinking`, `tool-trace` |
| `--debug`, `-d` | `false` | Habilitar logging de depuración |

Ejemplo:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### Configuración YAML

Usa `--config` para autenticación, almacenamiento, firma, ajustes específicos de transporte, entrega push y otros detalles de despliegue. Las claves pueden usar guiones o guiones bajos. Los flags comunes de CLI `--host`, `--port` y `--transport` sobrescriben los valores del archivo de configuración.

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
```

Ejecútalo con:

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### Autenticación HTTP

La autenticación es opcional. Configura la autenticación del servidor en YAML o con variables de entorno. Si no se configura ningún ajuste de autenticación, las solicitudes no están autenticadas. Cuando se configuran uno o más esquemas, una solicitud puede satisfacer cualquier esquema configurado.

| Clave de configuración | Variable de entorno | Descripción |
|--------|----------------------|-------------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Token Bearer |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Nombre de usuario de Basic auth |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Contraseña de Basic auth |
| `api-key` | `IACCODE_A2A_API_KEY` | Valor de clave de API |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | Nombre del encabezado de clave de API |

Token Bearer:

```yaml
token: local-dev-token
```

Basic auth:

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

Clave de API:

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### Persistencia y artefactos

| Clave de configuración | Predeterminado | Descripción |
|--------|---------|-------------|
| `persistence-dir` | `~/.iac-code/a2a` | Metadatos JSON locales para tareas, contextos, rutas y configuraciones push |
| `artifact-dir` | `<persistence-dir>/artifacts` | Almacén local de payloads de artefactos |

La persistencia refleja instantáneas de tareas y contextos como metadatos de restauración. No reinicia una tarea asyncio en curso después de un fallo del proceso.

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Firma de Agent Card

| Clave de configuración | Descripción |
|--------|-------------|
| `signing-secret` | Secreto HMAC usado para firmar la Agent Card pública |

El servidor emite campos JWS `AgentCardSignature` del SDK de A2A. El modo simétrico usa `HS256`.

```yaml
signing-secret: local-card-signing-secret
```

### Entrega de notificaciones push

| Clave de configuración | Predeterminado | Descripción |
|--------|---------|-------------|
| `push-notifications` | `false` | Habilitar métodos de configuración de notificaciones push de tareas A2A y entrega de estados terminales |
| `push-queue` | `local-file` | Backend de cola push: `local-file` o `redis-streams` |
| `push-redis-url` | vacío | URL de Redis para la cola push respaldada por Redis |
| `push-stream` | `iac-code:a2a:push` | Stream de Redis para trabajos push |
| `push-retry-key` | `iac-code:a2a:push:retry` | Conjunto ordenado de Redis para reintentos retrasados |
| `push-dead-stream` | `iac-code:a2a:push:dead` | Stream de Redis para trabajos de dead-letter |
| `push-consumer-group` | `iac-code-push` | Grupo de consumidores Redis para workers push |
| `push-consumer-name` | vacío | Nombre de consumidor Redis para este worker |
| `push-lease-timeout-ms` | `300000` | Timeout de lease pendiente de Redis |

Cola de archivo local:

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

Cola Redis Streams:

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

La entrega push respaldada por Redis requiere el extra `a2a-redis`.

### Opciones de transporte

| Transporte | Comando | Notas |
|------------|---------|-------|
| HTTP JSON-RPC y REST | `iac-code a2a --transport http` | Predeterminado. Anuncia interfaces `JSONRPC` y `HTTP+JSON`. |
| stdio | `iac-code a2a --transport stdio` | Frames JSON-RPC personalizados experimentales sobre entrada/salida estándar. |
| Socket Unix | `iac-code a2a --config a2a-server.yml --transport unix` | Requiere `socket-path` en la configuración. |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | Usa `ws-path` desde la configuración, con valor predeterminado `/a2a`. |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | Usa `grpc-host` y `grpc-port` desde la configuración. |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | Envoltorio JSON-RPC personalizado sobre gRPC. |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | Requiere `redis-url` en la configuración. |

Opciones de transporte Redis Streams:

| Clave de configuración | Predeterminado | Descripción |
|--------|---------|-------------|
| `redis-url` | vacío | URL de conexión Redis; requerida para `--transport redis-streams` |
| `request-stream` | `iac-code:a2a:requests` | Nombre del stream de solicitudes |
| `response-stream` | `iac-code:a2a:responses` | Nombre del stream de respuestas |
| `consumer-group` | `iac-code` | Grupo de consumidores del stream de solicitudes |

### Exposición de thinking

| Clave de configuración | Predeterminado | Descripción |
|--------|---------|-------------|
| `thinking-exposure` | `tool-trace` | Tipos de señales de runtime que no son respuesta expuestas mediante A2A `metadata.iac_code`. Usa una lista YAML, una cadena separada por comas o flags `--thinking-exposure` repetidas. Los valores admitidos son `tool-trace` y `raw-thinking`. |

`tool-trace` conserva los metadatos existentes de progreso de herramientas, permisos y resultados. `raw-thinking` emite chunks de razonamiento del provider como actualizaciones `metadata.iac_code.thinking` con `type: raw_thinking` y `text`. iac-code no produce actualmente eventos separados de thought-summary o progress-summary, por lo que no son tipos de exposición válidos.

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### Comportamiento de permisos

| Clave de configuración | Predeterminado | Descripción |
|--------|---------|-------------|
| `auto-approve-permissions` | `false` | Aprobar automáticamente solicitudes de permisos de herramientas generadas durante turnos A2A |

Sin `auto-approve-permissions: true`, el modo A2A rechaza solicitudes de permisos y emite metadatos de permisos. Cuando está habilitado, las decisiones de permisos se escriben en el registro local de auditoría de permisos; toda decisión allow que requiere un registro de auditoría falla en modo cerrado si ese registro no se puede persistir. Las API protegidas de escritura de Alibaba Cloud no se aprueban de forma global con reglas allow ordinarias; configura reglas allow exactas `aliyun_api(product:action)` para automatización confiable.

## `iac-code a2a-client call`

Descubre una Agent Card, elige el endpoint anunciado y envía un prompt.

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD"
```

| Opción | Predeterminado | Descripción |
|--------|----------------|-------------|
| `--url` | vacío | URL base del agente A2A o URL del endpoint JSON-RPC; puede venir de la configuración |
| `--route` | repetible | Especificación de ruta usada cuando `--url` se omite |
| `--route-name` | vacío | Ruta con nombre que seleccionar |
| `--prompt`, `-p` | requerido | Texto del prompt |
| `--cwd` | `.` | Ruta de espacio de trabajo enviada como `message.metadata.iac_code.cwd` |
| `--context-id` | vacío | ID de contexto A2A existente para un mensaje de seguimiento |
| `--iac-code-model` | vacío | Modelo LLM enviado como `message.metadata.iac_code.iac_code_model`; anula la configuración del modelo del servidor solo para este turno de mensaje |
| `--iac-code-api-key` | vacío | API key del proveedor LLM enviada como `message.metadata.iac_code.iac_code_api_key`; anula `IAC_CODE_API_KEY` y `.credentials.yml` solo para este turno de mensaje |
| `--thinking-enabled`, `--no-thinking-enabled` | vacío | Política booleana de thinking enviada como `message.metadata.iac_code.thinking.enabled`; si se omite, conserva el valor predeterminado del servidor/proveedor |
| `--thinking-effort` | vacío | Effort de thinking enviado como `message.metadata.iac_code.thinking.effort` solo para este turno de mensaje |
| `--thinking-budget` | vacío | Presupuesto de thinking como entero positivo enviado como `message.metadata.iac_code.thinking.budget` solo para este turno de mensaje |
| `--verify-card-secret`, `--signing-secret` | vacío | Secreto HMAC para verificación de Agent Card |
| `--verify-card-jwks-url` | vacío | URL JWKS remota usada para verificación de Agent Card |
| `--require-card-signature`, `--require-signature` | `false` | Rechazar Agent Cards sin firmar o inválidas |
| `--timeout` | `30.0` | Timeout de llamada en segundos |
| `--stream` | `false` | Usar `SendStreamingMessage` e imprimir eventos de stream |

`--iac-code-api-key` es la clave que usa el runtime iac-code remoto para llamar a su proveedor LLM. Es distinta de `--api-key`, que autentica la solicitud HTTP A2A en sí.

Las opciones de thinking son metadatos de solicitud por mensaje. También se pueden proporcionar en el YAML del cliente A2A como `thinking-enabled`, `thinking-effort` y `thinking-budget`; los flags de línea de comandos anulan los valores de configuración. Si se omiten las tres, el cliente no envía metadatos de thinking y el runtime remoto conserva los valores predeterminados configurados del proveedor. Para endpoints `openai_compatible` respaldados por el modo compatible de DashScope, una política de thinking explícita usa los parámetros wire nativos de DashScope, por lo que `--no-thinking-enabled` puede enviar `extra_body.enable_thinking=false`. Emitir raw thinking de vuelta al cliente A2A sigue requiriendo que el servidor habilite `thinking-exposure: raw-thinking`.

```yaml
thinking-enabled: false
thinking-effort: low
thinking-budget: 2048
```

Seguimiento en el mismo contexto:

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

Requerir una Agent Card firmada:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

Verificar usando una URL JWKS remota:

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

Obtiene e imprime una Agent Card remota.

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| Opción | Descripción |
|--------|-------------|
| `--url` | URL base del agente A2A; puede venir de la configuración |
| `--verify-card-secret`, `--signing-secret` | Secreto HMAC para verificación |
| `--verify-card-jwks-url` | URL JWKS remota para verificación |
| `--require-card-signature`, `--require-signature` | Requerir una firma válida |

Descubrimiento autenticado:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## Comandos de tareas

Los comandos de tareas llaman directamente a métodos de tarea JSON-RPC. Son útiles para herramientas operativas, paneles y depuración.

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| Opción | Descripción |
|--------|-------------|
| `--url` | URL del endpoint A2A JSON-RPC; puede venir de la configuración |
| `--task-id` | ID de tarea; puede venir de la configuración |
| `--history-length` | Entradas máximas de historial de tarea que devolver |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| Opción | Predeterminado | Descripción |
|--------|----------------|-------------|
| `--url` | vacío | URL del endpoint A2A JSON-RPC; puede venir de la configuración |
| `--context-id` | vacío | Filtrar por ID de contexto |
| `--status` | vacío | Filtrar por estado de tarea |
| `--page-size` | vacío | Máximo de tareas que devolver |
| `--page-token` | vacío | Token de paginación |
| `--include-artifacts` | `false` | Incluir artefactos de tarea en la respuesta |
| `--output` | `table` | `table` o `json` |

Salida JSON:

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

La cancelación es cooperativa. Una tarea completada, fallida, cancelada o que requiere entrada devuelve el error estándar A2A de tarea no cancelable.

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

El comando transmite eventos para tareas activas. Para un nuevo turno, prefiere `a2a-client call --stream`; inicia la tarea y transmite actualizaciones en un solo comando.

## Comandos de configuración de notificaciones push

Estos comandos requieren un servidor iniciado con `push-notifications: true`. Gestionan configuraciones estándar de notificaciones push de tareas A2A.

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

| Opción | Descripción |
|--------|-------------|
| `--url` | URL del endpoint A2A JSON-RPC; puede venir de la configuración |
| `--task-id` | ID de tarea; puede venir de la configuración |
| `--config-id` | ID de configuración push; puede venir de la configuración |
| `--callback-url` | URL de callback HTTP(S); puede venir de la configuración |
| `--notification-token` | Token enviado como `X-A2A-Notification-Token` |
| `--auth-scheme` | Esquema de autenticación del callback, como `bearer` o `basic` |
| `--auth-credentials` | Credenciales de autenticación del callback |

Las URL de callback se validan antes del almacenamiento y del despacho. El validador predeterminado rechaza URL que no sean HTTP(S), nombres localhost y direcciones IP literales privadas/locales.

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

Obtiene la Agent Card extendida autenticada.

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

La Agent Card pública anuncia `capabilities.extendedAgentCard=true`. La tarjeta extendida agrega detalles autenticados del runtime, incluidos metadatos de capacidades de gestión de tareas y configuración push.

## `iac-code a2a-client route-preview`

Previsualiza cómo `a2a-client call` resuelve rutas configuradas cuando `--url` se omite.

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| Opción | Descripción |
|--------|-------------|
| `--route` | Especificación de ruta repetible en formato `name=url;skills=a,b;tags=x,y` |
| `--name` | Nombre de ruta que resolver |
| `--skill` | ID de skill que resolver |
| `--prompt` | Texto de prompt usado para coincidencia de nombre/etiqueta |
| `--route-state-dir`, `--persistence-dir` | Directorio usado para persistir instantáneas de rutas |
| `--save-routes` | Guardar las rutas proporcionadas en el directorio de estado de rutas |

Guardar instantáneas de rutas:

```bash
iac-code a2a-client route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

Llamar mediante rutas:

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## Variables de entorno

| Variable | Descripción |
|----------|-------------|
| `IACCODE_A2A_HTTP_TOKEN` | Valor predeterminado del token Bearer de servidor/cliente |
| `IACCODE_A2A_BASIC_USERNAME` | Valor predeterminado del nombre de usuario de Basic auth de servidor/cliente |
| `IACCODE_A2A_BASIC_PASSWORD` | Valor predeterminado de la contraseña de Basic auth de servidor/cliente |
| `IACCODE_A2A_API_KEY` | Valor predeterminado de clave de API de servidor/cliente |
| `IACCODE_A2A_API_KEY_HEADER` | Nombre predeterminado del encabezado de clave de API |
| `IACCODE_A2A_ALLOWED_CWDS` | Lista separada por rutas del sistema operativo de raíces de espacio de trabajo permitidas para metadatos de mensajes entrantes y URL de archivos |
| `IACCODE_A2A_TEXT_MIME_TYPES` | Tipos MIME extra similares a texto separados por comas o punto y coma |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | Tipos MIME multimodales extra separados por comas o punto y coma |
| `IAC_CODE_A2A_PUSH_KEYRING` | Keyring de secretos push cifrados gestionado por el entorno |
