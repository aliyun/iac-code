---
title: Modo interactivo
description: Usar el REPL para trabajo iterativo de infraestructura.
---

# Modo interactivo

Ejecuta sin argumentos para entrar al REPL interactivo:

```bash
iac-code
```

El modo interactivo es util cuando quieres refinar los requisitos de infraestructura en multiples turnos.

Comienza con la autenticacion:

```text
/auth
```

Luego describe lo que quieres construir:

```text
Create a VPC, two ECS instances, and a security group that allows SSH from my office IP.
```

## Comandos

Escribe `/` para descubrir los comandos slash disponibles. Entre los comandos operativos habituales están `/status` para el estado de la sesión actual, `/skills` para gestionar habilidades, `/memory` para las memorias guardadas, `/rename` para nombrar la sesión activa y `/resume` para cambiar de sesión.

Escribe `$` para descubrir e invocar solo habilidades.

## Editar la entrada

Usa `Shift+Enter` para insertar una nueva linea sin enviar el prompt. Pulsa `Enter` solo para enviar el prompt completo.

Si tu terminal no informa `Shift+Enter` por separado, pulsa `Esc` y luego `Enter` para insertar una nueva linea. Los prompts de varias lineas se guardan como una sola entrada de historial, por lo que `Up` restaura el prompt completo.

## Shell escapes

Antepon `!` a una linea para ejecutar un comando local de shell desde el REPL mediante la herramienta `bash` integrada:

```text
!pwd
!git status --short
```

IaC Code aplica las comprobaciones normales de permisos de herramientas, ejecuta el comando en el contexto del proyecto actual y muestra la salida en el terminal. El comando no se envia al modelo como mensaje de chat.
