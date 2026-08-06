---
title: Aplicación de escritorio
description: Instala y utiliza la aplicación nativa de IaC Code en macOS, Windows y Linux.
---

# Aplicación de escritorio

La aplicación de escritorio ofrece el mismo agente, proveedores, integraciones con la nube, proyectos y conversaciones que la CLI y la aplicación web, pero como una aplicación nativa instalada. El anfitrión Tauri inicia el entorno de Python incluido y carga la interfaz local de IaC Code mediante una conexión de bucle invertido; no expone ningún servicio web público.

## Paquetes compatibles

Descarga el paquete de tu plataforma desde [GitHub Releases](https://github.com/aliyun/iac-code/releases).

| Sistema operativo | Arquitectura | Paquete | Método de actualización |
|---|---|---|---|
| macOS | Apple Silicon | `.dmg` | Actualización desde la aplicación |
| Windows | x64 | instalador `.exe` | Actualización desde la aplicación |
| Linux | x64 | `.AppImage` | Actualización desde la aplicación |
| Debian / Ubuntu | x64 | `.deb` | Instalación de un paquete más reciente |

Cada versión también incluye `SHA256SUMS`, una lista de materiales de software (SBOM) y los avisos de terceros.

## Instalación

### macOS

1. Descarga y abre el `.dmg`, y arrastra **IaC Code** a **Aplicaciones**.
2. Abre IaC Code desde Aplicaciones.
3. El paquete actual aún no tiene una firma de Apple Developer ID ni está notarizado, por lo que macOS puede bloquear el primer inicio. Después de comprobar la suma de verificación, haz clic en la aplicación mientras mantienes pulsada la tecla Control y elige **Abrir**, o autorízala en **Ajustes del Sistema > Privacidad y seguridad**.

### Windows

1. Descarga y ejecuta el instalador `.exe`. IaC Code se instala para el usuario actual y crea accesos directos.
2. Si Microsoft Defender SmartScreen indica que el editor es desconocido, comprueba `SHA256SUMS`, selecciona **Más información** y continúa únicamente si la suma coincide con la publicada.
3. El paquete incluye el soporte de arranque de WebView2 que necesita la interfaz. En el primer inicio, IaC Code también comprueba si Git Bash está instalado y, si falta, muestra una guía para instalarlo.

### Linux AppImage

Concede permiso de ejecución al archivo descargado y ejecútalo:

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

Después del primer inicio, tu entorno de escritorio puede ofrecerte la posibilidad de crear un lanzador. AppImage puede actualizarse por sí misma cuando haya una actualización firmada.

### Debian o Ubuntu

Instala el paquete deb con APT para que el sistema resuelva las dependencias:

```bash
sudo apt install ./iac-code_*_amd64.deb
```

Inicia **IaC Code** desde el menú de aplicaciones. La instalación deb no utiliza el actualizador integrado; para actualizar, descarga e instala el nuevo paquete deb.

## Primer inicio

La primera vez que se ejecuta, IaC Code solicita una carpeta de proyecto. Esa carpeta pasa a ser el espacio de trabajo para acceder a archivos, generar plantillas, ejecutar herramientas y guardar conversaciones. Más adelante puedes cambiar de proyecto con el selector correspondiente.

Si ya has utilizado la CLI o la aplicación web, la aplicación de escritorio reutiliza la configuración de `~/.iac-code/` (o `IAC_CODE_CONFIG_DIR`), incluidos los proveedores de modelos, las credenciales de Alibaba Cloud, los ajustes y las sesiones guardadas. Si aún no existe esa configuración, abre **Ajustes** y añade un proveedor de modelos y las credenciales de nube antes de iniciar una tarea.

La interfaz está disponible en inglés, chino simplificado, japonés, francés, alemán, español y portugués. Puedes cambiar el idioma y el tema de color en **Ajustes > General**.

## Actualizaciones y firmas de los paquetes

Las versiones para macOS, Windows y AppImage consultan periódicamente la información de la versión estable y pueden descargar e instalar una versión nueva. Antes de instalarla, cada actualización se verifica con la clave pública de actualización de IaC Code. El paquete deb sigue el procedimiento habitual de paquetes de Linux.

La firma de una actualización no equivale a la firma del editor que comprueba el sistema operativo. La primera confirma que IaC Code produjo la actualización; la notarización de macOS y la firma de código de Windows identifican al editor ante el sistema. Los instaladores actuales aún no tienen una firma comercial de editor, por lo que los avisos del sistema son previsibles. Descarga siempre los paquetes desde la página oficial y verifica `SHA256SUMS`.

## Solución de problemas

- **La aplicación permanece en la pantalla de inicio:** utiliza las opciones de recuperación para reintentar el inicio o abrir la carpeta de diagnósticos. El registro permite identificar archivos del entorno que falten, un puerto de bucle invertido ocupado o un fallo al iniciar el proceso auxiliar.
- **Windows indica que falta Git Bash:** sigue la guía de instalación, reinicia IaC Code y vuelve a ejecutar la comprobación. Las herramientas del agente basadas en shell necesitan Git Bash en Windows.
- **Linux abre el deb como si fuera un archivo comprimido:** instálalo con el comando APT anterior en lugar de abrirlo con el gestor de archivos comprimidos.
- **No se abre una pila o un enlace externo en Linux:** configura un navegador predeterminado para la sesión de escritorio y vuelve a intentarlo.
- **Los ajustes o las sesiones no se comparten con la CLI:** comprueba que ambas aplicaciones utilizan el mismo valor de `IAC_CODE_CONFIG_DIR` y se ejecutan con el mismo usuario del sistema.

Para la instalación y los comandos de la CLI, consulta [Instalación](./getting-started/installation.md) y [Uso de la CLI](./cli/usage.md). Para la interfaz en el navegador, consulta la [Aplicación web](./web-app.md).
