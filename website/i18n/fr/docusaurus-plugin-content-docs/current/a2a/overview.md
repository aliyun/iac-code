---
sidebar_position: 1
title: Protocole A2A
description: Vue d'ensemble de la prise en charge d'Agent2Agent dans iac-code.
---

# Protocole A2A

## Qu'est-ce qu'A2A

[Agent2Agent (A2A)](https://github.com/a2aproject/A2A) est un protocole qui permet de découvrir et d'appeler des agents distants. Il permet à un agent de publier une Agent Card, d'accepter des messages structurés, de diffuser des mises à jour de tâche en continu, et d'exposer des opérations d'annulation et de consultation de tâches via des transports standard.

## iac-code comme serveur A2A

iac-code peut s'exécuter comme serveur / agent A2A 1.0. Les autres clients compatibles A2A peuvent le découvrir, envoyer des demandes d'Infrastructure as Code, diffuser les mises à jour d'exécution en continu et annuler des tâches actives.

Utilisez A2A lorsqu'un autre agent, moteur de workflow ou service doit appeler iac-code comme spécialiste IaC interopérable. Utilisez ACP lorsqu'un client de type éditeur a besoin de gestion de session, de demandes d'autorisation et d'une intégration au développement local.

## Cas d'utilisation

- **Orchestration d'agents** — Un agent planificateur peut déléguer du travail Alibaba Cloud ROS ou Terraform à iac-code.
- **Automatisation de workflows** — Des outils internes peuvent soumettre des tâches de génération, de revue ou de conversion IaC via HTTP.
- **Découverte de service** — Les clients peuvent récupérer l'Agent Card et choisir des capacités comme la génération IaC ou la revue de modèles.
- **Intégrations en streaming** — Un client chatops ou tableau de bord peut afficher le texte du modèle, l'activité des outils, les métadonnées d'utilisation et l'état final de la tâche pendant l'exécution du tour.

## Comparaison des modes d'interaction

| Mode | Commande | Idéal pour |
|------|---------|----------|
| **REPL interactif** | `iac-code` | Exploration pratique et création itérative de modèles |
| **CLI non interactif** | `iac-code --prompt "..."` ou `--headless` | Scripts ponctuels et tâches CI |
| **Serveur ACP** | `iac-code acp` | Intégration IDE/éditeur et contrôle client multi-session |
| **Serveur A2A** | `iac-code a2a` | Interopérabilité agent-à-agent via les transports A2A |
| **Client A2A** | `iac-code a2a-client call` | Appel d'agents A2A distants depuis iac-code |

## Capacités principales

- **Découverte de l'Agent Card** — Publie `/.well-known/agent-card.json` avec le binding de protocole, la version, les compétences, les modes d'entrée/sortie et les métadonnées d'authentification optionnelles.
- **HTTP JSON-RPC et REST** — Sert les requêtes A2A JSON-RPC sur `/` et enregistre les routes REST du SDK.
- **Réponses en streaming** — Prend en charge `SendStreamingMessage` pour les mises à jour de tâche incrémentales.
- **Gestion des tâches** — Prend en charge la consultation des tâches, la liste authentifiée des tâches avec pagination par curseur, l'annulation des tâches actives et l'abonnement aux tâches actives.
- **Réutilisation du contexte** — Réutilise un runtime iac-code pour les messages de suivi dans le même `contextId` A2A.
- **Portée de l'espace de travail** — Lit le répertoire du projet depuis les métadonnées de message à `iac_code.cwd`.
- **Métadonnées d'outil** — Émet des métadonnées propres à iac-code pour les démarrages d'outils, les deltas d'entrée, les résultats d'outils terminés, les décisions d'autorisation et l'utilisation des jetons.
- **Parties d'entrée** — Accepte les parties de type texte, les parties de données JSON, le texte UTF-8 brut, les fichiers texte locaux `file://` de l'espace de travail et les pièces jointes multimodales bornées représentées comme manifestes de prompt.
- **Appels client** — Découvre les Agent Cards distantes, vérifie les signatures lorsqu'elles sont configurées, et envoie des prompts texte à des agents distants.
- **Routage** — Sélectionne les agents distants configurés par nom explicite, compétence ou correspondance prompt/tag.
- **Métadonnées de persistance** — Duplique les instantanés locaux de tâches/contextes A2A vers des fichiers JSON pour les métadonnées de restauration interprocessus.
- **Artefacts** — Stocke les charges utiles d'artefacts texte locaux pris en charge hors du corps de l'événement diffusé, émet des événements standard `TaskArtifactUpdateEvent` et enregistre les `artifacts` de la tâche.
- **Extensions et mise en cache** — Annonce l'extension optionnelle de métadonnées d'artefact iac-code, valide les `A2A-Extensions` obligatoires et sert les Agent Cards avec des en-têtes de cache.
- **Notifications push** — Prend en charge les méthodes de configuration des notifications push de tâche A2A lorsque `push-notifications: true` est configuré, avec des files de livraison adossées à des fichiers locaux ou à Redis.
- **Signature d'Agent Card** — Ajoute des signatures JWS optionnelles du SDK A2A pour les Agent Cards et prend en charge la vérification basée sur `kid` avec des clés configurées, des données JWKS octet locales ou une URL JWKS distante.
- **Transports multiples** — Fonctionne via HTTP, stdio, sockets Unix, WebSocket, gRPC officiel, gRPC JSON-RPC personnalisé et transports Redis Streams.
- **Opérations CLI** — Fournit des commandes pour la découverte, l'envoi de messages, la consultation/liste/annulation/abonnement aux tâches, le CRUD de configuration push, les cartes étendues et les aperçus de routage.

## Prise en charge Phase 1

iac-code prend en charge le mode serveur A2A via HTTP JSON-RPC/REST et plusieurs transports optionnels, ainsi que le mode client Phase 1 pour appeler des agents A2A distants. Il peut découvrir des Agent Cards distantes, sélectionner les endpoints annoncés, envoyer des prompts A2A 1.0, interroger/lister/annuler/s'abonner aux tâches, router vers des agents configurés, persister les métadonnées locales de restauration de tâches/contextes, stocker les charges utiles d'artefacts locaux comme artefacts de tâche standard, valider les extensions obligatoires, gérer les configurations de notifications push, et signer ou vérifier les Agent Cards avec des métadonnées HMAC ou JWKS.

## Non pris en charge en Phase 1 {#phase-1-unsupported}

- stdio, les sockets Unix, WebSocket, l'enveloppe gRPC JSON-RPC et Redis Streams sont des transports JSON-RPC personnalisés expérimentaux.
- Le gRPC officiel nécessite des dépendances optionnelles et utilise par défaut un binding serveur local non sécurisé.
- Pas de magasin de tâches distribué ou partagé. La persistance est un stockage de fichiers local sous la zone de configuration runtime d'iac-code.
- Pas de restauration d'une tâche asyncio en cours après le redémarrage du processus.
- Pas de continuation automatique en arrière-plan des tâches distantes interrompues.
- Pas de backend d'artefacts OSS, S3, base de données ou magasin d'objets externe.
- Pas d'ingestion d'URL HTTP distante, de découpage de gros binaires ou de protocole de téléversement reprenable. Les parties d'URL de fichier local doivent rester dans les racines d'espace de travail autorisées.
- Pas d'échec strict par défaut pour les Agent Cards non signées.
- Pas de signature asymétrique d'Agent Card côté serveur et pas de rotation automatique des clés de signature.
- Pas de DAG de planificateur autonome ni d'orchestration multi-agent complexe.
- La livraison push est au moins une fois pour les files adossées à Redis ; les récepteurs de callback doivent gérer les doublons et appliquer leur propre politique d'autorisation côté endpoint.

Les demandes d'autorisation d'outil sont rejetées automatiquement en mode serveur A2A. N'exécutez le mode A2A non authentifié que dans des environnements locaux de confiance, ou protégez-le avec une authentification par jeton Bearer, Basic auth ou clé API.
