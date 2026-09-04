---
sidebar_position: 3
title: Referencia de integración del Skill de IaC Code para hosts
description: Integra el puente del Skill de IaC Code en un agente host compatible.
---

# Referencia de integración del Skill de IaC Code para hosts

Este documento está dirigido a desarrolladores de agentes y sistemas de distribución de Skills. Los usuarios deben
consultar [Instalar y usar el Skill de IaC Code](./skill-integration.md).

## Modelo de integración y configuración

El paquete contiene `SKILL.md` y el puente `scripts/iac_code.py`, que solo usa la biblioteca estándar. Ejecútalo con
CPython 3.8 a 3.14. Trata stdout como resultado JSON estable y stderr como diagnóstico y progreso. Conserva `jobId`,
`contextId`, el cursor y los campos de correlación. Ante un error, no recurras a otro Runtime ni a llamadas directas a
las API cloud.

El distribuidor puede colocar este `config.json` junto a `SKILL.md`:

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

El puente antepone `skill/` a `channel`. El valor predeterminado de `pipelineName` es `selling_solution_first`;
`selling` queda para un flujo heredado solicitado explícitamente. `null` significa espera ilimitada. Se rechazan campos
desconocidos o inválidos. Esta política de instalación no se debe derivar de una petición, mostrar ni modificar durante
una tarea.

## Iniciar y seguir un trabajo

Escribe la petición completa en un archivo UTF-8 del workspace y usa una ruta absoluta:

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

Usa `normal` por defecto y `pipeline` solo para el flujo de comparación, confirmación y despliegue. El idioma puede ser
`en`, `zh`, `es`, `fr`, `de`, `ja`, `pt` o `auto`; conserva después `preferredLanguage`. `llm_not_configured` detiene
antes de crear el trabajo y `cloud_credentials_not_configured` indica credenciales ausentes en Pipeline.

`--follow` devuelve el siguiente límite de presentación o interacción, `turn_completed` o el estado terminal de un
Pipeline. Con `boundaryReached: true`, muestra todos los `userUpdates` y sigue el mismo trabajo:

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

`boundaryReached` no significa que haya terminado. `presentationRequired` exige mostrar la actualización antes de la
siguiente llamada. En modo normal, usa `finalText` y `artifacts` en `turn_completed`; en un Pipeline terminal, usa
`pipelineResult` y `artifacts` e informa de fallos de limpieza. Solo para diagnóstico o recuperación:

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

Si el estado es `input-required` sin `inputRequired`, informa del último texto o error y no cambies el trabajo.

## Gestionar la entrada del usuario

Cada `inputRequired` es un límite estricto: muéstralo en la interfaz nativa del host y espera una respuesta explícita.
No deduzcas valores predeterminados. Conserva `kind`, `inputId`, `requestTaskId`, `contextId` y, si existe, `toolUseId`.

| `kind` | Información que debe mostrar el host | Respuesta |
|---|---|---|
| `permission` | Propósito, efecto, objetivo, solo lectura, resúmenes de despliegue y seguridad, acciones | `allow_once` / `deny` |
| `ask_user_question` | Pregunta, opciones y texto libre permitido | Respuesta |
| `candidate_selection` | Todos los resúmenes, diagramas Mermaid, total mensual y partidas | ID o número |
| `deployment_confirmation` | Solución, URL, precio, parámetros efectivos y modificados, Preview, acciones | `confirm` / `adjust` / `reselect` / `cancel` |

Escribe la respuesta correlacionada en un archivo JSON UTF-8 nuevo y reanuda el mismo trabajo:

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

Omite `parameterOverrides` si no hay ajustes. No deduzcas la confirmación de la petición inicial ni de una aprobación
del host.

## Continuar, cancelar y recuperar

Después de un turno normal o de pasar un Pipeline terminado al modo normal, continúa el trabajo existente:

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

Conserva `jobId` y `contextId`; es normal recibir un `taskId` nuevo. Así también se recuperan esperas de permisos e
interrupciones del host. Para cancelar toda la operación:

```text
python3 scripts/iac_code.py cancel --job-id <job-id>
```

La cancelación completa no equivale a denegar un permiso.

## Errores y Runtime

Un error anterior a la creación es definitivo para esa llamada. Ante `incompatible_host`, muestra la información de
compatibilidad y detente, sin usar pip, otro Runtime ni API directas. El Runtime se guarda en
`<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`. Su estructura e integridad se definen en
`skill-runtime/skill-package-contract.json` y el manifiesto de versión. La limpieza requiere una petición explícita;
los paquetes actuales o activos están protegidos.

El Runtime usa un puerto aleatorio de `127.0.0.1` y un Bearer token por proceso. No expongas el token, estado local,
credenciales, valores del entorno ni entradas o salidas sin filtrar de herramientas.

## Documentación relacionada

- [Visión general de los Skills oficiales de IaC Code](./skill-overview.md)
- [Instalar y usar el Skill de IaC Code](./skill-integration.md)
- [Visión general de A2A](./overview.md)
- [Referencia de A2A](./protocol-reference.md)
