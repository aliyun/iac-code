---
sidebar_position: 1
title: Présentation des Skills IaC Code officiels
description: Comparez les Skills IaC Code officiels et choisissez la distribution adaptée.
---

# Présentation des Skills IaC Code officiels

IaC Code existe sous trois distributions Skill officielles. Toutes permettent de gérer l'infrastructure Alibaba Cloud
depuis un agent, mais diffèrent par leur canal de distribution et le lieu d'exécution de l'Agent IaC Code.

## Choisir un Skill

| Skill | Lieu d'exécution | À choisir lorsque |
|---|---|---|
| `iac-code` | Runtime IaC Code vérifié téléchargé sur votre machine | Vous souhaitez le paquet autonome du projet iac-code et gérer vous-même installation et mises à jour. |
| `alibabacloud-iac-code` | Même Runtime local, empaqueté pour le portail Alibaba Cloud Agent Skills | Vous gérez les Skills Alibaba Cloud via le portail ou `npx skills`. |
| `alibabacloud-ros-agent` | Agent ROS hébergé par Alibaba Cloud, appelé avec l'API ROS StartChat | Vous souhaitez une conversation distante sans télécharger le Runtime IaC Code local. |

`iac-code` et `alibabacloud-iac-code` fournissent la même capacité IaC Code. Choisissez une seule distribution dans un
même périmètre d'agent : les installer ensemble ajoute des règles de déclenchement concurrentes, pas des fonctions.

`alibabacloud-ros-agent` est une intégration distante distincte. Elle peut coexister avec une distribution locale si
l'utilisateur doit choisir explicitement entre IaC Code local et l'Agent ROS hébergé.

## Obtenir le Skill autonome

[Télécharger iac-code-skill.zip stable](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Cette distribution convient à une installation manuelle. Elle télécharge le Runtime au premier usage et réemploie la
configuration du modèle et d'Alibaba Cloud sous `~/.iac-code/`. Consultez
[Installer et utiliser le Skill IaC Code](./skill-integration.md).

## Obtenir les Skills du portail Alibaba Cloud

Recherchez leur nom exact sur le [portail Alibaba Cloud Agent Skills](https://skills.aliyun.com/) ou installez-les depuis
le dépôt officiel :

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

Téléchargements directs :

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) · [source](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) · [source](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

`npx skills` nécessite Node.js 18 ou version ultérieure et permet de choisir l'agent et la portée d'installation. Pour
un ZIP, extrayez le dossier Skill racine dans le répertoire utilisateur ou projet accepté par l'agent.

## Différences de capacité et de configuration

Les deux distributions locales prennent en charge les conversations normales et Pipeline, l'architecture, les
templates ROS/Terraform, les coûts, les stacks, le déploiement et les confirmations. Elles nécessitent un modèle
configuré, ainsi que des identifiants Alibaba Cloud lorsque la tâche consulte ou modifie des ressources.

`alibabacloud-ros-agent` utilise `ros:StartChat` pour joindre l'Agent ROS Alibaba Cloud. Il ne nécessite ni Runtime IaC
Code ni fournisseur de modèle local, mais utilise l'identité Alibaba Cloud du host. N'accordez que les droits RAM
nécessaires ; une annulation distante explicite emploie aussi `ros:StopChat`.

Dans tous les cas, contrôlez les ressources, la région, l'impact, le prix et les autorisations avant d'approuver. Ne
placez aucun identifiant dans `SKILL.md`, les prompts ou le projet.

## Documentation associée

- [Installer et utiliser le Skill IaC Code](./skill-integration.md)
- [Référence d'intégration hôte](./skill-host-integration.md)
- [Identifiants Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
