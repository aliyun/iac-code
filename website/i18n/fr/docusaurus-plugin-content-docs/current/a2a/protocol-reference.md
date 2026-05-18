---
title: Référence du protocole
description: Référence complète du protocole A2A pour l'intégration d'iac-code.
sidebar_position: 4
---

# Référence du protocole

Ce document décrit la surface A2A 1.0 exposée par le serveur iac-code et le comportement du client Phase 1 utilisé par `iac-code a2a-client call`. Pour les options CLI exactes, consultez la [référence des commandes](./command-reference.md).

## Vue d'ensemble du cycle de vie

Une interaction A2A typique suit ce flux :

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **Découvrir** — Récupérer `/.well-known/agent-card.json`.
2. **Envoyer** — Soumettre un message texte à l'endpoint JSON-RPC sur `/`.
3. **Diffuser** — Recevoir des charges utiles `Task`, `Message` et `TaskStatusUpdateEvent`.
4. **Continuer** — Envoyer un message de suivi avec le même `contextId`.
5. **Annuler ou interroger** — Utiliser `CancelTask`, `GetTask` ou `ListTasks`.

## Agent Card

L'Agent Card est disponible à :

```text
GET /.well-known/agent-card.json
```

Champs importants :

| Champ | Valeur | Signification |
|-------|-------|---------|
| `name` | `iac-code` | Nom de l'agent |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | Binding de transport |
| `supportedInterfaces[0].protocolVersion` | `1.0` | Version du protocole A2A |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | Endpoint JSON-RPC |
| `capabilities.streaming` | `true` | Prend en charge les mises à jour de tâche en streaming |
| `capabilities.pushNotifications` | `false` ou `true` | `true` lorsque `push-notifications: true` est configuré |
| `capabilities.extendedAgentCard` | `true` | Les appelants authentifiés peuvent demander des détails runtime étendus |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | Espace de noms optionnel de métadonnées iac-code pour l'état des outils et les métadonnées d'artefacts stockés |
| `defaultInputModes` | types MIME texte, JSON, YAML, image, audio et binaires | Modes MIME d'entrée acceptés |
| `defaultOutputModes` | `["text/plain"]` | Sortie texte uniquement |

Les réponses d'Agent Card incluent `Cache-Control: public, max-age=60`, `ETag` et `Last-Modified`. Les clients peuvent envoyer `If-None-Match` et recevoir `304 Not Modified` lorsque la carte n'a pas changé.

Compétences annoncées :

| ID de compétence | Objectif |
|----------|---------|
| `iac_generation` | Générer des modèles Alibaba Cloud ROS et Terraform à partir du langage naturel |
| `iac_review` | Inspecter les modèles IaC et suggérer des corrections |
| `aliyun_ros_operations` | Aider aux workflows de piles Alibaba Cloud ROS |
| `terraform_ros_conversion` | Aider à la conversion Terraform-vers-ROS avec les ressources de compétences groupées |

Lorsque l'authentification est activée, l'Agent Card annonce les schémas de sécurité configurés :

| Schéma | Quand il est annoncé |
|--------|-----------------|
| `bearerAuth` | `token` ou `IACCODE_A2A_HTTP_TOKEN` est défini |
| `basicAuth` | Le nom d'utilisateur et le mot de passe Basic sont tous deux définis |
| `apiKeyAuth` | `api-key` ou `IACCODE_A2A_API_KEY` est défini |

## Routes

| Route | Méthode | Description |
|-------|--------|-------------|
| `/health` | `GET` | Renvoie `{"status":"healthy"}` |
| `/.well-known/agent-card.json` | `GET` | Renvoie l'Agent Card |
| `/` | `POST` | Gère les requêtes A2A JSON-RPC |
| Routes REST | mixte | Les routes REST du SDK A2A enregistrées par `create_rest_routes` |

## Notes sur le client et les transports Phase 1

Le transport Phase 1 interopérable par défaut est JSON-RPC via HTTP. Le mode HTTP annonce également `HTTP+JSON` pour les routes REST du SDK.

