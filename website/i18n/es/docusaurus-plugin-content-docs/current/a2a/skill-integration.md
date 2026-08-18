---
sidebar_position: 7
title: Integración de Skill
description: Agentes externos impulsan iac-code mediante el Skill empaquetado de iac-code y el Skill Runtime.
---

# Integración de Skill

iac-code incluye un Skill empaquetado para agentes externos. Un agente externo (un agente planificador o una plataforma de agentes) no instala el paquete Python de iac-code ni invoca comandos headless; impulsa un runtime A2A local autenticado mediante un script puente de solo biblioteca estándar para ejecutar trabajo de infraestructura de Alibaba Cloud como generación de plantillas ROS/Terraform, estimación de costes, selección de recursos y despliegue.

## Componentes

| Componente | Ubicación | Descripción |
|---|---|---|
| Paquete del Skill | `skills/iac-code/` | Instrucciones en `SKILL.md`, metadatos de agente en `agents/` y `scripts/iac_code.py`, el script puente |
| Skill Runtime | Publicado por plataforma | Ejecutable nativo CPython 3.12 que incorpora el servidor A2A de iac-code |
| Contratos de distribución | `skill-runtime/skill-package-contract.json`, `skill-runtime/publisher-contract.json` | Restricciones de formato y verificación para paquetes de skill y publicadores |

El script puente está escrito por completo con la biblioteca estándar de Python y mantiene compatibilidad con Python 3.8+; el CI lo compila y lo ejecuta en pruebas de humo en la matriz completa 3.8–3.14. No añadas dependencias de terceros ni sintaxis exclusiva de versiones nuevas al puente.

## Obtención y caché del Runtime

En el primer uso, el puente lee el manifiesto, descarga el artefacto de la plataforma actual, verifica su tamaño y SHA-256, lo instala y lo almacena en caché bajo `<IAC_CODE_CONFIG_DIR o ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`.

- `python3 scripts/iac_code.py ensure-runtime` — prepara el runtime con antelación; si está en caché se reutiliza.
- `python3 scripts/iac_code.py cache list` — muestra los runtimes instalados y los paquetes candidatos.
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — limpia cachés del runtime o paquetes candidatos; requiere `--confirm` explícito.

## Preflight de configuración

Antes de crear un trabajo, `start` ejecuta una comprobación de preparación de la configuración a través del runtime. El preflight no lee valores secretos; solo informa del estado de preparación:

| Situación | Resultado |
|---|---|
| Proveedor LLM o API key incompletos | Devuelve `llm_not_configured` y rechaza crear el trabajo |
| Pipeline selling con credenciales de Alibaba Cloud incompletas | Devuelve `cloud_credentials_not_configured` y rechaza crear el trabajo |
| Modo normal con credenciales de Alibaba Cloud incompletas | Puede continuar para trabajo que no llama a API de la nube, con una advertencia de preflight |

## Referencia de comandos

