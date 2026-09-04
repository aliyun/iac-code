---
sidebar_position: 2
title: Instalar y usar el Skill de IaC Code
description: Añade IaC Code a un agente compatible con Skills para gestionar infraestructura de Alibaba Cloud.
---

# Instalar y usar el Skill de IaC Code

El Skill de IaC Code permite que un agente compatible delegue en IaC Code el diseño de arquitecturas cloud, la
generación o revisión de plantillas ROS y Terraform, la estimación de costes, la selección de recursos, las operaciones
con stacks ROS y los despliegues. El paquete incluye un Runtime de IaC Code verificado, por lo que no necesitas instalar
IaC Code por separado.

## Descargar

[Descargar el último iac-code-skill.zip estable](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Esta URL fija siempre apunta a la versión estable más reciente. Los instaladores automáticos pueden leer
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)
para obtener la versión, URL inmutable, tamaño y SHA-256, y verificar `skill.url` con `skill.sha256`.

## Instalar

Comprueba que el agente admite Skills locales definidos por `SKILL.md`, que dispone de CPython 3.8 a 3.14 y que puede
acceder a la descarga durante el primer uso. Utiliza `python3` en macOS/Linux y `py -3` en Windows. Los Runtimes
oficiales admiten macOS Apple Silicon, Linux x86_64 y Windows x86_64; el sistema y la ABI se comprueban antes de la
descarga.

Extrae el ZIP en el directorio de Skills indicado por el agente. El archivo ya contiene `iac-code/`:

```text
<Raíz de Skills del agente>/
└── iac-code/
    ├── SKILL.md
    ├── agents/openai.yaml
    └── scripts/iac_code.py
```

Ubicaciones habituales:

- **Codex**: `~/.agents/skills/iac-code/` para todos los proyectos o
  `<repositorio>/.agents/skills/iac-code/` para uno. Consulta la
  [documentación de Codex Skills](https://developers.openai.com/codex/skills#where-codex-loads-local-skills).
- **Claude Code**: `~/.claude/skills/iac-code/` para todos los proyectos o
  `<repositorio>/.claude/skills/iac-code/` para uno. Consulta la
  [documentación de Claude Code Skills](https://code.claude.com/docs/en/skills#where-skills-live).

Reinicia el agente o abre una sesión nueva. Para comprobar el Runtime desde el directorio `iac-code`:

```bash
python3 scripts/iac_code.py ensure-runtime
```

En Windows PowerShell utiliza `py -3 scripts\iac_code.py ensure-runtime`. La primera ejecución descarga el Runtime
adecuado y verifica su tamaño y SHA-256; las siguientes tareas reutilizan la copia local verificada.

## Configurar el modelo y la identidad de Alibaba Cloud

El Skill usa `~/.iac-code/` de forma predeterminada y reutiliza los ajustes del REPL, la aplicación Web o Desktop.
Puedes elegir otro directorio con `IAC_CODE_CONFIG_DIR`. En entornos automatizados, inyecta la configuración del modelo
y las credenciales de Alibaba Cloud desde un gestor de secretos. No las escribas en `SKILL.md`, prompts, archivos del
proyecto ni el historial del shell. Prefiere credenciales temporales, roles RAM u OAuth con permisos mínimos. Consulta
[Proveedores LLM](../configuration/llm-providers.md) y
[Credenciales de Alibaba Cloud](../configuration/alibaba-cloud-credentials.md).

## Elegir el modo de trabajo

- El **modo normal** es el predeterminado para consultar o cambiar recursos, trabajar con plantillas, resolver
  problemas y desplegar un objetivo claro.
- El **modo Pipeline** se usa cuando lo solicitas o cuando necesitas un flujo guiado con arquitecturas candidatas,
  comparación de costes, confirmación y despliegue.

Normalmente basta con describir el resultado. Menciona Pipeline solo si quieres comparar soluciones.

## Primer uso

Abre una sesión nueva en el agente host y escribe, por ejemplo:

```text
Usa iac-code para revisar la plantilla ROS de este proyecto. Enumera los riesgos de seguridad y las mejoras sin modificar el archivo.
```

Selecciona el Skill explícitamente con `$iac-code` en Codex o `/iac-code` en Claude Code. La comprobación de configuración y el
inicio del Runtime son automáticos; no necesitas iniciar un A2A Server manualmente. IaC Code puede pausar para pedirte:

- aprobar o rechazar una operación (`permission`);
- responder una pregunta (`ask_user_question`);
- elegir una arquitectura (`candidate_selection`);
- revisar la solución, precio y parámetros, y confirmar, ajustar, volver a seleccionar o cancelar
  (`deployment_confirmation`).

Revisa los recursos, región, impacto y precio antes de responder. La petición inicial de desplegar no aprueba por
adelantado la confirmación posterior. Al terminar, continúa en la misma sesión para conservar el contexto. El progreso
y las preguntas están disponibles en inglés, chino simplificado, español, francés, alemán, japonés y portugués.

## Actualizar y desinstalar

Para actualizar, descarga de nuevo el ZIP estable, reemplaza todo `iac-code/` y reinicia el agente. No sustituyas solo
el puente ni edites la URL o el hash del Runtime. Para desinstalar, elimina `iac-code/`. Los Runtimes quedan en caché;
si también quieres borrarlos, consulta `cache list` y después ejecuta `cache clean ... --confirm`.

## Solución de problemas

- `llm_not_configured`: completa la configuración del modelo.
- `cloud_credentials_not_configured`: configura las credenciales que requiere Pipeline. El modo normal puede continuar
  tareas sin API cloud mostrando una advertencia.
- `incompatible_host`: ejecuta `ensure-runtime` y comprueba Python, sistema, arquitectura, red y proxy. Actualiza o
  cambia el host en vez de omitir la comprobación.
- Tarea en pausa: está esperando una respuesta, permiso, selección o confirmación. Si la sesión sigue disponible tras
  una interrupción, pide continuar la misma tarea.

Usa `python3 scripts/iac_code.py cache list` para consultar la caché,
`cache clean --runtime-tag <tag> --confirm` para eliminar una versión anterior y
`cache clean --candidates --confirm` para paquetes candidatos. El Runtime actual o activo está protegido.

## Seguridad

- El Runtime solo escucha en un puerto aleatorio de `127.0.0.1` y usa un Bearer token nuevo por proceso.
- Los resultados permanecen en el workspace, bajo `.iac-code-skill-results/` cuando corresponde.
- Los estados de preparación y resúmenes de permisos no contienen valores de credenciales.

## Documentación relacionada

- [Visión general de los Skills oficiales de IaC Code](./skill-overview.md)
- [Referencia de integración del Skill de IaC Code para hosts](./skill-host-integration.md)
- [Visión general de A2A](./overview.md)
- [Configuración del Runtime](../configuration/runtime-configuration.md)
