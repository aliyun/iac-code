---
sidebar_position: 7
title: Instalar y usar el Skill de IaC Code
description: Descarga e instala el Skill de IaC Code para que un agente externo pueda gestionar recursos de Alibaba Cloud.
---

# Instalar y usar el Skill de IaC Code

El Skill de IaC Code está diseñado para agentes externos compatibles con Skills. Una vez instalado, un agente host
puede delegar en IaC Code la planificación de arquitecturas cloud, la generación y revisión de plantillas ROS o
Terraform, la estimación de costes, la selección de recursos, las operaciones con stacks y el despliegue. El Skill
utiliza un puente escrito únicamente con la biblioteca estándar de Python para iniciar un Runtime A2A local y
autenticado. No es necesario instalar IaC Code con pip y el host no debe recurrir a comandos headless.

## Descargar el Skill

### Última versión estable

Descarga directamente la última versión estable:

[Descargar iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Esta URL fija siempre apunta al paquete del Skill publicado en el canal estable. Es adecuada para descargarlo desde el
navegador o instalarlo manualmente, y no cambia cuando se publica una nueva versión.

Los instaladores que necesiten conocer la versión, el tamaño del archivo, el resumen SHA-256 y la URL inmutable de la
versión pueden consultar los metadatos del canal estable:

[Ver latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

El documento contiene:

- `skillVersion`: versión estable actual del Skill;
- `skill.url`: URL inmutable del archivo ZIP de esa versión;
- `skill.sha256` y `skill.size`: valores para verificar la descarga;
- `manifest.url`: manifiesto de publicación inmutable de esa versión.

Para realizar una verificación estricta o una instalación automatizada reproducible, lee `latest.json`, descarga
`skill.url` y verifica `skill.sha256`. No construyas por tu cuenta una URL a partir del número de versión.

## Instalar el Skill

### Requisitos previos

- El agente host admite Skills locales definidos mediante `SKILL.md`.
- CPython 3.8–3.14 está instalado. Usa `python3` en macOS/Linux y, preferiblemente, `py -3` en Windows.
- El entorno puede acceder a las URL de OSS anteriores para descargar el ZIP del Skill y el Runtime necesario en el
  primer uso.
- La configuración del servicio de modelos está disponible. Para las tareas que consultan o gestionan recursos cloud,
  también se necesita una identidad de Alibaba Cloud con privilegios mínimos.

Las versiones oficiales del Skill Runtime son compatibles con estas plataformas:

| Sistema operativo | Arquitectura |
|---|---|
| macOS | Apple Silicon (arm64) |
| Linux | x86_64 |
| Windows | x86_64 |

Las versiones mínimas del sistema operativo y de glibc en Linux se definen en el manifiesto del Runtime fijado por el
Skill. El puente comprueba la compatibilidad antes de descargar. En una plataforma no compatible, devuelve un error en
lugar de descargar un artefacto destinado a otra plataforma o ABI.

### Extraer en el directorio de Skills del agente host

Extrae el ZIP directamente en la raíz de Skills del agente host. La ubicación exacta depende de cada producto;
consulta la documentación del agente host. La estructura final debe ser:

```text
<Raíz de Skills del agente>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

El ZIP ya contiene el directorio superior `iac-code/`. No añadas otro directorio con el mismo nombre. Después de
instalar o actualizar, reinicia el agente host o abre una sesión nueva para que vuelva a detectar el Skill.

### Verificar la instalación

En el directorio `iac-code` extraído, ejecuta este comando en macOS o Linux:

```bash
python3 scripts/iac_code.py ensure-runtime
```

En Windows PowerShell, ejecuta:

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

En la primera ejecución, el comando descarga el Runtime para la plataforma actual, verifica su tamaño y su resumen
SHA-256, y muestra un objeto JSON con `skillVersion`, `runtimeTag` y la ruta de instalación. Un Runtime verificado que
ya esté en caché se reutiliza sin volver a descargarlo.

## Configurar el modelo y la identidad de Alibaba Cloud

El Skill Runtime utiliza el mismo directorio de configuración que los demás modos de IaC Code: `~/.iac-code/` de forma
predeterminada. Si ya has configurado IaC Code mediante el REPL, la aplicación web o la aplicación Desktop, el Skill
puede reutilizar esos ajustes. Define `IAC_CODE_CONFIG_DIR` para usar otro directorio de configuración.

En entornos automatizados, proporciona estas variables mediante una solución de gestión de secretos:

| Categoría | Variable de entorno | Descripción |
|---|---|---|
| Modelo | `IAC_CODE_PROVIDER` | Proveedor del modelo |
| Modelo | `IAC_CODE_MODEL` | Nombre del modelo |
| Modelo | `IAC_CODE_API_KEY` | Clave de API del servicio de modelos |
| Modelo | `IAC_CODE_BASE_URL` | Sustitución opcional del endpoint compatible |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_ID` | ID de AccessKey |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | Secreto de AccessKey |
| Alibaba Cloud | `ALIBABA_CLOUD_SECURITY_TOKEN` | Token de seguridad para credenciales STS |
| Alibaba Cloud | `ALIBABA_CLOUD_REGION_ID` | Región predeterminada |

No incluyas nunca credenciales reales en `SKILL.md`, los prompts del agente host, los archivos del proyecto ni el
historial del shell. Da preferencia a credenciales temporales, roles RAM u OAuth, y concede solo los permisos de API
cloud necesarios para la tarea. Consulta [Proveedores de LLM](../configuration/llm-providers.md) y
[Credenciales de Alibaba Cloud](../configuration/alibaba-cloud-credentials.md) para ver las instrucciones completas.

## Primer uso

Después de instalar y configurar el Skill, abre una sesión nueva en el agente host y describe directamente una tarea
de infraestructura de Alibaba Cloud. Por ejemplo:

```text
Usa iac-code para revisar la plantilla ROS de este proyecto. Enumera los riesgos de seguridad y los cambios recomendados sin modificar el archivo.
```

Los hosts compatibles con una sintaxis explícita de Skills pueden seleccionar el Skill mediante `$iac-code`. El
agente host lee `SKILL.md`, escribe la solicitud completa en un archivo UTF-8 del espacio de trabajo y utiliza el
puente para crear y seguir una única tarea. El usuario no tiene que iniciar manualmente un servidor A2A.

Flujo previsto:

1. El puente comprueba si la configuración del modelo y de Alibaba Cloud está lista.
2. En el primer uso, descarga y verifica el Runtime de IaC Code fijado por el Skill.
3. El Runtime escucha únicamente en un puerto aleatorio de `127.0.0.1` y genera un token Bearer específico del proceso.
4. El agente host muestra el progreso, las preguntas, los planes candidatos y las solicitudes de permisos devueltos
   por IaC Code.
5. Cuando termina la tarea, el agente host devuelve el resultado final y los archivos generados en el espacio de
   trabajo.

## Actualizar y desinstalar

Para realizar una actualización manual, vuelve a descargar `skill/stable/iac-code-skill.zip` y sustituye todo el
directorio `iac-code/` de la raíz de Skills del host. Un actualizador automático puede comparar el valor
`skillVersion` de `latest.json` y, después, descargar y verificar el paquete nuevo mediante su URL inmutable y su
resumen SHA-256. Cada Skill oficial está fijado a un Runtime verificado. No sustituyas únicamente
`scripts/iac_code.py` ni modifiques manualmente la URL o el resumen del Runtime.

Para desinstalarlo, elimina `iac-code/` de la raíz de Skills del agente host. La caché del Runtime no se elimina junto
con el directorio del Skill. Ejecuta `cache list` y `cache clean` solo si el usuario solicita expresamente eliminarla.

## Caché del Runtime

El Runtime descargado durante el primer uso se almacena en
`<IAC_CODE_CONFIG_DIR o ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` y se reutiliza automáticamente. Durante el
uso normal no es necesario gestionar este directorio. Para consultar el espacio en disco utilizado o eliminar
versiones antiguas, usa:

- `python3 scripts/iac_code.py cache list` — enumera los Runtimes instalados y los paquetes candidatos;
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — elimina cachés del Runtime
  o paquetes candidatos; `--confirm` es obligatorio.

El Runtime actual y cualquier Runtime utilizado por un proceso activo están protegidos frente a la limpieza. El
formato del paquete y las restricciones del Runtime se definen en `skill-runtime/skill-package-contract.json` dentro
del repositorio de código fuente; los usuarios no necesitan modificar este archivo.

## Solución de problemas

### La configuración está incompleta

El Skill comprueba la configuración antes de crear una tarea, pero nunca lee ni devuelve valores secretos:

| Situación | Resultado |
|---|---|
| El proveedor de LLM o la clave de API están incompletos | Devuelve `llm_not_configured` y no crea la tarea |
| Las credenciales de Alibaba Cloud están incompletas para el Pipeline de venta | Devuelve `cloud_credentials_not_configured` y no crea la tarea |
| Las credenciales de Alibaba Cloud están incompletas en el modo normal | Las tareas que no llaman a API cloud pueden continuar con una advertencia previa |

### Por qué se pausa la ejecución

IaC Code se pausa cuando necesita un permiso, información adicional o la selección de un plan. El agente host muestra
directamente la solicitud:

- una solicitud de permiso para una herramienta o un despliegue (`permission`);
- una pregunta de opción múltiple o una solicitud de información (`ask_user_question`);
- la selección de un plan candidato del Pipeline (`candidate_selection`).

Antes de confirmar, revisa el recurso de destino, la región, el impacto previsto y el precio. El agente host no puede
anular una denegación de IaC Code. En el protocolo, una autorización para una sola vez se representa como `allow_once`.

> **Nota sobre la integración del agente host**
>
> Cuando un resultado del puente contiene `inputRequired`, el agente host debe mostrar la solicitud actual y esperar
> una respuesta. `boundaryReached` indica un límite de presentación o interacción, no que la tarea haya finalizado; el
> host debe mostrar la actualización y continuar siguiendo la misma tarea.

## Seguridad

- El Runtime escucha únicamente en un puerto aleatorio de `127.0.0.1`. Cada inicio genera un token Bearer nuevo y cada
  solicitud del puente incluye ese token.
- El puente conserva los artefactos y resultados en el espacio de trabajo de la tarea. Los resultados se escriben en
  `.iac-code-skill-results/`.
- Los campos que se muestran durante las comprobaciones previas y las solicitudes de permisos se depuran; no contienen
  secretos ni credenciales.

## Documentación relacionada

- [Descripción general del protocolo A2A](./overview.md)
- [Referencia del protocolo A2A](./protocol-reference.md)
- [Proveedores de LLM](../configuration/llm-providers.md)
- [Credenciales de Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
- [Configuración del Runtime](../configuration/runtime-configuration.md)
