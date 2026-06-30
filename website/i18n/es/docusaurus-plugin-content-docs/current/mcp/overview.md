---
sidebar_position: 1
title: Integración MCP
description: Usa servidores Model Context Protocol para ampliar IaC Code con herramientas, recursos, prompts y skills externos.
---

# Integración MCP

IaC Code puede actuar como host de Model Context Protocol (MCP). Los servidores MCP amplían el agente con herramientas externas, recursos, prompts y skills reutilizables, sin salir de los flujos de permisos, sesión, registro y manejo de salida de IaC Code.

Usa MCP cuando quieras que IaC Code llame a una capacidad local o remota que no viene integrada, como un catálogo privado de plantillas, un revisor interno de despliegues, un servicio de inventario o una herramienta especializada de operaciones cloud.

## Superficies compatibles

| Superficie | Compatibilidad MCP |
|---|---|
| REPL interactivo | Carga servidores de usuario, locales y de proyecto aprobados. Pregunta antes de confiar en nuevos servidores de proyecto `.mcp.json`. |
| Modo no interactivo | Carga servidores de usuario, locales y de proyecto aprobados. Nunca pregunta; los servidores de proyecto pendientes se omiten con advertencias. |
| Servidor ACP | Acepta configuraciones MCP de clientes ACP en la sesión y expone las capacidades MCP descubiertas dentro de esa sesión. |
| Servidor A2A | Carga MCP mediante el runtime normal y puede publicar advertencias MCP y progreso de herramientas en metadatos de tareas A2A. |
| Modo pipeline | Usa las mismas integraciones de runtime que el modo normal, incluido el progreso de herramientas MCP y la propagación de advertencias. |

## Capacidades compatibles

| Capacidad | Estado |
|---|---|
| Transporte `stdio` | Compatible con procesos MCP locales. |
| Transporte Streamable HTTP | Compatible con servidores MCP remotos. |
| Transporte SSE | Compatible con servidores MCP remotos. |
| Herramientas MCP | Se exponen como herramientas de agente llamadas `mcp__<server>__<tool>`. |
| Recursos MCP | Se exponen mediante `list_mcp_resources` y `read_mcp_resource`. |
| Prompts MCP | Se exponen como comandos slash llamados `mcp__<server>__<prompt>`. |
| Recursos MCP `skill://` | Se exponen como comandos de skill llamados `mcp__<server>__<skill>`. |
| Autenticación OAuth loopback | Compatible con servidores remotos que tienen metadatos OAuth. |
| `roots/list` | Compatible. IaC Code devuelve la raíz activa del workspace como URI file. |
| Notificaciones `list_changed` | Compatibles para herramientas, recursos y prompts. Los registros se actualizan dinámicamente. |
| Elicitation MCP | Todavía no compatible. Los servidores que la soliciten reciben un error claro de no compatibilidad. |
| Transportes WebSocket, SDK e IDE | No compatibles. |
| Comandos dinámicos `headersHelper` | No compatibles. Usa headers estáticos o referencias a variables de entorno. |
| IaC Code como servidor MCP | No compatible. Actualmente IaC Code actúa solo como host MCP. |

## Cómo funciona

En tiempo de ejecución, IaC Code:

1. Carga configuración MCP desde fuentes de usuario, locales, de proyecto y de sesión.
2. Expande referencias `${VAR}` y `${VAR:-default}`.
3. Omite servidores inseguros o inválidos con advertencias visibles para el usuario.
4. Conecta servidores aprobados con concurrencia limitada.
5. Descubre herramientas, recursos, prompts y recursos `skill://`.
6. Registra esas capacidades en los registros existentes de herramientas y comandos.
7. Convierte resultados de herramientas MCP en resultados normales de IaC Code y guarda artefactos binarios bajo el directorio de configuración runtime.
8. Desconecta clientes MCP cuando se cierran el REPL, la ejecución headless, la sesión ACP o el runtime A2A.

Un servidor MCP fallido no bloquea a otros servidores configurados. Los errores de conexión y descubrimiento permanecen visibles como advertencias MCP.

## Nombres

Las herramientas y comandos MCP se normalizan en nombres públicos:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Los caracteres que no sean letras, números ni guiones bajos se convierten en guiones bajos. Si varias capacidades chocan tras la normalización, IaC Code añade un digest corto para mantener nombres únicos.

## Páginas relacionadas

- [Configuración MCP](./configuration.md)
- [Herramientas, recursos, prompts y skills](./capabilities.md)
- [OAuth y seguridad](./oauth-and-security.md)
- [Solución de problemas](./troubleshooting.md)
