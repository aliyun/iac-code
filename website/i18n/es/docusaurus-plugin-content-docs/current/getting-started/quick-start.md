---
title: Inicio rapido
description: Configurar IaC Code y ejecutar tu primer prompt.
---

# Inicio rapido

Ejecuta `iac-code` en una terminal para iniciar la aplicación web local y abrir el navegador. En el primer uso, configura el proveedor LLM y las credenciales de Alibaba Cloud en Ajustes.

```bash
iac-code
```

La interfaz de terminal se conserva como `iac-code tui`, pero ya no recibe actualizaciones ni mantenimiento. Para seguir utilizando el producto, usa la aplicación web o de escritorio.

Luego solicita infraestructura:

```text
Create a VPC and two ECS instances
```

Para un prompt de un solo uso, utiliza el modo no interactivo:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Tambien puedes leer el prompt desde la entrada estandar:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```