Le serveur dispose aussi de transports optionnels pour stdio, les sockets Unix, WebSocket, le gRPC officiel, l'enveloppe gRPC JSON-RPC et Redis Streams. stdio, les sockets Unix, WebSocket, gRPC JSON-RPC et Redis Streams sont des transports JSON-RPC personnalisés. Le gRPC officiel est annoncé comme `grpc` et nécessite des dépendances gRPC optionnelles.

Le client intégré utilise la découverte d'Agent Card (`GET /.well-known/agent-card.json`) avant les appels de message, sélectionne le premier `supportedInterfaces[].url` exécutable annoncé, puis envoie des requêtes JSON-RPC avec `A2A-Version: 1.0` et des noms de méthodes A2A 1.0 comme `SendMessage`.

`push-notifications: true` active les méthodes de configuration des notifications push A2A et la livraison des états terminaux.

La signature d'Agent Card utilise l'utilitaire de signature du SDK A2A et émet les champs JWS standard `AgentCardSignature`. Le mode à clé symétrique utilise `HS256` ; la vérification peut sélectionner un secret configuré par `kid` d'en-tête protégé, un JWKS local à clé octet ou une URL JWKS distante. La signature asymétrique côté serveur et la rotation automatique des clés ne sont pas implémentées en Phase 1.

