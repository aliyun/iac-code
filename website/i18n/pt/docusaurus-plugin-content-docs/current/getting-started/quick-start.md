---
title: Inicio rapido
description: Configure o IaC Code e execute seu primeiro prompt.
---

# Inicio rapido

Execute o CLI interativo:

```bash
iac-code
```

Na primeira utilizacao, configure o provedor de LLM e as credenciais da Alibaba Cloud:

```text
/auth
```

Em seguida, solicite infraestrutura:

```text
Create a VPC and two ECS instances
```

Para um prompt unico, use o modo nao interativo:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Tambem pode ler o prompt a partir da entrada padrao:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```
