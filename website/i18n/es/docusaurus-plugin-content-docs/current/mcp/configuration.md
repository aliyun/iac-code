---
sidebar_position: 2
title: Configuración MCP
description: Configura servidores MCP con comandos CLI, archivos de ajustes, archivos de proyecto y sesiones ACP.
---

# Configuración MCP

Los MCP servers se configuran bajo el `mcpServers` object. IaC Code admite un core schema compatible con Claude Code para `stdio`, `http`, `sse`, and URL-only `ws` servers.

## Inicio rapido

Para un servidor MCP HTTP remoto como Yuque, agregue el servidor con la forma de URL posicional y luego inicie OAuth:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Para wrappers stdio como `mcp-remote`, coloque el comando subprocess despues de `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

## Configuration Sources

El código IaC lee servidores MCP de estas fuentes:

| Fuente | Alcance | Archivo o punto de entrada | Modelo de confianza |
|---|---|---|---|
| Configuración de usuario | `user` | `~/.iac-code/settings.yml` o `IAC_CODE_CONFIG_DIR/settings.yml` | Confiado por el usuario actual. |
| Configuración local del proyecto | `local` | `<workspace>/.iac-code/settings.local.yml` | Privado a la caja local. |
| Archivo MCP del proyecto | `project` | `<workspace>/.mcp.json` | Compartido con el proyecto y requiere aprobación local. |
| Configuración de sesión ACP | `session` | `mcpServers` pasado por un cliente ACP | Se aplica únicamente al tiempo de ejecución de esa sesión ACP. |

La prioridad es usuario, proyecto, local y luego sesión. Las fuentes posteriores anulan las fuentes anteriores por nombre de servidor. Las configuraciones equivalentes también se deduplican mediante la firma del contenido.

Los archivos del proyecto `.mcp.json` se descubren desde la raíz del espacio de trabajo hasta el directorio actual. Los archivos de proyectos secundarios anulan los archivos principales por nombre de servidor.

## CLI Commands

Utilice `iac-code mcp` para administrar la configuración de MCP persistente:

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

Se pueden agregar servidores HTTP remotos con el formulario de URL posicional estilo Claude:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

Los servidores SSE y WebSocket usan el mismo formulario de URL posicional con su transporte correspondiente:

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

Para contenedores stdio como `mcp-remote`, coloque el comando de subproceso después de `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Comandos disponibles:

| Comando | Propósito |
|---|---|
| `iac-code mcp add` | Agregue un servidor desde indicadores CLI estructurados. |
| `iac-code mcp add-json` | Agregue un servidor desde un objeto JSON. |
| `iac-code mcp list` | Lista servers configurados, scopes, transports y estado de aprobación sin conectarse. |
| `iac-code mcp list --config-only` | Alias del listado de configuración predeterminado. |
| `iac-code mcp list --check` | Se conecta brevemente y muestra diagnostics de health acotados. |
| `iac-code mcp get` | Imprima una configuración de servidor redactada sin conectarse. |
| `iac-code mcp get --config-only` | Imprima una configuración de servidor redactada sin conectarse. |
| `iac-code mcp get --check` | Conéctese brevemente y muestre diagnósticos de salud limitados para un servidor. |
| `iac-code mcp remove` | Elimine un servidor de un ámbito persistente. |
| `iac-code mcp approve` | Aprobar un proyecto de servidor `.mcp.json`. |
| `iac-code mcp reject` | Rechazar un servidor de proyecto `.mcp.json`. |
| `iac-code mcp reset-project-choices` | Borre las opciones de aprobación de proyectos almacenadas. |
| `iac-code mcp auth` | Inicie la autenticación OAuth para un servidor. |
| `iac-code mcp reset-auth` | Elimine los tokens de OAuth almacenados y el secreto del cliente para un servidor. |
| `iac-code mcp reconnect` | Vuelva a conectar un servidor o todos los servidores persistentes con `--all`. |
| `iac-code mcp disable` | Deshabilite un servidor persistente sin editar la configuración del proyecto compartido. |
| `iac-code mcp enable` | Vuelva a habilitar un servidor persistente. |

## Opciones de comando

El siguiente option set coincide con `iac-code mcp <command> --help`:

| Comando | Opciones |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options; solo `--help`. |
| `iac-code mcp reject` | No command-specific options; solo `--help`. |
| `iac-code mcp reset-project-choices` | No command-specific options; solo `--help`. |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

Cuando se omite `--scope`, el código IaC escribe en `local` dentro de un proyecto y en `user` fuera de un proyecto.

