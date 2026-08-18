---
sidebar_position: 7
title: Intégration Skill
description: Des agents externes pilotent iac-code via le Skill empaqueté d'iac-code et le Skill Runtime.
---

# Intégration Skill

iac-code fournit un Skill empaqueté destiné aux agents externes. Un agent externe (un agent planificateur ou une plateforme d'agents) n'installe pas le paquet Python d'iac-code et n'invoque pas de commandes headless ; il pilote un runtime A2A local authentifié via un script pont utilisant uniquement la bibliothèque standard, afin d'exécuter des travaux d'infrastructure Alibaba Cloud tels que la génération de templates ROS/Terraform, l'estimation des coûts, la sélection de ressources et le déploiement.

## Composants

| Composant | Emplacement | Description |
|---|---|---|
| Paquet du Skill | `skills/iac-code/` | Instructions `SKILL.md`, métadonnées d'agent dans `agents/` et `scripts/iac_code.py`, le script pont |
| Skill Runtime | Publié par plateforme | Exécutable natif CPython 3.12 intégrant le serveur A2A d'iac-code |
| Contrats de distribution | `skill-runtime/skill-package-contract.json`, `skill-runtime/publisher-contract.json` | Contraintes de format et de vérification pour les paquets de skill et les éditeurs |

Le script pont est écrit entièrement avec la bibliothèque standard de Python et reste compatible avec Python 3.8+ ; la CI le compile et l'exécute en test de fumée sur toute la matrice 3.8–3.14. N'ajoutez ni dépendance tierce ni syntaxe réservée aux versions récentes au pont.

## Acquisition et cache du Runtime

À la première utilisation, le pont lit le manifeste, télécharge l'artefact correspondant à la plateforme courante, vérifie sa taille et son SHA-256, l'installe et le met en cache sous `<IAC_CODE_CONFIG_DIR ou ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`.

- `python3 scripts/iac_code.py ensure-runtime` — prépare le runtime à l'avance ; un runtime en cache est réutilisé.
- `python3 scripts/iac_code.py cache list` — affiche les runtimes installés et les paquets candidats.
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — nettoie les caches du runtime ou les paquets candidats ; nécessite `--confirm` explicite.

## Prévol de configuration

Avant de créer un job, `start` exécute une vérification de préparation de la configuration via le runtime. Le prévol ne lit pas les valeurs secrètes ; il signale uniquement l'état de préparation :

| Situation | Résultat |
|---|---|
| Fournisseur LLM ou clé API incomplet | Renvoie `llm_not_configured` et refuse de créer le job |
| Pipeline selling avec identifiants Alibaba Cloud incomplets | Renvoie `cloud_credentials_not_configured` et refuse de créer le job |
| Mode normal avec identifiants Alibaba Cloud incomplets | Peut continuer pour les travaux n'appelant pas d'API cloud, avec un avertissement de prévol |

## Référence des commandes

| Commande | Objet |
|---|---|
| `start` | Créer un job : `--mode normal|pipeline`, `--pipeline-name`, `--cwd` espace de travail absolu, `--prompt-file` fichier de prompt UTF-8, `--language auto|en|zh|es|fr|de|ja|pt`, `--follow` facultatif |
| `follow` | Consomme le flux d'événements jusqu'à la prochaine frontière d'interaction : `--job-id`, `--cursor`, `--wait-seconds` (60 s par défaut, 120 s maximum) |
| `continue` | Poursuit une conversation en mode normal dans le même job : `--job-id`, `--prompt-file`, `--follow` facultatif |
| `respond` | Répond à une entrée en attente, voir [Entrée utilisateur](#input-required) |
| `poll` | Interrogation unique réservée au diagnostic et à la récupération ; ne pas l'utiliser en remplacement de `follow` |
| `cancel` | Annule le job |
| `ensure-runtime` / `cache list` / `cache clean` | Gestion du runtime et du cache |

`start --follow` et `follow` écrivent les frontières d'étape et les battements basse fréquence sur stderr ; stdout produit exactement un résultat JSON borné.

## Frontières d'interaction {#boundaries}

`--follow` consomme le flux d'événements jusqu'à la prochaine frontière d'étape, demande de permission, question utilisateur, sélection de candidat, `turn_completed` ou état terminal. Un résultat de frontière porte :

- `boundaryReached: true` — une frontière est atteinte ; cela ne signifie **pas** que le job est terminé ;
- `presentationRequired: true` et `userUpdates` — des chaînes localisées prêtes à être affichées à l'utilisateur ;
- le `cursor` nécessaire pour continuer.

L'agent externe doit d'abord présenter chaque chaîne `userUpdates` reçue dans une réponse visible par l'utilisateur, puis rappeler immédiatement `follow` avec le `cursor` renvoyé. Ne répondez pas à la tâche d'infrastructure en parallèle et ne posez pas de questions sans rapport pendant qu'un follow est en cours.

## Entrée utilisateur {#input-required}

Un résultat contient `inputRequired` lorsqu'une entrée utilisateur est nécessaire. Il existe trois sortes :

- `permission` — une demande de permission d'outil ou de déploiement. L'enveloppe contient `inputId`, `toolUseId`, un titre, un objectif, un effet, une cible, un indicateur lecture seule, `safeSummary` et, pour les demandes de déploiement, `deploymentSummary`. L'agent externe doit décider selon sa propre politique de permissions : si la même opération se poursuivrait sans demande lorsque l'agent l'exécute directement, répondez `allow_once` ; si sa politique la refuserait, répondez `deny` ; sinon demandez à l'utilisateur. Les refus d'iac-code lui-même ne doivent pas être contournés.
- `ask_user_question` — une question à choix multiples ou en texte libre. Présentez l'invite et les options telles quelles ; n'acceptez le texte libre que si `allowFreeText` vaut `true`.
- `candidate_selection` — sélection de plan du pipeline. Présentez d'abord le résumé, le diagramme d'architecture (Mermaid), le coût mensuel total et les postes de coût de chaque candidat, puis renvoyez le candidat retenu. Ne remplacez jamais les prix fournis par des estimations approximatives.

`respond` existe sous deux formes :

```bash
# Décision en ligne pour les permissions
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# Les questions et sélections de candidat utilisent un fichier de réponse
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

Une réponse doit conserver tous les champs de corrélation de l'entrée en attente et reste liée aux `kind`, `inputId`, `requestTaskId` et `contextId` courants ; ne réutilisez jamais une réponse provenant d'une autre requête et ne réinterprétez jamais une sélection de ressource comme une confirmation de déploiement.

## Contrôle de la langue

`start --language` définit la langue préférée du job (utilisez `auto` en cas d'inconnue). Chaque résultat de ce job répète `preferredLanguage` ; traitez-le comme un état de contrôle durable : la progression, les questions, les invites de permission, les plans candidats et les résultats finaux sont présentés dans cette langue, tandis que les noms de champs du protocole, les énumérations, les identifiants et les commandes restent inchangés. Lorsque le texte faisant autorité utilise déjà cette langue, présentez-le directement ou résumez-le dans la même langue ; ne traduisez jamais un contenu chinois visible par l'utilisateur vers l'anglais.

## Relation avec le protocole A2A

Le pont communique avec le runtime local via HTTP A2A JSON-RPC ; les états de tâche, les artefacts et les interactions de permissions réutilisent le protocole A2A d'iac-code :

- Les réponses hors bande de permissions utilisent le format de message `schemaVersion 1` ; voir la [Référence du protocole](./protocol-reference.md) pour les champs et contraintes.
- En mode pipeline, transmettre `candidatePresentation: rich-v1` renvoie des charges structurées de présentation des candidats.
- Les états de résultat du job correspondent aux états de tâche A2A : `turn_completed` termine un tour normal ; les états terminaux du pipeline sont `completed`, `failed`, `canceled` et `rejected`, avec `pipelineResult` et `artifacts` comme résultat faisant autorité.

## Limite de sécurité

- Le runtime n'écoute que sur un port aléatoire de `127.0.0.1` ; chaque démarrage génère un nouveau jeton Bearer aléatoire, et chaque requête du pont le transmet.
- Le pont conserve les artefacts et les résultats dans l'espace de travail du job ; les résultats sont écrits dans `.iac-code-skill-results/` de l'espace de travail.
- Les rapports de prévol et les champs d'affichage des permissions sont assainis ; les secrets et identifiants n'apparaissent jamais dans les champs d'affichage.
