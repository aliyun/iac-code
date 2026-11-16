---
title: Autenticacion
description: Configurar credenciales de LLM y de la nube con el flujo de autenticacion.
---

# Autenticacion

Ejecuta `iac-code` en una terminal para iniciar la aplicación web local y abrir el navegador. En el primer uso, configura el proveedor LLM y las credenciales de Alibaba Cloud en Ajustes.

La interfaz de terminal se conserva como `iac-code tui`, pero ya no recibe actualizaciones ni mantenimiento. Para seguir utilizando el producto, usa la aplicación web o de escritorio.

Usa `/auth` en modo interactivo para configurar tanto el acceso al proveedor de modelos como el acceso a Alibaba Cloud.

```bash
iac-code tui
```

```text
/auth
```

El flujo de autenticacion te guia a traves de la seleccion de proveedor, la entrada de clave API, la seleccion de modelo y la configuracion de credenciales de Alibaba Cloud.

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
