---
title: Mode pipeline
description: Utilisez le mode pipeline, exécuté étape par étape, pour guider les tâches d'infrastructure complexes.
---

# Mode pipeline

Le mode pipeline est un mode interactif qui exécute le travail étape par étape. Il est utile pour les tâches d'infrastructure plus longues ou plus faciles à rater qu'une simple demande de chat : comprendre le besoin, planifier une approche, générer des artefacts, demander confirmation à l'utilisateur, puis poursuivre les actions suivantes.

Le pipeline est une capacité générale. IaC Code comprend deux pipelines d'achat Alibaba Cloud : `selling`, utilisé par défaut, et `selling_solution_first`, sélectionné explicitement. Tous deux couvrent la planification, les modèles ROS, les coûts, la confirmation et le déploiement, mais ils implémentent les candidats dans un ordre différent.

Exemples de demandes adaptées au mode pipeline :

```text
Sélectionner un VPC existant et créer un VSwitch
```

```text
Concevoir un déploiement d'application web Alibaba Cloud à faible coût et générer un modèle
```

## Démarrer le mode pipeline

Le mode pipeline est disponible dans les applications web et de bureau, via A2A et en mode processus SDK. Le mode ponctuel `--prompt` ne prend pas en charge les pipelines.

Démarrez l’application web normalement (ou ouvrez l’application de bureau) :

```bash
iac-code
```

Lors de la création d’une conversation, choisissez **Mode normal** ou **Mode pipeline**. En mode pipeline, sélectionnez **Pipeline de vente** (`selling`) ou **Pipeline de vente (solution d’abord)** (`selling_solution_first`). Les conversations d’une même application peuvent utiliser des modes et des pipelines différents, sans variable d’environnement ni redémarrage du serveur.

Pour les clients SDK utilisant un sous-processus, sélectionnez le mode pipeline par défaut avec une entrée et une sortie stream-json :

```bash
IAC_CODE_MODE=pipeline iac-code --input-format stream-json --output-format stream-json
```

## Pipelines disponibles

| Nom | Signification |
|---|---|
| Mode pipeline | Mode général d'exécution étape par étape de IaC Code, destiné aux flux longs, aux points de confirmation, à la reprise et à l'affichage de la progression. |
| Pipeline `selling` | Génère et évalue les modèles candidats avant que l'utilisateur n'en choisisse un à déployer. Il reste le choix par défaut. |
| Pipeline `selling_solution_first` | Fait d'abord choisir une architecture, puis génère, prévisualise et chiffre uniquement cette solution. |

Dans les applications web et de bureau, sélectionnez le pipeline lors de la création de la conversation. Pour A2A et le mode processus SDK, `IAC_CODE_PIPELINE_NAME` définit le pipeline par défaut. Consultez le [pipeline avec solution en premier](./solution-first-pipeline.md) pour le flux en trois étapes, les limites distinctes de confirmation et d'autorisation et la récupération.

## Variables d'environnement

Ces variables définissent les valeurs de démarrage par défaut pour A2A, le mode processus SDK et la TUI conservée. Les applications web et de bureau sélectionnent le mode et le pipeline par conversation ; l’API web n’utilise `IAC_CODE_MODE` et `IAC_CODE_PIPELINE_NAME` que si les champs correspondants sont absents de la requête.

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

Si le processus se ferme ou si la session est interrompue, IaC Code enregistre l’état du pipeline. Rouvrez la conversation depuis la barre latérale de Web ou Desktop pour consulter la progression et reprendre à un point récupérable.

Une fois le pipeline terminé, échoué, sorti plus tôt ou annulé, IaC Code revient au chat normal. Vous pouvez alors poser des questions de suivi, ajuster le plan ou traiter les problèmes après déploiement.

## Intégrations d'automatisation

Le mode pipeline est actuellement conçu principalement pour le REPL interactif. Le mode serveur A2A peut exposer la progression du pipeline, les artefacts, les résultats d'autorisation et les informations de reprise, ce qui est utile pour connecter un pipeline à une console externe ou à un système de tâches.

Lors de l'intégration du pipeline via A2A, les appelants peuvent aussi déclarer deux capacités dans les métadonnées du message : `metadata.iac_code.preferredLanguage` renvoie la progression, les questions et les invites de permission dans la langue préférée de l'appelant ; `metadata.iac_code.candidatePresentation: rich-v1` fait renvoyer à l'étape de confirmation du plan une charge structurée (nom du candidat, résumé, diagramme d'architecture, coût mensuel total, postes de coût) adaptée à un rendu enrichi dans des interfaces externes. Lorsqu'un déploiement ou une opération d'outil nécessite une permission, le pipeline publie une enveloppe de permission en attente et les appelants répondent par `allow_once` ou `deny` via un message hors bande ; voir la [Référence du protocole](../a2a/protocol-reference.md) pour les détails.

ACP ne prend pas actuellement en charge le mode pipeline. `--prompt` / le [mode non interactif](./non-interactive-mode.md) exécute une demande ponctuelle normale et n'exécute pas les étapes du pipeline.

## Limites actuelles

- La version actuelle comprend `selling` et `selling_solution_first`, tous deux principalement destinés aux workflows d'infrastructure Alibaba Cloud. `selling` reste la valeur par défaut.
- En mode pipeline dans le terminal, les images collées dans le REPL sont ignorées.
- `iac-code tui`: Pendant un pipeline, les shell escapes, les déclencheurs de skills et la plupart des slash commands sont limités, sauf autorisation explicite dans la définition du pipeline. Les commandes de base comme `/help`, `/status`, `/resume` et `/exit` restent disponibles.


## Points de contrôle de sauvegarde

Le mode Pipeline ne sauvegarde plus chaque step agent loop terminé. Il publie d'abord `input_required` ou `waiting_input`, puis exécute une unique sauvegarde critique; en cas d'échec, il publie `backup_blocked` et se met en pause dans un état récupérable. `pipeline_handoff_ready` et les états terminaux restent protégés par une sauvegarde critique avant publication. Pour les observateurs A2A, les publications protégées terminales et `pipeline_handoff_ready` sont suivies d'un événement `backup_committed` avec `committedEventId`, `committedEventType` et `committedSequence` une fois la limite de sauvegarde du mode courant durable. Avec `IAC_CODE_CONFIG_BACKUP_TMP_DIR`, cette limite est le snapshot local immuable et permet la reprise dans le même sandbox; la reprise dans un autre sandbox dépend de sa copie en arrière-plan vers `IAC_CODE_CONFIG_BACKUP_DIR`. Sans préparation locale, la limite reste le répertoire final. La progression de `parallel_sub_pipeline` est capturée par la prochaine sauvegarde d'attente, de handoff ou terminale, sans checkpoint par step.