Pour la liste canonique des comportements non pris en charge en Phase 1, consultez [Protocole A2A](./overview.md#phase-1-unsupported).

## Backends de livraison des notifications push

`iac-code a2a --config a2a-server.yml` prend en charge deux files de livraison push :

- `push-queue: local-file` stocke les tâches sous le répertoire de persistance A2A et est destiné à une utilisation locale sur un seul noeud.
- `push-queue: redis-streams` stocke les tâches dans Redis Streams et coordonne les workers via un groupe de consommateurs Redis.

La livraison push adossée à Redis nécessite l'extra optionnel `a2a-redis` et est au moins une fois. Les récepteurs de callback doivent gérer les mises à jour de tâche de manière idempotente, car une tâche peut être livrée à nouveau après des crashs de workers, l'expiration d'un bail, des reconnexions ou des courses de nouvelle tentative.

Options Redis courantes :

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

Les URL de callback sont validées avant le stockage puis de nouveau avant l'envoi. Le validateur par défaut rejette les URL non HTTP(S), les noms d'hôte localhost et les adresses IP littérales privées/locales. Les récepteurs de callback doivent tout de même appliquer leur propre politique d'authentification et d'idempotence.

## Méthodes JSON-RPC

### SendMessage

Exécute un tour de message A2A non streaming. La réponse contient une tâche ou un message une fois le tour terminé.

**Requête**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**Champs de message requis**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `messageId` | string | Oui | ID de message client unique |
| `role` | string | Oui | Utilisez `ROLE_USER` pour l'entrée utilisateur |
| `parts` | array | Oui | Parties de type texte, données JSON, texte brut, URL de fichier local ou parties multimodales bornées |
| `metadata.iac_code.cwd` | string | Recommandé | Chemin absolu de l'espace de travail ; utilise par défaut le répertoire du processus serveur si omis |

`metadata.iac_code.cwd` doit être un répertoire absolu existant lorsqu'il est fourni. Il doit se trouver dans une racine d'espace de travail autorisée. Par défaut, les racines autorisées sont le répertoire du processus serveur et le répertoire temporaire système ; `IACCODE_A2A_ALLOWED_CWDS` peut fournir une liste d'autorisation séparée par le séparateur de chemins du système d'exploitation.

Catégories d'entrée prises en charge :

| Catégorie | Forme acceptée | Limites et comportement |
|----------|----------------|---------------------|
| Parties de type texte | `text` avec `text/plain`, JSON, Markdown, YAML ou des types MIME texte supplémentaires configurés | Ajoutées directement au prompt |
| Parties de données JSON | `data` avec `application/json` | Sérialisées en JSON compact ; max 1 MiB en ligne |
| Parties de texte brut | `raw` avec un type MIME de type texte | Doit être UTF-8 valide ; max 1 MiB en ligne |
| URL de fichiers texte locaux | `url` avec `file://...` et un type MIME de type texte | Le fichier doit exister dans `cwd` et les racines autorisées ; max 1 MiB |
| Parties multimodales raw/data/file | image, audio ou types MIME multimodaux configurés | Converties en manifeste de prompt avec nom de fichier, type média, taille en octets, hash et source ; raw/data max 5 MiB, URL de fichier max 25 MiB |

L'ingestion d'URL HTTP(S) distante n'est pas prise en charge. Les parties d'URL de fichier doivent utiliser des URL locales `file://` et rester dans l'espace de travail autorisé.

### SendStreamingMessage

Exécute un tour de message A2A en streaming. Le corps de requête a la même forme que `SendMessage`, mais le serveur diffuse les réponses JSON-RPC comme Server-Sent Events.

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

Renvoie la tâche A2A sauvegardée par ID. Utilisez `historyLength` pour limiter l'historique renvoyé sans modifier l'historique de tâche stocké. Omettez-le pour recevoir l'historique par défaut actuel du serveur.

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

Renvoie les tâches connues visibles par l'appelant authentifié. Les résultats sont triés par horodatage de statut décroissant, puis par ID de tâche décroissant pour un ordre stable. Le serveur prend en charge `contextId`, `status`, `pageSize`, `pageToken`, `historyLength` et `includeArtifacts`.

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

`nextPageToken` est renvoyé lorsqu'une autre page est disponible. `includeArtifacts` vaut `false` par défaut, donc les réponses de liste omettent les artefacts de tâche sauf demande explicite.

### CancelTask

Demande l'annulation d'une tâche en cours.

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

Si la tâche est active, le serveur annule le tour de l'agent en cours et émet un état de tâche annulé. Si la tâche existe mais n'est pas en cours, le serveur renvoie l'erreur A2A standard `TaskNotCancelableError`.

### SubscribeToTask

S'abonne à un flux de mises à jour de tâche active lorsque le transport client le prend en charge.

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

Pour les tâches actives, le flux commence par la `Task` actuelle, puis émet les événements de tâche suivants et se ferme lorsque le tour actif se termine. S'abonner à une tâche terminée, échouée, annulée ou nécessitant une entrée renvoie une erreur de type tâche introuvable au lieu d'attendre indéfiniment. Pour les nouveaux tours, préférez `SendStreamingMessage` ; il démarre l'exécution et diffuse la réponse en une seule requête.

### Méthodes de configuration des notifications push

Lorsque le serveur démarre avec `push-notifications: true`, il prend en charge :

| Méthode | Objectif |
|--------|---------|
| `CreateTaskPushNotificationConfig` | Stocker une configuration de callback pour une tâche |
| `GetTaskPushNotificationConfig` | Récupérer une configuration de callback |
| `ListTaskPushNotificationConfigs` | Lister les configurations de callback d'une tâche |
| `DeleteTaskPushNotificationConfig` | Supprimer une configuration de callback |

Exemple de requête de création :

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

Le serveur chiffre les jetons de notification stockés et les identifiants d'authentification de callback lorsque le trousseau de clés push local est disponible.

### GetExtendedAgentCard

Les clients authentifiés peuvent demander l'Agent Card étendue :

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

La carte étendue inclut la carte publique plus les détails runtime authentifiés.

## Comportement des tâches et des contextes

iac-code mappe les contextes A2A vers des runtimes d'agent internes :

| Concept | Comportement |
|---------|----------|
| `contextId` omis | Le SDK/serveur génère un nouvel ID de contexte |
| Même `contextId` | Réutilise la même session iac-code interne et l'état de conversation |
| Même `contextId`, `cwd` différent | Rejeté comme espace de travail différent |
| Même `contextId`, message concurrent | Rejeté avec `Task is already working.` |
| Valeurs `contextId` différentes | Peuvent s'exécuter simultanément |
| Contexte inactif | Évincé de la mémoire après le délai d'inactivité configuré |

Les ID de tâche et de contexte doivent être non vides, comporter au plus 128 caractères et contenir uniquement des lettres, des chiffres, `_`, `.`, `:` ou `-`.

## États de tâche

| État | Signification |
|-------|---------|
| `TASK_STATE_SUBMITTED` | La tâche a été acceptée |
| `TASK_STATE_WORKING` | iac-code exécute le tour de l'agent |
| `TASK_STATE_INPUT_REQUIRED` | Le tour est terminé et l'agent est prêt pour une entrée de suivi |
| `TASK_STATE_CANCELED` | L'annulation a été demandée et appliquée |
| `TASK_STATE_FAILED` | La tâche a échoué lors de la validation ou de l'exécution |

iac-code utilise `TASK_STATE_INPUT_REQUIRED` comme état terminé normal, car le contexte reste disponible pour les messages de suivi.

## Mises à jour en streaming

Pendant l'exécution, iac-code émet des mises à jour `TaskStatusUpdateEvent`.

Le texte de l'assistant est livré comme message de statut :

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

Les détails d'outils et d'utilisation sont livrés via `metadata.iac_code` :

| Chemin de métadonnées | Description |
|---------------|-------------|
| `iac_code.tool.status` | `started`, `input_delta`, `input_complete`, `completed` ou `failed` |
| `iac_code.tool.toolUseId` | ID d'utilisation d'outil stable pour corréler les événements d'outil |
| `iac_code.tool.name` | Nom de l'outil lorsqu'il est disponible |
| `iac_code.tool.input` | Entrée d'outil terminée, tronquée à 4000 caractères par champ |
| `iac_code.tool.result` | Résultat d'outil, tronqué à 4000 caractères par champ |
| `iac_code.permission.autoApproved` | `false` lorsqu'une demande d'autorisation d'outil a été rejetée par le mode serveur A2A |
| `iac_code.usage.inputTokens` | Nombre de jetons d'entrée pour le tour |
| `iac_code.usage.outputTokens` | Nombre de jetons de sortie pour le tour |
| `iac_code.usage.totalTokens` | Nombre total de jetons pour le tour |

Lorsqu'un résultat d'outil inclut une charge utile d'artefact texte prise en charge, le serveur stocke la charge utile localement, émet un `TaskArtifactUpdateEvent` standard et enregistre l'artefact dans le champ `artifacts` de la tâche. La partie d'artefact utilise une URL `file://` plus des métadonnées comme `mediaType`, `byteSize` et `sha256` ; le contenu original de l'artefact n'est pas dupliqué dans les métadonnées d'outil.

## Extensions

L'Agent Card annonce l'extension optionnelle de métadonnées d'artefact iac-code :

```text
urn:iac-code:a2a:artifact-metadata:v1
```

Cette extension identifie l'espace de noms `metadata.iac_code` utilisé pour la progression des outils, les décisions d'autorisation, l'utilisation des jetons et les métadonnées d'artefacts locaux. Si le serveur est configuré avec une extension obligatoire, les clients doivent inclure son URI dans l'en-tête `A2A-Extensions`. Les extensions obligatoires manquantes renvoient l'erreur A2A standard `ExtensionSupportRequiredError`.

## Gestion des erreurs

| Scénario | Résultat |
|----------|--------|
| Entrée texte vide | `TASK_STATE_FAILED` avec `A2A server currently accepts text input only.` |
| Type média non pris en charge | Erreur de validation ou erreur de type de contenu A2A standard, selon l'endroit où le SDK rejette la requête |
| Partie d'URL distante | Erreur de validation, car les parties d'URL doivent utiliser des URL locales `file://` |
| URL de fichier hors de l'espace de travail autorisé | Erreur de validation |
| Extension A2A obligatoire manquante | `ExtensionSupportRequiredError` A2A standard |
| Métadonnées d'espace de travail invalides | `TASK_STATE_FAILED` avec un message d'espace de travail invalide |
| Authentification manquante ou invalide | HTTP `401` avec `{"error":"Unauthorized"}` |
| Dépendances serveur A2A manquantes | La CLI quitte avec une indication d'installation pour l'extra `a2a` |
| Identifiants fournisseur manquants | Erreur d'authentification nettoyée |
| Erreur runtime inattendue | Erreur interne nettoyée |

Le serveur évite de renvoyer des chemins locaux, des secrets et des détails de fournisseur dans les messages d'erreur inattendus.
