---
sidebar_position: 1
title: Présentation
description: Ce que fait IaC Code et par où commencer.
---

# Présentation

IaC Code est un assistant IA pour concevoir, générer, déployer et gérer l'infrastructure cloud. Il s'utilise depuis l'application Desktop, l'application Web locale, le terminal interactif, les interfaces d'automatisation ou comme Skill d'un autre agent. Son architecture est conçue pour des workflows multicloud ; la version actuelle prend en charge Alibaba Cloud ROS et Terraform.

Capacités principales :

- **Décrivez, déployez** -- décrivez ce dont vous avez besoin en langage naturel et obtenez des templates ROS validés, prêts au déploiement, ou des templates Terraform générés.
- **Du template à l'infrastructure** -- pour Alibaba Cloud ROS, passez du template à l'infrastructure en cours d'exécution ; créez, mettez à jour, supprimez et surveillez les stacks dans toutes les régions. La prise en charge de Terraform couvre la génération et la conversion de templates, pas le déploiement.
- **Intelligence cloud intégrée** -- recherchez dans la documentation, vérifiez la disponibilité des ressources et estimez les coûts avant de déployer ; chaque décision est étayée par des données cloud réelles.

Choisissez le point de départ adapté :

- Téléchargez l'[application Desktop](./desktop-app.md) pour une interface graphique prête à l'emploi.
- Suivez l'[installation](./getting-started/installation.md) et le [démarrage rapide](./getting-started/quick-start.md) pour utiliser le REPL, le mode headless ou l'[application Web](./web-app.md) locale.
- Choisissez une distribution dans la [présentation des Skills IaC Code officiels](./a2a/skill-overview.md) pour ajouter ses capacités Alibaba Cloud à un agent compatible.
- Utilisez [ACP](./acp/overview.md), [A2A](./a2a/overview.md) ou [AG-UI](./agui/overview.md) pour intégrer IaC Code à une application ou un service.

Tous les points d'entrée nécessitent un modèle configuré. Configurez aussi les [identifiants Alibaba Cloud](./configuration/alibaba-cloud-credentials.md) pour consulter, modifier ou déployer des ressources.
