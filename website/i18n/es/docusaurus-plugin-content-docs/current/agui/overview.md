---
sidebar_position: 1
title: Protocolo AG-UI
description: Arquitectura, funciones y casos de uso de la integración AG-UI de iac-code.
---

# Protocolo AG-UI

## Qué es AG-UI

El [Agent-User Interaction Protocol (AG-UI)](https://docs.ag-ui.com/concepts/architecture) es un protocolo de eventos entre agentes y aplicaciones orientadas al usuario. El cliente inicia una ejecución mediante `RunAgentInput` y recibe por HTTP Server-Sent Events (SSE) eventos estructurados de texto, razonamiento, herramientas, pasos, estado e interrupciones.

Resulta adecuado para consolas web, clientes de chat, extensiones de IDE y otras interfaces que deban mostrar la ejecución en tiempo real. En lugar de presentar solo el texto final, pueden representar por separado la respuesta del modelo, los argumentos y resultados de herramientas, los pasos del Pipeline y las operaciones pendientes de confirmación.

## Arquitectura de iac-code

iac-code utiliza un **núcleo de ejecución A2A y un adaptador de protocolo AG-UI**:

```text
AG-UI client
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Agent loop / Pipeline / LLM / Alibaba Cloud API
```

`iac-code a2a` es el único núcleo de ejecución y gestiona:

- conversaciones normales y ejecución de Pipelines;
- sesiones de iac-code, contextos y tareas A2A;
- permisos de herramientas, preguntas, selección de opciones y recuperación;
- ciclo de vida y cancelación;
- llamadas al LLM y a las API de Alibaba Cloud.

`iac-code agui` no crea otro Agent runtime ni ejecuta Pipelines directamente. Se limita a:

- convertir `RunAgentInput` en solicitudes A2A;
- proyectar eventos A2A como eventos AG-UI estándar;
- relacionar `threadId/runId` con `contextId/taskId`;
- convertir `resume[]` en recuperación de entrada A2A;
- conservar las relaciones de protocolo y los Interrupt pendientes;
- reenviar cancelaciones a A2A.

Por tanto, AG-UI y A2A no implementan reglas de ejecución distintas. El mismo runtime A2A aplica el modelo, las credenciales de nube, los permisos y el comportamiento del Pipeline.

## Protocolo estándar y extensiones de iac-code

El flujo externo utiliza eventos AG-UI estándar: `RUN_*`, `TEXT_MESSAGE_*`, `REASONING_*`, `TOOL_CALL_*`, `STEP_*` y `ACTIVITY_SNAPSHOT`.

Solo la información útil del Pipeline que no tenga un equivalente estándar se publica como `CUSTOM` con espacio de nombres. Un cliente AG-UI genérico puede ignorar esos eventos sin afectar al texto, las herramientas, los Interrupt ni el ciclo del run.

La solicitud sigue siendo un `RunAgentInput` estándar. iac-code usa `forwardedProps` para los datos de ejecución obligatorios:

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "request-identity",
      "cwd": "/absolute/workspace/path",
      "runMode": "normal"
    }
  }
}
```

Un cliente genérico puede consumir todos los eventos estándar. Si llama directamente a `iac-code agui`, debe proporcionar campos como `cwd` en `forwardedProps.iacCode`.

## Interacciones compatibles

### Conversaciones normales con varios turnos

Se conserva el mismo `threadId` durante toda la conversación y se crea un `runId` nuevo para cada turno. El adaptador vincula el thread con una sesión de iac-code. El turno siguiente abre otra solicitud HTTP/SSE; nunca continúa en una respuesta SSE ya finalizada.

### Pipeline

Con `forwardedProps.iacCode.runMode: "pipeline"`, el núcleo A2A sigue ejecutando el Pipeline. Los pasos superiores se convierten en `STEP_*`; el texto, el razonamiento y las herramientas usan sus eventos estándar. Los candidatos, el progreso del stack y la limpieza sin equivalente estándar se publican como `iac-code.pipeline.v1`.

Los sub-pipelines paralelos usan identificadores de mensaje y paso distintos, por lo que no se mezcla el texto de varios agent loops.

### Interrupt y Resume

Cuando una autorización, pregunta o selección exige intervención, el run actual termina con:

```json
{
  "type": "RUN_FINISHED",
  "outcome": {"type": "interrupt", "interrupts": []}
}
```

El Interrupt se guarda antes de enviarse al cliente. Este reúne las respuestas e inicia una solicitud nueva con el mismo `threadId`, un `runId` nuevo y `resume[]`. El SSE de Resume pertenece a esa nueva solicitud, no al flujo anterior.

### Estado del adaptador

El adaptador guarda por thread las relaciones de protocolo, los datos de idempotencia y los Interrupt pendientes. Este directorio no contiene el texto de la conversación, claves LLM ni credenciales de nube, y no sirve para exportar conversaciones.

## Cuándo usar AG-UI

| Necesidad | Modo recomendado |
|-----------|------------------|
| Interfaz de chat con texto, razonamiento, herramientas y pasos en directo | **AG-UI** |
| Permisos, preguntas y selección desde una UI | **AG-UI** |
| Otro agente u orquestador llama directamente a iac-code | **A2A** |
| Integración de IDE/editor con sesiones ACP | **ACP** |
| Uso manual local | **REPL interactivo o Web/Desktop** |

AG-UI y A2A pueden ejecutarse simultáneamente. Exponen endpoints distintos, pero comparten la misma implementación de ejecución.

## Límites actuales

- Transporte HTTP POST + SSE.
- El upstream A2A debe ser una dirección loopback.
- `cwd` es obligatorio en cada solicitud y debe estar bajo una raíz permitida.
- No se aceptan `tools` definidos por el cliente; iac-code administra las herramientas.
- Los mensajes admiten texto e imágenes base64 en línea, no URL remotas.
- Si el cliente desconecta un run activo antes de un Interrupt, se cancela la tarea A2A.
- Un comentario heartbeat se envía cada 15 segundos y los clientes conformes lo ignoran.

## Siguientes pasos

- [Primeros pasos](./getting-started.md)
- [Referencia del protocolo](./protocol-reference.md)