| Comando | Propósito |
|---|---|
| `start` | Crear un trabajo: `--mode normal|pipeline`, `--pipeline-name`, `--cwd` espacio de trabajo absoluto, `--prompt-file` archivo de prompt UTF-8, `--language auto|en|zh|es|fr|de|ja|pt`, opcional `--follow` |
| `follow` | Consume el flujo de eventos hasta el siguiente límite de interacción: `--job-id`, `--cursor`, `--wait-seconds` (60 s por defecto, máximo 120 s) |
| `continue` | Continúa una conversación en modo normal dentro del mismo trabajo: `--job-id`, `--prompt-file`, opcional `--follow` |
| `respond` | Responde a una entrada pendiente, consulta [Entrada del usuario](#input-required) |
| `poll` | Sondeo de un solo uso solo para diagnóstico y recuperación; no lo uses como sustituto de `follow` |
| `cancel` | Cancela el trabajo |
| `ensure-runtime` / `cache list` / `cache clean` | Gestión del runtime y la caché |

`start --follow` y `follow` escriben los límites de paso y latidos de baja frecuencia en stderr; stdout emite exactamente un resultado JSON acotado.

## Límites de interacción {#boundaries}

`--follow` consume el flujo de eventos hasta el siguiente límite de paso, solicitud de permiso, pregunta del usuario, selección de candidato, `turn_completed` o estado terminal. Un resultado de límite incluye:

- `boundaryReached: true` — se alcanzó un límite; esto **no** significa que el trabajo haya terminado;
- `presentationRequired: true` y `userUpdates` — cadenas localizadas listas para mostrar al usuario;
- el `cursor` necesario para continuar.

El agente externo debe presentar primero cada cadena `userUpdates` recibida en una respuesta visible para el usuario y luego llamar de inmediato a `follow` otra vez con el `cursor` devuelto. No respondas la tarea de infraestructura en paralelo ni plantees preguntas no relacionadas mientras hay un follow en ejecución.

## Entrada del usuario {#input-required}

Un resultado contiene `inputRequired` cuando se necesita entrada del usuario. Hay tres tipos:

- `permission` — una solicitud de permiso de herramienta o despliegue. El sobre contiene `inputId`, `toolUseId`, título, propósito, efecto, objetivo, indicador de solo lectura, `safeSummary` y, en solicitudes de despliegue, `deploymentSummary`. El agente externo debe decidir según su propia política de permisos: si la misma operación continuaría sin preguntar cuando el agente la ejecuta directamente, responde `allow_once`; si su política la denegaría, responde `deny`; en caso contrario, pregunta al usuario. Las denegaciones propias de iac-code no deben anularse.
- `ask_user_question` — una pregunta de opción múltiple o texto libre. Presenta el aviso y las opciones tal cual; acepta texto libre solo cuando `allowFreeText` es `true`.
- `candidate_selection` — selección de plan del pipeline. Presenta primero el resumen, el diagrama de arquitectura (Mermaid), el coste mensual total y las partidas de coste de cada candidato, y luego devuelve el candidato seleccionado. Nunca sustituyas los precios proporcionados por estimaciones aproximadas.

`respond` tiene dos formas:

```bash
# Decisión en línea para permisos
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# Las preguntas y selecciones de candidato usan un archivo de respuesta
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

Una respuesta debe conservar todos los campos de correlación de la entrada pendiente y queda vinculada al `kind`, `inputId`, `requestTaskId` y `contextId` actuales; nunca reutilices una respuesta de otra solicitud ni reinterpretes una selección de recurso como una confirmación de despliegue.

## Control de idioma

`start --language` establece el idioma preferido del trabajo (usa `auto` cuando sea desconocido). Cada resultado de ese trabajo repite `preferredLanguage`; trátalo como estado de control duradero: el progreso, las preguntas, los avisos de permisos, los planes candidatos y los resultados finales se presentan en ese idioma, mientras que los nombres de campos del protocolo, los enumerados, los ID y los comandos permanecen sin cambios. Cuando el texto autorizado ya usa ese idioma, preséntalo directamente o resúmelo en el mismo idioma; nunca traduzcas contenido chino visible para el usuario al inglés.

## Relación con el protocolo A2A

El puente se comunica con el runtime local mediante HTTP A2A JSON-RPC; los estados de tarea, los artefactos y las interacciones de permisos reutilizan el protocolo A2A de iac-code:

- Las respuestas de banda lateral de permisos usan el formato de mensaje `schemaVersion 1`; consulta la [Referencia del protocolo](./protocol-reference.md) para los campos y restricciones.
- En modo pipeline, pasar `candidatePresentation: rich-v1` devuelve cargas estructuradas de presentación de candidatos.
- Los estados de resultado del trabajo se corresponden con estados de tarea A2A: `turn_completed` termina un turno normal; los estados terminales del pipeline son `completed`, `failed`, `canceled` y `rejected`, con `pipelineResult` y `artifacts` como resultado autorizado.

## Límite de seguridad

- El runtime escucha únicamente en un puerto aleatorio de `127.0.0.1`; cada arranque genera un token Bearer aleatorio nuevo y cada solicitud del puente lo incluye.
- El puente mantiene los artefactos y resultados dentro del espacio de trabajo del trabajo; los resultados se escriben en `.iac-code-skill-results/` del espacio de trabajo.
- Los informes de preflight y los campos de visualización de permisos están saneados; los secretos y credenciales nunca aparecen en los campos de visualización.
