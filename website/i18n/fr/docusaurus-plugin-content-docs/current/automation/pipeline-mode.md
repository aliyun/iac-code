---
title: Mode pipeline
description: Utilisez le mode pipeline, exécuté étape par étape, pour guider les tâches d'infrastructure complexes.
---

# Mode pipeline

Le mode pipeline est un mode interactif qui exécute le travail étape par étape. Il est utile pour les tâches d'infrastructure plus longues ou plus faciles à rater qu'une simple demande de chat : comprendre le besoin, planifier une approche, générer des artefacts, demander confirmation à l'utilisateur, puis poursuivre les actions suivantes.

Le pipeline lui-même est une capacité générale. L'implémentation intégrée disponible aujourd'hui est le pipeline `selling`. `selling` vise les scénarios d'infrastructure Alibaba Cloud et peut faire passer une demande de déploiement par des architectures candidates, des modèles ROS, des estimations de coûts, puis un déploiement après confirmation.

Exemples de demandes adaptées au mode pipeline :

```text
Sélectionner un VPC existant et créer un VSwitch
```

```text
Concevoir un déploiement d'application web Alibaba Cloud à faible coût et générer un modèle
```

## Démarrer le mode pipeline

Le mode pipeline nécessite actuellement le REPL interactif. Il ne peut pas être combiné avec `--prompt`.

Sur macOS ou Linux :

```bash
IAC_CODE_MODE=pipeline iac-code
```

Dans PowerShell :

```powershell
$env:IAC_CODE_MODE = "pipeline"
iac-code
```

Le nom de pipeline par défaut est `selling`. Pour l'indiquer explicitement :

```bash
IAC_CODE_MODE=pipeline IAC_CODE_PIPELINE_NAME=selling iac-code
```

## Relation entre Pipeline et selling

| Nom | Signification |
|---|---|
| Mode pipeline | Mode général d'exécution étape par étape de IaC Code, destiné aux flux longs, aux points de confirmation, à la reprise et à l'affichage de la progression. |
| Pipeline `selling` | Pipeline intégré actuel pour la conception d'infrastructure Alibaba Cloud, la génération de modèles, l'estimation des coûts et le déploiement. |

Si d'autres pipelines sont ajoutés plus tard, vous pourrez les sélectionner avec `IAC_CODE_PIPELINE_NAME`. La version actuelle inclut `selling`.

## Variables d'environnement

| Variable | Rôle |
|---|---|
| `IAC_CODE_MODE=pipeline` | Active le mode pipeline. Toute autre valeur revient au mode normal. |
| `IAC_CODE_PIPELINE_NAME` | Sélectionne la définition de pipeline. La valeur par défaut est `selling`. |
| `IAC_CODE_CWD` | Remplace le répertoire de travail utilisé par le pipeline. |
| `IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING` | Active l'étape facultative de revue de modèle dans le pipeline `selling`. |

## Ce qui se passe dans le pipeline selling

Le pipeline `selling` découpe une demande d'infrastructure en étapes compréhensibles pour l'utilisateur :

| Étape | Ce que vous voyez |
|---|---|
| Comprendre le besoin | IaC Code vérifie s'il s'agit d'une tâche d'infrastructure Alibaba Cloud. S'il manque des informations importantes, il pose une question avant de générer un plan. |
| Planifier les architectures | IaC Code propose une ou plusieurs architectures candidates afin que vous puissiez comparer les compromis. |
| Générer et évaluer | IaC Code génère des modèles ROS pour les plans candidats et estime les coûts des ressources. |
| Confirmer un plan | IaC Code affiche les détails des candidats et attend que vous choisissiez le plan à poursuivre. |
| Déployer | Une fois un plan sélectionné, IaC Code entre dans l'étape de déploiement et traite les outils ou opérations plus risquées selon la politique d'autorisation. |

Si vous mentionnez des contraintes comme « utiliser un VPC existant » ou « ne pas créer ce type de ressource », le pipeline `selling` essaiera de les respecter dans les plans et modèles suivants. Vous n'avez pas besoin de connaître les champs internes ; il suffit d'écrire ces contraintes dans la demande.

## Interaction et reprise

Le mode pipeline peut se mettre en pause et attendre une saisie utilisateur, par exemple :

- Le besoin est ambigu et IaC Code doit connaître la cible, l'échelle, la région ou le budget.
- Plusieurs plans candidats existent et vous devez en choisir un.
- Une action d'outil ou de déploiement nécessite une autorisation.
- L'exécution a été interrompue et doit être reprise ou poursuivie.

Si le processus se termine ou si la session est interrompue, IaC Code enregistre l'état du pipeline. Lorsque vous revenez plus tard à cette session avec `--resume`, vous pouvez consulter la progression précédente et continuer depuis un point récupérable.

Une fois le pipeline terminé, échoué, sorti plus tôt ou annulé, IaC Code revient au chat normal. Vous pouvez alors poser des questions de suivi, ajuster le plan ou traiter les problèmes après déploiement.

## Intégrations d'automatisation

Le mode pipeline est actuellement conçu principalement pour le REPL interactif. Le mode serveur A2A peut exposer la progression du pipeline, les artefacts, les résultats d'autorisation et les informations de reprise, ce qui est utile pour connecter un pipeline à une console externe ou à un système de tâches.

ACP ne prend pas actuellement en charge le mode pipeline. `--prompt` / le [mode non interactif](./non-interactive-mode.md) exécute une demande ponctuelle normale et n'exécute pas les étapes du pipeline.

## Limites actuelles

- La version actuelle inclut uniquement le pipeline `selling`, principalement pour les workflows d'infrastructure Alibaba Cloud.
- Le mode pipeline nécessite le REPL interactif. `--prompt` est refusé lorsque `IAC_CODE_MODE=pipeline`.
- Le mode pipeline accepte les entrées texte. Les images collées dans le REPL sont ignorées lorsque le pipeline est actif.
- Pendant un pipeline, les shell escapes, les déclencheurs de skills et la plupart des slash commands sont limités, sauf autorisation explicite dans la définition du pipeline. Les commandes de base comme `/help`, `/status`, `/resume` et `/exit` restent disponibles.
