---
title: Aplicación web
description: Iniciar la aplicación web local y usar sus ajustes, conversaciones y controles de entrada.
---

# Aplicación web

Ejecuta `iac-code` en una terminal para iniciar la aplicación web local y abrir el navegador. En el primer uso, configura el proveedor LLM y las credenciales de Alibaba Cloud en Ajustes.

Desktop y Web comparten conversaciones y ajustes. Las conversaciones creadas desde otras interfaces pueden ser de solo lectura; consulta [Sesiones](./cli/sessions.md) para mostrarlas y continuarlas.

## Instalación

La instalación predeterminada incluye las dependencias de la aplicación web y del entorno HTTP. El extra `[http]` se conserva como alias de compatibilidad; ya no hace falta instalarlo por separado. Desde el repositorio basta con ejecutar `uv sync`.

```bash
pip install iac-code
```

## Iniciar la aplicación web

Inicia el servidor desde la terminal:

```bash
iac-code web
```

De forma predeterminada, se enlaza a `127.0.0.1:8766` y abre tu navegador predeterminado en `http://127.0.0.1:8766`.

| Opción | Predeterminado | Descripción |
|---|---|---|
| `--host` | `127.0.0.1` | Host del servidor HTTP. El modo local acepta direcciones de bucle local; otras IP requieren `--access-token-file`. |
| `--port` | `8766` | Puerto del servidor HTTP. |
| `--open` / `--no-open` | `--open` | Abre el navegador al iniciar. Usa `--no-open` para desactivarlo. |
| `--debug`, `-d` | `false` | Activar los registros de depuración del servidor para esta ejecución. |
| `--access-token-file <path>` | — | Archivo de token para acceso remoto explícito con transporte de API cifrado. |

```bash
iac-code web --port 9000 --no-open
```

### Seguridad

Sin `--access-token-file`, el servidor solo acepta hosts de bucle local (`127.0.0.1`, `localhost` o `::1`) y comprueba las solicitudes locales. El modo de token explícito también acepta otras IP y protege las solicitudes de API mediante el transporte de token cifrado.

## Desplegar una plantilla ROS existente

1. Configura las [credenciales de nube](./configuration/alibaba-cloud-credentials.md) y abre una conversación en el proyecto de la plantilla.

2. Escribe `@`, selecciona el archivo y pide una revisión de la plantilla y sus parámetros necesarios.

3. Indica región, nombre de pila y valores de parámetros; solicita validación antes de crearla.

4. Revisa el resultado y los cambios previstos, y después solicita la creación de la pila.

5. Si aparece una aprobación de API de nube, comprueba la operación y aprueba o rechaza. Una creación aprobada puede generar recursos facturables.

6. Consulta el ID de pila, el progreso y el resultado. En caso de fallo, pide un diagnóstico del error en la misma conversación.

## Descripción general de la interfaz

### Barra lateral de sesiones

La barra lateral agrupa las conversaciones guardadas por proyecto. Puedes:

- Iniciar un **Nuevo chat** y elegir su proyecto debajo del cuadro de entrada.
- **Buscar** conversaciones o abrir la paleta de comandos para ejecutar un comando.
- **Fijar**, **renombrar** o **archivar** una conversación y explorar las conversaciones archivadas.

Web, escritorio y headless comparten las conversaciones guardadas. Consulte [Sesiones](./cli/sessions.md) para reanudarlas o cambiarles el nombre.

### Zona de redacción (composer)

Describa la tarea en el campo de entrada y elija el comportamiento de la conversación con sus controles:

- La selección de **modelo y proveedor** para la sesión activa.
- Un interruptor de **Pensamiento** para activar o desactivar el razonamiento extendido en los modelos compatibles.
- Un control de **modo de permisos** que determina cómo se aprueban las acciones de las herramientas.
- **Adjuntos de imagen** para modelos multimodales.
- **Habilidades** seleccionados de las sugerencias `/` o `$` y **referencias `@` a archivos** del workspace.

### Chat normal y modo canalización

Seleccione el modo al crear una conversación y el pipeline si elige el modo pipeline. Las conversaciones de la misma aplicación pueden usar modos distintos sin reiniciar el servidor.

Una sesión se ejecuta como chat normal o en modo **canalización** (pipeline). El chat normal transmite en línea las respuestas del asistente, las llamadas a herramientas y los resultados. El modo canalización añade un espacio de trabajo que muestra las líneas de tiempo de los pasos, los diagnósticos, los diagramas, el progreso del despliegue, la limpieza y los detalles de traspaso a medida que se ejecuta la canalización. Consulta [Modo canalización](./automation/pipeline-mode.md) para saber qué hacen las canalizaciones.

La [canalización `selling_solution_first`](./automation/solution-first-pipeline.md) usa este espacio de trabajo para un proceso de compra en tres etapas: comparar arquitecturas candidatas, implementar la solución elegida y desplegarla tras la confirmación. Las aprobaciones de herramientas aparecen como tarjetas de permiso localizadas bajo el paso que las solicitó; las aprobaciones pendientes vuelven al mismo paso al restaurar la sesión.

### Herramientas y aprobaciones

Las llamadas a herramientas se muestran como tarjetas dentro de la transcripción. Cuando una herramienta requiere tu aprobación, aparece una solicitud de aprobación en línea; el modo de permisos definido en la zona de redacción determina cuándo se te consulta.

### Configuración

Escritorio y Web comparten la misma interfaz. Use los ajustes y los controles visibles para el trabajo diario.

- **Credenciales de la nube** para Alibaba Cloud (consulta [Credenciales de Alibaba Cloud](./configuration/alibaba-cloud-credentials.md)).
- **Modelos** y configuración de proveedores (consulta [Proveedores de LLM](./configuration/llm-providers.md)).
- **Complementos MCP** (consulta [Integración MCP](./mcp/overview.md)).
- Inspección y gestión de la **memoria**.

### Idioma de la interfaz

La aplicación web está disponible en siete idiomas —English, 简体中文, 日本語, Français, Deutsch, Español y Português— seleccionables desde la configuración. Tu elección se guarda para las sesiones futuras.

## Relación con la CLI

Web, Desktop y headless comparten proveedores, credenciales, skills, herramientas y sesiones. Configure el modelo y el acceso a la nube una vez en Configuración para reutilizarlos entre estas entradas.
