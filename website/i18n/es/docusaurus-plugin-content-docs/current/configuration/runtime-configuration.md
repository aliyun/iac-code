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

Puede reubicarlo estableciendo la variable de entorno `IAC_CODE_CONFIG_DIR` (admite expansión de `~` y `$VAR`). Cuando se establece, todos los artefactos persistidos — credenciales, ajustes, historial, `projects/`, `image-cache/`, `tool-results/`, `logs/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — siguen la nueva ubicación. Los logs de arranque/depuración van por defecto a `<config-dir>/logs/` y se pueden mover por separado con `IAC_CODE_LOG_DIR`; los registros de auditoría de permisos permanecen en `<config-dir>/logs/`.

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
    thinkingEnabled: true
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingEnabled: false
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Campo | Alcance | Descripción |
|---|---|---|
| `thinkingEnabled` | Proveedor o modelo | Interruptor booleano opcional de thinking. `true` pide a proveedores/modelos compatibles que lo habiliten; `false` pide deshabilitarlo; si se omite, conserva el valor predeterminado del proveedor/modelo. |
| `thinkingBudget` | Proveedor o modelo | Presupuesto de reasoning/thinking como entero positivo, enviado a los proveedores que lo admiten. |
| `maxCompletionTokens` | Proveedor o modelo | Valor entero positivo que anula `max_completion_tokens` para proveedores/modelos que usan ese campo de solicitud. |
| `effort` | Proveedor o modelo | Anulación opcional del effort de thinking, solo para modelos que admiten control de effort. |

Los valores válidos a nivel de modelo bajo `providers.<provider>.models.<model>` anulan los valores a nivel de proveedor. Los valores numéricos no válidos se ignoran, por lo que IaC Code recurre al valor del proveedor o a la política integrada del modelo.

Para Alibaba Cloud DashScope y DashScope Token Plan, IaC Code incluye un `thinkingBudget=8192` integrado para `glm-5.2` y `kimi-k2.7-code`. Si `maxCompletionTokens` no está configurado, el límite de la solicitud se calcula como el límite normal de tokens de respuesta más el thinking budget efectivo.

Las solicitudes A2A pueden anular estos ajustes para un solo turno de mensaje mediante `message.metadata.iac_code.thinking` o los flags `--thinking-enabled`, `--thinking-effort` y `--thinking-budget` de `iac-code a2a-client call`. Si no se envían metadatos de thinking A2A explícitos, el runtime usa los ajustes anteriores y los valores predeterminados normales del proveedor. Para proveedores genéricos `openai_compatible` con una base URL en modo compatible de DashScope, iac-code cambia al formato wire nativo de thinking de DashScope solo cuando hay una política de thinking explícita.

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
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| Campo | Descripción |
|---|---|
| `mode` | Modo de permisos: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | Lista de patrones de permisos de herramientas para aprobar automáticamente. |
| `deny` | Lista de patrones de permisos de herramientas para denegar automáticamente. |
| `ask` | Lista de patrones de permisos de herramientas que siempre requieren confirmación. |
| `additional_directories` | Directorios adicionales más allá de cwd en los que el agente puede escribir. |
| `audit` | Configuración local del registro de auditoría de permisos. |

### Sintaxis de patrones

Los patrones de permisos de herramientas siguen el formato `tool_name(rule)`:

| Patrón | Significado |
|---|---|
| `bash` | Coincidir con todos los comandos bash (nombre de herramienta simple). |
| `bash(git *)` | Coincidir con comandos bash que comienzan con `git`. |
| `bash(curl:*)` | Coincidir con comandos bash que comienzan con `curl`. |
| `write_file` | Coincidir con todas las llamadas a la herramienta write_file. |
| `aliyun_api(ros:CreateStack)` | Coincidir con un par producto/acción de API de Alibaba Cloud. |

Las reglas se evalúan en orden: **deny → ask → allow → comportamiento predeterminado**. Los argumentos CLI (`--allowed-tools`, `--disallowed-tools`) tienen la mayor precedencia.

### Permisos de API de Alibaba Cloud

`aliyun_api` distingue entre llamadas API de solo lectura y llamadas que pueden modificar recursos en la nube. Las acciones API de solo lectura se permiten automáticamente. Las llamadas API que no son de solo lectura requieren confirmación o una regla de allow exacta para ese producto/acción, por ejemplo:

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

Una regla allow simple `aliyun_api` no aprueba de forma global las API de escritura de Alibaba Cloud. Fuera de `bypass_permissions`, las reglas allow de escritura deben coincidir exactamente con el par canónico `product:action`. En modo `bypass_permissions`, las API protegidas de escritura de Alibaba Cloud se aprueban automáticamente, pero toda decisión allow que requiere un registro de auditoría falla en modo cerrado si falla la persistencia de auditoría. Los comodines siguen siendo útiles para reglas deny o ask, y para coincidencias de reglas de solo lectura.

Las solicitudes de estilo ROA se tratan como de solo lectura únicamente cuando el método es `GET` y la solicitud no tiene body. Las solicitudes ROA que no son de solo lectura siguen el mismo requisito de regla allow canónica exacta `product:action` que las API de escritura de estilo RPC: una regla exacta como `aliyun_api(cs:CreateCluster)` puede aprobar la escritura, mientras que las reglas allow con comodines siguen sin aprobar llamadas que no son de solo lectura.

### Registro de auditoría de permisos

Las decisiones de permisos que cruzan prompts de usuario, límites de caché de herramientas, aprobación de automatización o aprobación de un resolver se anexan a:

```text
<config-dir>/logs/permission-audit.jsonl
```

De forma predeterminada, esta ruta es `~/.iac-code/logs/permission-audit.jsonl`. El registro de auditoría de permisos sigue `IAC_CODE_CONFIG_DIR`; `IAC_CODE_LOG_DIR` solo mueve los logs de arranque/depuración. El escritor de auditoría anexa registros JSONL con bloqueo de archivo, rota el archivo y restringe los permisos del archivo local cuando el sistema operativo lo permite. Las aprobaciones automáticas rutinarias de solo lectura pueden omitirse, pero se registran denegaciones, prompts, decisiones en caché, aprobaciones de automatización, aprobaciones de resolver y otros límites de permisos auditados.

La configuración de auditoría se define en `permissions.audit`:

| Campo | Predeterminado | Descripción |
|---|---:|---|
| `include_tool_input` | `false` | Incluir entrada de herramienta solo con forma en los registros JSONL de auditoría. Los valores de cadena se guardan como tipo, longitud y huella; las claves que parecen secretas se redactan; los nombres de campo fuera de la lista permitida pueden representarse con huella; no se escriben cadenas de payload de negocio sin procesar. Las entradas de API de Alibaba Cloud también conservan un resumen seguro de la operación. |
| `max_file_bytes` | `10485760` | Rotar `permission-audit.jsonl` cuando supere este tamaño. |
| `max_files` | `5` | Número de archivos de auditoría rotados que se conservan. Los valores por encima del máximo integrado se limitan. |

Si una decisión allow que requiere un registro de auditoría no se puede persistir en el registro de auditoría, IaC Code falla en modo cerrado y deniega la acción en lugar de ejecutarla sin rastro de auditoría.
