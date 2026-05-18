---
title: Transport HTTP
description: Exécutez et appelez le serveur A2A iac-code via JSON-RPC HTTP.
sidebar_position: 5
---

# Transport HTTP

Le serveur A2A par défaut d'iac-code expose JSON-RPC via HTTP, ainsi que les routes REST du SDK A2A. Le serveur est construit avec Starlette et s'exécute sur Uvicorn.

## Démarrer le serveur

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

Installez d'abord les dépendances serveur optionnelles :

```bash
uv sync --extra a2a
```

## Résumé des endpoints

| Route | Méthode | Réponse |
|-------|--------|----------|
| `/health` | `GET` | Réponse de santé JSON simple |
| `/.well-known/agent-card.json` | `GET` | JSON de l'Agent Card |
| `/` | `POST` | Réponse JSON-RPC ou flux SSE |
| Routes REST du SDK | mixte | Endpoints REST A2A enregistrés par le SDK |

## En-têtes

En-têtes recommandés :

```text
Content-Type: application/json
A2A-Version: 1.0
```

Lorsque l'authentification Bearer est activée :

```text
Authorization: Bearer <token>
```

## Authentification

Le serveur prend en charge l'authentification optionnelle par jeton Bearer, Basic auth et clé API. Si aucune option d'authentification ni variable d'environnement n'est définie, les requêtes n'ont pas besoin d'authentification. Si un ou plusieurs schémas sont configurés, une requête peut s'authentifier avec n'importe lequel des schémas configurés.

### Jeton Bearer

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

Vous pouvez aussi définir `token` dans le fichier de configuration YAML A2A.

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Le nom d'utilisateur et le mot de passe doivent tous deux être définis pour activer Basic auth.

### Clé API

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

L'en-tête de clé API par défaut est `X-API-Key`. Vous pouvez le modifier en YAML :

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

ou avec `IACCODE_A2A_API_KEY_HEADER`.

| Scénario | Comportement |
|----------|----------|
| Aucun schéma d'authentification configuré | Aucune authentification requise |
| Un ou plusieurs schémas configurés, l'un correspond | La requête continue |
| Un ou plusieurs schémas configurés, aucun ne correspond | HTTP `401` avec `{"error":"Unauthorized"}` |

## Découverte de l'Agent Card

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

Authentifié :

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

Avec authentification par clé API :

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

L'URL de l'endpoint JSON-RPC est annoncée dans `supportedInterfaces[0].url`. Le mode HTTP annonce également une interface `HTTP+JSON` pour les clients capables d'utiliser REST.

## Message non streaming

`SendMessage` renvoie une réponse JSON-RPC unique une fois le tour de l'agent terminé.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Create a Terraform VPC module for Alibaba Cloud."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## Message en streaming

`SendStreamingMessage` renvoie des Server-Sent Events. Utilisez `curl -N` pour afficher les événements à mesure qu'ils arrivent.

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "stream-1",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Generate a ROS template for one VPC and two vSwitches."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

Chaque ligne SSE `data:` contient une réponse JSON-RPC dont le `result` est une `StreamResponse` A2A.

## Message de suivi

Utilisez les `taskId` et `contextId` renvoyés par la première réponse pour continuer la même conversation.

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-3",
        "taskId": "task-id-from-first-response",
        "contextId": "context-id-from-first-response",
        "role": "ROLE_USER",
        "parts": [{"text": "Now add tags for environment and owner."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

L'espace de travail doit rester le même pour le `contextId` réutilisé.

## Annuler une tâche en cours

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "CancelTask",
    "params": {
      "id": "task-id"
    }
  }'
```

L'annulation est coopérative : iac-code annule le tour actif de l'agent, émet un état annulé et libère le verrou de contexte. Annuler une tâche existante qui n'est plus en cours renvoie l'erreur A2A standard `TaskNotCancelableError`.

## Équivalents CLI

La plupart des workflows HTTP ont une commande CLI correspondante :

```yaml
url: http://127.0.0.1:41242/
```

```bash
# Discover the Agent Card
iac-code a2a-client --config a2a-client.yml discover

# Send a non-streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a Terraform VPC module for Alibaba Cloud." \
  --cwd "$PWD"

# Send a streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a ROS template for one VPC and two vSwitches." \
  --cwd "$PWD" \
  --stream

# Inspect task state
iac-code a2a-client --config a2a-client.yml task-get --task-id task-id
iac-code a2a-client --config a2a-client.yml task-list --output table

# Cancel an active task
iac-code a2a-client --config a2a-client.yml task-cancel --task-id task-id
```

Pour la liste complète des options, consultez la [référence des commandes](./command-reference.md).

## Notes opérationnelles

- Liez à `127.0.0.1` pour une utilisation locale uniquement.
- Utilisez `token` dans la configuration A2A ou `IACCODE_A2A_HTTP_TOKEN` avant de lier le serveur à une interface réseau partagée.
- Le mode A2A rejette automatiquement les demandes d'autorisation d'outil ; protégez les endpoints non authentifiés comme des services d'automatisation locaux.
- L'état runtime actif est en mémoire. La persistance duplique les métadonnées de tâche et de contexte, mais le redémarrage du processus ne reprend pas le travail asyncio en cours.
- Un contexte ne peut exécuter qu'une seule tâche à la fois ; des contextes séparés peuvent s'exécuter simultanément.
