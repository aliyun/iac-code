---
sidebar_position: 1
title: Protocolo A2A
description: Descripción general del soporte de Agent2Agent en iac-code.
---

# Protocolo A2A

## Qué es A2A

[Agent2Agent (A2A)](https://github.com/a2aproject/A2A) es un protocolo para descubrir y llamar agentes remotos. Permite que un agente publique una Agent Card, acepte mensajes estructurados, transmita actualizaciones de tareas y exponga operaciones de cancelación y consulta de tareas mediante transportes estándar.

## iac-code como servidor A2A

iac-code puede ejecutarse como un servidor / agente A2A 1.0. Otros clientes compatibles con A2A pueden descubrirlo, enviar solicitudes de infraestructura como código, transmitir actualizaciones de ejecución y cancelar tareas activas.

Usa A2A cuando otro agente, motor de flujos de trabajo o servicio necesite llamar a iac-code como especialista de IaC interoperable. Usa ACP cuando un cliente de estilo editor necesite gestión de sesiones, solicitudes de permisos e integración con el desarrollo local.

## Casos de uso

- **Orquestación de agentes** — Un agente planificador puede delegar trabajo de Alibaba Cloud ROS o Terraform a iac-code.
- **Automatización de flujos de trabajo** — Las herramientas internas pueden enviar tareas de generación, revisión o conversión de IaC por HTTP.
- **Descubrimiento de servicios** — Los clientes pueden obtener la Agent Card y elegir capacidades como generación de IaC o revisión de plantillas.
- **Integraciones con streaming** — Un cliente de chatops o panel puede mostrar texto del modelo, actividad de herramientas, metadatos de uso y el estado final de la tarea mientras se ejecuta el turno.

## Comparación de modos de interacción

| Modo | Comando | Ideal para |
|------|---------|------------|
| **REPL interactivo** | `iac-code` | Exploración práctica y creación iterativa de plantillas |
| **CLI no interactiva** | `iac-code --prompt "..."` o `--headless` | Scripts de una sola ejecución y trabajos de CI |
| **Servidor ACP** | `iac-code acp` | Integración con IDE/editor y control de clientes multisesión |
| **Servidor A2A** | `iac-code a2a` | Interoperabilidad agente a agente sobre transportes A2A |
| **Cliente A2A** | `iac-code a2a-client call` | Llamar a agentes A2A remotos desde iac-code |

## Capacidades principales

- **Descubrimiento de Agent Card** — Publica `/.well-known/agent-card.json` con enlace de protocolo, versión, skills, modos de entrada/salida y metadatos opcionales de autenticación.
- **HTTP JSON-RPC y REST** — Sirve solicitudes A2A JSON-RPC en `/` y registra las rutas REST del SDK.
- **Respuestas en streaming** — Soporta `SendStreamingMessage` para actualizaciones incrementales de tareas.
- **Gestión de tareas** — Soporta consulta de tareas, listado autenticado de tareas con paginación por cursor, cancelación de tareas activas y suscripción a tareas activas.
- **Reutilización de contexto** — Reutiliza un runtime de iac-code para mensajes de seguimiento en el mismo `contextId` de A2A.
- **Ámbito del espacio de trabajo** — Lee el directorio del proyecto desde los metadatos del mensaje en `iac_code.cwd`.
- **Metadatos de herramientas** — Emite metadatos específicos de iac-code para inicios de herramientas, deltas de entrada, resultados completados de herramientas, decisiones de permisos y uso de tokens.
- **Partes de entrada** — Acepta partes similares a texto, partes de datos JSON, texto UTF-8 sin procesar, archivos de texto locales `file://` del espacio de trabajo y adjuntos multimodales acotados representados como manifiestos de prompt.
- **Llamadas de cliente** — Descubre Agent Cards remotas, verifica firmas cuando está configurado y envía prompts de texto a agentes remotos.
- **Enrutamiento** — Selecciona agentes remotos configurados por nombre explícito, skill o coincidencia de prompt/etiqueta.
- **Metadatos de persistencia** — Refleja instantáneas locales de tareas/contextos A2A en archivos JSON como metadatos de restauración entre procesos.
- **Artefactos** — Almacena payloads de artefactos de texto locales soportados fuera del cuerpo del evento transmitido, emite eventos estándar `TaskArtifactUpdateEvent` y registra los `artifacts` de la tarea.
- **Extensiones y caché** — Anuncia la extensión opcional de metadatos de artefactos de iac-code, valida `A2A-Extensions` requeridas y sirve Agent Cards con encabezados de caché.
- **Notificaciones push** — Soporta métodos de configuración de notificaciones push de tareas A2A cuando `push-notifications: true` está configurado, con colas de entrega basadas en archivo local o Redis.
- **Firma de Agent Card** — Agrega firmas JWS opcionales del SDK de A2A para Agent Cards y soporta verificación basada en `kid` con claves configuradas, datos JWKS octet locales o una URL JWKS remota.
- **Múltiples transportes** — Se ejecuta sobre HTTP, stdio, sockets Unix, WebSocket, gRPC oficial, gRPC JSON-RPC personalizado y transportes Redis Streams.
- **Operaciones CLI** — Proporciona comandos para descubrimiento, envío de mensajes, consulta/listado/cancelación/suscripción de tareas, CRUD de configuración push, tarjetas extendidas y vistas previas de rutas.

## Soporte de Fase 1

iac-code soporta el modo servidor A2A sobre HTTP JSON-RPC/REST y varios transportes opcionales, además del modo cliente de Fase 1 para llamar a agentes A2A remotos. Puede descubrir Agent Cards remotas, seleccionar endpoints anunciados, enviar prompts A2A 1.0, consultar/listar/cancelar/suscribirse a tareas, enrutar a agentes configurados, persistir metadatos locales de restauración de tareas/contextos, almacenar payloads de artefactos locales como artefactos de tarea estándar, validar extensiones requeridas, gestionar configuraciones de notificaciones push y firmar o verificar Agent Cards con metadatos HMAC o JWKS.

## No soportado en Fase 1 {#phase-1-unsupported}

- stdio, sockets Unix, WebSocket, envoltorio gRPC JSON-RPC y Redis Streams son transportes JSON-RPC personalizados experimentales.
- gRPC oficial requiere dependencias opcionales y usa de forma predeterminada un enlace de servidor local inseguro.
- No hay almacén de tareas distribuido ni compartido. La persistencia es almacenamiento de archivos local bajo el área de configuración de runtime de iac-code.
- No se restaura una tarea asyncio en curso después de reiniciar el proceso.
- No hay continuación automática en segundo plano de tareas remotas interrumpidas.
- No hay backend de artefactos para OSS, S3, base de datos ni almacén de objetos externo.
- No hay ingesta de URL HTTP remota, fragmentación de binarios grandes ni protocolo de carga reanudable. Las partes de URL de archivo local deben permanecer dentro de las raíces permitidas del espacio de trabajo.
- No hay fallo estricto predeterminado para Agent Cards sin firmar.
- No hay firma asimétrica de Agent Card desde el servidor ni rotación automática de claves de firma.
- No hay DAG de planificador autónomo ni orquestación multiagente compleja.
- La entrega push es al menos una vez para colas respaldadas por Redis; los receptores de callback deben manejar duplicados y aplicar su propia política de autorización del lado del endpoint.

Las solicitudes de permisos de herramientas se rechazan automáticamente en modo servidor A2A, salvo que `auto-approve-permissions` o una regla de permisos explícita las permita. Las decisiones de permisos se auditan localmente; toda decisión allow que requiere un registro de auditoría falla en modo cerrado si ese registro no se puede persistir. Las API protegidas de escritura de Alibaba Cloud siguen requiriendo autorización exacta por API fuera de los modos de bypass global. Ejecuta el modo A2A sin autenticación solo en entornos locales de confianza o protégelo con autenticación mediante token Bearer, Basic auth o clave de API.
