---
title: Interfaz de terminal anterior
description: Entrada de compatibilidad para flujos de terminal existentes.
---

# Interfaz de terminal anterior

TUI es la interfaz anterior para conversar y usar herramientas directamente en una terminal. Esta página sirve de referencia para los flujos de terminal existentes.

:::note

TUI ya no recibe actualizaciones ni mantenimiento. Las nuevas funciones de interfaz se desarrollan para [Web](../web-app.md) y [Desktop](../desktop-app.md); no se garantiza que la terminal ofrezca los mismos controles o presentación.

:::

## Iniciar y usar TUI

1. Instala el [paquete Python](../getting-started/installation.md) y configura un [proveedor de modelo](../configuration/llm-providers.md). Para tareas de nube, configura también las [credenciales Alibaba Cloud](../configuration/alibaba-cloud-credentials.md). TUI reutiliza esa configuración guardada.

2. Abre una terminal interactiva, entra en el directorio del proyecto e inicia:

   ```bash
   cd /path/to/project
   iac-code tui
   ```

3. Tras el aviso de fin de mantenimiento aparece la entrada. Escribe y envía la solicitud. Las respuestas y acciones de herramientas se muestran en la terminal; responde allí a las preguntas y aprobaciones necesarias.

4. Usa `/help` para consultar las órdenes de tu versión, `/status` para ver la sesión actual y `/exit` para salir.

TUI necesita una terminal interactiva. Para scripts o CI sin terminal, usa la [automatización headless](../automation/non-interactive-mode.md). Sin subcomando, `iac-code` abre Web de forma predeterminada.

## Continuar una conversación de terminal

Ejecuta desde el directorio del proyecto de la conversación. Para elegir una sesión por ID o nombre único:

```bash
iac-code tui --resume <session-id-or-name>
```

Para continuar la sesión más reciente del proyecto actual:

```bash
iac-code tui --continue
```

No combines `--resume` y `--continue`. Consulta `iac-code tui --help` para ver las opciones de tu versión.

## Pasar a Web o Desktop

1. Ejecuta `iac-code web` o abre Desktop y elige el mismo directorio del proyecto.

2. Con el mismo usuario del sistema e `IAC_CODE_CONFIG_DIR`, puedes reutilizar la configuración del modelo, las credenciales de nube y las instrucciones guardadas en memoria. Después se gestionan en los ajustes; consulta [Operaciones habituales](./commands.md).

3. Crea una conversación nueva para continuar el trabajo. Para consultar una conversación anterior de terminal, activa la opción de solo lectura en **Ajustes → General → Visibilidad de sesiones externas**. Compartir almacenamiento no permite editarlas en Web/Desktop; consulta [Sesiones](./sessions.md).
