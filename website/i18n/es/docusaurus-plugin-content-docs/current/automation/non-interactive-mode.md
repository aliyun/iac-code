---
title: Modo no interactivo
description: Ejecutar prompts únicos desde argumentos o stdin.
---

# Modo no interactivo

El modo no interactivo ejecuta un único prompt y sale. Úselo cuando quiera que IaC Code produzca una salida para una tarea repetible sin permanecer en el REPL.

Use `--prompt` para pasar el prompt directamente:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Use `--prompt -` para leer el prompt desde la entrada estándar:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Use `--output-format` cuando el llamador necesite salida estructurada:

```bash
iac-code --prompt "Create an OSS Bucket" --output-format json
```

Use `--max-turns` para limitar cuánto tiempo puede trabajar el agente:

```bash
iac-code --prompt "Create a VPC" --max-turns 20
```

Los formatos de salida soportados son:

| Formato | Propósito |
|---|---|
| `text` | Salida legible para humanos. Este es el valor predeterminado. |
| `json` | Un único resultado JSON para llamadores que analizan la respuesta final. |
| `stream-json` | Eventos JSON en streaming para llamadores que procesan progreso incremental. |

## Control de permisos en automatización

Cuando ejecute en modo no interactivo, use `--permission-mode` para controlar cómo el agente maneja las aprobaciones de herramientas:

```bash
iac-code --prompt "Deploy the stack" --permission-mode bypass_permissions
```

En `bypass_permissions`, las acciones de herramientas se aprueban automáticamente excepto las comprobaciones de seguridad, pero toda decisión allow que requiere un registro de auditoría sigue fallando en modo cerrado si falla la persistencia de auditoría. Las API de escritura de Alibaba Cloud siguen protegidas por separado fuera de `bypass_permissions`; para una automatización confiable más acotada, no use `bypass_permissions` y permita explícitamente cada API de escritura requerida:

```bash
iac-code --prompt "Deploy the stack" \
  --allowed-tools 'aliyun_api(ros:CreateStack)' \
  --permission-mode dont_ask
```

Para restringir lo que el agente puede hacer, combine `--allowed-tools` y `--disallowed-tools`:

```bash
iac-code --prompt "Check the stack status" \
  --allowed-tools 'bash(git *),bash(ls:*)' \
  --disallowed-tools 'bash(rm *)' \
  --permission-mode dont_ask
```

Para todos los parámetros de inicio, consulte [Opciones de línea de comandos](../cli/command-line-options.md).
