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
| `/model [model_name]` | Muestra o cambia el modelo activo. Con `model_name`, cambia directamente a ese modelo para el proveedor activo. Sin argumento, abre un selector interactivo de modelos cuando hay un proveedor configurado, o imprime el modelo actual cuando no hay interfaz de consola disponible. |
| `/resume [conversation id or search term]` | Reanuda una sesion anterior. Con un argumento, IaC Code lo resuelve como un ID de sesion o un prefijo de ID unico. Sin argumento, abre el selector interactivo de sesiones. Las sesiones de otros proyectos imprimen un comando `cd ... && iac-code --resume <id>` en lugar de intercambiar el proyecto actual. |

La lista exacta de comandos puede cambiar entre versiones. Usa `/help` o escribe `/` en el REPL para inspeccionar los comandos disponibles en tu version instalada.
