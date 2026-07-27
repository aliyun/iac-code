---
title: Application Web
description: Exécuter IaC Code comme une application web locale, avec le même moteur que la CLI.
---

# Application Web

IaC Code est fourni avec une application web locale qui exécute le même moteur d'agent que le terminal, présenté dans un navigateur plutôt que dans un REPL. Elle est utile lorsque vous préférez une interface de discussion graphique, que vous souhaitez gérer plusieurs conversations côte à côte, ou que vous avez besoin de suivre la progression d'un pipeline et l'activité des outils dans une mise en page plus riche.

L'application web lit et écrit le même magasin de sessions que la CLI : une conversation démarrée d'un côté peut donc être reprise de l'autre.

## Démarrer l'application web

Lancez le serveur depuis le terminal :

```bash
iac-code web
```

Par défaut, il écoute sur `127.0.0.1:8766` et ouvre votre navigateur par défaut à l'adresse `http://127.0.0.1:8766`.

| Option | Défaut | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Hôte du serveur HTTP. Seules les adresses de bouclage sont acceptées. |
| `--port` | `8766` | Port du serveur HTTP. |
| `--open` / `--no-open` | `--open` | Ouvre le navigateur au démarrage. Utilisez `--no-open` pour désactiver. |

```bash
iac-code web --port 9000 --no-open
```

### Sécurité

Le serveur web n'écoute que sur les interfaces de bouclage (`127.0.0.1`, `localhost` ou `::1`). Il est prévu pour un usage sur votre propre machine et rejette les adresses d'écoute publiques. Ne l'exposez pas directement sur un réseau ; placez-le derrière votre propre proxy authentifié si un accès distant est nécessaire.

## Vue d'ensemble de l'interface

### Barre latérale des sessions

La barre latérale répertorie les conversations du projet sélectionné. Depuis cet endroit, vous pouvez :

- Démarrer une **nouvelle conversation**, ou changer de projet avec le sélecteur de projet.
- **Rechercher** des conversations, ou ouvrir la palette de commandes pour exécuter une commande.
- **Épingler**, **renommer** ou **archiver** une conversation, et parcourir les conversations archivées.

Comme les sessions sont partagées avec la CLI, une conversation que vous reprenez avec `iac-code --resume` apparaît aussi ici. Consultez [Sessions](./cli/sessions.md) pour comprendre le fonctionnement du magasin de sessions.

### Zone de saisie (composer)

La zone de saisie est l'endroit où vous rédigez vos requêtes. Elle expose les mêmes contrôles que la CLI propose via les commandes slash et les options :

- La sélection du **modèle et du fournisseur** pour la session active.
- Un commutateur **Réflexion** pour activer ou désactiver le raisonnement étendu sur les modèles compatibles.
- Un contrôle de **mode d'autorisation** pour la manière dont les actions des outils sont approuvées.
- Des **pièces jointes image** pour les modèles multimodaux.
- Les **commandes slash** (saisies avec `/`) et les **références de fichiers `@`** pour désigner des fichiers de votre espace de travail.

### Discussion normale et mode pipeline

Une session s'exécute soit en discussion normale, soit en mode **pipeline**. La discussion normale diffuse en ligne les réponses de l'assistant, les appels d'outils et les résultats. Le mode pipeline ajoute un espace de travail qui affiche les chronologies des étapes, les diagnostics, les diagrammes, la progression du déploiement, le nettoyage et les détails de transfert au fur et à mesure de l'exécution du pipeline. Consultez [Mode pipeline](./automation/pipeline-mode.md) pour savoir ce que font les pipelines.

### Outils et approbations

Les appels d'outils s'affichent sous forme de cartes dans la transcription. Lorsqu'un outil requiert votre approbation, une demande d'approbation apparaît en ligne ; le mode d'autorisation défini dans la zone de saisie détermine à quel moment vous êtes sollicité.

### Paramètres

La zone des paramètres regroupe la même configuration que celle gérée par la CLI :

- Les **identifiants cloud** pour Alibaba Cloud (voir [Identifiants Alibaba Cloud](./configuration/alibaba-cloud-credentials.md)).
- Les **modèles** et la configuration des fournisseurs (voir [Fournisseurs LLM](./configuration/llm-providers.md)).
- Les **plugins MCP** (voir [Intégration MCP](./mcp/overview.md)).
- L'inspection et la gestion de la **mémoire**.

### Langue de l'interface

L'application web est disponible en sept langues — English, 简体中文, 日本語, Français, Deutsch, Español et Português — sélectionnables depuis les paramètres. Votre choix est conservé pour les sessions futures.

## Relation avec la CLI

L'application web est une interface alternative, et non un produit distinct. Elle utilise les mêmes fournisseurs, identifiants, compétences, outils et stockage de sessions que le terminal. Configurez les fournisseurs et les identifiants une seule fois avec `/auth` dans la CLI, ou via les paramètres de l'application web, et les deux interfaces les partageront.
