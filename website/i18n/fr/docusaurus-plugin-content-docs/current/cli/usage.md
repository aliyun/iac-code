---
title: Aperçu CLI
description: Démarrer IaC Code depuis le terminal et choisir le bon flux de travail.
---

# Aperçu CLI

Exécutez `iac-code` dans un terminal pour lancer l’application web locale et ouvrir le navigateur. Configurez le fournisseur LLM et les identifiants Alibaba Cloud dans les paramètres lors de la première utilisation.

```bash
iac-code
iac-code web --no-open
```

Choisissez le chat normal ou un Pipeline à la création d’une conversation web ou de bureau. Pour automatiser, utilisez une requête unique ou un sous-processus stream-json :

```bash
iac-code --prompt "Générer un modèle VPC"
iac-code --input-format stream-json --output-format stream-json
```

Consultez les [options de ligne de commande](./command-line-options.md) et l’[automatisation headless](../automation/non-interactive-mode.md). Sans terminal interactif, démarrez explicitement le web avec `iac-code web --no-open`.

Pour les anciens usages du terminal, consultez l’[interface de compatibilité](./interactive-mode.md).
