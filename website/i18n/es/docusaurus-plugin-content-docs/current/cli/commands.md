---
title: Operaciones habituales
description: Usar ajustes, menús de conversación y controles de entrada en escritorio y web.
---

# Operaciones habituales

Los pasos se aplican tanto a Desktop como a Web, que comparten la misma interfaz de conversación.

## Iniciar una conversación

1. Abre Desktop o ejecuta `iac-code` en una terminal interactiva para abrir Web.

2. Pulsa **Nuevo chat** en la barra lateral. Debajo de la entrada, elige el directorio del proyecto con los archivos existentes o donde guardar las plantillas.

3. Antes del primer mensaje, elige **Modo normal** o **Modo pipeline**. Para pipeline, selecciona también el flujo que quieres ejecutar.

4. Escribe la tarea y envíala. La conversación aparece bajo su proyecto en la barra lateral y puedes continuar en ella.

## Referenciar archivos o adjuntar imágenes

1. Abre una conversación del proyecto que contiene el archivo.

2. Escribe `@` y parte del nombre del archivo y selecciona el resultado. Aparece una referencia junto a la entrada. Para una imagen, usa el control de archivos adjuntos.

3. Añade una instrucción, por ejemplo «Revisa esta plantilla ROS y explica sus parámetros», comprueba las referencias y envía el mensaje.

## Elegir el modelo y el razonamiento

1. Sigue primero la [configuración del modelo](../configuration/llm-providers.md).

2. Abre el selector de modelo debajo de la entrada y elige un proveedor y modelo disponibles.

3. Si el modelo lo permite, selecciona el esfuerzo de razonamiento y activa o desactiva **Pensamiento** para esta conversación.

4. Comprueba el modelo mostrado antes de enviar la siguiente solicitud. Las opciones dependen del modelo.

## Responder a una aprobación de herramienta

1. Antes del envío, abre el control de permisos de la entrada y elige el modo deseado.

2. Cuando aparezca una tarjeta de aprobación, revisa la herramienta y la operación propuesta, incluido el archivo o la API de nube de destino.

3. Aprueba o rechaza en la tarjeta. La aprobación permite continuar la operación; el rechazo se devuelve al asistente. Responde por separado a las tarjetas de preguntas.

## Editar instrucciones y gestionar la memoria

1. Abre **Ajustes → Memoria** y elige el proyecto en la parte superior.

2. En **Memoria del proyecto**, escribe las instrucciones propias del proyecto y pulsa su botón **Guardar**.

3. En **Memoria del usuario**, escribe las preferencias comunes a todos los proyectos y pulsa el **Guardar** correspondiente. Comprueba la confirmación de cada cambio.

4. Activa o desactiva **Memoria automática → Registro automático** para permitir que el asistente registre hechos útiles mientras trabaja. El cambio se guarda automáticamente.

5. Busca y consulta las entradas en **Biblioteca de memoria**. Para quitar una entrada obsoleta, usa su control de eliminación y confirma.

## Cambiar el idioma o la apariencia

1. Abre **Ajustes → General**.

2. Selecciona el **Idioma**. Se guarda automáticamente y la página se recarga.

3. Pulsa un tema en **Combinación de colores**. La interfaz se actualiza y guarda la elección automáticamente.

## Otras tareas

Consulta los pasos de [Sesiones](./sessions.md), [Skills](./skills.md) y [Inicio rápido de MCP](../mcp/quick-start.md).

## Atajos opcionales

En una conversación normal existente, `/` muestra algunos atajos:

| Atajo | Propósito |
|---|---|
| `/status` | Mostrar el estado actual de la sesión. |
| `/compact` | Resumir la conversación para reducir el contexto. |
| `/mcp` | Mostrar el estado MCP; gestione las conexiones en **Complementos → MCP**. |

Pipeline oculta `/compact`; estos atajos no aparecen en el borrador de una conversación nueva. Seleccione un skill en las sugerencias, añada su petición y envíe. Consulte [Skills](./skills.md).
