---
sidebar_position: 4
title: OAuth y seguridad
description: Autentica servidores MCP remotos y entiende el modelo de seguridad MCP en IaC Code.
---

# OAuth y seguridad

MCP puede iniciar procesos locales y llamar a servicios remotos, por lo que el código IaC trata la configuración y autenticación de MCP como cuestiones sensibles a la seguridad.

## OAuth

Los servers remotos `http` y `sse` pueden usar OAuth. Los servers compatibles con el estandar que publican OAuth metadata y admiten Dynamic Client Registration no requieren que proporciones un client id. Agrega el server y luego ejecuta auth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Si un servidor requiere un cliente previamente aprovisionado, configure los metadatos de OAuth en la configuración del servidor:

```json
{
  "mcpServers": {
    "secure-reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "clientId": "iac-code",
        "clientSecretEnv": "MCP_CLIENT_SECRET",
        "callbackPort": 38487,
        "authServerMetadataUrl": "https://auth.example.com/.well-known/oauth-authorization-server"
      }
    }
  }
}
```

Supported OAuth fields:

| Field | Purpose |
|---|---|
| `clientId` | OAuth client id. |
| `clientSecretEnv` | Variable de entorno que contiene el secreto del cliente. |
| `callbackPort` | Puerto de devolución de llamada de bucle opcional. Utilice `0` u omítalo para elegir un puerto libre. |
| `authServerMetadataUrl` | URL de metadatos del servidor de autorización explícita opcional. |
| `clientMetadataUrl` | URL del documento de metadatos del cliente HTTPS opcional para servidores de autorización que admiten documentos de metadatos de ID de cliente. |

Se rechaza el texto sin formato `oauth.clientSecret`. Utilice `clientSecretEnv` o el indicador CLI seguro.

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

El código IaC abre o imprime una URL de autorización e inicia un servidor de devolución de llamada de bucle invertido en `127.0.0.1`. Si el navegador no se puede abrir o la devolución de llamada no se puede completar automáticamente, pegue la URL de devolución de llamada o el código de autorización en el mensaje CLI. Después de la autorización, IaC Code intercambia el código por tokens y los almacena de forma segura.

Para servidores compatibles con DCR, el código IaC registra un cliente OAuth en el servidor y almacena la identificación del cliente devuelta y el secreto de cliente opcional a través del almacenamiento secreto de MCP. El intercambio y la actualización de tokens incluyen el parámetro de recurso seleccionado por la semántica del SDK de MCP cuando los metadatos de recursos protegidos lo requieren.

Si un servidor necesita autenticación durante una sesión normal, el Código IaC registra una herramienta de autenticación:

```text
mcp__<server>__authenticate
```

El modelo puede llamar a esa herramienta para proporcionar al usuario la URL de OAuth. Una vez que se completa el flujo, el código IaC vuelve a conectar el servidor MCP y actualiza las capacidades descubiertas.

## Token Storage

El código IaC almacena tokens OAuth y secretos del cliente MCP a través de `MCPSecretStorage`:

1. Los datos cifrados se guardan en `<config-dir>/mcp/secrets.json.enc`.
2. La clave de cifrado se guarda en `<config-dir>/mcp/secrets.key`.
3. Los permisos de ambos archivos están restringidos.

El almacén de secretos MCP no accede al llavero del sistema operativo, por lo que las comprobaciones de estado en
segundo plano no muestran solicitudes de autorización del sistema. El estado que solo existía en el llavero no se
migra automáticamente; autorice el servidor MCP una vez para crear la entrada local cifrada.

Utilice este comando para borrar el estado de autenticación almacenado:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` borra, para el scope persistente seleccionado, OAuth token state, dynamic client registration state,
el `client_id` almacenado, el `client_secret` opcional y OAuth signature index, pero conserva el server config.
Al eliminar un server persistente, se ejecuta el mismo auth-state cleanup antes de borrar la configuracion:

```bash
iac-code mcp remove secure-reviewer --scope user
```

Use `reset-auth` para volver a autorizar un server existente. Use `mcp remove` cuando tambien deba desaparecer el
server config; ambos caminos eliminan las entradas cifradas administradas por `MCPSecretStorage`.

## Project Trust

Los archivos del proyecto `.mcp.json` no son confiables automáticamente porque un repositorio puede agregar un servidor `stdio` que ejecuta código local arbitrario. La aprobación interactiva se realiza por firma de configuración del servidor. Cambiar el comando, los argumentos, el entorno, la URL, los encabezados o la configuración de OAuth invalida la aprobación previa.

Los modos de servidor de protocolo y sin cabeza omiten los servidores de proyectos no aprobados en lugar de solicitarlos.

## Secret Handling

El Código IaC protege los secretos de varias maneras:

- La salida de configuración de `iac-code mcp get` y `iac-code mcp get --config-only` redacta claves que parecen tokens, secretos, contraseñas, claves API y encabezados de autorización.
- Los valores sensibles de encabezados o entorno en texto claro se rechazan al añadir servidores mediante `iac-code mcp add` o `mcp add-json`, salvo que usen una referencia a variable de entorno. Los archivos de configuración editados a mano no se vuelven a validar al cargarse; evita almacenar secretos en texto claro directamente.
- Los servidores MCP stdio heredan solo una lista permitida de variables de entorno seguras más el entorno explícito del servidor.
- Los servidores stdio MCP no heredan las variables de entorno proxy con nombres de usuario o contraseñas integrados.
- Los comandos `headersHelper` se ejecutan sin shell, sin stdin, con un entorno mínimo, captura acotada de stdout/stderr y diagnósticos privados de stderr redactados.
- Los archivos de artefactos MCP se escriben en el directorio de configuración de tiempo de ejecución del código IaC privado.

## Permissions

Las herramientas MCP utilizan el mismo marco de permisos que las herramientas integradas. Un servidor MCP remoto no puede eludir las comprobaciones de permisos del Código IaC simplemente anunciando una herramienta. Tenga en cuenta estas reglas:

- Las herramientas MCP de solo lectura pueden permitirse automáticamente según la política de permisos activa.
- Las herramientas MCP destructivas deberían requerir aprobación a menos que se permita explícitamente.
- En la automatización sin cabeza, combine `--permission-mode`, `--allowed-tools` y `--disallowed-tools` para restringir lo que pueden hacer las herramientas MCP.
- Las habilidades de MCP remotas no otorgan sus propias `allowed_tools`.

## Funciones sensibles a la seguridad no compatibles

El Código IaC rechaza u omite intencionalmente estas características de MCP por ahora:

- Enterprise managed MCP policy.
- IDE and SDK transports.
- Headers de WebSocket, `headersHelper` de WebSocket y OAuth de WebSocket.
- IaC Code acting as an MCP server.
