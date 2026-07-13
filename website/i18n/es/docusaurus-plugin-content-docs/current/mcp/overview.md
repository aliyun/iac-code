---
sidebar_position: 1
title: Integración MCP
description: Usa servidores Model Context Protocol para ampliar IaC Code con herramientas, recursos, prompts y habilidades externas.
---

# Integración MCP

IaC Code puede actuar como host de Model Context Protocol (MCP). Los MCP servers amplian el agent con tools, resources, prompts y reusable skills externos sin salir de las rutas de permission, session, logging y output handling de IaC Code.

Utilice MCP cuando desee que IaC Code llame a una capacidad local o remota que no está integrada en el producto, como un catálogo de plantillas privado, un revisor de implementación interno, un servicio de consulta de inventario o una herramienta de operación en la nube especializada.

## Supported Surfaces

| Surface | MCP support |
|---|---|
| REPL interactivo | Carga servidores de proyectos de usuario, locales y aprobados. Avisos antes de confiar en los servidores `.mcp.json` del nuevo proyecto. |
| Modo no interactivo | Carga servidores de proyectos de usuario, locales y aprobados. Nunca pide; Los servidores de proyectos pendientes se omiten con advertencias. |
| Servidor ACP | Acepta configuraciones de servidor MCP de sesión de clientes ACP y expone las capacidades MCP descubiertas dentro de esa sesión. |
| Servidor A2A | Carga MCP a través del tiempo de ejecución normal y puede publicar advertencias de MCP y el progreso de la herramienta en metadatos de tareas A2A. |
| Modo canalización | Utiliza las mismas integraciones de tiempo de ejecución que el modo normal, incluido el progreso de la herramienta MCP y la propagación de advertencias. |

## Supported Capabilities

| Capability | Status |
|---|---|
| transporte `stdio` | Compatible con procesos del servidor MCP local. |
| Transporte HTTP transmitible | Compatible con servidores MCP remotos. |
| Transporte ESS | Compatible con servidores MCP remotos. |
| Herramientas MCP | Expuestas como herramientas de agente denominadas `mcp__<server>__<tool>`. |
| Recursos del PCM | Expuesto a través de `list_mcp_resources` y `read_mcp_resource`. |
| Indicaciones de MCP | Expuesto como comandos de barra diagonal denominados `mcp__<server>__<prompt>`. |
| MCP `skill://` recursos | Expuesto como comandos de habilidad llamados `mcp__<server>__<skill>`. |
| Autenticación de bucle invertido OAuth | Compatible con servidores remotos con metadatos OAuth. |
| `roots/list` | Apoyado. El código IaC devuelve la raíz del espacio de trabajo activo como un URI de archivo. |
| Notificaciones `list_changed` | Compatible con herramientas, recursos e indicaciones. Los registros se actualizan dinámicamente. |
| MCP elicitation | Compatible con sesiones interactivas. Las ejecuciones no interactivas se cancelan de forma segura. La URL elicitation puede reintentar el tool call original tras la confirmación del usuario. |
| WebSocket transport | Compatible con servers `ws://` y `wss://` solo con URL. WebSocket rechaza headers, `headersHelper` y OAuth porque el SDK transport instalado solo acepta una URL. |
| Comandos dinámicos `headersHelper` | Compatibles con servers `http` y `sse` de confianza. Los helpers se ejecutan sin shell, con timeout acotado, entorno mínimo y diagnostics redactados. |
| Transportes SDK e IDE | No compatible. |
| Código IaC como servidor MCP | No compatible. Actualmente, el código IaC actúa únicamente como host MCP. |

## How It Works

At runtime IaC Code:

1. Carga la configuración de MCP desde fuentes de usuario, local, proyecto y sesión.
2. Expande las referencias `${VAR}` y `${VAR:-default}`.
3. Omite servidores inseguros o no válidos con advertencias visibles para el usuario.
4. Conecta servidores aprobados con concurrencia limitada.
5. Descubre herramientas, recursos, indicaciones y recursos `skill://`.
6. Registra esas capacidades en los registros de herramientas y comandos existentes.
7. Inyecta instrucciones del servidor conectado en el indicador del agente como guía en el ámbito del servidor.
8. Convierte los resultados de la herramienta MCP en resultados normales de la herramienta Código IaC, almacenando artefactos binarios y artefactos de texto grandes en el directorio de configuración del tiempo de ejecución.
9. Desconecta a los clientes MCP cuando se cierra REPL, ejecución sin cabeza, sesión ACP o tiempo de ejecución A2A.

Un servidor MCP fallido no bloquea otros servidores configurados. Los errores de conexión y descubrimiento permanecen visibles como advertencias de MCP.

## Naming

Las herramientas y comandos de MCP están normalizados en nombres públicos:

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Los caracteres fuera de letras, números y guiones bajos se convierten en guiones bajos. Si dos capacidades descubiertas chocan después de la normalización, el código IaC agrega un breve resumen para mantener los nombres únicos.

Para las habilidades de MCP, el código IaC también registra un alias de compatibilidad como `<server>:<skill>` cuando ese alias no entra en conflicto con un comando existente. Los diagnósticos conservan los nombres originales del servidor, la herramienta, el indicador o la habilidad incluso cuando los nombres públicos están normalizados.

## Related Pages

- [Inicio rapido MCP](./quick-start.md)
- [Configuración de MCP](./configuration.md)
- [Herramientas, recursos, indicaciones y habilidades](./capabilities.md)
- [OAuth y seguridad](./oauth-and-security.md)
- [Solución de problemas](./troubleshooting.md)
