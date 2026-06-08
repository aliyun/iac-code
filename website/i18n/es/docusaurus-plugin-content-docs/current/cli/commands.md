---
title: Comandos slash
description: Referencia completa de los comandos interactivos integrados.
---

# Comandos slash

Los comandos slash controlan IaC Code desde dentro de una sesion interactiva. Escribe `/` para ver los comandos disponibles y luego sigue escribiendo para filtrar la lista. Un comando solo se reconoce cuando aparece al inicio de tu mensaje.

La lista `/` incluye tanto comandos integrados como skills que tengas configuradas. Para restringir las sugerencias solo a skills, usa `$` en su lugar — `$<nombre>` lista e invoca skills exclusivamente, y escribir `$` seguido del nombre de un comando integrado (por ejemplo `$help`) imprime un error apuntando al equivalente `/`.

El texto despues del nombre del comando se pasa como argumentos. En la tabla siguiente, `<arg>` indica un argumento obligatorio y `[arg]` indica un argumento opcional.

| Comando | Proposito |
|---|---|
| `/auth` | Configura el acceso al proveedor de modelos y las credenciales de Alibaba Cloud a traves del flujo de autenticacion interactivo. Usalo al configurar IaC Code por primera vez, al cambiar claves API, al cambiar de proveedor o al actualizar el acceso a la nube. Alias: `/login`. |
| `/clear` | Limpia el historial de conversacion actual y restablece el administrador de contexto activo. En modo interactivo, tambien limpia la pantalla de la terminal y vuelve a renderizar el banner de bienvenida. Usalo cuando quieras iniciar una nueva solicitud sin salir del REPL. |
| `/compact` | Resume la conversacion actual para reducir el uso de contexto, preservando los turnos recientes. Usalo despues de una sesion larga cuando quieras continuar trabajando con menos contexto acumulado. Si la conversacion esta vacia o es demasiado corta, el comando informa que no hay nada que compactar. |
| `/debug [on\|off\|status]` | Inspecciona o cambia el registro de depuracion en tiempo de ejecucion para la sesion activa. `/debug` y `/debug status` muestran si el registro esta habilitado y, cuando lo esta, la ruta del archivo de registro. `/debug on` habilita el registro para la sesion actual. `/debug off` lo deshabilita. |
| `/effort [level]` | Muestra o cambia el esfuerzo de pensamiento para el modelo activo cuando el modelo seleccionado admite control de esfuerzo. Con un nivel, aplica el valor solicitado si es valido para el modelo. Sin un nivel, abre un selector interactivo en el REPL, o imprime el esfuerzo actual en contextos no interactivos. |
| `/exit` | Sale del REPL interactivo. Alias: `/quit`, `/q`. |
| `/help` | Muestra los comandos disponibles y los atajos de teclado comunes dentro del REPL. Alias: `/?`. |
| `/memory` | Abre el selector de memoria. Edita los archivos `AGENTS.md` de proyecto o usuario, activa o desactiva auto-memory y abre la carpeta de auto-memory del proyecto cuando auto-memory está activada. |
| `/model [model_name]` | Muestra o cambia el modelo activo. Con `model_name`, cambia directamente a ese modelo para el proveedor activo. Sin argumento, abre un selector interactivo de modelos cuando hay un proveedor configurado, o imprime el modelo actual cuando no hay interfaz de consola disponible. |
| `/rename <nombre>` | Nombrar la sesión actual. Los nombres aparecen en el banner de bienvenida, en la sugerencia de salida y en el selector de `/resume`, y pueden usarse con `/resume` o `--resume` cuando identifican una sesión de forma única. |
| `/resume [id-de-sesion\|prefijo-unico-de-id\|nombre-unico-de-sesion]` | Reanudar una sesión anterior. Con un argumento, IaC Code lo resuelve como ID exacto, prefijo único de ID o nombre único de sesión. Sin argumento, abre el selector interactivo de sesiones. Las sesiones de otros proyectos imprimen un comando `cd ... && iac-code --resume <id>` en lugar de cambiar en caliente el proyecto actual. |
| `/skills` | Abrir el selector de gestión de habilidades. Busca habilidades por nombre o descripción, ordena por nombre/origen/tamaño y activa o desactiva habilidades de usuario o de proyecto. Las habilidades incluidas permanecen bloqueadas y activadas. |
| `/status` | Mostrar el ID de sesión actual, proveedor, modelo, región de Alibaba Cloud, directorio de trabajo, uso registrado de tokens de API, número de turnos y utilización del contexto. En modo de depuración también muestra los recuentos de side calls de memoria y su uso de tokens. |

La lista exacta de comandos puede cambiar entre versiones. Usa `/help` o escribe `/` en el REPL para inspeccionar los comandos disponibles en tu version instalada.

## Memoria

Usa `/memory` para editar los archivos de memoria que IaC Code carga en la conversación:

- La memoria del proyecto se guarda en `AGENTS.md` en la raíz del proyecto de forma predeterminada.
- La memoria de usuario se guarda en `AGENTS.md` dentro del directorio de configuración en tiempo de ejecución, `~/.iac-code/` de forma predeterminada.
- Defina `IAC_CODE_INSTRUCTION_MEMORY_FILE` para usar otro nombre de archivo, por ejemplo `IAC-CODE.md`.
- El editor es un editor compacto de pantalla completa similar a Vim. Usa `i`, `a` u `o` para entrar en modo de inserción, `Esc` para volver al modo normal, `:wq` para guardar y `:q!` para descartar.
- La fila `Auto-memory` se puede alternar con `Enter`. Cuando auto-memory está activada, IaC Code puede recuperar memorias de temas del proyecto como contexto de conversación oculto.
- La opción de carpeta de auto-memory solo aparece cuando auto-memory está activada.
