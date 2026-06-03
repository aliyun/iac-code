---
title: Sesiones
description: Persistir y reanudar conversaciones entre ejecuciones.
---

# Sesiones

IaC Code guarda automáticamente cada conversación en disco. Puedes reanudar cualquier sesión anterior para continuar donde la dejaste.

## Reanudar sesiones

### Interactivo: `/resume`

En el REPL, usa el comando `/resume`:

```text
/resume
```

Esto abre un selector interactivo con las sesiones recientes del proyecto actual. Si la sesión tiene nombre, se muestra como título; de lo contrario se usa el último prompt o, como alternativa, el primero.

Para reanudar una sesión concreta por ID exacto, prefijo único de ID o nombre único de sesión:

```text
/resume abc123
```

### Nombrar sesiones

Usa `/rename` para dar a la sesión activa un nombre estable y legible:

```text
/rename deploy-prod
```

El nombre se guarda en los metadatos de la sesión. Aparece en el banner de bienvenida al reanudar, en la sugerencia de salida y en el selector de `/resume`.

Puedes reanudar por nombre cuando identifica una sesión de forma única:

```text
/resume deploy-prod
iac-code --resume deploy-prod
```

### CLI: `--resume` y `--continue`

Reanuda una sesión concreta desde la línea de comandos por ID exacto, prefijo único de ID o nombre único de sesión:

```bash
iac-code --resume <id-o-nombre-de-sesion>
```

Reanuda la sesión más reciente:

```bash
iac-code --continue
```

También están disponibles las opciones cortas `-r` y `-c`:

```bash
iac-code -r <id-o-nombre-de-sesion>
iac-code -c
```

### Sesiones de otros proyectos

Cuando una sesión pertenece a otro directorio de proyecto, IaC Code no cambia el directorio de trabajo en caliente. En su lugar, imprime el comando para reanudarla en el contexto correcto:

```text
cd /path/to/other/project && iac-code --resume <session-id>
```

El comando también se copia al portapapeles cuando es posible.

## Recuperación ante interrupciones

Si una sesión se interrumpió durante la ejecución, por ejemplo porque el proceso se terminó mientras una herramienta estaba en curso, IaC Code detecta las llamadas de herramienta huérfanas al reanudar y agrega resultados de error sintéticos. Esto permite que el modelo se recupere sin quedarse esperando una salida de herramienta que nunca llegará.

## Selector de sesiones

El selector de `/resume` muestra:

| Columna | Descripción |
|---------|-------------|
| Título | Nombre de sesión si existe; de lo contrario, último o primer prompt del usuario |
| Rama | Rama de Git en el momento de la sesión |
| Hora | Última hora de modificación |

Las sesiones se ordenan de más reciente a más antigua. Puedes escribir para filtrar por contenido del título.
