---
title: Démarrage rapide
description: Configurer IaC Code et exécuter votre première requête.
---

# Démarrage rapide

Lancez `iac-code` dans un terminal pour démarrer l’application web locale et ouvrir le navigateur. À la première utilisation, configurez le fournisseur LLM et les identifiants Alibaba Cloud dans les paramètres.

```bash
iac-code
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
