---
title: Modo pipeline
description: Usa el modo pipeline paso a paso para guiar tareas de infraestructura complejas.
---

# Modo pipeline

El modo pipeline es un modo interactivo que ejecuta el trabajo paso a paso. Es útil para tareas de infraestructura que son más largas o más propensas a errores que una solicitud normal de chat: entender el requisito, planificar un enfoque, generar artefactos, pedir confirmación al usuario y continuar con las siguientes acciones.

Pipeline en sí es una capacidad general. La implementación integrada disponible hoy es el pipeline `selling`. `selling` está orientado a escenarios de infraestructura de Alibaba Cloud y puede llevar una solicitud de despliegue por arquitecturas candidatas, plantillas ROS, estimaciones de costo y despliegue después de la confirmación.

Ejemplos de solicitudes adecuadas para el modo pipeline:

```text
Seleccionar una VPC existente y crear un VSwitch
```

```text
Diseñar un despliegue de aplicación web de bajo costo en Alibaba Cloud y generar una plantilla
```

## Iniciar el modo pipeline

Actualmente el modo pipeline requiere el REPL interactivo. No se puede combinar con `--prompt`.

En macOS o Linux:

```bash
IAC_CODE_MODE=pipeline iac-code
```

En PowerShell:

```powershell
$env:IAC_CODE_MODE = "pipeline"
iac-code
```

El nombre de pipeline predeterminado es `selling`. Para indicarlo de forma explícita:

```bash
IAC_CODE_MODE=pipeline IAC_CODE_PIPELINE_NAME=selling iac-code
```

## Relación entre Pipeline y selling

| Nombre | Significado |
|---|---|
| Modo pipeline | Modo general de ejecución paso a paso de IaC Code para flujos largos, puntos de confirmación, recuperación y visualización de progreso. |
| Pipeline `selling` | Pipeline integrado actual para diseño de infraestructura de Alibaba Cloud, generación de plantillas, estimación de costos y despliegue. |

Si en el futuro se agregan más pipelines, se podrán seleccionar con `IAC_CODE_PIPELINE_NAME`. La versión actual incluye `selling`.

## Variables de entorno

| Variable | Propósito |
|---|---|
| `IAC_CODE_MODE=pipeline` | Activa el modo pipeline. Cualquier otro valor vuelve al modo normal. |
| `IAC_CODE_PIPELINE_NAME` | Selecciona la definición de pipeline. El valor predeterminado es `selling`. |
| `IAC_CODE_CWD` | Sobrescribe el directorio de trabajo usado por el pipeline. |
| `IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING` | Activa el paso opcional de revisión de plantilla en el pipeline `selling`. |

## Qué ocurre en el pipeline selling

El pipeline `selling` divide una solicitud de infraestructura en etapas comprensibles para el usuario:

| Etapa | Qué verás |
|---|---|
| Entender el requisito | IaC Code comprueba si la solicitud es una tarea de infraestructura en Alibaba Cloud. Si faltan detalles importantes, pregunta antes de generar un plan. |
| Planificar arquitecturas | IaC Code propone una o más arquitecturas candidatas para que puedas comparar compromisos. |
| Generar y evaluar | IaC Code genera plantillas ROS para los planes candidatos y estima costos de recursos. |
| Confirmar un plan | IaC Code muestra los detalles de los candidatos y espera a que elijas el plan con el que quieres continuar. |
| Desplegar | Después de seleccionar un plan, IaC Code entra en la etapa de despliegue y maneja herramientas u operaciones de mayor riesgo según la política de permisos. |

Si mencionas restricciones como “usar una VPC existente” o “no crear este tipo de recurso”, el pipeline `selling` intentará respetarlas en los planes y plantillas posteriores. No necesitas conocer campos internos; basta con escribir las restricciones en la solicitud.

## Interacción y recuperación

El modo pipeline puede pausar y esperar entrada del usuario, por ejemplo:

- El requisito no está claro y IaC Code necesita objetivo, escala, región o presupuesto.
- Hay varios planes candidatos y debes elegir uno.
- Una herramienta o acción de despliegue requiere aprobación de permisos.
- La ejecución fue interrumpida y necesita recuperarse o continuar.

Si el proceso termina o la sesión se interrumpe, IaC Code guarda el estado del pipeline. Cuando vuelvas más tarde a esa sesión con `--resume`, podrás revisar el progreso anterior y continuar desde un punto recuperable.

Cuando el pipeline termina, falla, sale antes de tiempo o se cancela, IaC Code vuelve al chat normal. Luego puedes hacer preguntas de seguimiento, ajustar el plan o resolver problemas posteriores al despliegue.

## Integraciones de automatización

El modo pipeline está diseñado actualmente sobre todo para el REPL interactivo. El modo servidor A2A puede exponer progreso del pipeline, artefactos, resultados de permisos e información de recuperación, lo que resulta útil para conectar un pipeline a una consola externa o a un sistema de tareas.

ACP no admite actualmente el modo pipeline. `--prompt` / el [modo no interactivo](./non-interactive-mode.md) ejecuta una solicitud normal de una sola vez y no ejecuta pasos de Pipeline.

## Limitaciones actuales

- La versión actual incluye solo el pipeline `selling`, principalmente para flujos de infraestructura de Alibaba Cloud.
- El modo pipeline requiere el REPL interactivo. `--prompt` se rechaza cuando `IAC_CODE_MODE=pipeline`.
- El modo pipeline admite entrada de texto. Las imágenes pegadas en el REPL se ignoran mientras el pipeline está activo.
- Durante el pipeline, los shell escapes, disparadores de skills y la mayoría de los slash commands están restringidos salvo que la definición del pipeline los permita explícitamente. Comandos básicos como `/help`, `/status`, `/resume` y `/exit` siguen disponibles.
