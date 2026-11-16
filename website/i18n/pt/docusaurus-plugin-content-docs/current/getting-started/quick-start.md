---
title: Inicio rapido
description: Configure o IaC Code e execute seu primeiro prompt.
---

# Inicio rapido

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais da Alibaba Cloud nas configurações.

```bash
iac-code
```

A interface de terminal continua disponível com `iac-code tui`, mas não recebe mais atualizações nem manutenção. Para continuar usando o produto, prefira o aplicativo web ou desktop.

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
