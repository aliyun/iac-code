---
title: Application Web
description: Démarrer l’application web locale et utiliser ses paramètres, conversations et contrôles de saisie.
---

# Application Web

Lancez `iac-code` dans un terminal pour démarrer l’application web locale et ouvrir le navigateur. À la première utilisation, configurez le fournisseur LLM et les identifiants Alibaba Cloud dans les paramètres.

Desktop et Web partagent les conversations et les paramètres. Les conversations créées ailleurs peuvent être accessibles en lecture seule ; consultez [Sessions](./cli/sessions.md) pour leur affichage et leur poursuite.

## Installation

L’installation par défaut comprend les dépendances de l’application web et de l’environnement HTTP. L’extra `[http]` reste un alias de compatibilité ; il n’est plus nécessaire de l’installer séparément. Depuis le dépôt, `uv sync` suffit.

```bash
pip install iac-code
```

## Démarrer l'application web

Lancez le serveur depuis le terminal :

```bash
iac-code web
```

Par défaut, il écoute sur `127.0.0.1:8766` et ouvre votre navigateur par défaut à l'adresse `http://127.0.0.1:8766`.

| Option | Défaut | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Hôte du serveur HTTP. Le mode local accepte les adresses de boucle locale ; les autres IP nécessitent `--access-token-file`. |
| `--port` | `8766` | Port du serveur HTTP. |
| `--open` / `--no-open` | `--open` | Ouvre le navigateur au démarrage. Utilisez `--no-open` pour désactiver. |
| `--debug`, `-d` | `false` | Activer les journaux de débogage du serveur pour cette exécution. |
| `--access-token-file <path>` | — | Fichier de jeton pour un accès distant explicite avec transport d’API chiffré. |

```bash
iac-code web --port 9000 --no-open
```

### Sécurité

Sans `--access-token-file`, le serveur n’accepte que les hôtes de boucle locale (`127.0.0.1`, `localhost` ou `::1`) et vérifie les requêtes locales. Le mode explicite avec jeton accepte aussi d’autres IP et protège les requêtes d’API par le transport chiffré avec jeton.

## Déployer un modèle ROS existant

1. Configurez les [identifiants cloud](./configuration/alibaba-cloud-credentials.md), puis ouvrez une conversation dans le projet contenant le modèle.

2. Tapez `@`, sélectionnez le fichier et demandez sa vérification ainsi que les paramètres nécessaires.

3. Indiquez la région, le nom de pile et les valeurs des paramètres. Demandez une validation avant la création.

4. Vérifiez le résultat et les changements proposés, puis demandez la création de la pile.

5. Si une carte d’autorisation cloud apparaît, vérifiez l’opération avant de l’approuver ou de la refuser. Une création approuvée peut produire des ressources facturables.

6. Consultez l’ID de pile, la progression et le résultat. En cas d’échec, demandez le diagnostic de l’erreur dans la même conversation.

## Vue d'ensemble de l'interface

### Barre latérale des sessions

La barre latérale regroupe les conversations enregistrées par projet. Vous pouvez :

- Démarrer une **Nouvelle discussion**, puis choisir son projet sous la zone de saisie.
- **Rechercher** des conversations, ou ouvrir la palette de commandes pour exécuter une commande.
- **Épingler**, **renommer** ou **archiver** une conversation, et parcourir les conversations archivées.

Web, bureau et headless partagent les conversations enregistrées. Consultez les [sessions](./cli/sessions.md) pour les reprendre ou les renommer.

### Zone de saisie (composer)

Décrivez la tâche dans le champ de saisie et utilisez ses contrôles pour choisir le fonctionnement de la conversation :

- La sélection du **modèle et du fournisseur** pour la session active.
- Un commutateur **Réflexion** pour activer ou désactiver le raisonnement étendu sur les modèles compatibles.
- Un contrôle de **mode d'autorisation** pour la manière dont les actions des outils sont approuvées.
- Des **pièces jointes image** pour les modèles multimodaux.
- **Compétences** choisis dans les suggestions `/` ou `$`, et **références `@` aux fichiers** du workspace.

### Discussion normale et mode pipeline

Choisissez le mode à la création d’une conversation, puis le pipeline si vous optez pour le mode pipeline. Les conversations d’une même application peuvent utiliser des modes différents sans redémarrage du serveur.

Une session s'exécute soit en discussion normale, soit en mode **pipeline**. La discussion normale diffuse en ligne les réponses de l'assistant, les appels d'outils et les résultats. Le mode pipeline ajoute un espace de travail qui affiche les chronologies des étapes, les diagnostics, les diagrammes, la progression du déploiement, le nettoyage et les détails de transfert au fur et à mesure de l'exécution du pipeline. Consultez [Mode pipeline](./automation/pipeline-mode.md) pour savoir ce que font les pipelines.

Le [pipeline `selling_solution_first`](./automation/solution-first-pipeline.md) utilise cet espace de travail pour un parcours d'achat en trois étapes : comparer les architectures candidates, mettre en œuvre la solution retenue, puis la déployer après confirmation. Les approbations d'outils apparaissent sous forme de cartes d'autorisation localisées sous l'étape qui les a demandées ; les approbations en attente reviennent à la même étape lors de la restauration de la session.

### Outils et approbations

Les appels d'outils s'affichent sous forme de cartes dans la transcription. Lorsqu'un outil requiert votre approbation, une demande d'approbation apparaît en ligne ; le mode d'autorisation défini dans la zone de saisie détermine à quel moment vous êtes sollicité.

### Paramètres

Les applications de bureau et web partagent la même interface. Les opérations quotidiennes passent par les paramètres et les contrôles visibles.

- Les **identifiants cloud** pour Alibaba Cloud (voir [Identifiants Alibaba Cloud](./configuration/alibaba-cloud-credentials.md)).
- Les **modèles** et la configuration des fournisseurs (voir [Fournisseurs LLM](./configuration/llm-providers.md)).
- Les **plugins MCP** (voir [Intégration MCP](./mcp/overview.md)).
- L'inspection et la gestion de la **mémoire**.

### Langue de l'interface

L'application web est disponible en sept langues — English, 简体中文, 日本語, Français, Deutsch, Español et Português — sélectionnables depuis les paramètres. Votre choix est conservé pour les sessions futures.

## Relation avec la CLI

Web, Desktop et headless partagent fournisseurs, identifiants, compétences, outils et sessions. Configurez une fois le modèle et l’accès cloud dans les paramètres pour les réutiliser entre ces entrées.
