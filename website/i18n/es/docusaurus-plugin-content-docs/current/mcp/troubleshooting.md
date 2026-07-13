---
sidebar_position: 5
title: Solución de problemas MCP
description: Diagnostica problemas de configuración, conexión, autenticación y descubrimiento de capacidades MCP.
---

# Solución de problemas MCP

Los MCP warnings no son fatales salvo que todas las capabilities que necesitas queden no disponibles. Un server fallido no deberia impedir que otros MCP servers o las tools integradas de IaC Code sigan funcionando.

## Inspect Configuration

Inspecciona los servers configurados sin conectarte:

```bash
iac-code mcp list
```

Ejecuta bounded health diagnostics para los servers configurados:

```bash
iac-code mcp list --check
```

Inspeccione una configuración de servidor redactada sin conectarse:

```bash
iac-code mcp get my-server --scope local
```

Ejecute diagnósticos de estado limitados para un servidor:

```bash
iac-code mcp get my-server --scope local --check
```

Inspeccione la configuración explícitamente, sin conectarse:

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

Vuelva a conectar un servidor o todos los servidores persistentes:

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

Sintoma:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

Correccion:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

Use el `--scope` exacto que muestra la lista de configuracion. Para archivos persistentes no predeterminados, agregue
tambien el `--source-path` correspondiente. Si el server se elimino, vuelva a agregarlo en vez de autenticar una configuracion ausente.

## Pending Project Server

Estado o warning code: `pending_approval`.

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

o inicie el REPL interactivo en ese proyecto y responda "y" cuando se le solicite. Presionar Enter significa `N` y rechaza el servidor.

Si la aprobación solía funcionar pero se detuvo, verifique si `.mcp.json` cambió. La aprobación está ligada a la firma de configuración.

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

Se omiten los servidores a los que les faltan variables de entorno requeridas.

## Connection Failed

Estado o warning code: `connection_failed`.

For stdio servers:

- Verify `command` exists on `PATH`.
- Utilice rutas absolutas para scripts al iniciar desde diferentes directorios.
- En Windows, ejecute servidores basados en Nodos a través de `cmd /c npx`.
- Verifique que las variables de entorno requeridas estén configuradas.

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
- Confirme que los encabezados estáticos estén presentes y que no contengan secretos de texto sin formato.
- Ejecute `iac-code mcp auth <server>` si el servidor requiere OAuth.

## Needs Authentication

Estado: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

Si el servidor utiliza tokens de actualización de OAuth y se requiere reautenticación, el código IaC borra los tokens obsoletos y solicita un flujo nuevo.

## OAuth Auth Failed

Sintoma (`auth-failed`):

```text
MCP auth failed for 'name':
```

El OAuth flow comenzo, pero no termino limpiamente: el callback URL puede estar incompleto, el authorization code puede
haber expirado o el authorization server puede haber devuelto un error. Si un flow nuevo falla antes de completarse,
IaC Code restaura el auth state anterior.

Correccion:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

Primero reintente `auth`. Use `reset-auth` antes de reintentar solo cuando el token guardado o el dynamic client state esten obsoletos.

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

El código IaC borra el cliente OAuth almacenado y el estado del token para ese servidor. Ejecute la autenticación nuevamente:

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

El servidor solicitó ámbitos OAuth adicionales. En la sesión actual, abra `/mcp` y elija `Autenticar` o
`Volver a autenticar` para ese servidor; El código IaC incluye los alcances informados por el desafío del servidor en ese flujo. el
El comando independiente `iac-code mcp auth name` inicia un flujo de autenticación normal y no incluye alcances de solo desafío desde un
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

Vuelva a ejecutar con el `--scope` command exacto impreso en el error. Esto es scope ambiguity: server name es valido, pero el comando necesita un scope persistente.

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

El servidor se conectó, pero falló una lista de capacidades. Es posible que otras capacidades del mismo servidor aún funcionen. Corrija el error del lado del servidor, luego reinicie el código IaC o active una reconexión/actualización de autenticación.

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

En caso de fallas repetidas, verifique si el servidor remoto abandonó la sesión o se reinició.

## Headers Helper Failed

Los síntomas pueden incluir errores de análisis del asistente, tiempo de espera, estado de salida distinto de cero, JSON no válido o valores de encabezado que no son cadenas. Verifique que el comando auxiliar sea válido desde el directorio fuente de configuración e imprima un objeto JSON como:

```json
{"X-Org": "platform"}
```

El stderr de tipo secreto está redactado en el diagnóstico.

## WebSocket Config Rejected

Los servidores WebSocket MCP admiten configuración de solo URL. Elimine `headers`, `headersHelper` y `oauth` de los servidores `type: "ws"`.

## Resources Are Missing

`list_mcp_resources` se registra solo cuando al menos un servidor conectado expone recursos. Si falta la herramienta:

- Confirm the server connected.
- Confirme que el servidor admite `resources/list`.
- Verifique las advertencias de inicio para detectar errores de descubrimiento de recursos.

## Prompt or Skill Command Missing

Los comandos rápidos y de habilidad aparecen solo después de un descubrimiento exitoso. Comprobar:

- El recurso `skill://` existe en el servidor MCP.
- El nombre del comando normalizado no entra en conflicto con un comando integrado.
- El recurso de habilidad remota se puede leer dentro del tiempo de espera de inicio.
- La descripción de la habilidad y el cuerpo se ajustan a los límites de seguridad del Código IaC.

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

Los artefactos binarios de MCP de los resultados de la herramienta se almacenan en el directorio de propiedad de la sesión para las sesiones v2:

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

Sesiones heredadas sin un uso de marcador de diseño compatible:

```text
<config-dir>/tool-results/<session-id>/mcp/
```

Evite compartir directorios de configuración, registros o artefactos sin revisarlos en busca de secretos.
