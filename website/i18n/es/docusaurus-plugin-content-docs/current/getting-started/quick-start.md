---
title: Inicio rapido
description: Configurar IaC Code y ejecutar tu primer prompt.
---

# Inicio rapido

Ejecuta el CLI interactivo:

```bash
iac-code
```

En el primer uso, configura el proveedor de LLM y las credenciales de Alibaba Cloud:

```text
/auth
```

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
