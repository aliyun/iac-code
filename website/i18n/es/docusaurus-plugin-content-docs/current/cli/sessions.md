---
title: Sesiones
description: Persistir y reanudar conversaciones entre ejecuciones.
---

# Sesiones

Web, escritorio y headless comparten el almacén de sesiones. Las conversaciones se guardan automáticamente en el directorio de configuración del entorno de ejecución.

## Web y escritorio

### Continuar una conversación

1. Abre la conversación bajo su proyecto en la barra lateral o búscala por título con **Buscar**.

2. Revisa los mensajes y archivos guardados.

3. Escribe y envía la siguiente solicitud. Para una tarea independiente, usa **Nuevo chat**.

### Cambiar el nombre o fijar una conversación

1. Abre la conversación y su menú **…**.

2. Elige **Cambiar nombre de la conversación**, escribe el título y pulsa **Guardar**. El nuevo título aparece en la barra lateral.

3. Para encontrarla fácilmente, usa la opción de fijar la conversación del mismo menú. Puedes deshacerla después desde ese menú.

### Archivar y recuperar una conversación

1. Elige **Archivar conversación** en el menú **…**. Se retira de la lista activa.

2. Abre **Ajustes → Conversaciones archivadas** y localízala con la búsqueda y el filtro de proyecto.

3. Pulsa **Desarchivar** junto a la conversación y vuelve a abrirla desde la barra lateral.

:::note

Las conversaciones creadas mediante CLI, A2A o ACP se ocultan de forma predeterminada. Para consultarlas, activa la opción de solo lectura correspondiente en **Ajustes → General → Visibilidad de sesiones externas**. Compartir el almacenamiento no permite editarlas en Web/Desktop.

:::

## Continuar en modo headless

Ejecute desde el directorio del proyecto de la sesión. `--resume` continúa una sesión concreta y `--continue` la más reciente del proyecto. Las formas cortas son `-r` y `-c`, que no se pueden combinar.

```bash
iac-code --resume deploy-prod --prompt "Generar una plantilla VPC"
iac-code --continue --prompt "Generar una plantilla VPC"
```

## Otros proyectos

En Web o Desktop, abre la conversación bajo su proyecto en la barra lateral. En headless, cambia primero al directorio de ese proyecto:

```bash
cd /path/to/project
iac-code --resume <session-id> --prompt "Generar una plantilla VPC"
```

## Conversaciones interrumpidas

Vuelva a abrir la conversación guardada para revisar el historial y continuar. La recuperación concilia las llamadas a herramientas incompletas. El progreso y las decisiones pendientes siguen la [recuperación de Pipeline](../automation/pipeline-mode.md). Las ejecuciones únicas con `--prompt` no ejecutan pipelines.
