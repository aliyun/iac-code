---
sidebar_position: 4
title: OAuth y seguridad
description: Autentica servidores MCP remotos y entiende el modelo de seguridad MCP en IaC Code.
---

# OAuth y seguridad

MCP puede iniciar procesos locales y llamar servicios remotos, por lo que IaC Code trata la configuración y autenticación MCP como sensibles para la seguridad.

## OAuth

Los servidores remotos `http` y `sse` pueden usar OAuth. Configura metadatos OAuth en la configuración del servidor:

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

Campos OAuth compatibles:

| Campo | Propósito |
|---|---|
| `clientId` | Id de cliente OAuth. |
| `clientSecretEnv` | Variable de entorno que contiene el client secret. |
| `callbackPort` | Puerto loopback opcional. Usa `0` u omítelo para elegir un puerto libre. |
| `authServerMetadataUrl` | URL explícita opcional de metadatos del servidor de autorización. |

`oauth.clientSecret` en texto claro se rechaza. Usa `clientSecretEnv` o el prompt CLI seguro.

## Autenticación

Ejecuta:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code abre o imprime una URL de autorización y arranca un servidor de callback loopback en `127.0.0.1`. Después de que el proveedor redirige con un código de autorización, IaC Code lo intercambia por tokens y los guarda de forma segura.

Si un servidor necesita autenticación durante una sesión normal, IaC Code registra una herramienta de autenticación:

```text
mcp__<server>__authenticate
```

El modelo puede llamar a esa herramienta para mostrar al usuario la URL OAuth. Cuando el flujo termina, IaC Code reconecta el servidor MCP y actualiza las capacidades descubiertas.

## Almacenamiento de tokens

IaC Code guarda tokens OAuth y secretos de cliente MCP con `MCPSecretStorage`:

1. Intenta usar el keyring del sistema operativo cuando está disponible.
2. Si el keyring está desactivado o no disponible, guarda datos fallback cifrados bajo `<config-dir>/mcp/`.
3. Los permisos de archivo se restringen para la clave fallback y el almacén cifrado.

Define `IAC_CODE_MCP_DISABLE_KEYRING=1` para forzar almacenamiento fallback cifrado, útil en pruebas aisladas.

Usa este comando para borrar el estado de autenticación guardado:

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## Confianza de proyecto

Los archivos de proyecto `.mcp.json` no se confían automáticamente porque un repositorio puede añadir un servidor `stdio` que ejecuta código local arbitrario. La aprobación interactiva se vincula a la firma de configuración del servidor. Cambiar command, args, env, URL, headers u OAuth config invalida la aprobación previa.

Los modos headless y servidor de protocolo omiten servidores de proyecto no aprobados en lugar de pedir confirmación.

## Manejo de secretos

IaC Code protege secretos de varias maneras:

- La salida de `iac-code mcp get` redacta claves que parecen tokens, secrets, passwords, API keys y authorization headers.
- Los valores sensibles de headers o env en texto claro se rechazan salvo que usen una referencia a variable de entorno.
- Los servidores MCP stdio heredan solo una allowlist de variables de entorno seguras más el env explícito del servidor.
- Las variables proxy con usernames o passwords no se heredan por servidores MCP stdio.
- Los archivos de artefactos MCP se escriben bajo el directorio privado de configuración runtime de IaC Code.

## Permisos

Las herramientas MCP usan el mismo marco de permisos que las herramientas integradas. Un servidor MCP remoto no puede saltarse las comprobaciones de permisos de IaC Code solo por anunciar una herramienta. Ten en cuenta:

- Las herramientas MCP de solo lectura pueden autoaprobarse según la política activa.
- Las herramientas MCP destructivas deben requerir aprobación salvo que estén permitidas explícitamente.
- En automatización headless, combina `--permission-mode`, `--allowed-tools` y `--disallowed-tools` para restringir lo que pueden hacer las herramientas MCP.
- Los skills MCP remotos no conceden sus propios `allowed_tools`.

## Funciones sensibles no compatibles

IaC Code rechaza u omite deliberadamente estas funciones MCP por ahora:

- Comandos dinámicos `headersHelper`.
- Interfaz de elicitation MCP.
- Transportes WebSocket, IDE y SDK.
- Política MCP empresarial administrada.
- IaC Code como servidor MCP.
