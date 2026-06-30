---
sidebar_position: 3
title: Herramientas, recursos, prompts y skills
description: Entiende cómo aparecen las capacidades MCP dentro de IaC Code.
---

# Herramientas, recursos, prompts y skills

Los servidores MCP conectados pueden exponer cuatro tipos de capacidades a IaC Code.

## Herramientas

Cada herramienta MCP se convierte en una herramienta de IaC Code:

```text
mcp__<server>__<tool>
```

Las descripciones de herramientas y los esquemas JSON de entrada vienen del servidor MCP. IaC Code reenvía la entrada de herramienta del modelo al servidor MCP y luego convierte los bloques de contenido MCP en un resultado normal.

Las anotaciones MCP se respetan cuando es posible:

| Anotación MCP | Comportamiento de IaC Code |
|---|---|
| `readOnlyHint: true` | La herramienta se trata como de solo lectura y segura para concurrencia. |
| `destructiveHint: true` | La herramienta se trata como destructiva en decisiones de permisos. |

Las herramientas MCP siguen pasando por el sistema de permisos existente de IaC Code. Configura la política con settings normales de `permissions` o flags CLI como `--allowed-tools`, `--disallowed-tools` y `--permission-mode`.

Las notificaciones de progreso MCP se muestran en el renderizado interactivo, la salida de progreso headless, las actualizaciones de progreso ACP y los metadatos de herramientas A2A.

## Resultados de herramientas y artefactos

IaC Code convierte bloques de contenido MCP en texto visible para el modelo:

| Contenido MCP | Resultado de IaC Code |
|---|---|
| Contenido de texto | Incluido directamente en el resultado de herramienta. |
| `structuredContent` | Renderizado como JSON formateado en una sección de contenido estructurado. |
| Recursos de texto | Renderizados con procedencia de servidor y URI. |
| `resource_link` | Renderizado como enlace de recurso con URI y tipo MIME. |
| Datos de imagen, audio y blob | Guardados como archivos de artefacto privados y referenciados por id de artefacto. |

Los artefactos binarios se guardan bajo:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

El modelo ve el id de artefacto y metadatos, no datos base64 sin procesar.

## Recursos

Cuando cualquier servidor conectado expone recursos, IaC Code registra dos herramientas globales:

| Herramienta | Propósito |
|---|---|
| `list_mcp_resources` | Lista recursos expuestos por servidores MCP conectados. Puede filtrar por nombre de servidor. |
| `read_mcp_resource` | Lee un recurso por `server` y `uri`. |

Las líneas de recurso incluyen nombre de servidor, URI, nombre de recurso opcional y tipo MIME opcional.

## Prompts

Los prompts MCP se convierten en comandos slash:

```text
/mcp__<server>__<prompt> key=value
```

Al invocarlo, IaC Code llama a MCP `prompts/get`, renderiza los mensajes de prompt devueltos, inyecta el prompt renderizado en la conversación y deja que el modelo continúe. Los argumentos se pueden pasar como:

```text
template_name=prod-vpc region=cn-hangzhou
```

o como JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Los argumentos requeridos se validan antes de la llamada MCP. Se admiten valores entre comillas, incluidos paths de Windows con barras invertidas.

## Skills

Los recursos MCP con URI `skill://` se convierten en comandos de skill:

```text
$mcp__<server>__<skill>
```

IaC Code lee el recurso de skill remoto, analiza el frontmatter y lo registra como comando de skill normal. Los skills MCP remotos están limitados por seguridad:

- Se eliminan los `allowed_tools` remotos.
- Se eliminan las reglas remotas de autoactivación por paths.
- El cuerpo y la descripción del skill remoto tienen límites de longitud.
- Si el skill remoto entra en conflicto con un comando existente, se omite con una advertencia MCP.

Los recursos de skill MCP pueden leerse durante el inicio para que el comando esté registrado antes de que el usuario lo invoque.

## Actualizaciones dinámicas

Si un servidor MCP envía `tools/list_changed`, `resources/list_changed` o `prompts/list_changed`, IaC Code actualiza la lista de capacidades afectada y el registro de herramientas o comandos. Los fallos de actualización se reportan como advertencias MCP y no detienen la sesión activa.
