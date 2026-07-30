---
title: Aplicación web
description: Ejecuta IaC Code como una aplicación web local con el mismo motor que la CLI.
---

# Aplicación web

IaC Code incluye una aplicación web local que ejecuta el mismo motor de agente que la terminal, presentado en un navegador en lugar de en un REPL. Resulta útil cuando prefieres una interfaz de chat gráfica, cuando quieres gestionar varias conversaciones en paralelo o cuando necesitas seguir el progreso de una canalización y la actividad de las herramientas en un diseño más completo.

La aplicación web lee y escribe en el mismo almacén de sesiones que la CLI, de modo que una conversación iniciada en un lado puede reanudarse en el otro.

## Instalación

La aplicación web es una función opcional que depende del extra `http` (Starlette y Uvicorn). Instálalo junto con el paquete base:

```bash
pip install 'iac-code[http]'
```

Si ejecutas `iac-code web` sin el extra, el comando falla con un mensaje que te pide instalar `iac-code[http]`. Cuando trabajas desde un clon del repositorio, `uv sync --extra http` instala las mismas dependencias.

## Iniciar la aplicación web

Inicia el servidor desde la terminal:

```bash
iac-code web
```

De forma predeterminada, se enlaza a `127.0.0.1:8766` y abre tu navegador predeterminado en `http://127.0.0.1:8766`.

| Opción | Predeterminado | Descripción |
|---|---|---|
| `--host` | `127.0.0.1` | Host del servidor HTTP. Solo se aceptan direcciones de bucle invertido (loopback). |
| `--port` | `8766` | Puerto del servidor HTTP. |
| `--open` / `--no-open` | `--open` | Abre el navegador al iniciar. Usa `--no-open` para desactivarlo. |

```bash
iac-code web --port 9000 --no-open
```

### Seguridad

El servidor web solo se enlaza a interfaces de bucle invertido (`127.0.0.1`, `localhost` o `::1`). Está pensado para usarse en tu propia máquina y rechaza las direcciones de enlace públicas. No lo expongas directamente en una red; colócalo detrás de tu propio proxy autenticado si necesitas acceso remoto.

## Descripción general de la interfaz

### Barra lateral de sesiones

La barra lateral enumera las conversaciones del proyecto seleccionado. Desde aquí puedes:

- Iniciar un **nuevo chat** o cambiar de proyecto con el selector de proyectos.
- **Buscar** conversaciones o abrir la paleta de comandos para ejecutar un comando.
- **Fijar**, **renombrar** o **archivar** una conversación y explorar las conversaciones archivadas.

Como las sesiones se comparten con la CLI, una conversación que reanudes con `iac-code --resume` también aparece aquí. Consulta [Sesiones](./cli/sessions.md) para entender cómo funciona el almacén de sesiones.

### Zona de redacción (composer)

La zona de redacción es donde escribes tus solicitudes. Ofrece los mismos controles que la CLI expone mediante comandos de barra y opciones:

- La selección de **modelo y proveedor** para la sesión activa.
- Un interruptor de **Pensamiento** para activar o desactivar el razonamiento extendido en los modelos compatibles.
- Un control de **modo de permisos** que determina cómo se aprueban las acciones de las herramientas.
- **Adjuntos de imagen** para modelos multimodales.
- **Comandos de barra** (escritos con `/`) y **referencias de archivo `@`** para señalar archivos de tu espacio de trabajo.

### Chat normal y modo canalización

Una sesión se ejecuta como chat normal o en modo **canalización** (pipeline). El chat normal transmite en línea las respuestas del asistente, las llamadas a herramientas y los resultados. El modo canalización añade un espacio de trabajo que muestra las líneas de tiempo de los pasos, los diagnósticos, los diagramas, el progreso del despliegue, la limpieza y los detalles de traspaso a medida que se ejecuta la canalización. Consulta [Modo canalización](./automation/pipeline-mode.md) para saber qué hacen las canalizaciones.

### Herramientas y aprobaciones

Las llamadas a herramientas se muestran como tarjetas dentro de la transcripción. Cuando una herramienta requiere tu aprobación, aparece una solicitud de aprobación en línea; el modo de permisos definido en la zona de redacción determina cuándo se te consulta.

### Configuración

La zona de configuración reúne la misma configuración que gestiona la CLI:

- **Credenciales de la nube** para Alibaba Cloud (consulta [Credenciales de Alibaba Cloud](./configuration/alibaba-cloud-credentials.md)).
- **Modelos** y configuración de proveedores (consulta [Proveedores de LLM](./configuration/llm-providers.md)).
- **Complementos MCP** (consulta [Integración MCP](./mcp/overview.md)).
- Inspección y gestión de la **memoria**.

### Idioma de la interfaz

La aplicación web está disponible en siete idiomas —English, 简体中文, 日本語, Français, Deutsch, Español y Português— seleccionables desde la configuración. Tu elección se guarda para las sesiones futuras.

## Relación con la CLI

La aplicación web es una interfaz alternativa, no un producto independiente. Utiliza los mismos proveedores, credenciales, habilidades, herramientas y almacenamiento de sesiones que la terminal. Configura los proveedores y las credenciales una sola vez con `/auth` en la CLI, o mediante la configuración de la aplicación web, y ambas interfaces las compartirán.
