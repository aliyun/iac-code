---
title: Autenticacion
description: Configurar el acceso al modelo y a la nube en los ajustes de Desktop o Web.
---

# Autenticacion

Abre Desktop o ejecuta `iac-code` en una terminal interactiva para abrir Web. Configura el modelo y las credenciales de nube necesarias en los ajustes.

- [Configurar un proveedor en Desktop o Web](./llm-providers.md)
- [Configurar el acceso a la nube en Desktop o Web](./alibaba-cloud-credentials.md)

Web, Desktop y las ejecuciones headless comparten las credenciales guardadas.

La configuracion en tiempo de ejecucion se almacena en el directorio de configuracion del usuario:

```text
~/.iac-code/
```

Los archivos importantes incluyen:

| Archivo | Proposito |
|---|---|
| `.credentials.yml` | Credenciales del proveedor de LLM |
| `.cloud-credentials.yml` | Credenciales de Alibaba Cloud |
| `settings.yml` | Configuracion en tiempo de ejecucion |
