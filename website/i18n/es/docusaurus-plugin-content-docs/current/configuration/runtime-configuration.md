---
title: Configuración
description: Orden de configuración en tiempo de ejecución y archivos locales.
---

# Configuración

IaC Code lee la configuración desde argumentos CLI, variables de entorno y archivos en el directorio de configuración en tiempo de ejecución.

Precedencia de configuración:

```text
Argumentos CLI > variables de entorno > archivos de configuración
```

El directorio de tiempo de ejecución por defecto es:

```text
~/.iac-code/
```

Puede reubicarlo estableciendo la variable de entorno `IAC_CODE_CONFIG_DIR` (admite expansión de `~` y `$VAR`). Cuando se establece, todos los artefactos persistidos — credenciales, ajustes, historial, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — siguen la nueva ubicación.

Archivos comunes:

| Archivo | Descripción |
|---|---|
| `.credentials.yml` | Credenciales de LLM |
| `.cloud-credentials.yml` | Credenciales del proveedor de nube |
| `settings.yml` | Proveedor seleccionado, modelo y configuraciones relacionadas |
| `AGENTS.md` | Memoria de usuario cargada como instrucciones persistentes |
| history files | Historial de entrada para flujos de trabajo interactivos |

Evite hacer commit o compartir archivos de este directorio porque pueden contener secretos o preferencias locales.

## Archivos de memoria

IaC Code tiene dos ubicaciones públicas de memoria:

| Ubicación | Propósito |
|---|---|
| `<project-root>/AGENTS.md` | Memoria del proyecto. Puede hacerse commit cuando las instrucciones son útiles para todas las personas que trabajan en el proyecto. |
| `<config-dir>/AGENTS.md` | Memoria de usuario. Sigue `IAC_CODE_CONFIG_DIR` y es privada para el usuario local. |

Defina `IAC_CODE_INSTRUCTION_MEMORY_FILE` para usar otro nombre de archivo de memoria de instrucciones, por ejemplo `IAC-CODE.md`.

Los archivos de temas de auto-memory del proyecto se almacenan en:

```text
<config-dir>/projects/<project-key>/memory/
```

`MEMORY.md` en esa carpeta es el índice de temas usado por las side calls de auto-memory. No se carga como contexto permanente. Cuando auto-memory está activada, IaC Code puede seleccionar archivos de temas relevantes y añadirlos como contexto oculto de la conversación.

## Configuración del proyecto

Además del archivo `~/.iac-code/settings.yml` a nivel de usuario, IaC Code carga configuraciones a nivel de proyecto desde el directorio de trabajo actual:

| Archivo | Alcance |
|---|---|
| `.iac-code/settings.yml` | Configuración compartida del proyecto (segura para hacer commit). |
| `.iac-code/settings.local.yml` | Anulaciones locales (debe estar en .gitignore). |

Orden de fusión: **configuración de usuario → configuración del proyecto → configuración local del proyecto → argumentos CLI** (las fuentes posteriores anulan las anteriores).

## Política de solicitudes del proveedor

Las entradas de proveedor en `settings.yml` pueden incluir campos de política de solicitudes para proveedores compatibles con OpenAI. Estas opciones son útiles cuando un modelo separa los tokens de respuesta visibles de los tokens de reasoning/thinking.

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Campo | Alcance | Descripción |
|---|---|---|
| `thinkingBudget` | Proveedor o modelo | Presupuesto de reasoning/thinking como entero positivo, enviado a los proveedores que lo admiten. |
| `maxCompletionTokens` | Proveedor o modelo | Valor entero positivo que anula `max_completion_tokens` para proveedores/modelos que usan ese campo de solicitud. |
| `effort` | Proveedor o modelo | Anulación opcional del effort de thinking, solo para modelos que admiten control de effort. |

Los valores válidos a nivel de modelo bajo `providers.<provider>.models.<model>` anulan los valores a nivel de proveedor. Los valores numéricos no válidos se ignoran, por lo que IaC Code recurre al valor del proveedor o a la política integrada del modelo.

Para Alibaba Cloud DashScope y DashScope Token Plan, IaC Code incluye un `thinkingBudget=8192` integrado para `glm-5.2` y `kimi-k2.7-code`. Si `maxCompletionTokens` no está configurado, el límite de la solicitud se calcula como el límite normal de tokens de respuesta más el thinking budget efectivo.

## Configuración de permisos de herramientas

La sección `permissions` en `settings.yml` configura qué acciones de herramientas se permiten, deniegan o requieren confirmación:

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| Campo | Descripción |
|---|---|
| `mode` | Modo de permisos: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Lista de patrones de permisos de herramientas para aprobar automáticamente. |
| `deny` | Lista de patrones de permisos de herramientas para denegar automáticamente. |
| `ask` | Lista de patrones de permisos de herramientas que siempre requieren confirmación. |
| `additional_directories` | Directorios adicionales más allá de cwd en los que el agente puede escribir. |

### Sintaxis de patrones

Los patrones de permisos de herramientas siguen el formato `tool_name(rule)`:

| Patrón | Significado |
|---|---|
| `bash` | Coincidir con todos los comandos bash (nombre de herramienta simple). |
| `bash(git *)` | Coincidir con comandos bash que comienzan con `git`. |
| `bash(curl:*)` | Coincidir con comandos bash que comienzan con `curl`. |
| `write_file` | Coincidir con todas las llamadas a la herramienta write_file. |

Las reglas se evalúan en orden: **deny → ask → allow → comportamiento predeterminado**. Los argumentos CLI (`--allowed-tools`, `--disallowed-tools`) tienen la mayor precedencia.
