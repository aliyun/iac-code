---
title: Opciones de línea de comandos
description: Referencia para las opciones de inicio y ejecución única de IaC Code.
---

# Opciones de línea de comandos

El comando principal inicia la web por defecto. Las opciones siguientes cubren el inicio web, la ejecución headless puntual y los subprocesos stream-json. Configure las conversaciones interactivas en Web o escritorio.

La columna de entrada indica dónde se acepta cada opción. `headless` significa `iac-code --prompt ...`; `process` indica el modo de subproceso stream-json. El modelo y los permisos de Web se configuran en la interfaz.

| Opción | Propósito | Entrada |
|---|---|---|
| `-h`, `--help` | Mostrar la ayuda del CLI y salir. Use esto para inspeccionar las opciones soportadas por su versión instalada. | `iac-code`, `web` |
| `-v`, `-V`, `--version` | Imprimir la versión instalada de IaC Code y salir. | `iac-code` |
| `-m <model>`, `--model <model>` | Iniciar con un modelo LLM específico. Esto anula el modelo guardado para la ejecución actual. | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | Ejecutar un único prompt y salir. Esto habilita el modo no interactivo. Use `--prompt -` para leer el prompt desde la entrada estándar. | `iac-code` |
| `--output-format <format>` | Establecer el formato de salida para el modo no interactivo. Los valores soportados son `text`, `json` y `stream-json`. El valor predeterminado es `text`. | `headless`, `process` |
| `--max-turns <number>` | Limitar el número máximo de turnos del agente en modo no interactivo. El valor predeterminado es `100`. | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | Controla si las solicitudes no interactivas de una sola ejecucion habilitan explicitamente thinking. El valor predeterminado es `--thinking-enabled`; usa `--no-thinking-enabled` para enviar `thinking_enabled=false` en esta ejecucion sin reescribir `settings.yml`. | `headless` |
| `-d`, `--debug` | Activar los registros de depuración para esta ejecución. | `headless`, `process`, `web` |
| `--verbose` | Mostrar el progreso del modo headless (no interactivo) en la salida de error estándar. | `headless` |
| `-r <id-o-nombre-de-sesion>`, `--resume <id-o-nombre-de-sesion>` | Reanudar por ID exacto, prefijo único o nombre único. Ejecute los comandos headless desde el directorio del proyecto de la sesión. | `headless` |
| `-c`, `--continue` | Reanudar la sesión más reciente. No se puede usar junto con `--resume`. | `headless` |
| `--allowed-tools <patterns>` | Patrones de permisos de herramientas separados por comas para permitir, ej. `'bash(git *),write_file'`. | `headless`, `process` |
| `--disallowed-tools <patterns>` | Patrones de permisos de herramientas separados por comas para denegar, ej. `'bash(rm *)'`. | `headless`, `process` |
| `--permission-mode <mode>` | Modo de permisos: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. | `headless`, `process` |

## Modos de permisos

El parámetro `--permission-mode` controla cómo el agente maneja las comprobaciones de permisos de herramientas:

| Modo | Comportamiento |
|---|---|
| `default` | El agente solicita confirmación cuando una acción de herramienta requiere aprobación. |
| `accept_edits` | Aprobar automáticamente comandos del sistema de archivos considerados como ediciones (ej. `mkdir`, `cp`). Otras acciones aún solicitan confirmación. |
| `bypass_permissions` | Aprobar automáticamente acciones de herramientas excepto comprobaciones de seguridad. Toda decisión allow que requiere un registro de auditoría falla en modo cerrado si falla la persistencia de auditoría. Destinado para automatización confiable. |
| `dont_ask` | Denegar las acciones que requieren aprobación, salvo que se aplique una regla de permiso explícita. |

Las llamadas a API de escritura de Alibaba Cloud realizadas mediante `aliyun_api` están protegidas por separado: una regla allow simple `aliyun_api` no las aprueba de forma global, y fuera de `bypass_permissions` las reglas allow de escritura deben coincidir exactamente con el par canónico `product:action`. `bypass_permissions` sí las aprueba automáticamente, pero igual que otras decisiones allow auditadas, la acción se deniega si el registro de auditoría de permisos no se puede persistir. Usa reglas exactas como `aliyun_api(ros:CreateStack)` cuando la automatización confiable deba permitir solo una API de escritura específica.

## Comandos de inicio comunes

Iniciar la aplicación web local:

```bash
iac-code
```

Iniciar con un modelo específico para esta ejecución:

```bash
iac-code --prompt "Crear una VPC" --model qwen3.7-max
```

Ejecutar un prompt único:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Ejecutar un prompt único con thinking deshabilitado para esta solicitud:

```bash
iac-code --prompt "Create an OSS Bucket" --no-thinking-enabled
```

Leer el prompt desde la entrada estándar:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Continuar la sesión más reciente con un prompt único:

```bash
iac-code --continue --prompt "Generar una plantilla VPC"
```

Preaprobar comandos bash que coincidan con `git *`:

```bash
iac-code --prompt "Mostrar el estado de git" --allowed-tools 'bash(git *)'
```

Preaprobar una API de escritura específica de Alibaba Cloud:

```bash
iac-code --prompt "Desplegar la pila" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

Ejecutar en automatización sin prompts interactivos:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
