---
sidebar_position: 1
title: Protocole ACP
description: Aperçu de la prise en charge du protocole Agent Client Protocol dans iac-code.
---

# Protocole ACP

## Qu'est-ce que l'ACP

[Agent Client Protocol (ACP)](https://agentclientprotocol.com/get-started/introduction) est un protocole de communication standardisé entre les agents IA et leurs clients. Il définit comment les clients (IDE, éditeurs, outils d'automatisation) démarrent, interagissent avec et gèrent les sessions d'agents via des messages JSON-RPC structurés.

## iac-code en tant que serveur ACP

iac-code expose ses capacités d'Infrastructure as Code via un serveur ACP. Tout client compatible ACP peut lancer `iac-code acp` en tant que sous-processus (ou se connecter via HTTP+SSE) et de manière programmatique :

- Créer des sessions liées à un répertoire de projet
- Envoyer des requêtes en langage naturel et recevoir des réponses en streaming
- Approuver ou rejeter les opérations d'écriture de fichiers et les opérations destructives
- Gérer plusieurs sessions simultanées

iac-code devient ainsi un **backend composable** pour les environnements de développement.

## Cas d'utilisation

- **Intégration IDE / Éditeur** -- Zed, VS Code ou des éditeurs personnalisés peuvent intégrer iac-code comme serveur de contexte pour fournir la génération IaC en ligne.
- **Orchestration agent-à-agent** -- D'autres agents IA peuvent appeler les capacités IaC d'iac-code via le protocole, permettant des flux de travail multi-agents.
- **Pipelines d'automatisation** -- Les scripts CI/CD ou les bots chatops peuvent invoquer iac-code sans interface pour générer et valider des templates.

## Comparaison des modes d'interaction

| Mode | Commande | Idéal pour |
|------|---------|----------|
| **Web** | `iac-code` | Exploration pratique, création itérative de templates |
| **CLI non interactif** | `iac-code --prompt "..."` | Scripting, génération ponctuelle, pipelines CI |
| **Serveur ACP** | `iac-code acp` | Intégration IDE, gestion multi-sessions, accès programmatique |

ACP fournit une intégration client orientée session avec des événements structurés en streaming. Web, A2A et le mode processus SDK disposent de leurs propres interfaces de session et de streaming.

## Capacités principales

- **Gestion multi-sessions** -- Créer, lister, dupliquer, reprendre et fermer des sessions indépendantes, chacune avec son propre historique de conversation et répertoire de travail.
- **Réponses en streaming** -- Événements en temps réel pour le texte de l'agent, la réflexion, les appels d'outils, la progression des outils et la complétion.
- **Cadre de permissions** -- Les outils en lecture seule sont auto-approuvés ; les outils d'écriture et destructifs nécessitent une approbation explicite du client avant exécution.
- **Double transport** -- Stdio pour une utilisation locale/sous-processus, HTTP+SSE pour les scénarios distants et réseau.
- **Transmission de configuration de serveur MCP** -- Les clients peuvent déclarer des serveurs MCP lors de la création de session pour l'augmentation des outils.
- **Prise en charge des commandes slash** -- Transmettre les commandes slash (`/compact`, `/clear`, `/debug`, etc.) via le protocole.
- **Métriques d'exécution** -- Statistiques d'utilisation des tokens, de latence et d'appels d'outils au niveau de la session.
