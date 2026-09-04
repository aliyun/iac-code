---
sidebar_position: 1
title: Vision general
description: Que hace IaC Code y por donde empezar.
---

# Vision general

IaC Code es un asistente de IA para diseñar, generar, desplegar y gestionar infraestructura cloud. Puedes usarlo desde la aplicación Desktop, la aplicación Web local, el terminal interactivo, interfaces de automatización o como Skill de otro agente. Su arquitectura está diseñada para flujos multicloud; la versión actual admite Alibaba Cloud ROS y Terraform.

Capacidades principales:

- **Dilo y despliegalo** — describe lo que necesitas en lenguaje natural y obtendras plantillas de ROS validadas y listas para desplegar, o plantillas de Terraform generadas.
- **De la plantilla a produccion** — para Alibaba Cloud ROS, pasa de la plantilla a la infraestructura en ejecucion: crea, actualiza, elimina y monitorea stacks en distintas regiones. El soporte de Terraform cubre la generacion y conversion de plantillas, no el despliegue.
- **Inteligencia de nube integrada** — consulta documentacion, verifica la disponibilidad de recursos y estima costos antes de desplegar; cada decision respaldada por datos reales de la nube.

Elige el punto de entrada que corresponda:

- Descarga la [aplicación Desktop](./desktop-app.md) para una interfaz gráfica lista para usar.
- Sigue la [instalación](./getting-started/installation.md) y el [inicio rápido](./getting-started/quick-start.md) para usar REPL, modo headless o la [aplicación Web](./web-app.md) local.
- Elige una distribución en la [visión general de los Skills oficiales de IaC Code](./a2a/skill-overview.md) para añadir sus capacidades de Alibaba Cloud a un agente compatible.
- Usa [ACP](./acp/overview.md), [A2A](./a2a/overview.md) o [AG-UI](./agui/overview.md) para integrarlo en una aplicación o servicio.

Todos los puntos de entrada requieren un modelo configurado. Configura también las [credenciales de Alibaba Cloud](./configuration/alibaba-cloud-credentials.md) para consultar, modificar o desplegar recursos.
