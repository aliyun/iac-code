---
title: Démarrage rapide
description: Configurer IaC Code et exécuter votre première requête.
---

# Démarrage rapide

Lancez le CLI interactif :

```bash
iac-code
```

Lors de la première utilisation, configurez le fournisseur LLM et les identifiants Alibaba Cloud :

```text
/auth
```

Puis demandez votre infrastructure :

```text
Create a VPC and two ECS instances
```

Pour une requête ponctuelle, utilisez le mode non interactif :

```bash
iac-code --prompt "Create an OSS Bucket"
```

Vous pouvez également lire la requête depuis l'entrée standard :

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```
