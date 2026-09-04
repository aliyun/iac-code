---
sidebar_position: 1
title: Visión general de los Skills oficiales de IaC Code
description: Compara los Skills oficiales de IaC Code y elige la distribución adecuada.
---

# Visión general de los Skills oficiales de IaC Code

IaC Code está disponible en tres distribuciones oficiales de Skill. Todas permiten gestionar infraestructura de
Alibaba Cloud desde un agente, pero difieren en el canal de distribución y en dónde se ejecuta el Agent de IaC Code.

## Elegir un Skill

| Skill | Dónde se ejecuta | Cuándo elegirlo |
|---|---|---|
| `iac-code` | Runtime verificado de IaC Code descargado en tu equipo | Quieres el paquete independiente del proyecto iac-code y controlar directamente instalación y actualizaciones. |
| `alibabacloud-iac-code` | El mismo Runtime local, empaquetado para el portal Alibaba Cloud Agent Skills | Gestionas Alibaba Cloud Skills mediante el portal o `npx skills`. |
| `alibabacloud-ros-agent` | Agent ROS alojado por Alibaba Cloud, llamado con la API ROS StartChat | Quieres una conversación remota sin descargar el Runtime local de IaC Code. |

`iac-code` y `alibabacloud-iac-code` ofrecen la misma capacidad. Elige una distribución por ámbito del agente;
instalar ambas añade activadores solapados, no funciones nuevas.

`alibabacloud-ros-agent` es una integración remota independiente. Puede convivir con una distribución local si el
usuario debe elegir explícitamente entre IaC Code local y el Agent ROS alojado.

## Obtener el Skill independiente

[Descargar iac-code-skill.zip estable](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Esta distribución es adecuada para una instalación gestionada manualmente. Descarga el Runtime en el primer uso y
reutiliza la configuración del modelo y Alibaba Cloud de `~/.iac-code/`. Consulta
[Instalar y usar el Skill de IaC Code](./skill-integration.md).

## Obtener los Skills del portal de Alibaba Cloud

Busca los nombres exactos en el [portal Alibaba Cloud Agent Skills](https://skills.aliyun.com/) o instala desde el
repositorio oficial:

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

Descargas directas:

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) · [código fuente](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) · [código fuente](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

`npx skills` requiere Node.js 18 o posterior y permite elegir interactivamente el agente y el ámbito. Si descargas un
ZIP, extrae su directorio Skill superior en el directorio de usuario o proyecto admitido por el agente.

## Diferencias de capacidad y configuración

Las dos distribuciones locales admiten conversaciones normales y Pipeline, arquitectura, plantillas ROS/Terraform,
costes, stacks, despliegue y confirmaciones. Requieren un modelo configurado y credenciales de Alibaba Cloud cuando la
tarea consulta o modifica recursos.

`alibabacloud-ros-agent` usa `ros:StartChat` para conectar con el Agent ROS de Alibaba Cloud. No requiere Runtime local
ni proveedor de modelo local, pero usa la identidad de Alibaba Cloud del host. Concede solo los permisos RAM necesarios;
una cancelación remota explícita también usa `ros:StopChat`.

En todos los casos, revisa recursos, región, impacto, precio y permisos antes de aprobar. No guardes credenciales en
`SKILL.md`, prompts ni archivos del proyecto.

## Documentación relacionada

- [Instalar y usar el Skill de IaC Code](./skill-integration.md)
- [Referencia de integración para hosts](./skill-host-integration.md)
- [Credenciales de Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