Para los comandos que operan en un servidor persistente existente, el código IaC puede encontrar un servidor único en ámbitos persistentes cuando se omite `--scope`. Si el mismo nombre existe en varios ámbitos, el comando falla con los comandos exactos `--scope` para eliminar la ambigüedad.

## Gestor MCP interactivo

Dentro del REPL interactivo, `/mcp` abre un gestor MCP de pantalla completa. Agrupa los servidores por origen y muestra el estado de conexión, el estado de autenticación, los diagnósticos de configuración, los detalles de fallos y la ubicación configurada.

Desde el gestor puedes inspeccionar las tools, resources y prompts de un servidor conectado; autenticar, volver a autenticar o borrar la autenticación de servidores remotos; reconectar servidores; activar o desactivar servidores persistentes; aprobar o rechazar servidores `.mcp.json` de proyecto; y eliminar entradas persistentes. Los flujos OAuth muestran la URL de autorización, permiten copiarla y aceptan una URL de callback o código de autorización pegado cuando la redirección del navegador no puede llegar al listener callback local.

`/mcp enable <name>`, `/mcp disable <name>` y `/mcp reconnect <name>` ejecutan acciones rápidas sin abrir el gestor. Si `/mcp` llega por stdin canalizado u otra entrada no TTY, IaC Code imprime un mensaje indicando que se requiere una terminal; usa `iac-code mcp <command>` para automatización no interactiva.

## Stdio Servers

Stdio servers launch a local command:

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

El campo `type` se puede omitir cuando está presente `command`. El código IaC pasa un entorno heredado seguro más el servidor `env`. En Windows, prefiera `cmd /c npx` en lugar de `npx` simple para servidores basados ​​en nodos.

## HTTP and SSE Servers

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

Utilice `type: "sse"` para servidores SSE. Los encabezados estáticos se admiten con la sintaxis CLI `KEY=VALUE` o `Name: Value`.

Los encabezados dinámicos se pueden proporcionar con `headersHelper`:

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "X-Org": "platform"
      },
      "headersHelper": "python ./scripts/mcp_headers.py"
    }
  }
}
```

El helper debe imprimir un JSON object cuyas claves y valores sean cadenas. Los encabezados dinámicos sobrescriben los encabezados estáticos con el mismo nombre. IaC Code ejecuta helpers sin shell, sin stdin, con un entorno heredado mínimo, el directorio de la fuente de configuración como cwd, timeout de 5 segundos y diagnostics de stderr redactados. La cadena de comando `headersHelper` no expande variables de entorno; las variables referenciadas se pasan en el entorno del helper, y el helper debe leerlas por su cuenta. Los helpers de project `.mcp.json` requieren aprobación de proyecto antes de ejecutarse.

## WebSocket Servers

WebSocket servers use `type: "ws"`:

```json
{
  "mcpServers": {
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/mcp"
    }
  }
}
```

El transporte WebSocket del SDK de MCP instalado acepta solo una URL. El código IaC rechaza las configuraciones de WebSocket que también establecen `headers`, `headersHelper` u `oauth`.

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

Las variables faltantes sin valor predeterminado producen una advertencia MCP y se omite el server afectado. La expansión de entorno se aplica recursivamente a cadenas dentro de listas y objetos, excepto a la cadena de comando `headersHelper`, que se conserva literal y recibe las variables referenciadas mediante el entorno del helper.

No almacene secretos de texto sin formato en encabezados o valores ambientales. Utilice referencias de variables de entorno o almacenamiento secreto de OAuth.

## Project Approval

El proyecto `.mcp.json` se puede enviar a un repositorio, por lo que el código IaC no confía en él automáticamente.

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Al presionar Enter se mantiene la `N` predeterminada y se rechaza esa configuración exacta del servidor del proyecto. Escriba "y" o "sí" para aprobarlo. La aprobación se almacena localmente en el directorio de configuración del Código IaC e incluye la ruta del espacio de trabajo, la ruta del archivo del proyecto, el nombre del servidor y la firma de la configuración. Si la configuración del servidor `.mcp.json` cambia, la aprobación se invalida y el servidor vuelve a quedar pendiente.

Las startups sin cabeza, ACP y A2A nunca hacen preguntas de aprobación interactivas. Los servidores de proyectos pendientes se omiten y se notifican como advertencias.

## Disabled Servers

`iac-code mcp disable <name>` almacena una entrada privada de estado deshabilitado en el directorio de configuración del código IaC. Para servidores con ámbito de proyecto, esto no muta el archivo compartido `.mcp.json`. Las entradas deshabilitadas se codifican por alcance, archivo fuente, nombre del servidor y firma de configuración, por lo que cambiar la configuración del servidor invalida el estado obsoleto deshabilitado.
