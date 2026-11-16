---
title: Autenticacion
description: Configurar credenciales de LLM y de la nube con el flujo de autenticacion.
---

# Autenticacion

Ejecuta `iac-code` en una terminal para iniciar la aplicación web local y abrir el navegador. En el primer uso, configura el proveedor LLM y las credenciales de Alibaba Cloud en Ajustes.

- **Ajustes → Modelos**: seleccione el proveedor, introduzca su clave API y elija un modelo.
- **Ajustes → Credenciales de nube**: seleccione Alibaba Cloud, el método de autenticación y la región.

Web, Desktop y las ejecuciones headless comparten las credenciales guardadas.

Para configurar desde la terminal, inicie `iac-code tui` y ejecute `/auth`.

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
