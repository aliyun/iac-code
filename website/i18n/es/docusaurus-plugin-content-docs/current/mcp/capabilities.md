---
sidebar_position: 3
title: Herramientas, recursos, prompts y habilidades
description: Entiende como aparecen las capacidades MCP dentro de IaC Code.
---

# Herramientas, recursos, prompts y habilidades

Los MCP servers conectados pueden exponer cuatro tipos de capabilities a IaC Code.

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

Las descripciones de herramientas y los esquemas de entrada JSON provienen del servidor MCP. El código IaC reenvía la entrada de la herramienta del modelo al servidor MCP y luego convierte los bloques de contenido de MCP en un resultado de herramienta normal.

Las solicitudes de permiso y los metadatos de auditoría incluyen el nombre del servidor MCP, el nombre de la herramienta original, el nombre de la herramienta pública normalizada y anotaciones destructivas/de solo lectura.

Las anotaciones de la herramienta MCP se respetan siempre que sea posible:

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` | La herramienta se trata como de solo lectura y segura para la simultaneidad. |
| `destructiveHint: true` | La herramienta se trata como destructiva para las decisiones de permisos. |

Las herramientas MCP aún pasan por el sistema de permisos existente del Código IaC. Configure la política de permisos con configuraciones normales de `permissions` o indicadores CLI como `--allowed-tools`, `--disallowed-tools` y `--permission-mode`.

Las notificaciones de progreso de MCP aparecen en renderizado interactivo, salida de progreso sin cabeza, actualizaciones de progreso de la herramienta ACP y metadatos de la herramienta A2A.

## Tool Results and Artifacts

El código IaC convierte bloques de contenido MCP en texto visible para el modelo:

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result when small; el texto grande se guarda como artifact privado `.txt`, `.json` o `.md`. |
| `structuredContent` | Representado como JSON formateado en una sección de contenido estructurado. |
| Recursos de texto | Renderizado con servidor y procedencia de URI. |
| `resource_link` | Representado como un enlace de recurso con tipo URI y MIME. |
| Datos de imágenes, audio y blobs | Almacenados como archivos de artefactos privados y referenciados por ID de artefacto. |

Los artefactos binarios se almacenan en el directorio de resultados de la herramienta MCP propiedad de la sesión para las sesiones v2:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/<server>/<tool>/
```

Las sesiones heredadas sin un marcador de diseño compatible siguen utilizando:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data. Los artifacts de texto grande incluyen un path so the full output can be read without flooding the conversation.

## Resources

Cuando cualquier servidor conectado expone recursos, el Código IaC registra dos herramientas globales:

| Tool | Purpose |
|---|---|
| `list_mcp_resources` | Enumera los recursos de los servidores MCP conectados. Opcionalmente, filtre por nombre de servidor. |
| `read_mcp_resource` | Lee un recurso por `server` y `uri`. |

Las líneas de recursos incluyen el nombre del servidor, URI, nombre de recurso opcional y tipo MIME opcional.

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

Cuando se invoca, el código IaC llama a MCP `prompts/get`, presenta los mensajes de solicitud devueltos, inyecta el mensaje representado en la conversación y permite que el modelo continúe. Los argumentos rápidos se pueden pasar como:

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Los argumentos de solicitud requeridos se validan antes de la llamada a MCP. Se admiten valores entrecomillados, incluidas las rutas de Windows con barras invertidas.

## Skills

Los recursos de MCP con URI `skill://` se convierten en comandos de habilidad:

```text
$mcp__<server>__<skill>
```

El código IaC lee el recurso de habilidad remota, analiza el contenido inicial y lo registra como un comando de habilidad normal. Las habilidades de MCP remotas están limitadas por motivos de seguridad:

- Remote `allowed_tools` are cleared.
- Se borran las reglas de ruta de activación automática remota.
- El cuerpo de la habilidad remota y la longitud de la descripción están limitados.
- Si la habilidad remota entra en conflicto con un comando existente, se omite con una advertencia de MCP.

Los recursos de habilidades de MCP se pueden leer durante el inicio para que el comando pueda registrarse antes de que el usuario lo invoque.

Cuando no hay conflicto de comandos, las habilidades de MCP también obtienen un alias de compatibilidad:

```text
$<server>:<skill>
```

Por ejemplo, `$mcp__yuque__search` y `$yuque:search` pueden resolverse en la misma habilidad remota.

## Server Instructions (instrucciones del servidor)

Si un servidor conectado devuelve "instrucciones" desde la inicialización, el código IaC las inyecta en el indicador del agente como una sección dedicada de instrucciones del servidor MCP. Estas instrucciones se tratan como una guía centrada en el servidor y no anulan las instrucciones del proyecto local.

## Elicitation (solicitudes interactivas)

Las sesiones interactivas pueden enrutar solicitudes de MCP elicitation al usuario. La elicitation en modo URL puede pedir al usuario completar un flujo de URL externo y luego reintentar el MCP tool call original hasta un límite acotado. Los contextos no interactivos cancelan la elicitation de forma segura.

## Dynamic Updates

Si un servidor MCP envía `tools/list_changed`, `resources/list_changed` o `prompts/list_changed`, el código IaC actualiza la lista de capacidades afectadas y actualiza el registro de herramientas o comandos. Los errores de actualización se informan como advertencias de MCP y no detienen la sesión activa.
