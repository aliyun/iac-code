---
sidebar_position: 5
title: Solución de problemas MCP
description: Diagnostica problemas de configuración, conexión, autenticación y descubrimiento de capacidades MCP.
---

# Solución de problemas MCP

Las advertencias MCP no son fatales salvo que todas las capacidades que necesitas estén no disponibles. Un servidor fallido no debería impedir que funcionen otros servidores MCP o herramientas integradas de IaC Code.

## Inspeccionar configuración

Lista servidores configurados:

```bash
iac-code mcp list
```

Inspecciona una configuración de servidor redactada:

```bash
iac-code mcp get my-server --scope local
```

Elimina un servidor incorrecto:

```bash
iac-code mcp remove my-server --scope local
```

Borra decisiones de aprobación de proyecto:

```bash
iac-code mcp reset-project-choices
```

## Servidor de proyecto pendiente

Síntoma:

```text
Project MCP server 'name' is pending approval.
```

Solución:

```bash
iac-code mcp approve name
```

o inicia el REPL interactivo en ese proyecto y responde `y` cuando lo pida. Pulsar Enter significa `N` y rechaza el servidor.

Si la aprobación funcionaba y dejó de funcionar, comprueba si `.mcp.json` cambió. La aprobación está ligada a la firma de configuración.

## Variable de entorno faltante

Síntoma:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Soluciona con una de estas opciones:

```bash
export TOKEN=...
```

o usa un valor por defecto:

```json
"Authorization": "${TOKEN:-}"
```

Los servidores con variables de entorno requeridas faltantes se omiten.

## Fallo de conexión

Para servidores stdio:

- Verifica que `command` exista en `PATH`.
- Usa paths absolutos para scripts cuando se ejecutan desde distintos directorios.
- En Windows, ejecuta servidores Node con `cmd /c npx`.
- Comprueba que las variables de entorno requeridas estén configuradas.

Para servidores HTTP o SSE:

- Verifica la URL y el tipo de transporte.
- Revisa TLS y configuración de proxy.
- Confirma que los headers estáticos existan y no contengan secretos en texto claro.
- Ejecuta `iac-code mcp auth <server>` si el servidor requiere OAuth.

## Necesita autenticación

Síntoma:

```text
MCP server 'name' requires authentication.
```

Solución:

```bash
iac-code mcp auth name --scope user
```

Si el servidor usa refresh tokens OAuth y requiere reautenticación, IaC Code borra tokens obsoletos y solicita un flujo nuevo.

## Falló el descubrimiento de capacidades

Los síntomas pueden incluir:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

El servidor se conectó, pero falló una lista de capacidades. Otras capacidades del mismo servidor pueden seguir funcionando. Corrige el error del lado del servidor y reinicia IaC Code o provoca un reconnect/auth refresh.

## Faltan recursos

`list_mcp_resources` se registra solo cuando al menos un servidor conectado expone recursos. Si falta la herramienta:

- Confirma que el servidor esté conectado.
- Confirma que el servidor soporte `resources/list`.
- Revisa las advertencias de inicio por errores de discovery de recursos.

## Falta un comando prompt o skill

Los comandos de prompt y skill aparecen solo después de un descubrimiento exitoso. Revisa:

- El prompt o recurso `skill://` existe en el servidor MCP.
- El nombre normalizado del comando no choca con un comando integrado.
- El recurso de skill remoto puede leerse dentro del timeout de inicio.
- La descripción y el cuerpo del skill caben en los límites de seguridad de IaC Code.

## Logs y artefactos

Los logs runtime van por defecto a:

```text
<config-dir>/logs/
```

o a `IAC_CODE_LOG_DIR` cuando está definido.

Los artefactos binarios de resultados de herramientas MCP se guardan bajo:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Evita compartir directorios de config, logs o artefactos sin revisarlos antes por si contienen secretos.
