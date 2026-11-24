---
sidebar_position: 1
title: Protocolo ACP
description: Vision general del soporte del Protocolo de Cliente de Agente en iac-code.
---

# Protocolo ACP

## Que es ACP

[Agent Client Protocol (ACP)](https://agentclientprotocol.com/get-started/introduction) es un protocolo de comunicacion estandarizado entre agentes de IA y sus clientes. Define como los clientes (IDEs, editores, herramientas de automatizacion) inician, interactuan y gestionan sesiones de agentes a traves de mensajes JSON-RPC estructurados.

## iac-code como servidor ACP

iac-code expone sus capacidades de Infraestructura como Codigo a traves de un servidor ACP. Cualquier cliente compatible con ACP puede lanzar `iac-code acp` como un subproceso (o conectarse a traves de HTTP+SSE) y programaticamente:

- Crear sesiones vinculadas a un directorio de proyecto
- Enviar prompts en lenguaje natural y recibir respuestas en streaming
- Aprobar o rechazar operaciones de escritura de archivos y operaciones destructivas
- Gestionar multiples sesiones concurrentes

Así, iac-code funciona como un **backend componible** para los entornos de desarrollo.

## Casos de uso

- **Integracion con IDE / Editor** — Zed, VS Code u otros editores pueden integrar iac-code como un servidor de contexto para proporcionar generacion de IaC en linea.
- **Orquestacion agente a agente** — Otros agentes de IA pueden invocar las capacidades de IaC de iac-code a traves del protocolo, habilitando flujos de trabajo multi-agente.
- **Pipelines de automatizacion** — Scripts de CI/CD o bots de chatops pueden invocar iac-code sin interfaz para generar y validar plantillas.

## Comparacion de modos de interaccion

| Modo | Comando | Ideal para |
|------|---------|----------|
| **Web** | `iac-code` | Exploracion practica, creacion iterativa de plantillas |
| **CLI no interactivo** | `iac-code --prompt "..."` | Scripting, generacion de un solo uso, pipelines de CI |
| **Servidor ACP** | `iac-code acp` | Integracion con IDE, gestion multi-sesion, acceso programatico |

ACP ofrece integración de clientes por sesión con eventos estructurados en streaming. Web, A2A y el modo de proceso SDK tienen sus propias interfaces de sesión y streaming.

## Capacidades principales

- **Gestion multi-sesion** — Crear, listar, bifurcar, reanudar y cerrar sesiones independientes, cada una con su propio historial de conversacion y directorio de trabajo.
- **Respuestas en streaming** — Eventos en tiempo real para texto del agente, razonamiento, llamadas a herramientas, progreso de herramientas y finalizacion.
- **Marco de permisos** — Las herramientas de solo lectura se permiten automaticamente; las herramientas de escritura y destructivas requieren aprobacion explicita del cliente antes de la ejecucion.
- **Transporte dual** — Stdio para uso local/subproceso, HTTP+SSE para escenarios remotos y de red.
- **Paso de configuracion de servidores MCP** — Los clientes pueden declarar servidores MCP en la creacion de sesiones para aumentar las herramientas.
- **Soporte de comandos slash** — Reenviar comandos slash (`/compact`, `/clear`, `/debug`, etc.) a traves del protocolo.
- **Metricas de tiempo de ejecucion** — Estadisticas de uso de tokens, latencia y llamadas a herramientas a nivel de sesion.
