---
sidebar_position: 2
title: Configuración MCP
description: Configura servidores MCP mediante comandos CLI, archivos settings, archivos de proyecto y sesiones ACP.
---

# Configuración MCP

Los servidores MCP se configuran bajo el objeto `mcpServers`. IaC Code admite un esquema central compatible con Claude Code para servidores `stdio`, `http` y `sse`.

## Fuentes de configuración

IaC Code lee servidores MCP desde estas fuentes:

| Fuente | Scope | Archivo o punto de entrada | Modelo de confianza |
|---|---|---|---|
| Settings de usuario | `user` | `~/.iac-code/settings.yml` o `IAC_CODE_CONFIG_DIR/settings.yml` | De confianza para el usuario actual. |
| Settings locales de proyecto | `local` | `<workspace>/.iac-code/settings.local.yml` | Privado del checkout local. |
| Archivo MCP de proyecto | `project` | `<workspace>/.mcp.json` | Compartido con el proyecto y requiere aprobación local. |
| Configuración de sesión ACP | `session` | `mcp_servers` enviados por un cliente ACP | Solo aplica al runtime de esa sesión ACP. |

La precedencia es user, project, local y luego session. Las fuentes posteriores sobrescriben las anteriores por nombre de servidor. Las configuraciones equivalentes también se deduplican por firma de contenido.

Los archivos `.mcp.json` de proyecto se descubren desde la raíz del workspace hasta el directorio actual. Los archivos hijo sobrescriben a los padre por nombre de servidor.

## Comandos CLI

Usa `iac-code mcp` para administrar la configuración MCP persistida:

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --type http \
  --url https://mcp.example.com/mcp \
  --header "Authorization=${MCP_REVIEWER_TOKEN}"
```

Comandos disponibles:

| Comando | Propósito |
|---|---|
| `iac-code mcp add` | Añade un servidor desde flags CLI estructurados. |
| `iac-code mcp add-json` | Añade un servidor desde un objeto JSON. |
| `iac-code mcp list` | Lista servidores configurados, scopes, transportes y estado de aprobación. |
| `iac-code mcp get` | Imprime una configuración de servidor con secretos redactados. |
| `iac-code mcp remove` | Elimina un servidor de un scope persistido. |
| `iac-code mcp approve` | Aprueba un servidor de proyecto `.mcp.json`. |
| `iac-code mcp reject` | Rechaza un servidor de proyecto `.mcp.json`. |
| `iac-code mcp reset-project-choices` | Borra las decisiones guardadas de aprobación de proyecto. |
| `iac-code mcp auth` | Inicia autenticación OAuth para un servidor. |
| `iac-code mcp reset-auth` | Elimina tokens OAuth y client secret guardados para un servidor. |

Cuando se omite `--scope`, IaC Code escribe en `local` dentro de un proyecto y en `user` fuera de un proyecto.

## Servidores Stdio

Los servidores stdio lanzan un comando local:

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["./tools/catalog_mcp.py"],
      "env": {
        "CATALOG_ENV": "prod"
      }
    }
  }
}
```

El campo `type` puede omitirse cuando existe `command`. IaC Code pasa un entorno heredado seguro más el `env` del servidor. En Windows, prefiere `cmd /c npx` en lugar de `npx` directo para servidores basados en Node.

## Servidores HTTP y SSE

Los servidores remotos requieren `type` y `url`:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "${MCP_REVIEWER_TOKEN}"
      }
    }
  }
}
```

Usa `type: "sse"` para servidores SSE. Los headers estáticos son compatibles. Los comandos dinámicos `headersHelper` se rechazan porque requieren un diseño independiente de ejecución confiable.

## Expansión de entorno

Los valores string admiten:

```text
${VAR}
${VAR:-default-value}
```

Las variables faltantes sin valor por defecto producen una advertencia MCP y el servidor afectado se omite. La expansión se aplica recursivamente a strings dentro de listas y objetos.

No guardes secretos en texto claro en headers ni valores env. Usa referencias a variables de entorno o almacenamiento secreto OAuth.

## Aprobación de proyecto

Un `.mcp.json` de proyecto puede enviarse al repositorio, por lo que IaC Code no confía en él automáticamente.

Al iniciar el REPL interactivo pregunta:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Pulsar Enter mantiene el valor por defecto `N` y rechaza esa configuración exacta de servidor de proyecto. Escribe `y` o `yes` para aprobarla. La aprobación se guarda localmente bajo el directorio de configuración de IaC Code e incluye la ruta del workspace, la ruta del archivo de proyecto, el nombre del servidor y la firma de configuración. Si cambia la configuración del servidor en `.mcp.json`, la aprobación se invalida y el servidor vuelve a quedar pending.

Los inicios headless, ACP y A2A nunca hacen preguntas interactivas de aprobación. Los servidores de proyecto pendientes se omiten y se reportan como advertencias.
