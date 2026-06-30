---
title: Opciones de línea de comandos
description: Referencia para las opciones de inicio y ejecución única de IaC Code.
---

# Opciones de línea de comandos

Las opciones de línea de comandos cambian cómo se inicia IaC Code. Úselas antes de entrar en el REPL interactivo, o combínelas con `--prompt` para automatización única.

| Opción | Propósito |
|---|---|
| `-h`, `--help` | Mostrar la ayuda del CLI y salir. Use esto para inspeccionar las opciones soportadas por su versión instalada. |
| `-v`, `-V`, `--version` | Imprimir la versión instalada de IaC Code y salir. |
| `-m <model>`, `--model <model>` | Iniciar con un modelo LLM específico. Esto anula el modelo guardado para la ejecución actual. |
| `-p <prompt>`, `--prompt <prompt>` | Ejecutar un único prompt y salir. Esto habilita el modo no interactivo. Use `--prompt -` para leer el prompt desde la entrada estándar. |
| `--output-format <format>` | Establecer el formato de salida para el modo no interactivo. Los valores soportados son `text`, `json` y `stream-json`. El valor predeterminado es `text`. |
| `--max-turns <number>` | Limitar el número máximo de turnos del agente en modo no interactivo. El valor predeterminado es `100`. |
| `-d`, `--debug` | Habilitar el registro de depuración para la ejecución actual. En modo interactivo, use `/debug` para inspeccionar o cambiar el registro de depuración después del inicio. |
| `-r <id-o-nombre-de-sesion>`, `--resume <id-o-nombre-de-sesion>` | Reanudar una sesión anterior por ID exacto, prefijo único de ID o nombre único de sesión. Las sesiones resueltas en otro proyecto imprimen un comando `cd ... && iac-code --resume <id>` en lugar de cambiar en caliente el proyecto actual. |
| `-c`, `--continue` | Reanudar la sesión más reciente. No se puede usar junto con `--resume`. |
| `--allowed-tools <patterns>` | Patrones de permisos de herramientas separados por comas para permitir, ej. `'bash(git *),write_file'`. |
| `--disallowed-tools <patterns>` | Patrones de permisos de herramientas separados por comas para denegar, ej. `'bash(rm *)'`. |
| `--permission-mode <mode>` | Modo de permisos: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |

## Modos de permisos

El parámetro `--permission-mode` controla cómo el agente maneja las comprobaciones de permisos de herramientas:

| Modo | Comportamiento |
|---|---|
| `default` | El agente solicita confirmación cuando una acción de herramienta requiere aprobación. |
| `accept_edits` | Aprobar automáticamente comandos del sistema de archivos considerados como ediciones (ej. `mkdir`, `cp`). Otras acciones aún solicitan confirmación. |
| `bypass_permissions` | Aprobar automáticamente acciones de herramientas excepto comprobaciones de seguridad. Toda decisión allow que requiere un registro de auditoría falla en modo cerrado si falla la persistencia de auditoría. Destinado para automatización confiable. |
| `dont_ask` | Denegar silenciosamente cualquier acción que normalmente solicitaría confirmación. Útil para ejecuciones estrictamente de solo lectura. |

Las llamadas a API de escritura de Alibaba Cloud realizadas mediante `aliyun_api` están protegidas por separado: una regla allow simple `aliyun_api` no las aprueba de forma global, y fuera de `bypass_permissions` las reglas allow de escritura deben coincidir exactamente con el par canónico `product:action`. `bypass_permissions` sí las aprueba automáticamente, pero igual que otras decisiones allow auditadas, la acción se deniega si el registro de auditoría de permisos no se puede persistir. Usa reglas exactas como `aliyun_api(ros:CreateStack)` cuando la automatización confiable deba permitir solo una API de escritura específica.

## Comandos de inicio comunes

Iniciar el REPL interactivo con el modelo guardado:

```bash
iac-code
```

Iniciar con un modelo específico para esta ejecución:

```bash
iac-code --model qwen3.6-plus
```

Ejecutar un prompt único:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Leer el prompt desde la entrada estándar:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Reanudar la sesión más reciente:

```bash
iac-code --continue
```

Permitir solo comandos git y bash de solo lectura:

```bash
iac-code --allowed-tools 'bash(git *)'
```

Permitir una API de escritura específica de Alibaba Cloud:

```bash
iac-code --allowed-tools 'aliyun_api(ros:CreateStack)'
```

Ejecutar en automatización sin prompts interactivos:

```bash
iac-code --prompt "Create a VPC" --permission-mode bypass_permissions
```
